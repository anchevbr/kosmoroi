from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from greek_news.api import create_app
from greek_news.config import Settings, get_settings
from greek_news.db import Base, get_session
from greek_news.generation import generate_article
from greek_news.jobs import run_once
from greek_news.models import Job, Story
from greek_news.publishing import approve_version, publish_pending, request_publication, set_pause
from greek_news.seeds import seed_demo


NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)


def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, admin_token="admin-test", editor_token="editor-test",
                        draft_only=False, publication_paused=False, fixture_mode=True)
    app = create_app()

    def database():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app), factory, settings


def test_admin_auth_and_source_activation_are_fail_closed(tmp_path):
    client, factory, settings = client_and_factory(tmp_path)
    assert client.get("/health").status_code == 200
    assert client.get("/admin/sources").status_code == 401
    assert client.get("/admin/sources", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/admin/sources", headers={"Authorization": "Bearer editor-test"},
                       json={"name": "Local", "canonical_domain": "local.example",
                             "feed_url": "https://local.example/feed"}).status_code == 403
    admin_headers = {"Authorization": "Bearer admin-test"}
    response = client.post("/admin/sources", headers=admin_headers,
                           json={"name": "Local", "canonical_domain": "local.example",
                                 "feed_url": "https://local.example/feed"})
    assert response.status_code == 201, response.text
    source_id = response.json()["id"]
    assert not response.json()["can_process_ai"] and not response.json()["enabled"]
    invalid = client.patch(f"/admin/sources/{source_id}", headers=admin_headers,
                           json={"enabled": True})
    assert invalid.status_code == 422
    assert client.patch(f"/admin/sources/{source_id}", headers=admin_headers,
                        json={"feed_url": "https://127.0.0.1/feed"}).status_code == 422


def test_public_edition_filters_one_published_article_three_municipalities(tmp_path):
    client, factory, settings = client_and_factory(tmp_path)
    with factory() as session:
        seed_demo(session)
        stories = session.scalars(select(Story).where(Story.status == "new")).all()
        for story in stories:
            session.add(Job(idempotency_key=f"draft:{story.id}", kind="generate",
                            payload={"story_id": story.id}))
        session.commit()
        jobs = session.scalars(select(Job)).all()
    with factory() as session:
        assert client.get("/articles").json()["total"] == 0
    for job in jobs:
        result = generate_article(factory, job.payload["story_id"], job.id,
                                  Settings(_env_file=None), now=NOW)
        with factory() as database, database.begin():
            approve_version(database, result.article_id, 1, editor_name="Test editor",
                            reason="Fiction verified")
            set_pause(database, publication_paused=False, generation_paused=False,
                      actor="Admin")
            record = request_publication(database, result.article_id, settings,
                                         reason="Manual fixture pilot", now=NOW)
            record_id = record.id
        with factory() as database, database.begin():
            publish_pending(database, record_id, settings, now=NOW)
    ids = []
    for municipality in ("m-patra", "m-aigialeia", "m-west-achaia"):
        response = client.get("/articles", params={"municipality": f"fixture-2026-demo:{municipality}"})
        assert response.status_code == 200 and response.json()["total"] == 1
        ids.append(response.json()["items"][0]["id"])
    assert len(set(ids)) == 1
    regional = client.get("/articles", params={"region": "fixture-2026-demo:r-west-greece"})
    assert regional.json()["total"] == 1
    national = client.get("/articles")
    assert national.json()["total"] == 2
    assert all(item["id"] != ids[0] for item in client.get("/articles", params={
        "municipality": "fixture-2026-demo:m-heraklion-attica",
    }).json()["items"])
    assert client.get(f"/articles/{ids[0]}").json()["geo_registers"][0]["fixture"]


def test_admin_review_pause_publication_and_withdrawal_over_http(tmp_path):
    client, factory, settings = client_and_factory(tmp_path)
    with factory() as database:
        seed_demo(database)
        story = database.scalar(select(Story).where(Story.event_type == "water_notice"))
        database.add(Job(idempotency_key="initial:draft", kind="generate",
                         payload={"story_id": story.id}))
        database.commit()
        initial_job = database.scalar(select(Job).where(Job.idempotency_key == "initial:draft"))
        story_id, job_id = story.id, initial_job.id
    assert client.post(f"/admin/stories/{story_id}/generate").status_code == 401
    editor_headers = {"Authorization": "Bearer editor-test"}
    admin_headers = {"Authorization": "Bearer admin-test"}
    queued = client.post(f"/admin/stories/{story_id}/generate", headers=editor_headers)
    assert queued.status_code == 202
    result = generate_article(factory, story_id, job_id, Settings(_env_file=None), now=NOW)
    assert client.get(f"/articles/{result.article_id}").status_code == 404
    details = client.get(f"/admin/articles/{result.article_id}", headers=editor_headers)
    assert details.status_code == 200 and details.json()["versions"][0]["evidence_packet"]
    assert client.post(f"/admin/articles/{result.article_id}/versions/1/approve",
                       headers=editor_headers,
                       json={"editor_name": "Editor", "reason": "Checked fiction"}).status_code == 200
    assert client.put("/admin/control", headers=admin_headers,
                      json={"publication_paused": True, "generation_paused": False}).status_code == 200
    assert client.post(f"/admin/articles/{result.article_id}/publish", headers=editor_headers,
                       json={"reason": "Approved pilot"}).status_code == 409
    assert client.put("/admin/control", headers=editor_headers,
                      json={"publication_paused": False, "generation_paused": False}).status_code == 403
    assert client.put("/admin/control", headers=admin_headers,
                      json={"publication_paused": False, "generation_paused": False}).status_code == 200
    pending = client.post(f"/admin/articles/{result.article_id}/publish", headers=editor_headers,
                          json={"reason": "Approved pilot"})
    assert pending.status_code == 202
    assert client.get(f"/articles/{result.article_id}").status_code == 404
    assert run_once(factory, settings, now=NOW, schedule=False)["succeeded"] >= 1
    assert client.get(f"/articles/{result.article_id}").status_code == 200
    assert client.post(f"/admin/articles/{result.article_id}/withdraw", headers=editor_headers,
                       json={"reason": "Fixture retired"}).status_code == 200
    assert client.get(f"/articles/{result.article_id}").status_code == 404


def test_manual_story_split_merge_and_honest_coverage(tmp_path):
    client, factory, settings = client_and_factory(tmp_path)
    with factory() as database:
        seed_demo(database)
        story = database.scalar(select(Story).where(Story.event_type == "water_notice"))
        story_id = story.id
        database.commit()
    headers = {"Authorization": "Bearer editor-test"}
    item_id = client.get(f"/admin/stories/{story_id}", headers=headers).json()["items"][0]["id"]
    split = client.post(f"/admin/stories/{story_id}/split", headers=headers,
                        json={"source_item_id": item_id, "reason": "Separate reporting angle"})
    assert split.status_code == 200, split.text
    new_id = split.json()["new_id"]
    merged = client.post(f"/admin/stories/{story_id}/merge", headers=headers,
                         json={"source_story_id": new_id, "reason": "Same event after review"})
    assert merged.status_code == 200, merged.text
    assert client.get(f"/admin/stories/{new_id}", headers=headers).json()["status"] == "merged"
    metrics = client.get("/admin/metrics", headers=headers)
    assert metrics.status_code == 200
    assert len(metrics.json()["coverage"]["regions"]) == 13
    assert all(region["gap"] for region in metrics.json()["coverage"]["regions"])
    assert metrics.json()["coverage"]["fixture"] and metrics.json()["daily_usage"]["cost_available"]


def test_human_revision_needs_valid_evidence_and_correction_note(tmp_path):
    client, factory, settings = client_and_factory(tmp_path)
    with factory() as database:
        seed_demo(database)
        story = database.scalar(select(Story).where(Story.event_type == "water_notice"))
        database.add(Job(idempotency_key="revision:draft", kind="generate",
                         payload={"story_id": story.id}))
        database.commit()
        job = database.scalar(select(Job).where(Job.idempotency_key == "revision:draft"))
        job_id, story_id = job.id, story.id
    result = generate_article(factory, story_id, job_id, Settings(_env_file=None), now=NOW)
    editor = {"Authorization": "Bearer editor-test"}
    admin = {"Authorization": "Bearer admin-test"}
    assert client.post(f"/admin/articles/{result.article_id}/versions/1/approve", headers=editor,
                       json={"editor_name": "Editor", "reason": "Verified"}).status_code == 200
    client.put("/admin/control", headers=admin,
               json={"publication_paused": False, "generation_paused": False})
    first = client.post(f"/admin/articles/{result.article_id}/publish", headers=editor,
                        json={"reason": "Verified"})
    assert first.status_code == 202
    run_once(factory, settings, now=NOW, schedule=False)
    original = client.get(f"/articles/{result.article_id}").json()
    draft = client.get(f"/admin/articles/{result.article_id}", headers=editor).json()["versions"][0]["content"]
    draft["summary"] = "Η ανακοίνωση προβλέπει διακοπή νερού για 99 ώρες."
    bad = client.post(f"/admin/articles/{result.article_id}/versions", headers=editor,
                      json={"reason": "Questionable change", "content": draft})
    assert bad.status_code == 201 and not bad.json()["validation"]["valid"]
    assert client.post(f"/admin/articles/{result.article_id}/versions/2/approve", headers=editor,
                       json={"editor_name": "Editor", "reason": "Unverified"}).status_code == 409
    draft["summary"] = "Προγραμματισμένη διακοπή νερού στις 10 Οκτωβρίου 2026, από 09:00 έως 13:00."
    good = client.post(f"/admin/articles/{result.article_id}/versions", headers=editor,
                       json={"reason": "Clarified opening", "content": draft})
    assert good.status_code == 201 and good.json()["validation"]["valid"]
    assert client.post(f"/admin/articles/{result.article_id}/versions/3/approve", headers=editor,
                       json={"editor_name": "Editor", "reason": "Reviewed change"}).status_code == 409
    approved = client.post(f"/admin/articles/{result.article_id}/versions/3/approve", headers=editor,
                           json={"editor_name": "Editor", "reason": "Reviewed change",
                                 "correction_note": "Clarified the summary."})
    assert approved.status_code == 200
    assert client.get(f"/articles/{result.article_id}").json()["summary"] == original["summary"]
    updated = client.post(f"/admin/articles/{result.article_id}/publish", headers=editor,
                          json={"reason": "Reviewed correction"})
    assert updated.status_code == 202
    run_once(factory, settings, now=NOW, schedule=False)
    visible = client.get(f"/articles/{result.article_id}").json()
    assert visible["datePublished"] == original["datePublished"]
    assert visible["correction_note"] == "Clarified the summary."
    assert visible["summary"] != original["summary"]