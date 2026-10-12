"""Permission-aware RSS/Atom polling with robots, conditional GET and backoff."""

import calendar
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
import trafilatura
from sqlalchemy import text
from sqlalchemy.orm import Session

from greek_news.db import utc_date, utc_now
from greek_news.fetching import (
    MAX_HTML_BYTES, Fetched, USER_AGENT, public_addresses, safe_fetch,
)
from greek_news.ingestion import SourceInput, ingest_item, source_permits_storage
from greek_news.models import AuditEntry, Source


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.skip = max(self.skip - 1, 0)

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


class _ArticleLinks(HTMLParser):
    def __init__(self, base_url: str, host: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.host = host
        self.depth = 0
        self.href: str | None = None
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article":
            self.depth += 1
        elif tag == "a" and self.depth and self.href is None:
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            try:
                url = urljoin(self.base_url, self.href)
                title = " ".join(" ".join(self.parts).split())[:1000]
                if (title and urlsplit(url).scheme == "https"
                        and urlsplit(url).hostname == self.host and len(self.links) < 30):
                    self.links.append((url, title))
            except ValueError:
                pass
            self.href = None
            self.parts = []
        elif tag == "article":
            self.depth = max(self.depth - 1, 0)


def parse_article_links(content: bytes, base_url: str, host: str) -> list[tuple[str, str]]:
    parser = _ArticleLinks(base_url, host)
    parser.feed(content.decode("utf-8", errors="replace"))
    return parser.links


def parsed_feed_time(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)


def retry_after(value: str | None, now: datetime) -> datetime | None:
    if not value:
        return None
    try:
        return now + timedelta(seconds=max(0, int(value)))
    except ValueError:
        try:
            return utc_date(parsedate_to_datetime(value))
        except (TypeError, ValueError):
            return None


def _next_poll(source: Source, now: datetime, minimum_seconds: int = 0) -> datetime:
    interval = max(1, source.poll_minutes) * 60
    return now + timedelta(seconds=max(interval + random.uniform(0, interval * 0.1), minimum_seconds))


@dataclass(frozen=True)
class PollResult:
    state: str
    new: int = 0
    revised: int = 0
    skipped: int = 0
    reason: str = ""


def poll_source(
    session: Session, source: Source, *, transport: httpx.BaseTransport | None = None,
    resolver=public_addresses,
) -> PollResult:
    now = utc_now()
    html_index = source.source_type == "html_index" and source.extraction_strategy == "index_links"
    if (source.source_type not in {"rss", "atom"} and not html_index
            or not source_permits_storage(source, now)):
        return PollResult("blocked", reason="Source type or reviewed permissions do not allow polling")
    if source.endpoint_verified_at is None:
        return PollResult("blocked", reason="Endpoint has not been verified by an operator")
    host = urlsplit(source.feed_url).hostname
    if host != source.canonical_domain:
        return PollResult("blocked", reason="Feed URL does not match its registered domain")
    if session.get_bind().dialect.name == "postgresql":
        locked = session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:domain))"), {"domain": host},
        ).scalar_one()
        if not locked:
            return PollResult("deferred", reason="Another worker is polling this domain")
    if source.next_poll_at and source.next_poll_at > now:
        return PollResult("deferred", reason="Configured poll interval or backoff not elapsed")

    def fetch(url: str, **kwargs) -> Fetched:
        return safe_fetch(url, allowed_host=host, transport=transport, resolver=resolver, **kwargs)

    try:
        robots = fetch(f"https://{host}/robots.txt", max_bytes=65536)
        if robots.status_code not in {200, 404}:
            raise PermissionError("robots.txt is inaccessible")
        parser = RobotFileParser()
        parser.parse(robots.content.decode("utf-8", errors="replace").splitlines()
                     if robots.status_code == 200 else [])
        if not parser.can_fetch(USER_AGENT, source.feed_url):
            raise PermissionError("Robots rules forbid this feed")
        delay = parser.crawl_delay(USER_AGENT) or 0
        request_headers = {}
        if source.etag:
            request_headers["If-None-Match"] = source.etag
        if source.last_modified_header:
            request_headers["If-Modified-Since"] = source.last_modified_header
        response = fetch(source.feed_url, headers=request_headers)
        source.last_status_code = response.status_code
        source.last_latency_ms = response.latency_ms
        source.retry_after = retry_after(response.headers.get("Retry-After"), now)
        if response.status_code == 304:
            source.last_successful_fetch = now
            source.consecutive_failures = 0
            source.next_poll_at = _next_poll(source, now, delay)
            return PollResult("unchanged")
        if response.status_code != 200:
            raise ValueError(f"Feed returned HTTP {response.status_code}")
        if html_index:
            entries = [SourceInput(url=url, title=title) for url, title in
                       parse_article_links(response.content, response.url, host)]
        else:
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError("Feed could not be parsed")
            entries = parsed.entries[:30]
        new = revised = skipped = 0
        for entry in entries:
            try:
                if html_index:
                    item = entry
                else:
                    title = plain_text(entry.get("title", ""))
                    url = entry.get("link", "")
                    content = None
                    if source.extraction_strategy == "feed_summary":
                        content = plain_text(entry.get("summary", ""))[:100000] or None
                    elif source.extraction_strategy == "approved_html":
                        if not parser.can_fetch(USER_AGENT, url):
                            raise PermissionError("Robots rules forbid article extraction")
                        page = fetch(url, max_bytes=MAX_HTML_BYTES)
                        if page.status_code != 200:
                            raise ValueError("Article page could not be extracted")
                        content = trafilatura.extract(page.content.decode("utf-8", errors="replace"))
                        if not content:
                            raise ValueError("Article text extraction returned no content")
                    elif source.extraction_strategy != "feed_metadata":
                        raise ValueError("Unconfigured extractor")
                    item = SourceInput(
                        url=url, title=title, guid=entry.get("id"),
                        published_at=parsed_feed_time(entry["published_parsed"])
                        if "published_parsed" in entry.keys() else None,
                        updated_at=parsed_feed_time(entry["updated_parsed"])
                        if "updated_parsed" in entry.keys() else None, text=content,
                    )
                outcome = ingest_item(session, source, item)
                new += outcome.state == "new"
                revised += outcome.state == "revised"
            except (ValueError, PermissionError, TypeError):
                skipped += 1
        source.etag = response.headers.get("ETag", source.etag)
        source.last_modified_header = response.headers.get("Last-Modified", source.last_modified_header)
        source.last_successful_fetch = now
        source.last_successful_extraction = now if len(entries) > skipped else source.last_successful_extraction
        source.last_new_item = now if new else source.last_new_item
        source.consecutive_failures = 0
        source.next_poll_at = _next_poll(source, now, delay)
        return PollResult("ok", new, revised, skipped)
    except (httpx.HTTPError, ValueError, PermissionError) as exc:
        source.consecutive_failures += 1
        exponential = min(3600, 60 * (2 ** min(source.consecutive_failures, 6)))
        source.next_poll_at = max(
            _next_poll(source, now, exponential), source.retry_after or now,
        )
        if source.consecutive_failures >= 5:
            source.enabled = False
            source.verification_note = f"Automatically disabled after failed polls: {exc}"
            session.add(AuditEntry(actor="pipeline", action="source_auto_disabled",
                                   subject="source", subject_id=source.id,
                                   detail={"reason": source.verification_note}))
        return PollResult("failed", reason=str(exc))