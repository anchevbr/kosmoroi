"""HTML source catalogs, automatic source verification and bounded publisher discovery."""

import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from greek_news.config import Settings
from greek_news.db import utc_now
from greek_news.fetching import MAX_HTML_BYTES, USER_AGENT, public_addresses, safe_fetch
from greek_news.html_pages import document, parse_links
from greek_news.ingestion import canonical_url
from greek_news.models import AuditEntry, Source
from greek_news.polling import robots_rules


@dataclass(frozen=True)
class DiscoveryResult:
    state: str
    reason: str = ""


class CatalogCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    canonical_domain: str
    feed_url: str
    source_type: Literal["html", "html_index"] = "html"
    extraction_strategy: Literal["article_html", "index_links"] = "article_html"
    organization: str = ""
    language: str = "el"
    country_code: str = Field(default="GR", pattern=r"^[A-Z]{2}$")
    coverage_ids: list[str] = Field(default_factory=list)
    entry_urls: list[str] = Field(default_factory=list, max_length=12)
    article_url_pattern: str | None = Field(default=None, max_length=300)
    index_url_pattern: str | None = Field(default=None, max_length=300)
    link_selector: str | None = None
    is_directory: bool = False
    trust_category: str = "publisher"
    priority: int = 0
    poll_minutes: int = Field(default=15, ge=5, le=1440)
    syndication_group: str | None = None
    permission_basis: str = ""
    terms_url: str | None = None
    permission_reviewed_at: datetime | None = None
    permission_expires_at: datetime | None = None
    rights_status: Literal["unverified", "restricted", "documented_grant", "public_facts"] = "public_facts"
    endpoint_note: str = "Public HTML candidate; live endpoint verification pending"
    target_region: str = ""
    locality_hint: str = Field(default="", max_length=200)
    can_discover: bool = False
    can_fetch: bool = False
    can_store: bool = False
    can_process_ai: bool = False
    can_reuse_text: bool = False
    can_reuse_images: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self):
        for value in [self.feed_url, *self.entry_urls]:
            normalized = canonical_url(value)
            if urlsplit(normalized).hostname != self.canonical_domain:
                raise ValueError("HTML entry URLs must use the registered HTTPS domain")
        for value in (self.permission_reviewed_at, self.permission_expires_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Policy timestamps must be timezone-aware")
        for value in (self.article_url_pattern, self.index_url_pattern):
            if value:
                # Only bounded simple patterns; nested repetition is not accepted.
                if re.search(r"\([^)]*[+*][^)]*\)[+*{]", value):
                    raise ValueError("Nested regex repetition is not allowed")
                re.compile(value)
        if self.rights_status == "documented_grant" and (not self.permission_basis or not self.permission_reviewed_at):
            raise ValueError("A documented grant needs its basis and date")
        if self.can_reuse_text or self.can_reuse_images:
            if self.rights_status != "documented_grant":
                raise ValueError("Verbatim text or image reuse needs a documented grant")
        return self


class SourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[CatalogCandidate]


PUBLIC_FACTS_BASIS = "Operator policy: fetch robots-permitted public HTML for internal factual analysis and original attributed reporting; no verbatim article or image reuse. Public access is not a redistribution license."


def import_catalog(session: Session, path: str | Path, settings: Settings | None = None) -> int:
    settings = settings or Settings(_env_file=None)
    catalog = SourceCatalog.model_validate_json(Path(path).read_text(encoding="utf-8"))
    added = 0
    for item in catalog.sources:
        source = session.scalar(select(Source).where(Source.name == item.name))
        if source is None:
            source = session.scalar(select(Source).where(Source.feed_url == item.feed_url))
        values = item.model_dump(exclude={"rights_status", "endpoint_note", "target_region"})
        if item.rights_status == "public_facts" and settings.public_html_enabled:
            values.update(can_discover=True, can_fetch=True, can_store=True, can_process_ai=True,
                          permission_basis=PUBLIC_FACTS_BASIS,
                          permission_reviewed_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        if source:
            # Upgrade untouched legacy feed endpoints; preserve explicit operator changes.
            if source.source_type in {"rss", "atom"} and not source.operator_disabled:
                for key, value in values.items():
                    setattr(source, key, value)
                source.enabled = False
                source.endpoint_verified_at = None
                source.next_poll_at = None
                source.verification_note = "Legacy feed replaced by HTML; automatic verification pending"
            continue
        if session.scalar(select(func.count()).select_from(Source)) >= settings.maximum_sources:
            break
        source = Source(**values, enabled=False, verification_note=item.endpoint_note)
        session.add(source)
        session.flush()
        session.add(AuditEntry(actor="discovery", action="source_discovered", subject="source",
                               subject_id=source.id, detail={"entry_url": source.feed_url,
                               "access_policy": item.rights_status}))
        added += 1
    return added


def eligibility_reason(source: Source, now: datetime) -> str | None:
    if source.operator_disabled:
        return "Source excluded by operator configuration"
    if source.source_type not in {"html", "html_index"}:
        return "Only public HTML collection is supported"
    if source.extraction_strategy not in {"article_html", "index_links"}:
        return "Unsupported HTML extractor"
    try:
        if urlsplit(canonical_url(source.feed_url)).hostname != source.canonical_domain:
            return "HTML endpoint is outside the registered domain"
    except ValueError:
        return "Invalid HTML entry URL"
    if (not all((source.can_discover, source.can_fetch, source.can_store))
            or not source.permission_basis.strip() or not source.permission_reviewed_at
            or source.permission_reviewed_at > now
            or source.permission_expires_at and source.permission_expires_at <= now):
        return "Current discovery, fetch and internal-analysis policy required"
    return None


def verify_candidate(session: Session, source: Source, *, now: datetime | None = None,
                     transport: httpx.BaseTransport | None = None, resolver=public_addresses) -> DiscoveryResult:
    now = now or utc_now()
    if source.enabled:
        return DiscoveryResult("active")
    if source.next_poll_at and source.next_poll_at > now:
        return DiscoveryResult("deferred", "Verification backoff")
    source.last_discovery_check = now
    reason = eligibility_reason(source, now)
    if reason:
        source.verification_note = reason
        return DiscoveryResult("blocked", reason)
    def fetch(url, **kwargs):
        return safe_fetch(url, allowed_host=source.canonical_domain, transport=transport,
                          resolver=resolver, **kwargs)
    try:
        robots = robots_rules(source, fetch, now)
        if not robots.can_fetch(USER_AGENT, source.feed_url):
            raise PermissionError("Robots disallow HTML entry")
        response = fetch(source.feed_url, max_bytes=MAX_HTML_BYTES)
        if response.status_code != 200:
            raise ValueError(f"HTML entry returned HTTP {response.status_code}")
        document(response.content, response.headers.get("Content-Type", ""))
        links = parse_links(response.content, response.url, source.canonical_domain,
                            selector=source.link_selector, external=source.is_directory,
                            article_pattern=source.article_url_pattern,
                            index_pattern=source.index_url_pattern)
        if not links:
            raise ValueError("No readable news links found; selector or entry URL needs adjustment")
    except (httpx.HTTPError, ValueError, PermissionError, OSError, socket.gaierror) as exc:
        source.consecutive_failures += 1
        source.verification_note = f"Automatic HTML verification failed: {str(exc)[:500]}"
        source.next_poll_at = now + timedelta(minutes=min(1440, 5 * 2 ** min(source.consecutive_failures, 8)))
        return DiscoveryResult("deferred", source.verification_note)
    source.enabled = True
    source.endpoint_verified_at = now
    source.verification_note = "Public HTML and robots verified automatically; configured factual reporting policy"
    source.consecutive_failures = 0
    source.next_poll_at = None
    session.add(AuditEntry(actor="discovery", action="source_activated", subject="source",
                           subject_id=source.id, detail={"entry_url": source.feed_url}))
    return DiscoveryResult("active")


def discover_publisher(session: Session, url: str, referring: Source | None, settings: Settings) -> Source | None:
    if not settings.public_html_enabled or not settings.auto_discover_publishers:
        return None
    try:
        url = canonical_url(url)
    except ValueError:
        return None
    host = urlsplit(url).hostname
    excluded = {"facebook.com", "x.com", "twitter.com", "instagram.com", "youtube.com", "tiktok.com", "google.com"}
    if not host or any(host == name or host.endswith("." + name) for name in excluded):
        return None
    existing = session.scalar(select(Source).where(Source.canonical_domain == host).order_by(Source.priority.desc()).limit(1))
    if existing:
        return existing if not existing.operator_disabled and existing.can_fetch else None
    if session.scalar(select(func.count()).select_from(Source)) >= settings.maximum_sources:
        return None
    # A discovered publisher never receives a text/image reuse license or high trust rating.
    source = Source(name=f"Discovered: {host}", canonical_domain=host, feed_url=f"https://{host}/",
                    source_type="html", extraction_strategy="article_html", organization=host,
                    trust_category="unreviewed", language="unknown", country_code="ZZ",
                    locality_hint="", can_discover=True, can_fetch=True, can_store=True,
                    can_process_ai=True, permission_basis=PUBLIC_FACTS_BASIS,
                    permission_reviewed_at=utc_now(), enabled=False,
                    verification_note="Discovered from HTML citation or AI search; HTML verification pending")
    session.add(source)
    session.flush()
    session.add(AuditEntry(actor="discovery", action="publisher_discovered", subject="source",
                           subject_id=source.id, detail={"url": url,
                           "referring_source": referring.id if referring else None}))
    return source
