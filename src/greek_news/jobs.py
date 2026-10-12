"""Database-owned scheduling, staged HTML/AI jobs, recoverable leases and bounded retries."""

import logging
import os
import socket
from contextlib import nullcontext
from datetime import timedelta
from sqlalchemy import case, or_, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from greek_news.budget import BudgetExceeded, mark_uncertain
from greek_news.db import new_id, use_clock, utc_now
from greek_news.discovery import eligibility_reason, import_catalog, verify_candidate
from greek_news.enrichment import enrich_revision
from greek_news.evidence import freeze_evidence
from greek_news.generation import generate_article
from greek_news.ingestion import source_permits_storage
from greek_news.llm import ModelConfigurationError, ModelFailure
from greek_news.models import (Article, ArticleVersion, CrawlURL, Job, LLMCall,
                               PublicationRecord, Source, SourceItem, SourceRevision, WorkerHeartbeat)
from greek_news.polling import fetch_frontier, poll_source
from greek_news.publishing import control, publish_pending, resume_pending
from greek_news.searching import search_news, search_queries
from greek_news.stories import select_candidates

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
KINDS = {"poll", "fetch", "enrich", "generate", "publish", "discover", "catalog", "search", "maintenance"}


def enqueue(session, kind, payload, key, *, next_run_at=None):
    if kind not in KINDS:
        raise ValueError("Unknown durable job type")
    statement = postgres_insert(Job) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(Job)
    session.execute(statement.values(id=new_id(), idempotency_key=key, kind=kind, payload=payload,
                    state="pending", attempts=0, next_run_at=next_run_at or utc_now(), created_at=utc_now())
                    .on_conflict_do_nothing(index_elements=[Job.idempotency_key]))
    return session.scalar(select(Job).where(Job.idempotency_key == key))


def schedule_due(session, settings, now=None):
    now = now or utc_now()
    added = 0
    def add(kind, payload, key, when=now):
        nonlocal added
        if not session.scalar(select(Job.id).where(Job.idempotency_key == key)):
            enqueue(session, kind, payload, key, next_run_at=when)
            added += 1
    if settings.source_catalog_path:
        bucket = int(now.timestamp() // (settings.source_discovery_interval_minutes * 60))
        add("catalog", {"path": settings.source_catalog_path}, f"catalog:{bucket}")
    add("maintenance", {}, f"maintenance:{now.date()}")
    if settings.web_search_enabled and settings.llm_provider == "openai":
        queries = search_queries()
        bucket = int(now.timestamp() // (settings.web_search_interval_minutes * 60))
        for offset in range(min(settings.web_search_queries_per_tick, len(queries))):
            index = (bucket * settings.web_search_queries_per_tick + offset) % len(queries)
            add("search", {"query": queries[index]}, f"search:{bucket}:{index}")
    resume_pending(session, settings, now)
    discovered = 0
    for source in session.scalars(select(Source).order_by(Source.priority.desc(), Source.id)):
        if source.source_type == "fixture":
            continue
        if source.enabled:
            if not source_permits_storage(source, now):
                source.enabled = False
                source.verification_note = "Source policy unavailable or expired"
                continue
            if source.source_type not in {"html", "html_index"} or not source.endpoint_verified_at or source.next_poll_at and source.next_poll_at > now:
                continue
            pending = session.scalar(select(Job.id).where(Job.kind == "poll", Job.state.in_(["pending", "running"]),
                                      Job.idempotency_key.like(f"poll:{source.id}:%")).limit(1))
            if not pending:
                add("poll", {"source_id": source.id}, f"poll:{source.id}:{int(now.timestamp() // 60)}")
        elif (discovered < settings.source_discovery_max_per_tick and eligibility_reason(source, now) is None
              and (not source.next_poll_at or source.next_poll_at <= now)
              and (not source.last_discovery_check or source.last_discovery_check <= now - timedelta(minutes=settings.source_discovery_interval_minutes))):
            key = f"discover:{source.id}:{int(now.timestamp() // 60 // settings.source_discovery_interval_minutes)}"
            if not session.scalar(select(Job.id).where(Job.idempotency_key == key)):
                add("discover", {"source_id": source.id}, key)
                discovered += 1
    frontier = session.scalars(select(CrawlURL).join(Source).where(
        Source.enabled.is_(True), CrawlURL.state.in_(["pending", "watched"]),
        CrawlURL.next_fetch_at <= now,
        or_(Source.next_request_at.is_(None), Source.next_request_at <= now),
        or_(CrawlURL.kind == "index", CrawlURL.discovered_at >= now - timedelta(days=settings.article_watch_days)),
    ).order_by(case((CrawlURL.state == "pending", 0), else_=1), Source.priority.desc(), CrawlURL.discovered_at)
        .limit(settings.crawl_articles_per_tick)).all()
    for row in frontier:
        if not session.scalar(select(Job.id).where(Job.kind == "fetch", Job.state.in_(["pending", "running"]),
                              Job.idempotency_key.like(f"fetch:{row.id}:%")).limit(1)):
            add("fetch", {"crawl_id": row.id}, f"fetch:{row.id}:{int(row.next_fetch_at.timestamp())}")
    if not control(session).generation_paused:
        for revision in session.scalars(select(SourceRevision).join(SourceItem).join(Source).where(
            Source.enabled.is_(True), Source.can_process_ai.is_(True), SourceRevision.analysis_state == "pending",
            SourceRevision.number == SourceItem.current_revision, SourceRevision.permitted_text.is_not(None),
        ).order_by(Source.priority.desc(), SourceRevision.revised_at).limit(settings.crawl_articles_per_tick)):
            add("enrich", {"revision_id": revision.id}, f"enrich:{revision.id}")
        for story in select_candidates(session, now=now, national_max=settings.national_selection_max,
                       regional_max=settings.regional_selection_max, international_max=settings.international_selection_max,
                       general_max=settings.general_selection_max, minimum_score=settings.selection_min_score):
            try:
                fingerprint = freeze_evidence(session, story, now=now).fingerprint()
            except (ValueError, PermissionError):
                continue
            key = f"generate:{story.id}:{fingerprint}"
            article = session.scalar(select(Article).where(Article.story_id == story.id))
            latest = session.scalar(select(ArticleVersion).where(ArticleVersion.article_id == article.id,
                                    ArticleVersion.number == article.current_version)) if article else None
            if latest and not latest.evidence_packet:
                key = f"regenerate:{story.id}:{fingerprint}:{article.current_version}"
            add("generate", {"story_id": story.id}, key)
    for record in session.scalars(select(PublicationRecord).where(PublicationRecord.state == "pending")):
        add("publish", {"record_id": record.id}, f"publish:{record.id}")
    return added


def claim_due(session, *, now=None, limit=10, lease_seconds=1800):
    now = now or utc_now()
    for job in session.scalars(select(Job).where(Job.state == "running", Job.lease_until < now).with_for_update(skip_locked=True)):
        mark_uncertain(session, job.id)
        job.state = "failed" if job.attempts >= MAX_ATTEMPTS else "pending"
        job.last_error = "Expired worker lease; model usage retained conservatively, next attempt is bounded"
        job.next_run_at = now
    due = session.scalars(select(Job).where(Job.state == "pending", Job.next_run_at <= now)
        .order_by(case((Job.kind == "publish", 0), (Job.kind == "generate", 1), (Job.kind == "enrich", 2),
                       (Job.kind == "fetch", 3), (Job.kind == "poll", 4), (Job.kind == "discover", 5),
                       (Job.kind == "catalog", 6), else_=7), Job.created_at)
        .limit(limit).with_for_update(skip_locked=True)).all()
    for job in due:
        job.state, job.attempts = "running", job.attempts + 1
        job.lease_until = now + timedelta(seconds=lease_seconds)
    session.flush()
    return [job.id for job in due]


def _complete(session, job_id, result):
    job = session.get(Job, job_id, with_for_update=True)
    job.state, job.completed_at, job.lease_until = "completed", utc_now(), None
    job.payload = {**job.payload, "result": result}


def _failed(session, job_id, exc, now):
    job = session.get(Job, job_id, with_for_update=True)
    job.last_error = f"{type(exc).__name__}: {str(exc)[:900]}"
    job.lease_until = None
    mark_uncertain(session, job_id)
    if isinstance(exc, ModelConfigurationError):
        job.state, job.attempts = "pending", max(0, job.attempts - 1)
        job.next_run_at = now + timedelta(minutes=15)
        return
    if isinstance(exc, ValueError):
        for call in session.scalars(select(LLMCall).where(LLMCall.job_id == job_id,
                                   LLMCall.attempt == job.attempts, LLMCall.status == "completed")):
            call.status = "rejected"
    if job.attempts >= MAX_ATTEMPTS or isinstance(exc, ModelFailure) and not exc.retryable:
        job.state = "failed"
    elif job.kind == "publish" and isinstance(exc, PermissionError):
        job.state, job.attempts = "pending", max(0, job.attempts - 1)
        job.next_run_at = now + timedelta(minutes=1)
    else:
        job.state = "pending"
        job.next_run_at = now + timedelta(seconds=min(3600, 60 * 2 ** job.attempts))


def maintenance(session, settings):
    from sqlalchemy import update
    cutoff = utc_now() - timedelta(days=settings.raw_text_retention_days)
    # Used evidence excerpts are durable; full original prose expires independently.
    result = session.execute(update(SourceRevision).where(SourceRevision.revised_at < cutoff,
                             SourceRevision.permitted_text.is_not(None))
                             .values(permitted_text=None, analysis_state=case(
                                 (SourceRevision.analysis_state == "pending", "failed"),
                                 else_=SourceRevision.analysis_state)))
    return {"expired_raw_texts": result.rowcount}


def _heartbeat(session, state, result=None):
    key = f"{socket.gethostname()}:{os.getpid()}"
    row = session.get(WorkerHeartbeat, key)
    if row is None:
        row = WorkerHeartbeat(id=key)
        session.add(row)
    row.updated_at, row.detail = utc_now(), {"state": state, "result": result or {}}


def run_once(session_factory, settings, *, now=None, schedule=True):
    # Freeze only explicit offline/test clocks. Production uses a running UTC clock.
    with use_clock(now) if now is not None else nullcontext():
        lock = None
        with session_factory() as session:
            engine = session.get_bind()
        if engine.dialect.name == "postgresql":
            lock = engine.connect()
            if not lock.execute(text("SELECT pg_try_advisory_lock(hashtext('kosmoroi-worker-tick'))")).scalar_one():
                lock.close()
                return {"scheduled": 0, "succeeded": 0, "failed": 0}
        try:
            return _run(session_factory, settings, schedule=schedule)
        finally:
            if lock:
                lock.execute(text("SELECT pg_advisory_unlock(hashtext('kosmoroi-worker-tick'))"))
                lock.close()


def _run(session_factory, settings, *, schedule):
    added = succeeded = failed = 0
    for _ in range(settings.tick_rounds if schedule else 1):
        now = utc_now()
        with session_factory() as session, session.begin():
            _heartbeat(session, "working")
            added += schedule_due(session, settings, now) if schedule else 0
            ids = claim_due(session, now=now, limit=settings.tick_jobs_per_round, lease_seconds=settings.job_lease_seconds)
        if not ids:
            break
        for job_id in ids:
            with session_factory() as session:
                job = session.get(Job, job_id)
                kind, payload = job.kind, job.payload
            try:
                if kind in {"generate", "enrich", "search"}:
                    with session_factory() as session:
                        paused = control(session).generation_paused
                    if paused:
                        raise BudgetExceeded("AI processing is paused")
                    if kind == "generate":
                        generate_article(session_factory, payload["story_id"], job_id, settings, now=now)
                    else:
                        result = enrich_revision(session_factory, payload["revision_id"], job_id, settings) if kind == "enrich" else search_news(session_factory, job_id, payload["query"], settings)
                        with session_factory() as session, session.begin():
                            _complete(session, job_id, result)
                else:
                    with session_factory() as session, session.begin():
                        if kind == "publish":
                            result = {"publication_state": publish_pending(session, payload["record_id"], settings, now=now).state}
                        elif kind == "catalog":
                            result = {"new_candidates": import_catalog(session, payload["path"], settings)}
                        elif kind == "maintenance":
                            result = maintenance(session, settings)
                        elif kind == "discover":
                            source = session.get(Source, payload["source_id"], with_for_update=True)
                            value = verify_candidate(session, source, now=now)
                            result = {"discovery_state": value.state, "reason": value.reason}
                        elif kind in {"poll", "fetch"}:
                            source = session.get(Source, payload["source_id"], with_for_update=True) if kind == "poll" else None
                            row = session.get(CrawlURL, payload["crawl_id"], with_for_update=True) if kind == "fetch" else None
                            value = poll_source(session, source, settings=settings) if source else fetch_frontier(session, row, settings)
                            if value.state == "deferred":
                                job = session.get(Job, job_id)
                                job.state, job.lease_until, job.attempts = "pending", None, max(0, job.attempts - 1)
                                job.next_run_at = (source.next_request_at or source.next_poll_at) if source else row.next_fetch_at
                                job.next_run_at = max(job.next_run_at or now, now + timedelta(seconds=1))
                                continue
                            if value.state == "failed":
                                job = session.get(Job, job_id)
                                job.state, job.lease_until, job.last_error = "failed", None, value.reason
                                failed += 1
                                continue
                            result = {"state": value.state, "new": value.new, "revised": value.revised}
                        _complete(session, job_id, result)
                succeeded += 1
            except BudgetExceeded as exc:
                with session_factory() as session, session.begin():
                    job = session.get(Job, job_id)
                    job.state, job.lease_until = "pending", None
                    job.attempts = max(0, job.attempts - 1)
                    job.last_error = str(exc)
                    job.next_run_at = (now + timedelta(minutes=1)) if "paused" in str(exc) else (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            except Exception as exc:
                logger.error("job_failed kind=%s id=%s error=%s", kind, job_id, type(exc).__name__)
                with session_factory() as session, session.begin():
                    _failed(session, job_id, exc, now)
                failed += 1
    result = {"scheduled": added, "succeeded": succeeded, "failed": failed}
    with session_factory() as session, session.begin():
        _heartbeat(session, "idle", result)
    return result


def retry_failed(session, job_id):
    job = session.get(Job, job_id, with_for_update=True)
    if not job or job.state != "failed" or job.attempts >= MAX_ATTEMPTS:
        raise ValueError("Job has exhausted its automatic attempts")
    job.state, job.next_run_at, job.last_error = "pending", utc_now(), None
    return job
