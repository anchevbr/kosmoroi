import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from greek_news.api import create_app
from greek_news.budget import reserve_generation
from greek_news.config import Settings, get_settings
from greek_news.db import Base, get_session
from greek_news.discovery import DiscoveryResult
from greek_news.geography import import_register
from greek_news.ingestion import EvidenceInput, PlaceHint, SourceInput, ingest_item
from greek_news.jobs import claim_due, enqueue, retry_failed, run_once
from greek_news.models import Article, ArticleVersion, Job, LLMCall, Source, SourceItem
from greek_news.polling import PollResult
from greek_news.publishing import approve_version, request_publication, set_pause
from greek_news.seeds import seed_demo
from greek_news.stories import group_item
from test_geography import load_fixture
from test_publishing import prepared


NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)


def add_notice(session, *, national=False):
    domain = "national.example" if national else "patra.example"
    owner = Source(
        name="National fixture" if national else "Municipal fixture",
        canonical_domain=domain, feed_url=f"https://{domain}/feed", source_type="fixture",
        organization=domain, enabled=True, can_discover=True, can_fetch=True,
        can_store=True, can_process_ai=True, permission_basis="Original fictional notice",
        permission_reviewed_at=NOW - timedelta(days=1),
    )
    session.add(owner)
    session.flush()
    interval = "Στις 10 Οκτωβρίου 2026, από 09:00 έως 13:00, προβλέπεται διακοπή νερού."
    locations = "Η ανακοίνωση αφορά τον δήμο Πάτρας."
    input_value = SourceInput(
        url=f"https://{domain}/notice", published_at=NOW,
        title="Πανελλαδική ενημέρωση για δημοτικές υπηρεσίες" if national
        else "Πάτρα: Προγραμματισμένη διακοπή νερού στις 10 Οκτωβρίου 2026",
        text="Δημοσιεύτηκε πανελλαδική ενημέρωση για δημοτικές υπηρεσίες."
        if national else f"{interval} {locations}",
        event_type="national_notice" if national else "water_notice", topic="services",
        audience_scope="national" if national else "local",
        event_at=None if national else datetime(2026, 10, 10, 6, tzinfo=timezone.utc),
        event_until=None if national else datetime(2026, 10, 10, 10, tzinfo=timezone.utc),
        claims=[EvidenceInput(text="Δημοσιεύτηκε πανελλαδική ενημέρωση για δημοτικές υπηρεσίες.",
                              excerpt="Δημοσιεύτηκε πανελλαδική ενημέρωση για δημοτικές υπηρεσίες.")]
        if national else [
            EvidenceInput(text=interval, excerpt=interval, fact_key="interval", value="09:00-13:00"),
            EvidenceInput(text=locations, excerpt=locations, fact_key="municipalities",
                          value="Πάτρα"),
        ],
        place_hints=[] if national else [PlaceHint(name="Πάτρα", kind="municipality",
                                                  role="affected")],
    )
    item = ingest_item(session, owner, input_value)
    story = group_item(session, session.get(SourceItem, item.item_id), "fixture-2026-demo")
    return owner, story, input_value


def fixture_client(factory, settings):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    def database():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = database
    return TestClient(app)


def auto_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'automatic.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_redisless_pending_job_and_expired_lease_are_recoverable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = enqueue(session, "poll", {"source_id": "demo"}, "poll:demo:1", next_run_at=NOW)
        assert enqueue(session, "poll", {"source_id": "demo"}, "poll:demo:1").id == first.id
        first_id = first.id
        session.commit()
    with Session(engine) as session:
        assert claim_due(session, now=NOW) == [first_id]
        session.commit()
    with Session(engine) as session:
        assert claim_due(session, now=NOW + timedelta(minutes=31)) == [first_id]
        assert session.get(Job, first_id).attempts == 2
        session.commit()
    engine.dispose()


def test_newly_scheduled_fixture_drafts_run_in_the_same_tick(session):
    seed_demo(session)
    session.commit()
    factory = sessionmaker(session.get_bind(), expire_on_commit=False)
    result = run_once(factory, Settings(_env_file=None), now=NOW)
    assert result["scheduled"] >= 2
    assert result["succeeded"] >= 2
    with factory() as database:
        assert database.scalar(select(func.count()).select_from(Article)) >= 2


def test_old_review_gated_draft_revalidates_and_publishes_automatically(session):
    article_id, factory, story_id = prepared(session)
    with factory() as database, database.begin():
        database.get(Article, article_id).status = "needs_review"
    result = run_once(factory, Settings(_env_file=None, fixture_mode=True), now=NOW)
    assert result["succeeded"] >= 1
    with factory() as database:
        article = database.get(Article, article_id)
        assert article.status == "published" and article.published_version == 1
        assert article.editor_name is None


def test_legacy_review_without_snapshot_regenerates_before_publishing(session):
    article_id, factory, story_id = prepared(session)
    with factory() as database, database.begin():
        database.get(Article, article_id).status = "needs_review"
        version = database.scalar(select(ArticleVersion).where(ArticleVersion.article_id == article_id))
        version.evidence_packet = None
    result = run_once(factory, Settings(_env_file=None, fixture_mode=True), now=NOW)
    assert result["succeeded"] >= 2, result
    with factory() as database:
        article = database.get(Article, article_id)
        assert article.current_version == article.published_version == 2
        versions = database.scalars(select(ArticleVersion).where(
            ArticleVersion.article_id == article_id,
        ).order_by(ArticleVersion.number)).all()
        assert versions[0].status == "skipped" and versions[1].evidence_packet


def test_single_local_primary_source_publishes_in_one_tick_with_no_approval(tmp_path):
    factory = auto_factory(tmp_path)
    with factory() as session, session.begin():
        import_register(session, load_fixture())
        source, story, input_value = add_notice(session)
        source_id, story_id = source.id, story.id
    settings = Settings(_env_file=None, fixture_mode=True, source_catalog_path="")
    result = run_once(factory, settings, now=NOW)
    assert result["scheduled"] == 3 and result["succeeded"] == 3, result
    with factory() as database:
        article = database.scalar(select(Article).where(Article.story_id == story_id))
        assert article.status == "published" and article.editor_name is None
        article_id = article.id
    public = fixture_client(factory, settings).get(f"/articles/{article_id}")
    assert public.status_code == 200 and public.json()["geography"][0]["role"] == "affected"
    assert public.json()["sources"][0]["source_id"] == source_id
    production = fixture_client(factory, Settings(_env_file=None))
    assert production.get("/articles").json()["total"] == 0
    assert production.get(f"/articles/{article_id}").status_code == 404


def test_local_story_retains_capacity_alongside_national_news(tmp_path):
    factory = auto_factory(tmp_path)
    with factory() as session, session.begin():
        import_register(session, load_fixture())
        source, local_story, notice = add_notice(session)
        add_notice(session, national=True)
        local_id = local_story.id
    settings = Settings(_env_file=None, fixture_mode=True, source_catalog_path="", national_selection_max=1,
                        regional_selection_max=1)
    result = run_once(factory, settings, now=NOW)
    assert result["succeeded"] == 5, result
    assert fixture_client(factory, settings).get("/articles").json()["total"] == 2
    with factory() as database:
        assert database.scalar(select(Article).where(Article.story_id == local_id)).status == "published"


def test_source_revision_republishes_existing_article_without_duplicate(tmp_path):
    factory = auto_factory(tmp_path)
    with factory() as session, session.begin():
        import_register(session, load_fixture())
        source, story, notice = add_notice(session)
        source_id, story_id = source.id, story.id
    settings = Settings(_env_file=None, fixture_mode=True, source_catalog_path="")
    assert run_once(factory, settings, now=NOW)["succeeded"] == 3
    with factory() as session:
        article_id = session.scalar(select(Article).where(Article.story_id == story_id)).id
    client = fixture_client(factory, settings)
    original = client.get(f"/articles/{article_id}").json()
    later = NOW + timedelta(minutes=1)
    with factory() as session, session.begin():
        revised = notice.model_copy(update={
            "updated_at": later,
            "text": notice.text + " Η ανακοίνωση επικαιροποιήθηκε από τον φορέα.",
        })
        outcome = ingest_item(session, session.get(Source, source_id), revised)
        assert outcome.state == "revised"
        group_item(session, session.get(SourceItem, outcome.item_id), settings.geo_register_id)
    assert run_once(factory, settings, now=later)["succeeded"] == 2
    current = client.get(f"/articles/{article_id}").json()
    assert current["id"] == original["id"] and current["datePublished"] == original["datePublished"]
    assert current["dateModified"] != original["dateModified"]
    assert current["correction_note"] and current["sources"] != original["sources"]
    assert client.get("/articles").json()["total"] == 1
    with factory() as session:
        article = session.get(Article, article_id)
        assert article.current_version == article.published_version == 2


def test_eligible_new_source_activates_on_worker_tick_without_click(tmp_path, monkeypatch):
    factory = auto_factory(tmp_path)
    with factory() as session, session.begin():
        source = Source(name="Authorized test feed", canonical_domain="town.example",
                        feed_url="https://town.example/feed", source_type="html",
                        enabled=False, can_discover=True, can_fetch=True, can_store=True,
                        can_process_ai=True, permission_basis="Operator-configured use grant",
                        permission_reviewed_at=NOW - timedelta(days=1))
        session.add(source)
        session.flush()
        source_id = source.id

    def verified(database, source, *, now):
        source.enabled = True
        source.endpoint_verified_at = now
        return DiscoveryResult("active")

    monkeypatch.setattr("greek_news.jobs.verify_candidate", verified)
    monkeypatch.setattr("greek_news.jobs.poll_source", lambda database, source, **kwargs: PollResult("unchanged"))
    result = run_once(factory, Settings(_env_file=None), now=NOW)
    assert result["scheduled"] >= 1 and result["succeeded"] >= 1
    with factory() as database:
        assert database.get(Source, source_id).enabled
        assert database.scalar(select(Job).where(Job.kind == "discover")).state == "completed"


def test_catalog_job_adds_new_candidates_while_other_jobs_continue(tmp_path):
    factory = auto_factory(tmp_path)
    catalog = tmp_path / "sources.json"
    candidate = {"name": "Regional candidate", "canonical_domain": "region.example",
                 "feed_url": "https://region.example/", "coverage_ids": ["GR-region"]}
    catalog.write_text(json.dumps({"sources": [candidate]}), encoding="utf-8")
    settings = Settings(_env_file=None, source_catalog_path=str(catalog))
    result = run_once(factory, settings, now=NOW)
    assert result["succeeded"] == 2, result
    with factory() as database:
        source = database.scalar(select(Source).where(Source.name == "Regional candidate"))
        assert source and not source.enabled and source.coverage_ids == ["GR-region"]


def test_default_worker_imports_catalog_without_authorizing_polling(tmp_path):
    factory = auto_factory(tmp_path)
    settings = Settings(_env_file=None)
    first = run_once(factory, settings, now=NOW)
    assert first == {"scheduled": 2, "succeeded": 2, "failed": 0}
    with factory() as database:
        sources = database.scalars(select(Source)).all()
        assert len(sources) > 60
        assert all(not source.enabled and not source.can_fetch and not source.can_process_ai
                   for source in sources)
        assert set(database.scalars(select(Job.kind))) == {"catalog", "maintenance"}
        assert database.scalar(select(func.count()).select_from(Article)) == 0
    assert run_once(factory, settings, now=NOW) == {"scheduled": 0, "succeeded": 0, "failed": 0}
    with factory() as database:
        assert database.scalar(select(func.count()).select_from(Source)) == len(sources)


def test_expired_model_lease_recovers_automatically_with_bounded_new_attempt(session):
    settings = Settings(_env_file=None)
    job = enqueue(session, "generate", {"story_id": "manual"}, "generate:manual", next_run_at=NOW)
    session.flush()
    claim_due(session, now=NOW)
    reserve_generation(session, job.id, settings, now=NOW)
    session.commit()
    with Session(session.get_bind()) as database:
        assert claim_due(database, now=NOW + timedelta(minutes=31)) == [job.id]
        assert database.get(Job, job.id).state == "running"
        assert database.scalar(select(LLMCall)).status == "uncertain"
        try:
            retry_failed(database, job.id)
        except ValueError:
            pass
        else:
            raise AssertionError("An uncertain call must not be retried")


def test_worker_publishes_pending_outbox_without_redis(session):
    article_id, factory, story_id = prepared(session)
    settings = Settings(_env_file=None, draft_only=False, publication_paused=False)
    with factory() as database, database.begin():
        approve_version(database, article_id, 1, editor_name="Test editor", reason="Verified")
        set_pause(database, publication_paused=False, generation_paused=False, actor="Admin")
        record = request_publication(database, article_id, settings, reason="Approved")
        enqueue(database, "publish", {"record_id": record.id}, f"publish:{record.id}",
                next_run_at=NOW)
    assert run_once(factory, settings, now=NOW, schedule=False)["succeeded"] == 1
    assert run_once(factory, settings, now=NOW, schedule=False)["succeeded"] == 0
    with factory() as database:
        assert database.get(Article, article_id).status == "published"
        assert database.scalar(select(func.count()).select_from(Job).where(
            Job.state == "completed", Job.kind == "publish",
        )) == 1


def test_poll_failure_commits_source_backoff_and_failed_job(session, monkeypatch):
    owner = Source(name="Test RSS", canonical_domain="source.example",
                   feed_url="https://source.example/feed", enabled=True)
    session.add(owner)
    session.flush()
    job = enqueue(session, "poll", {"source_id": owner.id}, "poll:test:1", next_run_at=NOW)
    job_id, source_id = job.id, owner.id
    session.commit()

    def failed_poll(database, source, **kwargs):
        source.consecutive_failures += 1
        source.next_poll_at = NOW + timedelta(minutes=5)
        return PollResult("failed", reason="HTTP 429")

    monkeypatch.setattr("greek_news.jobs.poll_source", failed_poll)
    factory = sessionmaker(session.get_bind(), expire_on_commit=False)
    assert run_once(factory, Settings(_env_file=None), now=NOW, schedule=False)["failed"] == 1
    with factory() as database:
        assert database.get(Source, source_id).consecutive_failures == 1
        assert database.get(Job, job_id).state == "failed"