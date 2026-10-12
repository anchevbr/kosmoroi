"""End-to-end HTML processing with a scripted AI boundary, never real paid requests."""

import json
from datetime import timedelta
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from greek_news.api import create_app
from greek_news.article_schema import ArticleDraft
from greek_news.config import Settings, get_settings
from greek_news.db import Base, get_session, utc_now
from greek_news.enrichment import EvidenceAnalysis
from greek_news.geography import RegisterInput, import_register
from greek_news.jobs import run_once
from greek_news.llm import ModelDraftAdapter, ModelResult
from greek_news.models import Article, ArticleVersion, Job, LLMCall, Source, SourceRevision, Story
from greek_news.polling import fetch_frontier, poll_source
from greek_news.verification import VerificationResult

GR_FACT = "Στην Πάτρα ανακοινώθηκε προγραμματισμένη διακοπή νερού λόγω εργασιών στο δίκτυο ύδρευσης."
EN_FACT = "London opened a new public library for residents, with reading rooms and community activities."


def register():
    return RegisterInput.model_validate({"id": "test-world", "version": "1", "provenance": "Synthetic test gazetteer", "is_fixture": False,
        "places": [{"official_id": "GR", "kind": "country", "name_el": "Ελλάδα", "aliases": ["Greece", "GR"]},
                   {"official_id": "GR-G", "kind": "region", "name_el": "Δυτική Ελλάδα", "parent_official_id": "GR"},
                   {"official_id": "patras", "kind": "city", "name_el": "Πάτρα", "aliases": ["Patras"], "parent_official_id": "GR-G"},
                   {"official_id": "GB", "kind": "country", "name_el": "Ηνωμένο Βασίλειο", "aliases": ["GB", "United Kingdom"]},
                   {"official_id": "london", "kind": "city", "name_el": "Λονδίνο", "aliases": ["London"], "parent_official_id": "GB"}]})


class ScriptedAI:
    def __init__(self, *, unsupported=False, invented_excerpt=False):
        self.calls = []
        self.unsupported = unsupported
        self.invented_excerpt = invented_excerpt

    def request(self, system, payload, schema, cap, **kwargs):
        kind = schema["title"]
        self.calls.append(kind)
        if kind == EvidenceAnalysis.__name__:
            international = "London" in payload["text"]
            fact = EN_FACT if international else GR_FACT
            data = {"category": "culture" if international else "services", "subcategory": "books" if international else "water",
                    "topic_tags": ["libraries"] if international else ["water-supply"],
                    "event_type": "library_opening" if international else "water_notice",
                    "audience_scope": "international" if international else "local", "language": "en" if international else "el",
                    "facts": [{"excerpt": "Invented unsupported extract from another article." if self.invented_excerpt else fact}],
                    "places": [{"name": "London" if international else "Πάτρα", "role": "occurred" if international else "affected",
                                "kind": "city", "context": "GB" if international else "Ελλάδα", "evidence_excerpt": fact}],
                    "entities": [], "risk_flags": [], "newsworthy": True, "reason": "A concrete community development"}
        elif kind == ArticleDraft.__name__:
            packet = payload["packet"]
            international = packet["topic"] == "culture"
            claim = packet["claims"][0]
            titles = ["Λονδίνο: Νέα βιβλιοθήκη για τους κατοίκους", "Νέα δημόσια βιβλιοθήκη στο Λονδίνο", "Λονδίνο: Άνοιξε βιβλιοθήκη με χώρους ανάγνωσης"] if international else ["Πάτρα: Ανακοινώθηκε διακοπή νερού", "Διακοπή νερού στην Πάτρα λόγω εργασιών", "Πάτρα: Εργασίες στο δίκτυο ύδρευσης"]
            summary = "Νέα δημόσια βιβλιοθήκη άνοιξε στο Λονδίνο για τους κατοίκους." if international else "Στην Πάτρα ανακοινώθηκαν εργασίες στο δίκτυο και διακοπή νερού."
            if self.unsupported:
                titles[0] = "Πάτρα: Παραιτήθηκε ο δήμαρχος"
            data = {"title_candidates": titles, "selected_title": titles[0], "title_rationale": "Concrete sourced development",
                    "title_evidence_ids": [claim["id"]], "summary": summary, "summary_evidence_ids": [claim["id"]],
                    "body_sections": [{"text": "Η βιβλιοθήκη διαθέτει χώρους ανάγνωσης και φιλοξενεί δραστηριότητες για την κοινότητα." if international else "Η διακοπή συνδέεται με τις προγραμματισμένες εργασίες ύδρευσης στην πόλη.", "evidence_ids": [claim["id"]]}],
                    "claim_links": [{"statement": summary, "evidence_ids": [claim["id"]]}],
                    "category": packet["topic"], "topic_tags": [packet["topic"]], "geographic_ids": [place["id"] for place in packet["places"]],
                    "audience_scope": packet["audience_scope"], "slug": f"news-{packet['story_id'][:8]}",
                    "seo_description": summary, "seo_evidence_ids": [claim["id"]],
                    "source_references": [{key: source[key] for key in ["source_id", "revision_id", "url"]} for source in packet["sources"]],
                    "risk_flags": packet["risk_flags"]}
        elif kind == VerificationResult.__name__:
            data = {"checks": [{"segment_id": value["segment_id"],
                       "verdict": "insufficient" if "Παραιτήθηκε" in value["text"] else "supported",
                       "reason": "Mayor resignation absent" if "Παραιτήθηκε" in value["text"] else "The linked source supports the paraphrase",
                       "supporting_excerpts": [entry["excerpt"] for entry in value["evidence"]]}
                      for value in payload["segments"]]}
        else:
            raise AssertionError(f"Unexpected model schema: {kind}")
        return ModelResult(data, "scripted-ai", 400, 400, 1)


def backend(tmp_path, monkeypatch, *, world=True, ai=None):
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, llm_provider="openai", llm_api_key="test-model-key",
                        geo_register_id="test-world", source_catalog_path="", fixture_mode=False,
                        public_html_enabled=True, web_search_enabled=False, admin_token="x" * 32)
    with factory() as session, session.begin():
        import_register(session, register())
        for name, host, code in [("Patras News", "town.example", "GR")] + ([("London News", "world.example", "GB")] if world else []):
            session.add(Source(name=name, canonical_domain=host, feed_url=f"https://{host}/", source_type="html",
                        country_code=code, organization=name, language="el" if code == "GR" else "en",
                        can_discover=True, can_fetch=True, can_store=True, can_process_ai=True,
                        permission_basis="Synthetic test analysis", permission_reviewed_at=utc_now() - timedelta(days=1),
                        enabled=True, endpoint_verified_at=utc_now() - timedelta(hours=1)))
    seen = []
    def respond(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(200, text='<h2><a href="/news/community-report">A useful community news article</a></h2>', headers={"Content-Type": "text/html"})
        international = request.url.host == "world.example"
        title = "New public library opens in London" if international else "Πάτρα: Προγραμματισμένη διακοπή νερού"
        fact = EN_FACT if international else GR_FACT
        content = f'<html><head><script type="application/ld+json">{json.dumps({"@type":"NewsArticle","headline":title,"datePublished":"2026-10-09T09:00:00Z"},ensure_ascii=False)}</script></head><body><article><h1>{title}</h1><p>{fact}</p><p>{"Residents can read the official announcement for further information about the community facilities." if international else "Οι κάτοικοι μπορούν να διαβάσουν τη σχετική ανακοίνωση για περισσότερες πληροφορίες και ενημέρωση σχετικά με το πρόγραμμα των εργασιών."}</p></article></body></html>'
        return httpx.Response(200, text=content, headers={"Content-Type": "text/html"})
    transport = httpx.MockTransport(respond)
    options = {"transport": transport, "resolver": lambda host, port: ["93.184.215.14"]}
    monkeypatch.setattr("greek_news.jobs.poll_source", lambda session, source, **kwargs: poll_source(session, source, **kwargs, **options))
    monkeypatch.setattr("greek_news.jobs.fetch_frontier", lambda session, row, settings: fetch_frontier(session, row, settings, **options))
    ai = ai or ScriptedAI()
    monkeypatch.setattr("greek_news.enrichment.configured_client", lambda settings: ai)
    monkeypatch.setattr("greek_news.generation.configured_adapter", lambda settings: ModelDraftAdapter(settings, ai))
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    def database():
        with factory() as session:
            yield session
    app.dependency_overrides[get_session] = database
    return factory, settings, TestClient(app), ai, seen


def drain(factory, settings, *, start=None, ticks=4):
    start = start or utc_now()
    return [run_once(factory, settings, now=start + timedelta(seconds=3 * index)) for index in range(ticks)]


def test_html_to_local_and_world_publication_without_any_manual_review(tmp_path, monkeypatch):
    factory, settings, client, ai, seen = backend(tmp_path, monkeypatch)
    results = drain(factory, settings)
    assert all(result["failed"] == 0 for result in results), results
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 2
        assert all(row.status == "published" and row.editor_name is None for row in session.scalars(select(Article)))
        assert session.scalar(select(func.count()).select_from(LLMCall)) == 6
        assert all(row.validation["semantic_checked"] and row.validation["semantic_passed"] for row in session.scalars(select(ArticleVersion)))
    assert client.get("/articles", params={"edition": "greece", "region": "test-world:GR-G"}).json()["total"] == 1
    world = client.get("/articles", params={"edition": "world", "country": "GB", "category": "culture", "subcategory": "books"}).json()
    assert world["total"] == 1 and world["items"][0]["language"] == "el"
    assert world["items"][0]["topic_tags"] == ["libraries"]
    assert client.get("/articles", params={"q": "βιβλιοθήκη"}).json()["total"] == 1
    assert all("/feed" not in url and "/rss" not in url for url in seen)
    assert len(ai.calls) == 6


def test_unsupported_factual_headline_is_retried_then_skipped_without_publishing(tmp_path, monkeypatch):
    factory, settings, client, ai, seen = backend(tmp_path, monkeypatch, world=False, ai=ScriptedAI(unsupported=True))
    start = utc_now()
    drain(factory, settings, start=start)
    run_once(factory, settings, now=start + timedelta(minutes=2))
    run_once(factory, settings, now=start + timedelta(minutes=4))
    assert client.get("/articles").json()["total"] == 0
    with factory() as session:
        versions = session.scalars(select(ArticleVersion)).all()
        assert len(versions) == 3 and all(row.status == "skipped" for row in versions)
        assert all(any("insufficient" in message for message in row.validation["errors"]) for row in versions)
        assert session.scalar(select(Job).where(Job.kind == "generate")).state == "completed"


def test_invented_extracted_fact_never_becomes_evidence(tmp_path, monkeypatch):
    factory, settings, client, ai, seen = backend(tmp_path, monkeypatch, world=False, ai=ScriptedAI(invented_excerpt=True))
    drain(factory, settings)
    with factory() as session:
        revision = session.scalar(select(SourceRevision))
        assert revision.evidence_claims == [] and revision.analysis_state == "pending"
        assert session.scalar(select(func.count()).select_from(Story)) == 0
    assert client.get("/articles").json()["total"] == 0


def test_public_html_and_seo_exports_use_published_snapshot(tmp_path, monkeypatch):
    factory, settings, client, ai, seen = backend(tmp_path, monkeypatch, world=False)
    drain(factory, settings)
    value = client.get("/articles").json()["items"][0]
    path = f'/articles/{value["id"]}/{value["slug"]}'
    page = client.get(path)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'application/ld+json' in page.text and '<link rel="canonical"' in page.text
    assert client.get(path, headers={"Accept": "application/json"}).json()["id"] == value["id"]
    assert client.get(f'/articles/{value["id"]}/outdated-slug', follow_redirects=False).status_code == 301
    assert client.get("/sitemap.xml").status_code == 200
    assert value["canonical_url"] in client.get("/sitemaps/articles-1.xml").text
    assert value["title"] in client.get("/sitemaps/news.xml").text
    # An unpublished revision changes mutable story geography; visible tags remain published.
    with factory() as session, session.begin():
        from greek_news.models import StoryPlace
        for row in session.scalars(select(StoryPlace)):
            row.place_id = "test-world:london"
        session.scalar(select(Story)).audience_scope = "international"
    current = client.get(f'/articles/{value["id"]}').json()
    assert current["geography"] == value["geography"] and current["edition"] == "greece"
    assert client.get("/articles", params={"region": "test-world:GR-G"}).json()["total"] == 1
