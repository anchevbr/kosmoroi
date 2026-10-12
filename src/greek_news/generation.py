"""Durable, idempotent evidence-to-review workflow outside HTTP handlers."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.article_schema import ArticleDraft
from greek_news.budget import complete_generation, mark_uncertain, reserve_generation
from greek_news.config import Settings
from greek_news.db import utc_now
from greek_news.evidence import freeze_evidence
from greek_news.llm import DraftAdapter, PROMPT_VERSION, configured_adapter
from greek_news.models import AnalysisCache, Article, ArticleVersion, AuditEntry, Job, Story
from greek_news.publishing import auto_decision, request_publication
from greek_news.validation import validate_draft


@dataclass(frozen=True)
class GenerationOutcome:
    state: str
    article_id: str | None = None
    version: int | None = None
    reason: str = ""


def _completed(job: Job, article_id: str | None, version: int | None) -> GenerationOutcome:
    job.state = "completed"
    job.completed_at = utc_now()
    job.payload = {**job.payload, "article_id": article_id, "version": version}
    return GenerationOutcome("completed", article_id, version)


def generate_article(
    session_factory: Callable[[], Session], story_id: str, job_id: str, settings: Settings,
    *, adapter: DraftAdapter | None = None, now: datetime | None = None,
) -> GenerationOutcome:
    now = now or utc_now()
    with session_factory() as session, session.begin():
        job = session.get(Job, job_id, with_for_update=True)
        if not job or job.kind != "generate" or job.payload.get("story_id") != story_id:
            raise ValueError("A durable generation job for this story is required")
        if job.state == "completed":
            return GenerationOutcome("completed", job.payload.get("article_id"),
                                     job.payload.get("version"))
        if job.state == "failed":
            raise ValueError("A failed model call must be reconciled before retry")
        story = session.get(Story, story_id, with_for_update=True)
        if not story:
            raise ValueError("Unknown story")
        packet = freeze_evidence(session, story, now=now)
        if packet.conflict or "ambiguous_geography" in packet.risk_flags:
            story.status = "skipped"
            session.add(AuditEntry(actor="pipeline", action="story_skipped", subject="story",
                                   subject_id=story.id,
                                   detail={"reason": "Conflicting or ambiguous evidence"}))
            _completed(job, None, None)
            return GenerationOutcome("blocked", reason="Conflicting or ambiguous evidence")
        if packet.event_until and packet.event_until < now and packet.event_type in {
            "water_notice", "road_closure", "power_notice", "transport_notice",
        }:
            story.status = "skipped"
            session.add(AuditEntry(actor="pipeline", action="story_skipped", subject="story",
                                   subject_id=story.id, detail={"reason": "Expired service notice"}))
            _completed(job, None, None)
            return GenerationOutcome("blocked", reason="Service notice has passed its effective interval")
        previous = session.scalar(select(Article).where(Article.story_id == story.id))
        latest = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == previous.id,
            ArticleVersion.number == previous.current_version,
        )) if previous else None
        if latest and latest.evidence_hash == packet.fingerprint() and latest.evidence_packet:
            return _completed(job, previous.id, previous.current_version)
        key = hashlib.sha256((packet.fingerprint() + settings.llm_provider
                              + settings.llm_model + PROMPT_VERSION).encode()).hexdigest()
        cached = session.get(AnalysisCache, key)
        if cached is None:
            reserve_generation(session, job_id, settings, now=now)
            job.state = "running"

    result = None
    if cached:
        draft = ArticleDraft.model_validate(cached.result)
    else:
        try:
            result = (adapter or configured_adapter(settings)).draft(packet)
            draft = result.draft
        except Exception as exc:
            with session_factory() as session, session.begin():
                mark_uncertain(session, job_id)
                failed = session.get(Job, job_id, with_for_update=True)
                failed.state = "failed"
                failed.last_error = str(exc)[:1000]
            raise

    with session_factory() as session, session.begin():
        job = session.get(Job, job_id, with_for_update=True)
        story = session.get(Story, story_id, with_for_update=True)
        if freeze_evidence(session, story, now=now).fingerprint() != packet.fingerprint():
            if result:
                mark_uncertain(session, job_id)
            job.state = "failed"
            job.last_error = "Evidence changed while drafting; proposal discarded"
            story.status = "new"
            return GenerationOutcome("blocked", reason=job.last_error)
        article = session.scalar(select(Article).where(
            Article.story_id == story_id,
        ).with_for_update())
        previous = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article.id,
            ArticleVersion.number == article.current_version,
        )) if article else None
        if previous and previous.evidence_hash == packet.fingerprint() and previous.evidence_packet:
            if result:
                complete_generation(session, job_id, result, settings, article.id)
            return _completed(job, article.id, article.current_version)
        report = validate_draft(draft, packet, now)
        if article is None:
            article = Article(
                story_id=story_id, slug=draft.slug, status="draft", current_version=1,
                author_name="Αυτοματοποιημένη σύνταξη",
                automation_disclosure="Συντάχθηκε και ελέγχθηκε αυτόματα βάσει των αναφερόμενων πηγών.",
            )
            session.add(article)
            session.flush()
            number = 1
        else:
            number = article.current_version + 1
            article.current_version = number
        version = ArticleVersion(
            article_id=article.id, number=number, content=draft.model_dump(mode="json"),
            evidence_packet=packet.model_dump(mode="json"),
            evidence_hash=packet.fingerprint(), validation=report.model_dump(),
            status="proposed", change_summary="New evidence revision; automated validation"
            if number > 1 else "Initial evidence-backed proposal",
        )
        session.add(version)
        decision = auto_decision(session, packet, report, settings, now)
        session.flush()
        if decision.allowed:
            version.status = "approved"
            version.approved_by = "automated_validation"
            version.approved_at = now
            if article.published_version:
                version.correction_note = "Ενημέρωση βάσει νεότερων στοιχείων από τις πηγές."
            article.approved_version = number
            article.status = "published" if article.published_version else "approved"
            story.status = "processed"
            request_publication(session, article.id, settings,
                                reason="Validated evidence and automatic policy", now=now)
        else:
            version.status = "skipped" if not report.valid or packet.conflict else "proposed"
            if article.published_version is None:
                article.status = "skipped" if version.status == "skipped" else "deferred"
            story.status = "skipped" if version.status == "skipped" else "deferred"
            session.add(AuditEntry(
                actor="pipeline", action="automatic_decision_deferred", subject="article",
                subject_id=article.id,
                detail={"version": number, "reasons": decision.reasons},
            ))
        if cached is None:
            session.add(AnalysisCache(key=key, result=draft.model_dump(mode="json")))
            complete_generation(session, job_id, result, settings, article.id)
        session.add(AuditEntry(
            actor="pipeline", action="draft_proposed", subject="article", subject_id=article.id,
            detail={"version": number, "validation": report.model_dump(),
                    "evidence_hash": packet.fingerprint(),
                    "auto_publication_allowed": decision.allowed,
                    "decision_reasons": decision.reasons},
        ))
        return _completed(job, article.id, number)