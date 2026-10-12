"""PostgreSQL-owned scheduling, recoverable leases and safe worker dispatch."""

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.budget import BudgetExceeded, mark_uncertain
from greek_news.config import Settings
from greek_news.db import new_id, utc_now
from greek_news.discovery import eligibility_reason, import_catalog, verify_candidate
from greek_news.evidence import freeze_evidence
from greek_news.generation import generate_article
from greek_news.ingestion import source_permits_storage
from greek_news.models import (
    Article, ArticleVersion, AuditEntry, Job, LLMCall, PublicationRecord,
    Source, SourceItem, SourceRevision,
)
from greek_news.polling import poll_source
from greek_news.publishing import control, publish_pending, resume_pending
from greek_news.stories import group_item, select_candidates


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def enqueue(
    session: Session, kind: str, payload: dict, key: str, *, next_run_at: datetime | None = None,
) -> Job:
    if kind not in {"poll", "generate", "publish", "discover", "catalog"}:
        raise ValueError("Unknown durable job type")
    dialect = session.get_bind().dialect.name
    statement = postgres_insert(Job) if dialect == "postgresql" else sqlite_insert(Job)
    session.execute(statement.values(
        id=new_id(), idempotency_key=key, kind=kind, payload=payload, state="pending",
        attempts=0, next_run_at=next_run_at or utc_now(), created_at=utc_now(),
    ).on_conflict_do_nothing(index_elements=[Job.idempotency_key]))
    return session.scalar(select(Job).where(Job.idempotency_key == key))


def schedule_due(session: Session, settings: Settings, now: datetime | None = None) -> int:
    now = now or utc_now()
    added = 0
    if settings.source_catalog_path:
        bucket = int(now.timestamp() // (settings.source_discovery_interval_minutes * 60))
        key = f"catalog:{bucket}"
        if not session.scalar(select(Job).where(Job.idempotency_key == key)):
            enqueue(session, "catalog", {"path": settings.source_catalog_path}, key,
                    next_run_at=now)
            added += 1
    resume_pending(session, settings, now)
    discovered = 0
    for source in session.scalars(select(Source).order_by(Source.priority.desc(), Source.id)):
        if source.enabled:
            if not source_permits_storage(source, now):
                source.enabled = False
                source.verification_note = "Source permissions are missing or expired"
                session.add(AuditEntry(actor="pipeline", action="source_auto_disabled",
                                       subject="source", subject_id=source.id,
                                       detail={"reason": source.verification_note}))
                continue
            if (source.source_type not in {"rss", "atom"}
                    or not source.endpoint_verified_at or source.next_poll_at and source.next_poll_at > now):
                continue
            pending = session.scalar(select(Job).where(
                Job.kind == "poll", Job.state.in_(["pending", "running"]),
                Job.idempotency_key.like(f"poll:{source.id}:%"),
            ).limit(1))
            if not pending:
                enqueue(session, "poll", {"source_id": source.id},
                        f"poll:{source.id}:{int(now.timestamp() // 60)}", next_run_at=now)
                added += 1
        elif (discovered < settings.source_discovery_max_per_tick
              and eligibility_reason(source, now) is None
              and (not source.next_poll_at or source.next_poll_at <= now)
              and (not source.last_discovery_check or source.last_discovery_check <= now - timedelta(
                  minutes=settings.source_discovery_interval_minutes,
              ))):
            key = f"discover:{source.id}:{int(now.timestamp() // 60 // settings.source_discovery_interval_minutes)}"
            existing = session.scalar(select(Job).where(Job.idempotency_key == key))
            if not existing:
                enqueue(session, "discover", {"source_id": source.id}, key, next_run_at=now)
                discovered += 1
                added += 1
    for record in session.scalars(select(PublicationRecord).where(PublicationRecord.state == "pending")):
        enqueue(session, "publish", {"record_id": record.id},
            f"publish:{record.id}", next_run_at=now)
    if not control(session).generation_paused:
        for story in select_candidates(
            session, now=now, national_max=settings.national_selection_max,
            regional_max=settings.regional_selection_max,
            minimum_score=settings.selection_min_score,
        ):
            try:
                fingerprint = freeze_evidence(session, story, now=now).fingerprint()
            except (PermissionError, ValueError):
                continue
            key = f"generate:{story.id}:{fingerprint}"
            existing = session.scalar(select(Job).where(Job.idempotency_key == key))
            article = session.scalar(select(Article).where(Article.story_id == story.id))
            latest = session.scalar(select(ArticleVersion).where(
                ArticleVersion.article_id == article.id,
                ArticleVersion.number == article.current_version,
            )) if article else None
            if latest and not latest.evidence_packet:
                key = f"regenerate:{story.id}:{fingerprint}:{article.current_version}"
                existing = session.scalar(select(Job).where(Job.idempotency_key == key))
            if not existing:
                enqueue(session, "generate", {"story_id": story.id}, key, next_run_at=now)
                added += 1
    return added


def claim_due(session: Session, *, now: datetime | None = None, limit: int = 10) -> list[str]:
    now = now or utc_now()
    expired = session.scalars(select(Job).where(
        Job.state == "running", Job.lease_until < now,
    ).with_for_update(skip_locked=True)).all()
    for job in expired:
        usage = session.scalar(select(LLMCall).where(LLMCall.job_id == job.id))
        if job.kind == "generate" and usage:
            mark_uncertain(session, job.id)
            job.state = "failed"
            job.last_error = "A previous model call has an uncertain outcome; manual reconciliation required"
        elif job.attempts >= MAX_ATTEMPTS:
            job.state = "failed"
            job.last_error = "Worker lease expired too many times"
        else:
            job.state = "pending"
            job.next_run_at = now
    due = session.scalars(select(Job).where(
        Job.state == "pending", Job.next_run_at <= now,
    ).order_by(case((Job.kind == "publish", 0), (Job.kind == "generate", 1),
                    (Job.kind == "poll", 2), (Job.kind == "discover", 3), else_=4),
               Job.created_at).limit(limit)
        .with_for_update(skip_locked=True)).all()
    for job in due:
        job.state = "running"
        job.attempts += 1
        job.lease_until = now + timedelta(minutes=5)
    session.flush()
    return [job.id for job in due]


def _complete(session: Session, job_id: str, result: dict) -> None:
    job = session.get(Job, job_id, with_for_update=True)
    job.state = "completed"
    job.completed_at = utc_now()
    job.lease_until = None
    job.payload = {**job.payload, "result": result}


def _failed(session: Session, job_id: str, exc: Exception, now: datetime) -> None:
    job = session.get(Job, job_id, with_for_update=True)
    usage = session.scalar(select(LLMCall).where(LLMCall.job_id == job_id))
    job.last_error = f"{type(exc).__name__}: {str(exc)[:900]}"
    job.lease_until = None
    if (job.attempts >= MAX_ATTEMPTS or job.kind == "generate" and usage
            or job.kind == "publish" and isinstance(exc, PermissionError)):
        job.state = "failed"
        if usage:
            mark_uncertain(session, job_id)
    else:
        job.state = "pending"
        job.next_run_at = now + timedelta(seconds=min(3600, 60 * (2 ** job.attempts)))


def run_once(
    session_factory: Callable[[], Session], settings: Settings, *,
    now: datetime | None = None, schedule: bool = True,
) -> dict[str, int]:
    now = now or utc_now()
    added = succeeded = failed = 0
    for _ in range(3 if schedule else 1):
        with session_factory() as session, session.begin():
            added += schedule_due(session, settings, now) if schedule else 0
            job_ids = claim_due(session, now=now)
        if not job_ids:
            break
        for job_id in job_ids:
            with session_factory() as session:
                job = session.get(Job, job_id)
                kind, payload = job.kind, job.payload
            try:
                if kind == "generate":
                    with session_factory() as session:
                        if control(session).generation_paused:
                            raise BudgetExceeded("Generation is paused by an operator")
                    generate_article(session_factory, payload["story_id"], job_id, settings, now=now)
                elif kind == "publish":
                    with session_factory() as session, session.begin():
                        record = publish_pending(session, payload["record_id"], settings, now=now)
                        _complete(session, job_id, {"publication_state": record.state})
                elif kind == "discover":
                    with session_factory() as session, session.begin():
                        source = session.get(Source, payload["source_id"], with_for_update=True)
                        result = verify_candidate(session, source, now=now)
                        _complete(session, job_id, {"discovery_state": result.state,
                                                    "reason": result.reason})
                elif kind == "catalog":
                    with session_factory() as session, session.begin():
                        added_sources = import_catalog(session, payload["path"])
                        _complete(session, job_id, {"new_candidates": added_sources})
                elif kind == "poll":
                    with session_factory() as session, session.begin():
                        source = session.get(Source, payload["source_id"])
                        result = poll_source(session, source)
                        if result.state == "failed":
                            failed_job = session.get(Job, job_id, with_for_update=True)
                            failed_job.state = "failed"
                            failed_job.last_error = result.reason
                            failed_job.lease_until = None
                            failed += 1
                            continue
                        if result.state == "deferred":
                            waiting = session.get(Job, job_id, with_for_update=True)
                            waiting.state = "pending"
                            waiting.lease_until = None
                            waiting.next_run_at = source.next_poll_at or now + timedelta(minutes=1)
                            continue
                        if result.state == "ok":
                            items = session.scalars(select(SourceItem).where(
                                SourceItem.source_id == source.id, SourceItem.story_id.is_(None),
                            ))
                            for item in items:
                                revision = session.scalar(select(SourceRevision).where(
                                    SourceRevision.source_item_id == item.id,
                                    SourceRevision.number == item.current_revision,
                                ))
                                if revision.evidence_claims:
                                    group_item(session, item, settings.geo_register_id)
                        _complete(session, job_id, {"poll_state": result.state, "new": result.new})
                succeeded += 1
            except BudgetExceeded as exc:
                with session_factory() as session, session.begin():
                    job = session.get(Job, job_id, with_for_update=True)
                    job.state = "pending"
                    job.last_error = str(exc)
                    job.lease_until = None
                    job.next_run_at = (now + timedelta(days=1)).replace(
                        hour=0, minute=5, second=0, microsecond=0,
                    )
            except Exception as exc:
                logger.error("job_failed kind=%s id=%s error=%s", kind, job_id, exc)
                with session_factory() as session, session.begin():
                    _failed(session, job_id, exc, now)
                failed += 1
    return {"scheduled": added, "succeeded": succeeded, "failed": failed}


def retry_failed(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id, with_for_update=True)
    if not job or job.state != "failed" or job.attempts >= MAX_ATTEMPTS:
        raise ValueError("Job is not safely retryable")
    usage = session.scalar(select(LLMCall).where(LLMCall.job_id == job.id))
    if usage:
        raise ValueError("An uncertain model attempt must be reconciled, not retried")
    job.state = "pending"
    job.next_run_at = utc_now()
    job.last_error = None
    return job