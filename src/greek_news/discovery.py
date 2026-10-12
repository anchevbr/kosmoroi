"""Automatically verify eligible feed candidates without inferring content rights."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.db import utc_now
from greek_news.fetching import MAX_HTML_BYTES, USER_AGENT, UnsafeURL, public_addresses, safe_fetch
from greek_news.models import AuditEntry, Source
from greek_news.polling import parse_article_links


@dataclass(frozen=True)
class DiscoveryResult:
    state: str
    reason: str = ""


class CatalogCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    canonical_domain: str
    feed_url: str
    source_type: str = "rss"
    extraction_strategy: str = "feed_metadata"
    organization: str = ""
    language: str = "el"
    coverage_ids: list[str] = Field(default_factory=list)
    trust_category: str = "unreviewed"
    priority: int = 0
    poll_minutes: int = Field(default=30, ge=5)
    syndication_group: str | None = None
    permission_basis: str = ""
    terms_url: str | None = None
    permission_reviewed_at: datetime | None = None
    permission_expires_at: datetime | None = None
    rights_status: Literal["unverified", "restricted", "documented_grant"] = "unverified"
    endpoint_note: str = "Endpoint not verified"
    target_region: str = Field(default="", pattern=r"^[a-z_]*$")
    locality_hint: str = Field(default="", max_length=200)
    can_discover: bool = False
    can_fetch: bool = False
    can_store: bool = False
    can_process_ai: bool = False
    can_reuse_text: bool = False
    can_reuse_images: bool = False

    @model_validator(mode="after")
    def valid_candidate(self) -> "CatalogCandidate":
        url = urlsplit(self.feed_url)
        if (url.scheme != "https" or url.hostname != self.canonical_domain
                or url.username or url.password or url.port not in {None, 443}):
            raise ValueError("Candidate endpoint must use its registered HTTPS domain")
        for value in (self.permission_reviewed_at, self.permission_expires_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Permission timestamps must be timezone-aware")
        if any((self.can_discover, self.can_fetch, self.can_store, self.can_process_ai,
                self.can_reuse_text, self.can_reuse_images)) and (
                self.rights_status != "documented_grant" or not self.permission_basis.strip()
                or not self.permission_reviewed_at):
            raise ValueError("Catalog permission flags require a dated, documented grant")
        return self


class SourceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[CatalogCandidate]


def import_catalog(session: Session, path: str | Path) -> int:
    catalog = SourceCatalog.model_validate_json(Path(path).read_text(encoding="utf-8"))
    added = 0
    for item in catalog.sources:
        if session.scalar(select(Source.id).where(Source.feed_url == item.feed_url)):
            continue
        existing = session.scalar(select(Source).where(Source.name == item.name))
        if existing:
            if (existing.canonical_domain == item.canonical_domain
                    and existing.feed_url == f"https://{item.canonical_domain}/"
                    and existing.source_type == "html_index"
                    and existing.extraction_strategy == "requires_site_plugin"
                    and item.source_type in {"rss", "atom"}
                    and not existing.enabled and not existing.operator_disabled
                    and not any((existing.can_discover, existing.can_fetch, existing.can_store,
                                 existing.can_process_ai, existing.can_reuse_text,
                                 existing.can_reuse_images, existing.permission_basis,
                                 existing.permission_reviewed_at, existing.endpoint_verified_at))
                    and "rights: unverified; research lead:" in existing.verification_note):
                old_url = existing.feed_url
                existing.feed_url = item.feed_url
                existing.source_type = item.source_type
                existing.extraction_strategy = item.extraction_strategy
                existing.verification_note = (f"{item.endpoint_note}; rights: {item.rights_status}; "
                                              f"research lead: {item.target_region}/{item.locality_hint}")
                session.add(AuditEntry(actor="discovery", action="source_endpoint_discovered",
                                       subject="source", subject_id=existing.id,
                                       detail={"previous_endpoint": old_url, "endpoint": item.feed_url,
                                               "rights_status": item.rights_status}))
            continue
        source = Source(**item.model_dump(exclude={
            "rights_status", "endpoint_note", "target_region", "locality_hint",
        }), enabled=False, verification_note=(
            f"{item.endpoint_note}; rights: {item.rights_status}; "
            f"research lead: {item.target_region}/{item.locality_hint}"
        ))
        session.add(source)
        session.flush()
        session.add(AuditEntry(actor="discovery", action="source_discovered",
                               subject="source", subject_id=source.id,
                               detail={"endpoint": source.feed_url,
                                       "permission_basis": source.permission_basis,
                                       "rights_status": item.rights_status,
                                       "target_region": item.target_region,
                                       "locality_hint": item.locality_hint}))
        added += 1
    return added


def eligibility_reason(source: Source, now: datetime) -> str | None:
    if source.operator_disabled:
        return "Source disabled by an operator"
    html_index = source.source_type == "html_index" and source.extraction_strategy == "index_links"
    if source.source_type not in {"rss", "atom"} and not html_index:
        return "No configured RSS/Atom feed or supported article index"
    url = urlsplit(source.feed_url)
    if (url.scheme != "https" or url.hostname != source.canonical_domain
            or url.username or url.password or url.port not in {None, 443}):
        return "Endpoint is not the registered HTTPS domain"
    if (not all((source.can_discover, source.can_fetch, source.can_store))
            or not html_index and not source.can_process_ai
            or not source.permission_basis.strip()
            or not source.permission_reviewed_at or source.permission_reviewed_at > now
            or source.permission_expires_at and source.permission_expires_at <= now):
        return "Discovery, storage, applicable AI, and current documented permission required"
    if not html_index and source.extraction_strategy not in {
        "feed_metadata", "feed_summary", "approved_html",
    }:
        return "Extraction strategy is not supported"
    return None


def verify_candidate(
    session: Session, source: Source, *, now: datetime | None = None,
    transport: httpx.BaseTransport | None = None, resolver=public_addresses,
) -> DiscoveryResult:
    now = now or utc_now()
    if source.enabled:
        return DiscoveryResult("active")
    if source.next_poll_at and source.next_poll_at > now:
        return DiscoveryResult("deferred", "Verification backoff not elapsed")
    source.last_discovery_check = now
    reason = eligibility_reason(source, now)
    if reason:
        if source.verification_note != reason:
            source.verification_note = reason
            session.add(AuditEntry(actor="discovery", action="source_blocked",
                                   subject="source", subject_id=source.id,
                                   detail={"reason": reason}))
        return DiscoveryResult("blocked", reason)
    host = source.canonical_domain

    def fetch(url: str, max_bytes: int):
        return safe_fetch(url, allowed_host=host, max_bytes=max_bytes,
                          transport=transport, resolver=resolver)

    try:
        robots = fetch(f"https://{host}/robots.txt", 65536)
        if robots.status_code not in {200, 404}:
            raise PermissionError("robots.txt unavailable")
        parser = RobotFileParser()
        parser.parse(robots.content.decode("utf-8", errors="replace").splitlines()
                     if robots.status_code == 200 else [])
        if not parser.can_fetch(USER_AGENT, source.feed_url):
            raise PermissionError("Robots rules forbid this endpoint")
        response = fetch(source.feed_url, MAX_HTML_BYTES if source.source_type == "html_index"
                         else 2_000_000)
        if response.status_code != 200:
            raise ValueError(f"Endpoint returned HTTP {response.status_code}")
        if source.source_type == "html_index":
            if not parse_article_links(response.content, response.url, host):
                raise ValueError("Index has no same-host article links")
        else:
            parsed = feedparser.parse(response.content)
            expected = "atom" if source.source_type == "atom" else "rss"
            if not parsed.version.startswith(expected) or parsed.bozo and not parsed.entries:
                raise ValueError("Endpoint is not a usable RSS/Atom feed")
    except (httpx.HTTPError, UnsafeURL, ValueError, PermissionError) as exc:
        source.consecutive_failures += 1
        source.verification_note = f"Automatic verification failed: {exc}"
        source.next_poll_at = now + timedelta(minutes=min(1440, 5 * 2 ** min(source.consecutive_failures, 8)))
        session.add(AuditEntry(actor="discovery", action="source_verification_failed",
                               subject="source", subject_id=source.id,
                               detail={"reason": source.verification_note}))
        return DiscoveryResult("deferred", source.verification_note)
    source.enabled = True
    source.endpoint_verified_at = now
    source.verification_note = "Endpoint and robots checked automatically; rights from configured grant"
    source.consecutive_failures = 0
    source.next_poll_at = None
    session.add(AuditEntry(actor="discovery", action="source_activated",
                           subject="source", subject_id=source.id,
                           detail={"endpoint": source.feed_url, "permission": source.permission_basis}))
    return DiscoveryResult("active")