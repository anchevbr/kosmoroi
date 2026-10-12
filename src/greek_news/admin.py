"""Authenticated source registry and operator routes."""

import hashlib
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from greek_news.article_schema import ArticleDraft
from greek_news.config import Settings, get_settings
from greek_news.db import get_session, utc_now
from greek_news.evidence import freeze_evidence
from greek_news.jobs import enqueue, retry_failed
from greek_news.models import (
    Article, ArticleVersion, AuditEntry, DailyBudget, GeoRegister, Job, Place,
    PublicationPolicy, Source, SourceItem, SourceRevision, Story, StoryPlace,
)
from greek_news.publishing import (
    approve_version, control, request_publication, set_pause, withdraw_article,
)
from greek_news.security import Actor, require_admin, require_editor
from greek_news.stories import ATHENS, independent_origins, refresh_story
from greek_news.validation import validate_draft


router = APIRouter(prefix="/admin", tags=["admin"])


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    canonical_domain: str
    feed_url: str
    source_type: str = "rss"
    extraction_strategy: str = "feed_metadata"
    language: str = "el"
    coverage_ids: list[str] = Field(default_factory=list)
    enabled: bool = False
    operator_disabled: bool = False
    poll_minutes: int = Field(default=30, ge=5, le=1440)
    priority: int = 0
    trust_category: str = "unreviewed"
    organization: str = ""
    syndication_group: str | None = None
    can_discover: bool = False
    can_fetch: bool = False
    can_store: bool = False
    can_process_ai: bool = False
    can_reuse_text: bool = False
    can_reuse_images: bool = False
    permission_basis: str = ""
    terms_url: str | None = None
    permission_reviewed_at: datetime | None = None
    permission_expires_at: datetime | None = None
    verification_note: str = "Not verified"
    endpoint_verified_at: datetime | None = None

    @model_validator(mode="after")
    def check_activation(self) -> "SourceConfig":
        url = urlsplit(self.feed_url)
        if (url.scheme != "https" or url.hostname != self.canonical_domain
                or url.username or url.password or url.port not in {None, 443}):
            raise ValueError("Source URL must use its registered HTTPS domain")
        if self.source_type not in {"rss", "atom", "html_index"}:
            raise ValueError("Unsupported source type")
        if self.enabled and (
            self.operator_disabled or
            self.source_type not in {"rss", "atom"}
            or self.extraction_strategy not in {"feed_metadata", "feed_summary", "approved_html"}
            or not all((self.can_discover, self.can_fetch, self.can_store))
            or not self.permission_basis.strip() or not self.permission_reviewed_at
            or not self.endpoint_verified_at
            or self.permission_expires_at and self.permission_expires_at <= utc_now()
        ):
            raise ValueError("Activation requires a verified feed and reviewed discovery/fetch/storage rights")
        return self


def source_record(source: Source) -> dict:
    return {**{key: getattr(source, key) for key in SourceConfig.model_fields},
            "id": source.id, "last_successful_fetch": source.last_successful_fetch,
            "last_successful_extraction": source.last_successful_extraction,
            "last_new_item": source.last_new_item, "last_status_code": source.last_status_code,
            "consecutive_failures": source.consecutive_failures,
            "last_latency_ms": source.last_latency_ms, "etag": source.etag,
            "last_modified_header": source.last_modified_header,
            "next_poll_at": source.next_poll_at,
            "last_discovery_check": source.last_discovery_check}


@router.get("/sources")
def list_sources(
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> list[dict]:
    return [source_record(source) for source in session.scalars(select(Source).order_by(Source.name))]


@router.post("/sources", status_code=201)
def create_source(
    config: SourceConfig, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        if session.scalar(select(Source).where(Source.feed_url == config.feed_url)):
            raise HTTPException(status_code=409, detail="Source endpoint already registered")
        source = Source(**config.model_dump())
        session.add(source)
        session.flush()
        session.add(AuditEntry(actor=actor.role, action="source_created", subject="source",
                               subject_id=source.id, detail={"enabled": source.enabled}))
        result = source_record(source)
    return result


@router.patch("/sources/{source_id}")
def edit_source(
    source_id: str, changes: dict, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        source = session.get(Source, source_id, with_for_update=True)
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        previous = {key: getattr(source, key) for key in SourceConfig.model_fields}
        if changes.get("enabled") is False and "operator_disabled" not in changes:
            changes = {**changes, "operator_disabled": True}
        try:
            updated = SourceConfig.model_validate({**previous, **changes})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for key, value in updated.model_dump().items():
            setattr(source, key, value)
        session.add(AuditEntry(actor=actor.role, action="source_changed", subject="source",
                               subject_id=source.id, detail={"changed_fields": sorted(changes)}))
        result = source_record(source)
    return result


class ReasonInput(BaseModel):
    reason: str = Field(min_length=3)


class ApprovalInput(ReasonInput):
    editor_name: str = Field(min_length=2)
    correction_note: str | None = None


class PauseInput(BaseModel):
    publication_paused: bool
    generation_paused: bool


class PolicyInput(BaseModel):
    source_id: str
    topic: str
    enabled: bool = False
    rationale: str = Field(min_length=5)
    pilot_approved_at: datetime | None = None


def article_record(session: Session, article: Article) -> dict:
    versions = session.scalars(select(ArticleVersion).where(
        ArticleVersion.article_id == article.id,
    ).order_by(ArticleVersion.number.desc())).all()
    return {
        "id": article.id, "story_id": article.story_id, "status": article.status,
        "slug": article.slug, "current_version": article.current_version,
        "approved_version": article.approved_version, "published_version": article.published_version,
        "published_at": article.published_at, "modified_at": article.modified_at,
        "withdrawal_reason": article.withdrawal_reason,
        "versions": [{"number": version.number, "status": version.status,
                      "validation": version.validation, "content": version.content,
                      "evidence_packet": version.evidence_packet,
                      "change_summary": version.change_summary,
                      "correction_note": version.correction_note,
                      "human_edited": version.human_edited,
                      "approved_by": version.approved_by} for version in versions],
    }


@router.get("/stories")
def list_stories(
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> list[dict]:
    return [{"id": story.id, "status": story.status, "event_type": story.event_type,
             "topic": story.topic, "audience_scope": story.audience_scope,
             "score": story.score, "selection_reason": story.selection_reason}
            for story in session.scalars(select(Story).order_by(Story.updated_at.desc()).limit(100))]


@router.get("/stories/{story_id}")
def inspect_story(
    story_id: str, session: Session = Depends(get_session),
    actor: Actor = Depends(require_editor),
) -> dict:
    story = session.get(Story, story_id)
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    items = session.scalars(select(SourceItem).where(SourceItem.story_id == story.id)).all()
    return {
        "id": story.id, "status": story.status, "conflict": story.conflict,
        "risk_flags": story.risk_flags, "score": story.score,
        "selection_reason": story.selection_reason,
        "independent_origins": independent_origins(session, story.id),
        "places": [{"id": link.place_id, "role": link.role}
                   for link in session.scalars(select(StoryPlace).where(
                       StoryPlace.story_id == story.id,
                   ))],
        "items": [{"id": item.id, "source_id": item.source_id,
                   "original_url": item.original_url, "published_at": item.source_published_at,
                   "revisions": [{"id": revision.id, "number": revision.number,
                                  "claims": revision.evidence_claims,
                                  "place_hints": revision.place_hints}
                                 for revision in session.scalars(select(SourceRevision).where(
                                     SourceRevision.source_item_id == item.id,
                                 ).order_by(SourceRevision.number))]}
                  for item in items],
    }


class MergeInput(ReasonInput):
    source_story_id: str


class SplitInput(ReasonInput):
    source_item_id: str


@router.post("/stories/{story_id}/merge")
def merge_stories(
    story_id: str, payload: MergeInput,
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_editor),
) -> dict:
    if story_id == payload.source_story_id:
        raise HTTPException(status_code=409, detail="Cannot merge a story into itself")
    with session.begin():
        target = session.get(Story, story_id, with_for_update=True)
        other = session.get(Story, payload.source_story_id, with_for_update=True)
        if not target or not other or target.status == "merged" or other.status == "merged":
            raise HTTPException(status_code=404, detail="Active stories are required")
        if (target.event_type != other.event_type or target.anchor_day != other.anchor_day
                or session.scalar(select(Article).where(Article.story_id.in_([
                    target.id, other.id,
                ])))):
            raise HTTPException(status_code=409, detail="Published/drafted or distinct events cannot be merged")
        for item in session.scalars(select(SourceItem).where(SourceItem.story_id == other.id)):
            item.story_id = target.id
        session.flush()
        for link in session.scalars(select(StoryPlace).where(StoryPlace.story_id == other.id)):
            session.delete(link)
        refresh_story(session, target, settings.geo_register_id)
        target.status = "needs_review"
        other.status = "merged"
        session.add(AuditEntry(actor=actor.role, action="stories_merged", subject="story",
                               subject_id=story_id,
                               detail={"merged_id": other.id, "reason": payload.reason}))
        return {"story_id": target.id, "merged_id": other.id, "status": target.status}


@router.post("/stories/{story_id}/split")
def split_story(
    story_id: str, payload: SplitInput,
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        story = session.get(Story, story_id, with_for_update=True)
        item = session.get(SourceItem, payload.source_item_id, with_for_update=True)
        if not story or not item or item.story_id != story_id:
            raise HTTPException(status_code=404, detail="Story item not found")
        if session.scalar(select(Article).where(Article.story_id == story_id)):
            raise HTTPException(status_code=409, detail="Drafted/published stories require editorial handling")
        reference = item.event_at or item.source_published_at
        if reference is None:
            raise HTTPException(status_code=409, detail="Event timestamp is required for a split")
        fingerprint = hashlib.sha256(f"split:{item.id}".encode()).hexdigest()
        created = Story(
            fingerprint=fingerprint, event_type=item.event_type, topic=item.topic,
            anchor_day=reference.astimezone(ATHENS).date(), event_at=item.event_at,
            event_until=item.event_until, audience_scope=item.audience_scope,
            status="needs_review", risk_flags=["manual_split"],
        )
        session.add(created)
        session.flush()
        item.story_id = created.id
        session.flush()
        refresh_story(session, created, settings.geo_register_id)
        refresh_story(session, story, settings.geo_register_id)
        story.status = "needs_review" if session.scalar(select(SourceItem).where(
            SourceItem.story_id == story.id,
        )) else "empty"
        session.add(AuditEntry(actor=actor.role, action="story_split", subject="story",
                               subject_id=story_id,
                               detail={"new_id": created.id, "item_id": item.id,
                                       "reason": payload.reason}))
        return {"original_id": story_id, "new_id": created.id, "status": created.status}


@router.post("/stories/{story_id}/generate", status_code=202)
def queue_generation(
    story_id: str, session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings), actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        if control(session).generation_paused:
            raise HTTPException(status_code=409, detail="Generation is paused")
        story = session.get(Story, story_id)
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        try:
            packet = freeze_evidence(session, story)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job = enqueue(session, "generate", {"story_id": story_id},
                      f"generate:{story_id}:{packet.fingerprint()}")
        session.add(AuditEntry(actor=actor.role, action="generation_requested",
                               subject="story", subject_id=story_id,
                               detail={"job_id": job.id}))
        return {"job_id": job.id, "state": job.state}


@router.get("/articles")
def list_drafts(
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> list[dict]:
    return [{"id": article.id, "story_id": article.story_id,
             "status": article.status, "current_version": article.current_version,
             "published_version": article.published_version}
            for article in session.scalars(select(Article).order_by(Article.created_at.desc()).limit(100))]


@router.get("/articles/{article_id}")
def inspect_article(
    article_id: str, session: Session = Depends(get_session),
    actor: Actor = Depends(require_editor),
) -> dict:
    article = session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article_record(session, article)


class EditInput(ReasonInput):
    content: ArticleDraft


@router.post("/articles/{article_id}/versions", status_code=201)
def propose_human_revision(
    article_id: str, payload: EditInput, session: Session = Depends(get_session),
    actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        article = session.get(Article, article_id, with_for_update=True)
        if not article or article.status == "withdrawn":
            raise HTTPException(status_code=404, detail="Active article not found")
        story = session.get(Story, article.story_id)
        try:
            packet = freeze_evidence(session, story)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        report = validate_draft(payload.content, packet)
        number = article.current_version + 1
        version = ArticleVersion(
            article_id=article_id, number=number, content=payload.content.model_dump(mode="json"),
            evidence_packet=packet.model_dump(mode="json"),
            evidence_hash=packet.fingerprint(), validation=report.model_dump(),
            status="proposed", change_summary=payload.reason, human_edited=True,
        )
        session.add(version)
        article.current_version = number
        if article.published_version is None:
            article.status = "needs_review"
        session.add(AuditEntry(actor=actor.role, action="human_revision_proposed",
                               subject="article", subject_id=article_id,
                               detail={"version": number, "reason": payload.reason,
                                       "validation": report.model_dump()}))
        return {"article_id": article_id, "version": number,
                "status": version.status, "validation": report.model_dump()}


@router.post("/articles/{article_id}/versions/{number}/approve")
def approve_article(
    article_id: str, number: int, payload: ApprovalInput,
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        try:
            approved = approve_version(session, article_id, number,
                                       editor_name=payload.editor_name, reason=payload.reason,
                                       correction_note=payload.correction_note)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"article_id": article_id, "version": number, "status": approved.status}


@router.post("/articles/{article_id}/versions/{number}/reject")
def reject_version(
    article_id: str, number: int, payload: ReasonInput,
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        article = session.get(Article, article_id, with_for_update=True)
        version = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article_id, ArticleVersion.number == number,
        ))
        if not article or not version or version.status != "proposed":
            raise HTTPException(status_code=409, detail="Only a proposed version may be rejected")
        version.status = "rejected"
        if article.published_version is None:
            article.status = "rejected"
        session.add(AuditEntry(actor=actor.role, action="reject_version",
                               subject="article", subject_id=article_id,
                               detail={"version": number, "reason": payload.reason}))
        return {"article_id": article_id, "version": number, "status": version.status}


@router.post("/articles/{article_id}/publish", status_code=202)
def queue_publication(
    article_id: str, payload: ReasonInput, session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings), actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        try:
            record = request_publication(session, article_id, settings, reason=payload.reason)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        job = enqueue(session, "publish", {"record_id": record.id}, f"publish:{record.id}")
        return {"record_id": record.id, "job_id": job.id, "state": record.state}


@router.post("/articles/{article_id}/withdraw")
def withdraw(
    article_id: str, payload: ReasonInput, session: Session = Depends(get_session),
    actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        try:
            article = withdraw_article(session, article_id, actor=actor.role, reason=payload.reason)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"article_id": article.id, "status": article.status}


@router.get("/jobs")
def list_jobs(
    state: str | None = None, session: Session = Depends(get_session),
    actor: Actor = Depends(require_editor),
) -> list[dict]:
    statement = select(Job).order_by(Job.created_at.desc()).limit(100)
    if state:
        statement = statement.where(Job.state == state)
    return [{"id": job.id, "kind": job.kind, "state": job.state, "attempts": job.attempts,
             "last_error": job.last_error, "next_run_at": job.next_run_at, "payload": job.payload}
            for job in session.scalars(statement)]


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        try:
            job = retry_failed(session, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.add(AuditEntry(actor=actor.role, action="retry_job", subject="job",
                               subject_id=job_id, detail={"attempts": job.attempts}))
        return {"job_id": job.id, "state": job.state}


@router.post("/sources/{source_id}/poll", status_code=202)
def queue_poll(
    source_id: str, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        source = session.get(Source, source_id)
        if not source or not source.enabled or not source.endpoint_verified_at:
            raise HTTPException(status_code=409, detail="Verified active source required")
        job = enqueue(session, "poll", {"source_id": source_id},
                      f"poll:{source_id}:{int(utc_now().timestamp() // 60)}")
        return {"job_id": job.id, "state": job.state}


@router.get("/policies")
def list_policies(
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> list[dict]:
    return [{"id": row.id, "source_id": row.source_id, "topic": row.topic,
             "enabled": row.enabled, "rationale": row.rationale,
             "pilot_approved_at": row.pilot_approved_at}
            for row in session.scalars(select(PublicationPolicy))]


@router.put("/policies")
def configure_policy(
    payload: PolicyInput, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        source = session.get(Source, payload.source_id)
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        if payload.enabled and (
            payload.topic not in {"services", "events", "transport"}
            or
            not source.enabled or not source.can_process_ai or not payload.pilot_approved_at
            or not source.permission_reviewed_at or source.source_type == "fixture"
        ):
            raise HTTPException(status_code=409, detail="Reviewed real-source pilot required")
        policy = session.scalar(select(PublicationPolicy).where(
            PublicationPolicy.source_id == payload.source_id,
            PublicationPolicy.topic == payload.topic,
        ).with_for_update())
        if policy is None:
            policy = PublicationPolicy(source_id=payload.source_id, topic=payload.topic)
            session.add(policy)
        policy.enabled = payload.enabled
        policy.rationale = payload.rationale
        policy.pilot_approved_at = payload.pilot_approved_at
        session.add(AuditEntry(actor=actor.role, action="policy_changed", subject="source",
                               subject_id=payload.source_id,
                               detail={"topic": payload.topic, "enabled": policy.enabled,
                                       "rationale": payload.rationale}))
        return {"source_id": payload.source_id, "topic": payload.topic,
                "enabled": policy.enabled}


@router.get("/control")
def get_control(
    session: Session = Depends(get_session), actor: Actor = Depends(require_editor),
) -> dict:
    with session.begin():
        item = control(session)
        return {"publication_paused": item.publication_paused,
                "generation_paused": item.generation_paused, "updated_at": item.updated_at}


@router.put("/control")
def update_control(
    payload: PauseInput, session: Session = Depends(get_session),
    actor: Actor = Depends(require_admin),
) -> dict:
    with session.begin():
        item = set_pause(session, actor=actor.role, **payload.model_dump())
        return {"publication_paused": item.publication_paused,
                "generation_paused": item.generation_paused}


@router.get("/metrics")
def operational_metrics(
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings),
    actor: Actor = Depends(require_editor),
) -> dict:
    now = utc_now()
    source_rows = session.scalars(select(Source)).all()
    healthy = sum(bool(source.enabled and source.last_successful_fetch
                       and (now - source.last_successful_fetch).total_seconds()
                       < source.poll_minutes * 120) for source in source_rows)
    published = session.scalars(select(Article).where(Article.status == "published")).all()
    live_register = session.get(GeoRegister, settings.geo_register_id)
    regions = session.scalars(select(Place).where(
        Place.register_id == settings.geo_register_id, Place.kind == "region",
    ).order_by(Place.id)).all()
    coverage = []
    from greek_news.geography import region_of

    for region in regions:
        relevant = []
        for article in published:
            links = session.scalars(select(StoryPlace).where(
                StoryPlace.story_id == article.story_id,
                StoryPlace.role.in_(["affected", "occurred"]),
            ))
            if any(region_of(session, session.get(Place, link.place_id)).id == region.id
                   for link in links):
                relevant.append(article)
        coverage.append({
            "region_id": region.id, "name_el": region.name_el,
            "published_articles": len(relevant),
            "last_substantive_story_at": max(
                (article.published_at for article in relevant), default=None,
            ), "gap": not relevant,
        })
    day = session.get(DailyBudget, now.date())
    accepted = session.scalar(select(func.count()).select_from(Article).where(
        Article.status.in_(["approved", "published"]),
    ))
    priced = settings.input_cost_per_million is not None and settings.output_cost_per_million is not None
    usage = {
        "calls_reserved": day.calls_reserved if day else 0,
        "tokens_reserved": day.tokens_reserved if day else 0,
        "generations_reserved": day.generations_reserved if day else 0,
        "input_tokens_used": day.input_tokens_used if day else 0,
        "output_tokens_used": day.output_tokens_used if day else 0,
        "cost_usd": day.cost_usd if day and priced else None,
        "cost_per_accepted_article_usd": (
            day.cost_usd / accepted if day and priced and accepted and day.cost_usd is not None
            else None
        ),
        "cost_available": priced,
    }
    return {
        "sources": {"total": len(source_rows), "enabled": sum(source.enabled for source in source_rows),
                    "healthy": healthy},
        "articles": {"published": len(published), "accepted": accepted},
        "jobs": {"pending": session.scalar(select(func.count()).select_from(Job).where(
            Job.state == "pending",
        )), "failed": session.scalar(select(func.count()).select_from(Job).where(
            Job.state == "failed",
        ))},
        "coverage": {"register": live_register.id if live_register else None,
                     "fixture": live_register.is_fixture if live_register else None,
                     "regions": coverage},
        "daily_usage": usage,
    }