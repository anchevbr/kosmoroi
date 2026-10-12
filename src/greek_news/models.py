"""Durable editorial, geographic, permission and pipeline records."""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from greek_news.db import Base, UTCDateTime, new_id, utc_now


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    canonical_domain: Mapped[str] = mapped_column(String(255))
    feed_url: Mapped[str] = mapped_column(String(2000), unique=True)
    source_type: Mapped[str] = mapped_column(String(40), default="html")
    extraction_strategy: Mapped[str] = mapped_column(String(40), default="article_html")
    country_code: Mapped[str] = mapped_column(String(2), default="GR")
    locality_hint: Mapped[str] = mapped_column(String(200), default="")
    entry_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    article_url_pattern: Mapped[str | None] = mapped_column(String(500))
    index_url_pattern: Mapped[str | None] = mapped_column(String(500))
    link_selector: Mapped[str | None] = mapped_column(String(500))
    is_directory: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(12), default="el")
    coverage_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    operator_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_discovery_check: Mapped[datetime | None] = mapped_column(UTCDateTime())
    poll_minutes: Mapped[int] = mapped_column(Integer, default=30)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    trust_category: Mapped[str] = mapped_column(String(40), default="unreviewed")
    organization: Mapped[str] = mapped_column(String(200), default="")
    syndication_group: Mapped[str | None] = mapped_column(String(100))
    can_discover: Mapped[bool] = mapped_column(Boolean, default=False)
    can_fetch: Mapped[bool] = mapped_column(Boolean, default=False)
    can_store: Mapped[bool] = mapped_column(Boolean, default=False)
    can_process_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    can_reuse_text: Mapped[bool] = mapped_column(Boolean, default=False)
    can_reuse_images: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_basis: Mapped[str] = mapped_column(Text, default="")
    terms_url: Mapped[str | None] = mapped_column(String(2000))
    permission_reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    permission_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    verification_note: Mapped[str] = mapped_column(Text, default="Not verified")
    endpoint_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_fetch: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_successful_extraction: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_new_item: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified_header: Mapped[str | None] = mapped_column(String(500))
    retry_after: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_poll_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_request_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    robots_txt: Mapped[str | None] = mapped_column(Text)
    robots_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class GeoRegister(Base):
    __tablename__ = "geo_registers"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(100))
    provenance: Mapped[str] = mapped_column(String(2000))
    is_fixture: Mapped[bool] = mapped_column(Boolean)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Place(Base):
    __tablename__ = "places"
    __table_args__ = (
        UniqueConstraint("register_id", "official_id"),
        Index("ix_places_parent_id", "parent_id"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    register_id: Mapped[str] = mapped_column(ForeignKey("geo_registers.id"))
    official_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    name_el: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("places.id"))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)


class PlaceRelation(Base):
    __tablename__ = "place_relations"
    __table_args__ = (UniqueConstraint("place_id", "related_id", "relation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id"))
    related_id: Mapped[str] = mapped_column(ForeignKey("places.id"))
    relation: Mapped[str] = mapped_column(String(40))


class PlaceAlias(Base):
    __tablename__ = "place_aliases"
    __table_args__ = (UniqueConstraint("place_id", "normalized_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id"), index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)


class CrawlURL(Base):
    __tablename__ = "crawl_urls"
    __table_args__ = (UniqueConstraint("source_id", "url"),
                      Index("ix_crawl_urls_due", "state", "next_fetch_at"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    title_hint: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="article")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    next_fetch_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(500))
    last_error: Mapped[str | None] = mapped_column(Text)
    source_item_id: Mapped[str | None] = mapped_column(ForeignKey("source_items.id"))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Story(Base):
    __tablename__ = "stories"
    __table_args__ = (Index("ix_stories_topic_day", "topic", "anchor_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    topic: Mapped[str] = mapped_column(String(80))
    anchor_day: Mapped[date] = mapped_column(Date)
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    event_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    audience_scope: Mapped[str] = mapped_column(String(20), default="local")
    status: Mapped[str] = mapped_column(String(30), default="new")
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    score: Mapped[int] = mapped_column(Integer, default=0)
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class StoryPlace(Base):
    __tablename__ = "story_places"
    __table_args__ = (UniqueConstraint("story_id", "place_id", "role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    place_id: Mapped[str] = mapped_column(ForeignKey("places.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))


class SourceItem(Base):
    __tablename__ = "source_items"
    __table_args__ = (UniqueConstraint("source_id", "canonical_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    story_id: Mapped[str | None] = mapped_column(ForeignKey("stories.id"), index=True)
    canonical_url: Mapped[str] = mapped_column(String(2000))
    original_url: Mapped[str] = mapped_column(String(2000))
    feed_guid: Mapped[str | None] = mapped_column(String(2000))
    original_title: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(80), default="unclassified")
    topic: Mapped[str] = mapped_column(String(80), default="general")
    audience_scope: Mapped[str] = mapped_column(String(20), default="unknown")
    source_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    event_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    event_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)


class SourceRevision(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (
        UniqueConstraint("source_item_id", "number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_item_id: Mapped[str] = mapped_column(ForeignKey("source_items.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    permitted_text: Mapped[str | None] = mapped_column(Text)
    evidence_claims: Mapped[list[dict]] = mapped_column(JSON, default=list)
    place_hints: Mapped[list[dict]] = mapped_column(JSON, default=list)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    revised_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), unique=True)
    slug: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    approved_version: Mapped[int | None] = mapped_column(Integer)
    published_version: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    modified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    author_name: Mapped[str] = mapped_column(String(200), default="Editorial team")
    editor_name: Mapped[str | None] = mapped_column(String(200))
    automation_disclosure: Mapped[str] = mapped_column(Text, default="Assisted draft; editorial review required")
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    withdrawal_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ArticleVersion(Base):
    __tablename__ = "article_versions"
    __table_args__ = (UniqueConstraint("article_id", "number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    evidence_packet: Mapped[dict | None] = mapped_column(JSON)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    change_summary: Mapped[str] = mapped_column(Text, default="")
    correction_note: Mapped[str | None] = mapped_column(Text)
    human_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ArticleFacet(Base):
    """Published snapshot facets for indexed, SQL-filtered frontend queries."""
    __tablename__ = "article_facets"
    __table_args__ = (UniqueConstraint("article_id", "kind", "value"),
                      Index("ix_article_facets_lookup", "kind", "value", "article_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(200))


class PublicationPolicy(Base):
    __tablename__ = "publication_policies"
    __table_args__ = (UniqueConstraint("source_id", "topic"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    topic: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pilot_approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rationale: Mapped[str] = mapped_column(Text, default="")


class OperationalControl(Base):
    __tablename__ = "operational_controls"

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default="global")
    publication_paused: Mapped[bool] = mapped_column(Boolean, default=True)
    generation_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class PublicationRecord(Base):
    __tablename__ = "publication_records"
    __table_args__ = (UniqueConstraint("article_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.id"))
    version: Mapped[int] = mapped_column(Integer)
    publisher: Mapped[str] = mapped_column(String(30), default="local")
    state: Mapped[str] = mapped_column(String(30), default="pending")
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_due", "state", "next_run_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DailyBudget(Base):
    __tablename__ = "daily_budgets"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls_reserved: Mapped[int] = mapped_column(Integer, default=0)
    tokens_reserved: Mapped[int] = mapped_column(Integer, default=0)
    generations_reserved: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_reserved_usd: Mapped[float] = mapped_column(default=0.0)
    cost_usd: Mapped[float | None] = mapped_column(default=None)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    __table_args__ = (UniqueConstraint("job_id", "stage", "attempt", name="uq_llm_calls_stage_attempt"),)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30), default="draft")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    response: Mapped[dict | None] = mapped_column(JSON)
    request_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    article_id: Mapped[str | None] = mapped_column(ForeignKey("articles.id"))
    model: Mapped[str] = mapped_column(String(150))
    prompt_version: Mapped[str] = mapped_column(String(30))
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cost_usd: Mapped[float | None] = mapped_column(default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(default=None)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class AnalysisCache(Base):
    __tablename__ = "analysis_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class AuditEntry(Base):
    __tablename__ = "audit_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    subject: Mapped[str] = mapped_column(String(60))
    subject_id: Mapped[str] = mapped_column(String(160))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
