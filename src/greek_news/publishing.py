"""Manual review, narrow automation decisions and a durable local publisher."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.article_schema import ArticleDraft
from greek_news.config import Settings
from greek_news.db import utc_now
from greek_news.evidence import EvidencePacket, freeze_evidence
from greek_news.models import (
    Article, ArticleFacet, ArticleVersion, AuditEntry, OperationalControl, PublicationPolicy,
    PublicationRecord, Source, Story,
)
from greek_news.validation import HIGH_RISK, ValidationReport, validate_draft


AUTO_EVENT_TYPES = {"water_notice", "power_notice", "event_announcement", "road_closure"}


class Publisher(Protocol):
    def publish(self, session: Session, record: PublicationRecord, now: datetime) -> None: ...


def control(session: Session) -> OperationalControl:
    dialect = session.get_bind().dialect.name
    statement = postgres_insert(OperationalControl) if dialect == "postgresql" else sqlite_insert(OperationalControl)
    session.execute(statement.values(
        id="global", publication_paused=False, generation_paused=False, updated_at=utc_now(),
    ).on_conflict_do_nothing(index_elements=[OperationalControl.id]))
    return session.get(OperationalControl, "global")


def set_pause(
    session: Session, *, publication_paused: bool, generation_paused: bool,
    actor: str,
) -> OperationalControl:
    record = control(session)
    record.publication_paused = publication_paused
    record.generation_paused = generation_paused
    record.updated_at = utc_now()
    session.add(AuditEntry(
        actor=actor, action="set_global_pause", subject="control", subject_id="global",
        detail={"publication_paused": publication_paused,
                "generation_paused": generation_paused},
    ))
    return record


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


def auto_decision(
    session: Session, packet: EvidencePacket, report: ValidationReport,
    settings: Settings, now: datetime | None = None,
) -> PolicyDecision:
    now = now or utc_now()
    reasons = []
    if settings.draft_only or settings.publication_paused or control(session).publication_paused:
        reasons.append("Publication is paused by an operator")
    if packet.fixture_only and not settings.fixture_mode:
        reasons.append("Fictional sources require explicit offline fixture mode")
    if settings.llm_provider == "fake" and not packet.fixture_only:
        reasons.append("The fixture adapter cannot draft live reporting")
    if not report.valid:
        reasons.append("Article validation failed")
    if not packet.fixture_only and not (report.semantic_checked and report.semantic_passed):
        reasons.append("Live article requires successful automated semantic verification")
    if packet.conflict or "ambiguous_geography" in packet.risk_flags:
        reasons.append("Conflicting evidence or unresolved geography")
    if set(packet.risk_flags) & HIGH_RISK and packet.independent_origins < 2:
        reasons.append("Sensitive claim needs independent corroboration")
    if set(packet.risk_flags) - HIGH_RISK - {"ambiguous_geography"}:
        reasons.append("Unknown risk flags require additional evidence")
    if packet.event_until and packet.event_until <= now and packet.event_type in AUTO_EVENT_TYPES:
        reasons.append("Effective interval has ended")
    for reference in packet.sources:
        source = session.get(Source, reference.source_id)
        policy = session.scalar(select(PublicationPolicy).where(
            PublicationPolicy.source_id == source.id,
            PublicationPolicy.topic == packet.topic,
        ))
        if (not source.enabled or not source.can_process_ai or not source.permission_basis
                or source.permission_expires_at and source.permission_expires_at <= now):
            reasons.append(f"Source permissions unavailable for {source.id}")
        if policy and not policy.enabled:
            reasons.append(f"Source/topic explicitly disabled for {source.id}")
    return PolicyDecision(allowed=not reasons, reasons=tuple(reasons))


def resume_pending(session: Session, settings: Settings, now: datetime) -> int:
    queued = 0
    candidates = session.scalars(select(Article).where(
        Article.status.in_(["draft", "needs_review", "deferred", "approved", "published"]),
    ).order_by(Article.created_at).limit(100).with_for_update(skip_locked=True)).all()
    for article in candidates:
        if article.published_version == article.current_version:
            continue
        version = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article.id,
            ArticleVersion.number == article.current_version,
        ).with_for_update())
        if not version or version.status not in {"proposed", "approved"}:
            continue
        if session.scalar(select(PublicationRecord).where(
            PublicationRecord.article_id == article.id,
            PublicationRecord.version == version.number,
        )):
            continue
        story = session.get(Story, article.story_id)
        reason = None
        if not version.evidence_packet:
            reason = "Legacy version lacks a frozen evidence packet"
        else:
            try:
                packet = freeze_evidence(session, story, now=now)
                if packet.fingerprint() != version.evidence_hash:
                    reason = "Evidence changed since this version was drafted"
                else:
                    draft = ArticleDraft.model_validate(version.content)
                    report = validate_draft(draft, packet, now)
                    saved = version.validation
                    if saved.get("semantic_checked") and saved.get("semantic_passed"):
                        report.semantic_checked = True
                        report.semantic_passed = True
                        report.semantic_verification = saved.get("semantic_verification")
                    version.validation = report.model_dump()
                    if not report.valid:
                        reason = "; ".join(report.errors)
                    else:
                        decision = auto_decision(session, packet, report, settings, now)
                        if decision.allowed:
                            version.status = "approved"
                            version.approved_by = version.approved_by or "automated_validation"
                            version.approved_at = version.approved_at or now
                            if article.published_version and not version.correction_note:
                                version.correction_note = "Ενημέρωση βάσει νεότερων στοιχείων από τις πηγές."
                            article.approved_version = version.number
                            article.status = "published" if article.published_version else "approved"
                            record = request_publication(session, article.id, settings,
                                                         reason="Automatic revalidation", now=now)
                            queued += record.state == "pending"
                            session.add(AuditEntry(
                                actor="pipeline", action="version_revalidated",
                                subject="article", subject_id=article.id,
                                detail={"version": version.number, "valid": True},
                            ))
                        elif article.published_version is None:
                            article.status = "deferred"
            except (PermissionError, ValueError) as exc:
                reason = str(exc)
        if reason:
            version.status = "skipped"
            article.status = "published" if article.published_version else "skipped"
            if "Legacy version" in reason or "Evidence changed" in reason:
                story.status = "new"
            session.add(AuditEntry(
                actor="pipeline", action="version_skipped", subject="article",
                subject_id=article.id, detail={"version": version.number, "reason": reason},
            ))
    return queued


def approve_version(
    session: Session, article_id: str, number: int, *, editor_name: str,
    reason: str, correction_note: str | None = None,
) -> ArticleVersion:
    if not editor_name.strip() or not reason.strip():
        raise ValueError("Editor identity and decision reason are required")
    article = session.get(Article, article_id, with_for_update=True)
    if not article or article.status == "withdrawn" or article.current_version != number:
        raise ValueError("Only the latest active article version may be approved")
    version = session.scalar(select(ArticleVersion).where(
        ArticleVersion.article_id == article_id, ArticleVersion.number == number,
    ).with_for_update())
    if version.status != "proposed" or not version.validation.get("valid"):
        raise ValueError("A proposed version must pass factual validation first")
    if not version.evidence_packet:
        raise ValueError("Legacy article version has no evidence snapshot; regenerate it")
    packet = EvidencePacket.model_validate(version.evidence_packet)
    if freeze_evidence(session, session.get(Story, article.story_id)).fingerprint() != packet.fingerprint():
        raise ValueError("Evidence changed after drafting; generate a new version")
    if article.published_version and not (correction_note and correction_note.strip()):
        raise ValueError("A published article update needs a correction note")
    version.status = "approved"
    version.approved_by = editor_name.strip()
    version.approved_at = utc_now()
    version.correction_note = correction_note.strip() if correction_note else None
    article.approved_version = number
    article.editor_name = editor_name.strip()
    if article.published_version is None:
        article.status = "approved"
    session.add(AuditEntry(
        actor=editor_name.strip(), action="approve_version", subject="article", subject_id=article_id,
        detail={"version": number, "reason": reason, "correction_note": version.correction_note},
    ))
    return version


def request_publication(
    session: Session, article_id: str, settings: Settings, *, reason: str,
    now: datetime | None = None,
) -> PublicationRecord:
    now = now or utc_now()
    if settings.draft_only or settings.publication_paused or control(session).publication_paused:
        raise PermissionError("Draft-only mode or emergency publication pause is active")
    if not reason.strip():
        raise ValueError("A publication decision reason is required")
    article = session.get(Article, article_id, with_for_update=True)
    if not article or article.status == "withdrawn" or not article.approved_version:
        raise ValueError("An active approved article is required")
    number = article.approved_version
    version = session.scalar(select(ArticleVersion).where(
        ArticleVersion.article_id == article_id, ArticleVersion.number == number,
    ))
    existing = session.scalar(select(PublicationRecord).where(
        PublicationRecord.article_id == article_id, PublicationRecord.version == number,
    ))
    if existing:
        return existing
    if version.status != "approved" or not version.validation.get("valid"):
        raise ValueError("The approved version must pass validation")
    if not version.evidence_packet:
        raise ValueError("Legacy article version has no evidence snapshot; regenerate it")
    packet = EvidencePacket.model_validate(version.evidence_packet)
    if freeze_evidence(session, session.get(Story, article.story_id), now=now).fingerprint() != packet.fingerprint():
        raise ValueError("Evidence changed after approval")
    if packet.event_until and packet.event_until <= now and packet.event_type in AUTO_EVENT_TYPES:
        raise ValueError("Cannot publish an expired active service notice")
    record = PublicationRecord(article_id=article_id, version=number,
                               decision_reason=reason.strip(), state="pending")
    session.add(record)
    session.flush()
    session.add(AuditEntry(
        actor=version.approved_by or "policy", action="queue_publication",
        subject="article", subject_id=article_id,
        detail={"version": number, "reason": reason},
    ))
    return record


class LocalPublisher:
    def publish(self, session: Session, record: PublicationRecord, now: datetime) -> None:
        article = session.get(Article, record.article_id, with_for_update=True)
        version = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article.id, ArticleVersion.number == record.version,
        ))
        if record.state == "published":
            return
        if (record.state != "pending" or article.status == "withdrawn"
                or version.status != "approved" or article.approved_version != record.version):
            raise ValueError("Publication record is no longer eligible")
        previous = session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article.id,
            ArticleVersion.number == article.published_version,
        )) if article.published_version else None
        content_changed = (previous is None or previous.content != version.content
                           or previous.correction_note != version.correction_note)
        article.published_at = article.published_at or now
        if content_changed:
            article.modified_at = now
        article.published_version = record.version
        article.status = "published"
        draft = ArticleDraft.model_validate(version.content)
        packet = EvidencePacket.model_validate(version.evidence_packet)
        session.execute(delete(ArticleFacet).where(ArticleFacet.article_id == article.id))
        values = {("edition", draft.edition), ("category", draft.category),
                  ("language", draft.language), ("scope", draft.audience_scope),
                  ("format", draft.article_format)}
        if draft.subcategory:
            values.add(("subcategory", draft.subcategory))
        values.update(("topic", tag) for tag in draft.topic_tags)
        values.update(("entity", key) for key in draft.entity_ids)
        values.update(("country", code) for code in draft.country_codes)
        relevant = [place for place in packet.places if place.role == "affected"] or [place for place in packet.places if place.role == "occurred"]
        for place in relevant:
            values.update(("place", key) for key in (place.ancestor_ids or (place.id, place.region_id)))
        if packet.fixture_only:
            values.add(("fixture", "true"))
        from greek_news.models import Place, GeoRegister
        if any(session.get(GeoRegister, session.get(Place, place.id).register_id).is_fixture for place in packet.places):
            values.add(("fixture", "true"))
        session.add_all(ArticleFacet(article_id=article.id, kind=kind, value=value)
                        for kind, value in sorted(values))
        version.status = "published"
        record.state = "published"
        record.published_at = now
        session.add(AuditEntry(
            actor="local_publisher", action="published", subject="article",
            subject_id=article.id, detail={"version": version.number, "changed": content_changed},
        ))


def publish_pending(
    session: Session, record_id: str, settings: Settings, *, now: datetime | None = None,
    publisher: Publisher | None = None,
) -> PublicationRecord:
    now = now or utc_now()
    if settings.draft_only or settings.publication_paused or control(session).publication_paused:
        raise PermissionError("Publication is paused")
    record = session.get(PublicationRecord, record_id, with_for_update=True)
    if record is None:
        raise ValueError("Unknown publication record")
    if record.state == "published":
        return record
    if record.state != "pending":
        return record
    article = session.get(Article, record.article_id, with_for_update=True)
    version = session.scalar(select(ArticleVersion).where(
        ArticleVersion.article_id == article.id, ArticleVersion.number == record.version,
    ))
    packet = EvidencePacket.model_validate(version.evidence_packet) if version and version.evidence_packet else None
    current = freeze_evidence(session, session.get(Story, article.story_id), now=now) if packet else None
    if (packet is None or packet.fingerprint() != current.fingerprint()
            or packet.event_until and packet.event_until <= now
            and packet.event_type in AUTO_EVENT_TYPES):
        record.state = "blocked"
        record.error = ("Missing evidence snapshot; regenerate this article version" if packet is None
                        else "Evidence changed or the effective interval ended after queueing")
        session.add(AuditEntry(
            actor="local_publisher", action="publication_blocked",
            subject="article", subject_id=article.id,
            detail={"version": record.version, "reason": record.error},
        ))
        return record
    if version.approved_by == "automated_validation":
        report = ValidationReport.model_validate(version.validation)
        decision = auto_decision(session, current, report, settings, now)
        if not decision.allowed:
            record.state = "blocked"
            record.error = "; ".join(decision.reasons)
            return record
    (publisher or LocalPublisher()).publish(session, record, now)
    return record


def withdraw_article(session: Session, article_id: str, *, actor: str, reason: str) -> Article:
    if not reason.strip():
        raise ValueError("Withdrawal reason is required")
    article = session.get(Article, article_id, with_for_update=True)
    if not article or article.status != "published":
        raise ValueError("Only a published article may be withdrawn")
    article.status = "withdrawn"
    article.withdrawn_at = utc_now()
    article.withdrawal_reason = reason.strip()
    session.add(AuditEntry(
        actor=actor, action="withdraw", subject="article", subject_id=article_id,
        detail={"reason": reason.strip(), "published_version": article.published_version},
    ))
    return article
