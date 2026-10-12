"""HTML indexes, bounded durable article frontiers, robots and per-host backoff."""

import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy import select, func, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.config import Settings
from greek_news.db import new_id, utc_date, utc_now
from greek_news.fetching import MAX_HTML_BYTES, USER_AGENT, public_addresses, safe_fetch
from greek_news.html_pages import clean_text, document, parse_article, parse_links
from greek_news.ingestion import ingest_item, source_permits_storage
from greek_news.models import AuditEntry, CrawlURL, Source, SourceRevision

FETCH_ERRORS = (httpx.HTTPError, ValueError, PermissionError, socket.gaierror, OSError)


def plain_text(value: str) -> str:
    return clean_text(document(value.encode()).text_content()) if "<" in value else clean_text(value)


def parse_article_links(content: bytes, base_url: str, host: str) -> list[tuple[str, str]]:
    return [(link.url, link.title) for link in parse_links(content, base_url, host)]


def retry_after(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        return now + timedelta(seconds=max(0, min(int(value), 86400)))
    except ValueError:
        try:
            return utc_date(parsedate_to_datetime(value))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class PollResult:
    state: str
    new: int = 0
    revised: int = 0
    skipped: int = 0
    reason: str = ""


def robots_rules(source: Source, fetch, now: datetime) -> RobotFileParser:
    if not source.robots_checked_at or source.robots_checked_at < now - timedelta(hours=6):
        response = fetch(f"https://{source.canonical_domain}/robots.txt", max_bytes=65536)
        if response.status_code not in {200, 404, 410}:
            raise PermissionError("robots.txt unavailable; retry later")
        source.robots_txt = response.content.decode("utf-8", errors="replace") if response.status_code == 200 else ""
        source.robots_checked_at = now
    parser = RobotFileParser()
    parser.parse((source.robots_txt or "").splitlines())
    return parser


def host_lock(session: Session, host: str) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        return True
    return bool(session.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:domain))"),
                                {"domain": f"html:{host}"}).scalar_one())


def add_frontier(session: Session, source: Source, url: str, title: str = "", *,
                 kind: str = "article", depth: int = 0, now: datetime | None = None) -> bool:
    now = now or utc_now()
    statement = postgres_insert(CrawlURL) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(CrawlURL)
    result = session.execute(statement.values(
        id=new_id(), source_id=source.id, url=url, title_hint=title, kind=kind, depth=depth,
        state="pending", attempts=0, discovered_at=now, next_fetch_at=now,
    ).on_conflict_do_nothing(index_elements=[CrawlURL.source_id, CrawlURL.url]))
    return result.rowcount == 1


def discover_links(session: Session, source: Source, content: bytes, url: str, settings: Settings,
                   *, depth: int = 0) -> int:
    links = parse_links(content, url, source.canonical_domain, limit=settings.crawl_links_per_page,
                        article_pattern=source.article_url_pattern, index_pattern=source.index_url_pattern,
                        selector=source.link_selector,
                        external=settings.auto_discover_publishers)
    added = 0
    for link in links:
        if urlsplit(link.url).hostname != source.canonical_domain:
            # External citations/directories introduce candidates; downloaded HTML remains mandatory.
            if link.kind == "article" and (source.is_directory or len(link.title) >= 40):
                from greek_news.discovery import discover_publisher
                candidate = discover_publisher(session, link.url, source, settings)
                if candidate:
                    added += add_frontier(session, candidate, link.url, link.title, depth=0)
            continue
        if link.kind == "index" and (depth >= settings.crawl_max_depth or session.scalar(select(func.count()).select_from(CrawlURL).where(CrawlURL.source_id == source.id, CrawlURL.kind == "index")) >= settings.crawl_pages_per_source):
            continue
        added += add_frontier(session, source, link.url, link.title, kind=link.kind, depth=depth + 1)
    return added


def _failure(session: Session, source: Source, exc: Exception, now: datetime) -> PollResult:
    source.consecutive_failures += 1
    source.next_poll_at = max(now + timedelta(seconds=min(86400, 60 * 2 ** min(source.consecutive_failures, 10))), source.retry_after or now)
    source.next_request_at = source.next_poll_at
    if source.consecutive_failures >= 5:
        source.enabled = False
        source.endpoint_verified_at = None
        source.verification_note = f"Automatic retry and re-verification after failures: {str(exc)[:500]}"
        session.add(AuditEntry(actor="crawler", action="source_backoff", subject="source",
                               subject_id=source.id, detail={"reason": source.verification_note}))
    return PollResult("failed", reason=f"{type(exc).__name__}: {str(exc)[:500]}")


def poll_source(session: Session, source: Source, *, settings: Settings | None = None,
                transport: httpx.BaseTransport | None = None, resolver=public_addresses) -> PollResult:
    settings = settings or Settings(_env_file=None)
    now = utc_now()
    if source.source_type not in {"html", "html_index"} or not source_permits_storage(source, now):
        return PollResult("blocked", reason="An enabled public HTML source is required")
    if source.endpoint_verified_at is None:
        return PollResult("blocked", reason="HTML endpoint has not passed automatic verification")
    if not host_lock(session, source.canonical_domain):
        return PollResult("deferred", reason="Another worker is fetching this host")
    if source.next_poll_at and source.next_poll_at > now or source.next_request_at and source.next_request_at > now:
        return PollResult("deferred", reason="Host crawl interval or backoff not elapsed")
    def fetch(url, **kwargs):
        return safe_fetch(url, allowed_host=source.canonical_domain, transport=transport,
                          resolver=resolver, **kwargs)
    try:
        robots = robots_rules(source, fetch, now)
        if not robots.can_fetch(USER_AGENT, source.feed_url):
            raise PermissionError("Robots disallow this HTML index")
        headers = {key: value for key, value in (("If-None-Match", source.etag),
                    ("If-Modified-Since", source.last_modified_header)) if value}
        response = fetch(source.feed_url, headers=headers, max_bytes=MAX_HTML_BYTES)
        source.last_status_code = response.status_code
        source.last_latency_ms = response.latency_ms
        source.retry_after = retry_after(response.headers.get("Retry-After"), now)
        delay = max(settings.crawl_delay_seconds, robots.crawl_delay(USER_AGENT) or 0)
        rate = robots.request_rate(USER_AGENT)
        if rate:
            delay = max(delay, rate.seconds / rate.requests)
        source.next_request_at = now + timedelta(seconds=delay)
        if response.status_code not in {200, 304}:
            raise ValueError(f"HTML index returned HTTP {response.status_code}")
        added = 0
        if response.status_code == 200:
            document(response.content, response.headers.get("Content-Type", ""))
            added = discover_links(session, source, response.content, response.url, settings)
            for entry in source.entry_urls:
                if urlsplit(entry).hostname == source.canonical_domain:
                    added += add_frontier(session, source, entry, kind="index")
            source.etag = response.headers.get("ETag")
            source.last_modified_header = response.headers.get("Last-Modified")
        source.last_successful_fetch = now
        source.last_new_item = now if added else source.last_new_item
        source.consecutive_failures = 0
        source.next_poll_at = now + timedelta(minutes=source.poll_minutes)
        return PollResult("ok" if response.status_code == 200 else "unchanged", new=added)
    except FETCH_ERRORS as exc:
        return _failure(session, source, exc, now)


def fetch_frontier(session: Session, row: CrawlURL, settings: Settings, *,
                   transport: httpx.BaseTransport | None = None, resolver=public_addresses) -> PollResult:
    now = utc_now()
    source = session.get(Source, row.source_id, with_for_update=True)
    if not source_permits_storage(source, now):
        return PollResult("blocked", reason="Source is unavailable or excluded")
    if not host_lock(session, source.canonical_domain) or source.next_request_at and source.next_request_at > now:
        row.next_fetch_at = source.next_request_at or now + timedelta(seconds=5)
        return PollResult("deferred", reason="Host request cadence")
    def fetch(url, **kwargs):
        return safe_fetch(url, allowed_host=source.canonical_domain, transport=transport,
                          resolver=resolver, **kwargs)
    try:
        robots = robots_rules(source, fetch, now)
        if not robots.can_fetch(USER_AGENT, row.url):
            raise PermissionError("Robots disallow this page")
        headers = {key: value for key, value in (("If-None-Match", row.etag),
                    ("If-Modified-Since", row.last_modified)) if value}
        response = fetch(row.url, headers=headers, max_bytes=MAX_HTML_BYTES)
        row.attempts += 1
        rate = robots.request_rate(USER_AGENT)
        delay = max(settings.crawl_delay_seconds, robots.crawl_delay(USER_AGENT) or 0,
                    rate.seconds / rate.requests if rate else 0)
        source.next_request_at = now + timedelta(seconds=delay)
        source.last_status_code = response.status_code
        source.last_latency_ms = response.latency_ms
        if response.status_code == 304:
            row.next_fetch_at = now + timedelta(hours=settings.article_refetch_hours)
            row.state = "watched"
            return PollResult("unchanged")
        if response.status_code != 200:
            source.retry_after = retry_after(response.headers.get("Retry-After"), now)
            raise ValueError(f"Page returned HTTP {response.status_code}")
        document(response.content, response.headers.get("Content-Type", ""))
        if row.kind == "index":
            added = discover_links(session, source, response.content, response.url, settings, depth=row.depth)
            row.next_fetch_at = now + timedelta(minutes=source.poll_minutes)
            row.state = "watched"
            result = PollResult("ok", new=added)
        else:
            data, meta = parse_article(response.content, response.url, source.canonical_domain,
                                       title_hint=row.title_hint, content_type=response.headers.get("Content-Type", ""),
                                       timezone_name="Europe/Athens" if source.country_code == "GR" else "UTC")
            outcome = ingest_item(session, source, data)
            row.source_item_id = outcome.item_id
            revision = session.get(SourceRevision, outcome.revision_id)
            if revision.analysis_state == "pending":
                revision.analysis = {"html_metadata": meta}
            row.next_fetch_at = now + timedelta(hours=settings.article_refetch_hours)
            row.state = "watched"
            source.last_successful_extraction = now
            source.last_new_item = now if outcome.state == "new" else source.last_new_item
            result = PollResult("ok", new=int(outcome.state == "new"), revised=int(outcome.state == "revised"))
        row.etag = response.headers.get("ETag")
        row.last_modified = response.headers.get("Last-Modified")
        row.fetched_at = now
        row.last_error = None
        source.consecutive_failures = 0
        source.last_successful_fetch = now
        return result
    except FETCH_ERRORS as exc:
        row.attempts += 1
        row.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
        row.next_fetch_at = max(now + timedelta(minutes=min(1440, 2 ** min(row.attempts, 10))), source.retry_after or now)
        # Bad individual pages do not disable an otherwise healthy newspaper.
        row.state = "failed" if row.attempts >= 3 or isinstance(exc, PermissionError) else "pending"
        return PollResult("failed", reason=row.last_error)
