"""Automatic evidence-to-article workflow with durable model responses and a second check."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import select
from greek_news.article_schema import ArticleDraft
from greek_news.budget import recorded_call
from greek_news.db import utc_now
from greek_news.evidence import freeze_evidence
from greek_news.llm import DRAFT_SYSTEM, ModelResult, PROMPT_VERSION, configured_adapter
from greek_news.models import AnalysisCache, Article, ArticleVersion, AuditEntry, Job, LLMCall, Story
from greek_news.publishing import auto_decision, request_publication
from greek_news.validation import validate_draft
from greek_news.verification import verify_article


@dataclass(frozen=True)
class GenerationOutcome:
    state: str
    article_id: str | None = None
    version: int | None = None
    reason: str = ""


def _completed(job, article_id, version):
    job.state, job.completed_at, job.lease_until = "completed", utc_now(), None
    job.payload = {**job.payload, "article_id": article_id, "version": version}
    return GenerationOutcome("completed", article_id, version)


def assign_metadata(draft, packet, settings):
    relevant = [place for place in packet.places if place.role == "affected"] or [place for place in packet.places if place.role == "occurred"]
    countries = sorted({place.country_code for place in relevant if place.country_code})
    foreign = any(code != "GR" for code in countries) or packet.audience_scope == "international"
    if not countries and any(source.country_code not in {"GR", "ZZ"} for source in packet.sources):
        foreign = True
    # Publisher coverage is labeled separately; it never becomes an event or ad location.
    draft.language = settings.output_language
    draft.edition = "world" if foreign else "greece"
    draft.category, draft.subcategory = packet.topic, packet.subcategory
    draft.topic_tags = list(packet.topic_tags) or [packet.topic]
    draft.entity_ids = sorted({entity["key"] for entity in packet.entities if entity.get("key")})
    draft.country_codes = countries
    draft.publisher_coverage_ids = sorted({value for source in packet.sources for value in source.coverage_ids})
    draft.article_format = "service" if packet.event_type in {"water_notice", "power_notice", "road_closure", "transport_notice"} else "event" if packet.topic == "events" else "news"
    return draft


class _AdapterClient:
    def __init__(self, adapter, packet):
        self.adapter, self.packet = adapter, packet
    def request(self, *args, **kwargs):
        result = self.adapter.draft(self.packet)
        return ModelResult(result.draft.model_dump(mode="json"), result.model,
                           result.input_tokens, result.output_tokens, result.latency_ms)


def generate_article(session_factory, story_id, job_id, settings, *, adapter=None, now: datetime | None = None):
    now = now or utc_now()
    with session_factory() as session, session.begin():
        job = session.get(Job, job_id, with_for_update=True)
        if not job or job.kind != "generate" or job.payload.get("story_id") != story_id:
            raise ValueError("A durable generation job is required")
        if job.state == "completed":
            return GenerationOutcome("completed", job.payload.get("article_id"), job.payload.get("version"))
        story = session.get(Story, story_id, with_for_update=True)
        if not story:
            raise ValueError("Unknown story")
        packet = freeze_evidence(session, story, now=now)
        if packet.conflict or "ambiguous_geography" in packet.risk_flags:
            story.status = "skipped"
            _completed(job, None, None)
            return GenerationOutcome("blocked", reason="Conflicting or ambiguous evidence")
        if packet.event_until and packet.event_until <= now and packet.event_type in {"water_notice", "road_closure", "power_notice", "transport_notice"}:
            story.status = "skipped"
            _completed(job, None, None)
            return GenerationOutcome("blocked", reason="Effective service interval ended")
        previous = session.scalar(select(Article).where(Article.story_id == story_id))
        latest = session.scalar(select(ArticleVersion).where(ArticleVersion.article_id == previous.id,
                                ArticleVersion.number == previous.current_version)) if previous else None
        if latest and latest.evidence_hash == packet.fingerprint() and latest.evidence_packet and latest.validation.get("valid"):
            return _completed(job, previous.id, previous.current_version)
        key = hashlib.sha256((packet.fingerprint() + settings.llm_provider + settings.llm_model + PROMPT_VERSION + settings.output_language).encode()).hexdigest()
        cached = session.get(AnalysisCache, key)
        cached_data = cached.result if cached else None
        feedback = job.payload.get("validation_feedback", [])
    verification = None
    if cached_data:
        draft = ArticleDraft.model_validate(cached_data["draft"])
        verification = cached_data.get("verification")
    else:
        writer = adapter or configured_adapter(settings)  # Configuration check precedes reservation.
        client = getattr(writer, "client", None) or _AdapterClient(writer, packet)
        payload = {"packet": packet.model_dump(mode="json"), "output_language": settings.output_language,
                   "previous_validation_errors": feedback, "maximum_words": 350}
        result = recorded_call(session_factory, job_id, settings, "draft", DRAFT_SYSTEM, payload,
                               ArticleDraft.model_json_schema(), settings.max_tokens_per_call, client)
        draft = ArticleDraft.model_validate(result.data)
        draft = assign_metadata(draft, packet, settings)
        preliminary = validate_draft(draft, packet, now)
        if preliminary.valid and not packet.fixture_only:
            if not hasattr(writer, "client"):
                raise RuntimeError("Live drafts require an independently configured verifier")
            verification = verify_article(session_factory, job_id, settings, packet, draft, writer.client)
    draft = assign_metadata(draft, packet, settings)
    report = validate_draft(draft, packet, now)
    if verification:
        report.semantic_checked = True
        report.semantic_passed = verification["passed"]
        report.semantic_verification = verification
        report.errors.extend(verification["errors"])
        report.valid = report.valid and verification["passed"]
        report.warnings = [value for value in report.warnings if "cannot establish semantic" not in value]
        report.warnings.append("Automated semantic verification reduces errors; it is not a guarantee of truth")
    with session_factory() as session, session.begin():
        job = session.get(Job, job_id, with_for_update=True)
        story = session.get(Story, story_id, with_for_update=True)
        if freeze_evidence(session, story, now=now).fingerprint() != packet.fingerprint():
            story.status = "new"
            _completed(job, None, None)
            return GenerationOutcome("blocked", reason="Evidence changed while drafting; new evidence will be scheduled")
        article = session.scalar(select(Article).where(Article.story_id == story_id).with_for_update())
        if article is None:
            article = Article(story_id=story_id, slug=draft.slug, current_version=1,
                              author_name=settings.publisher_name,
                              automation_disclosure="Συντάχθηκε με τεχνητή νοημοσύνη και ελέγχθηκε αυτόματα βάσει των αναφερόμενων πηγών.")
            session.add(article)
            session.flush()
            number = 1
        else:
            number = article.current_version + 1
            article.current_version = number
        version = ArticleVersion(article_id=article.id, number=number,
                                 content=draft.model_dump(mode="json"), evidence_packet=packet.model_dump(mode="json"),
                                 evidence_hash=packet.fingerprint(), validation=report.model_dump(),
                                 change_summary="New evidence revision; automatic update" if number > 1 else "Automatic HTML-backed article")
        session.add(version)
        session.flush()
        decision = auto_decision(session, packet, report, settings, now)
        if decision.allowed:
            version.status, version.approved_by, version.approved_at = "approved", "automated_validation", now
            if article.published_version:
                version.correction_note = "Ενημέρωση βάσει νεότερων στοιχείων από τις πηγές."
            article.approved_version = number
            article.status = "published" if article.published_version else "approved"
            story.status = "processed"
            request_publication(session, article.id, settings, reason="Automatic structural and semantic checks passed", now=now)
        else:
            version.status = "skipped" if not report.valid else "proposed"
            if article.published_version is None:
                article.status = "skipped" if not report.valid else "deferred"
            story.status = "skipped" if not report.valid else "deferred"
        for usage in session.scalars(select(LLMCall).where(LLMCall.job_id == job_id)):
            usage.article_id = article.id
        if report.valid and cached_data is None:
            session.add(AnalysisCache(key=key, result={"draft": draft.model_dump(mode="json"), "verification": verification}))
        session.add(AuditEntry(actor="pipeline", action="draft_proposed", subject="article",
                               subject_id=article.id, detail={"version": number, "auto_publication_allowed": decision.allowed, "decision_reasons": list(decision.reasons),
                               "reasons": list(decision.reasons), "validation": report.model_dump()}))
        if not decision.allowed:
            session.add(AuditEntry(actor="pipeline", action="automatic_decision_deferred", subject="article",
                                   subject_id=article.id, detail={"reasons": list(decision.reasons)}))
        if not report.valid and job.attempts < 3:
            # Retry autonomously with specific feedback and a new budgeted attempt.
            from datetime import timedelta
            job.state, job.lease_until = "pending", None
            job.next_run_at = now + timedelta(minutes=1)
            job.payload = {**job.payload, "validation_feedback": report.errors, "article_id": article.id}
            story.status = "new"
            return GenerationOutcome("retrying", article.id, number, "; ".join(report.errors)[:1000])
        return _completed(job, article.id, number)
