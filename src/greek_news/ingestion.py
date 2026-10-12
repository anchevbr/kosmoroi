"""Permission-gated, idempotent source item and evidence revision writes."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.db import new_id, utc_date, utc_now
from greek_news.models import Article, Source, SourceItem, SourceRevision, Story


class EvidenceInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    excerpt: str = Field(min_length=1, max_length=2000)
    fact_key: str | None = None
    value: str | None = None
    speaker: str | None = None


class PlaceHint(BaseModel):
    name: str
    role: str
    kind: str | None = None
    context: str | None = None
    evidence_excerpt: str | None = None

    @model_validator(mode="after")
    def validate_role(self) -> "PlaceHint":
        if self.role not in {"occurred", "affected", "background"}:
            raise ValueError("Unknown geographic role")
        return self


class SourceInput(BaseModel):
    url: str
    title: str = Field(min_length=1, max_length=1000)
    guid: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    event_at: datetime | None = None
    event_until: datetime | None = None
    event_type: str = "unclassified"
    topic: str = "general"
    audience_scope: str = "unknown"
    text: str | None = Field(default=None, max_length=100000)
    claims: list[EvidenceInput] = Field(default_factory=list)
    place_hints: list[PlaceHint] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_evidence(self) -> "SourceInput":
        for name in ("published_at", "updated_at", "event_at", "event_until"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, utc_date(value))
        if self.event_until and self.event_at and self.event_until < self.event_at:
            raise ValueError("End of event precedes its start")
        if self.audience_scope not in {"unknown", "local", "regional", "national", "mixed", "international"}:
            raise ValueError("Invalid audience scope")
        content = self.text or self.title
        if any(claim.excerpt not in content for claim in self.claims):
            raise ValueError("Evidence excerpts must be present in the supplied source text")
        return self


@dataclass(frozen=True)
class IngestResult:
    item_id: str
    revision_id: str
    state: str


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Source item URLs must be public HTTPS URLs without credentials")
    if parts.port not in {None, 443}:
        raise ValueError("Nonstandard URL ports are not accepted")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
              if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    path = quote(parts.path.rstrip("/") or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit(("https", host, path, urlencode(sorted(params)), ""))


def source_permits_storage(source: Source, now: datetime | None = None) -> bool:
    now = now or utc_now()
    return bool(
        source.enabled and source.can_discover and source.can_fetch and source.can_store
        and source.permission_basis.strip() and source.permission_reviewed_at
        and source.permission_reviewed_at <= now
        and (source.permission_expires_at is None or source.permission_expires_at > now)
    )


def _hash_input(data: SourceInput) -> str:
    payload = {
        "title": data.title, "text": data.text,
        "claims": [claim.model_dump() for claim in data.claims],
        "places": [hint.model_dump() for hint in data.place_hints],
        "event_at": data.event_at.isoformat() if data.event_at else None,
        "event_until": data.event_until.isoformat() if data.event_until else None,
        "event_type": data.event_type, "topic": data.topic,
        "audience_scope": data.audience_scope,
        "risk_flags": sorted(data.risk_flags),
        "published_at": data.published_at.isoformat() if data.published_at else None,
        "updated_at": data.updated_at.isoformat() if data.updated_at else None,
    }
    normalized = unicodedata.normalize("NFC", json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _insert_item(session: Session, source: Source, data: SourceInput, url: str) -> SourceItem:
    values = dict(
        id=new_id(), source_id=source.id, canonical_url=url, original_url=data.url,
        feed_guid=data.guid, original_title=data.title, event_type=data.event_type,
        topic=data.topic, audience_scope=data.audience_scope,
        source_published_at=data.published_at,
        source_updated_at=data.updated_at, event_at=data.event_at,
        event_until=data.event_until, discovered_at=utc_now(), fetched_at=utc_now(),
        current_revision=1,
    )
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(SourceItem)
    elif dialect == "sqlite":
        statement = sqlite_insert(SourceItem)
    else:
        raise ValueError("Only PostgreSQL and SQLite are supported")
    session.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=[SourceItem.source_id, SourceItem.canonical_url],
    ))
    return session.scalar(select(SourceItem).where(
        SourceItem.source_id == source.id, SourceItem.canonical_url == url,
    ).with_for_update())


def ingest_item(session: Session, source: Source, data: SourceInput) -> IngestResult:
    if not source_permits_storage(source):
        raise PermissionError("Discovery, fetch, storage and reviewed permissions are required")
    url = canonical_url(data.url)
    if urlsplit(url).hostname != source.canonical_domain:
        raise ValueError("Item URL is outside the registered source domain")
    if data.claims and not source.can_process_ai:
        raise PermissionError("Evidence processing is not permitted for this source")
    content_hash = _hash_input(data)
    item = _insert_item(session, source, data, url)
    latest = session.scalar(select(SourceRevision).where(
        SourceRevision.source_item_id == item.id,
    ).order_by(SourceRevision.number.desc()).limit(1))
    if latest and latest.content_hash == content_hash:
        item.fetched_at = utc_now()
        return IngestResult(item.id, latest.id, "unchanged")
    number = latest.number + 1 if latest else 1
    revision = SourceRevision(
        source_item_id=item.id, number=number, content_hash=content_hash,
        title=data.title, permitted_text=data.text,
        evidence_claims=[claim.model_dump() for claim in data.claims],
        place_hints=[hint.model_dump() for hint in data.place_hints],
        risk_flags=data.risk_flags,
        analysis_state="completed" if data.claims else "pending",
    )
    session.add(revision)
    item.current_revision = number
    item.original_title = data.title
    item.source_published_at = data.published_at
    item.source_updated_at = data.updated_at
    item.event_at = data.event_at
    item.event_until = data.event_until
    item.event_type = data.event_type
    item.topic = data.topic
    item.audience_scope = data.audience_scope
    item.fetched_at = utc_now()
    session.flush()
    if item.story_id:
        story = session.get(Story, item.story_id)
        story.status = "new"
        story.updated_at = utc_now()
        article = session.scalar(select(Article).where(Article.story_id == item.story_id))
        if article and article.status != "published":
            article.status = "deferred"
        if data.claims:
            from greek_news.stories import refresh_story
            from greek_news.config import get_settings
            refresh_story(session, story, get_settings().geo_register_id)
    return IngestResult(item.id, revision.id, "revised" if latest else "new")


def normalize_headline(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
