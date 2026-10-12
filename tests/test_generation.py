import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from greek_news.config import Settings
from greek_news.generation import generate_article
from greek_news.ingestion import SourceInput, ingest_item
from greek_news.llm import FakeAdapter
from greek_news.models import (
    Article, ArticleVersion, AuditEntry, Job, LLMCall, PublicationRecord, Source, SourceItem, Story,
)
from greek_news.publishing import publish_pending
from greek_news.seeds import seed_demo


NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)


def setup(session):
    seed_demo(session)
    story = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    job = Job(idempotency_key=f"generate:{story.id}:initial", kind="generate",
              payload={"story_id": story.id}, attempts=1)
    session.add(job)
    session.commit()
    return story.id, job.id, sessionmaker(session.get_bind(), expire_on_commit=False)


def test_draft_job_is_durable_idempotent_and_never_publishes(session):
    story_id, job_id, factory = setup(session)
    settings = Settings(_env_file=None)
    result = generate_article(factory, story_id, job_id, settings, now=NOW)
    assert result.state == "completed" and result.version == 1
    assert generate_article(factory, story_id, job_id, settings, now=NOW) == result
    with factory() as database:
        article = database.get(Article, result.article_id)
        assert article.status == "deferred" and article.published_at is None
        assert database.scalar(select(func.count()).select_from(Article)) == 1
        assert database.scalar(select(func.count()).select_from(LLMCall)) == 1
        assert database.scalar(select(ArticleVersion)).validation["valid"]
        audit = database.scalar(select(AuditEntry).where(AuditEntry.action == "draft_proposed"))
        assert not audit.detail["auto_publication_allowed"]
        assert any("fixture mode" in reason for reason in audit.detail["decision_reasons"])


def test_fixture_automatically_approved_and_published_with_no_editor(session):
    story_id, job_id, factory = setup(session)
    settings = Settings(_env_file=None, fixture_mode=True)
    result = generate_article(factory, story_id, job_id, settings, now=NOW)
    with factory() as database:
        article = database.get(Article, result.article_id)
        record = database.scalar(select(PublicationRecord))
        assert article.status == "approved" and article.approved_version == 1
        assert article.editor_name is None and record.state == "pending"
        record_id = record.id
    with factory() as database, database.begin():
        assert publish_pending(database, record_id, settings, now=NOW).state == "published"
    with factory() as database:
        assert database.get(Article, result.article_id).published_version == 1


def test_unsupported_draft_is_skipped_automatically_without_review_queue(session):
    story_id, job_id, factory = setup(session)
    settings = Settings(_env_file=None, fixture_mode=True)

    class UnsupportedAdapter:
        def draft(self, packet):
            result = FakeAdapter().draft(packet)
            invalid = result.draft.model_copy(deep=True)
            invalid.title_candidates = ["Πάτρα: διακοπή νερού για 99 ώρες",
                                        *invalid.title_candidates[1:]]
            invalid.selected_title = invalid.title_candidates[0]
            return replace(result, draft=invalid)

    result = generate_article(factory, story_id, job_id, settings, adapter=UnsupportedAdapter(), now=NOW)
    with factory() as database:
        article = database.get(Article, result.article_id)
        version = database.scalar(select(ArticleVersion))
        assert article.status == "skipped" and version.status == "skipped"
        assert not version.validation["valid"] and any("99" in error for error in version.validation["errors"])
        assert database.scalar(select(func.count()).select_from(PublicationRecord)) == 0
        assert database.scalar(select(AuditEntry).where(
            AuditEntry.action == "automatic_decision_deferred",
        )).detail["reasons"]


def test_source_correction_proposes_version_without_overwriting_public_identity(session):
    story_id, job_id, factory = setup(session)
    settings = Settings(_env_file=None)
    first = generate_article(factory, story_id, job_id, settings, now=NOW)
    with factory() as database, database.begin():
        article = database.get(Article, first.article_id)
        article.status = "published"
        article.published_version = 1
        article.published_at = NOW
        article.modified_at = NOW
        owner = database.scalar(select(Source).where(Source.name == "Fictional municipal notice"))
        original = json.loads((Path(__file__).parents[1] / "fixtures" / "scenarios.json").read_text())[
            "items"
        ][0]["input"]
        original["text"] += " Η ανακοίνωση επικαιροποιήθηκε ως προς τη διατύπωση."
        changed = ingest_item(database, owner, SourceInput.model_validate(original))
        assert changed.state == "revised"
        assert database.get(SourceItem, changed.item_id).story_id == story_id
        job = Job(idempotency_key=f"generate:{story_id}:correction", kind="generate",
                  payload={"story_id": story_id})
        database.add(job)
        database.flush()
        next_job_id = job.id
    second = generate_article(factory, story_id, next_job_id, settings, now=NOW)
    with factory() as database:
        article = database.get(Article, first.article_id)
        assert second.article_id == first.article_id and second.version == 2
        assert article.slug and article.status == "published" and article.published_version == 1
        assert article.published_at == NOW and article.modified_at == NOW
        assert database.scalar(select(func.count()).select_from(ArticleVersion)) == 2
        proposal = database.scalar(select(ArticleVersion).where(
            ArticleVersion.number == 2,
        ))
        assert "New evidence revision" in proposal.change_summary
        assert proposal.evidence_packet["story_id"] == story_id
        assert proposal.evidence_packet != database.scalar(select(ArticleVersion).where(
            ArticleVersion.number == 1,
        )).evidence_packet