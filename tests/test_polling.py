import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from greek_news.discovery import CatalogCandidate, SourceCatalog, import_catalog, verify_candidate
from greek_news.models import AuditEntry
from greek_news.models import Source, SourceItem, SourceRevision
from greek_news.polling import poll_source


FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Notices</title>
<item><title>Notice A</title><link>https://source.example/a</link><guid>bad-guid</guid>
<pubDate>Thu, 08 Oct 2026 09:00:00 +0300</pubDate></item>
<item><title>Notice B</title><link>https://source.example/b</link><guid>bad-guid</guid>
<pubDate>Thu, 08 Oct 2026 09:00:00 +0300</pubDate></item>
</channel></rss>"""


def eligible_source():
    return Source(
        name="Feed test", canonical_domain="source.example", feed_url="https://source.example/feed",
        source_type="rss", extraction_strategy="feed_metadata", enabled=True,
        can_discover=True, can_fetch=True, can_store=True,
        permission_basis="Synthetic feed created for this test",
        permission_reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        endpoint_verified_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_poll_handles_duplicate_guids_dates_and_etag(session):
    seen = []

    def respond(request):
        seen.append((request.url.path, request.headers.get("If-None-Match")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=FEED, headers={"ETag": '"v1"'})

    source = eligible_source()
    session.add(source)
    session.flush()
    options = dict(transport=httpx.MockTransport(respond),
                   resolver=lambda host, port: ["93.184.215.14"])
    first = poll_source(session, source, **options)
    assert (first.state, first.new, first.skipped) == ("ok", 2, 0)
    assert session.scalar(select(func.count()).select_from(SourceItem)) == 2
    assert {item.source_published_at for item in session.scalars(select(SourceItem))} == {
        datetime(2026, 10, 8, 6, tzinfo=timezone.utc)
    }
    source.next_poll_at = None
    assert poll_source(session, source, **options).state == "unchanged"
    assert ("/feed", '"v1"') in seen


def test_html_index_collects_article_links_without_copying_text(session):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="""<main>
            <article><h2><a href="/news/local-notice">Local notice</a></h2></article>
            <article><a href="https://source.example/news/second">Second notice</a></article>
            <article><a href="https://outside.example/news">Foreign notice</a></article>
            <nav><a href="/about">About us</a></nav>
        </main>""")

    source = eligible_source()
    source.feed_url = "https://source.example/"
    source.source_type = "html_index"
    source.extraction_strategy = "index_links"
    session.add(source)
    session.flush()
    outcome = poll_source(session, source, transport=httpx.MockTransport(respond),
                          resolver=lambda host, port: ["93.184.215.14"])
    assert (outcome.state, outcome.new) == ("ok", 2)
    assert paths == ["/robots.txt", "/"]
    items = session.scalars(select(SourceItem)).all()
    assert {item.canonical_url for item in items} == {
        "https://source.example/news/local-notice", "https://source.example/news/second",
    }
    assert all(not revision.permitted_text and not revision.evidence_claims
               for revision in session.scalars(select(SourceRevision)))


def test_html_index_verification_requires_grant_and_article_links(session):
    source = eligible_source()
    source.feed_url = "https://source.example/"
    source.source_type = "html_index"
    source.extraction_strategy = "index_links"
    source.enabled = False
    source.endpoint_verified_at = None
    session.add(source)
    session.flush()
    never = httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("network used")))
    source.permission_basis = ""
    assert verify_candidate(session, source, transport=never).state == "blocked"
    source.permission_basis = "Authorized to collect headline links"
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text="<article><a href='/notice'>Local notice</a></article>")

    result = verify_candidate(session, source, transport=httpx.MockTransport(respond),
                              resolver=lambda host, port: ["93.184.215.14"])
    assert result.state == "active" and source.enabled
    assert source.endpoint_verified_at and requested == ["/robots.txt", "/"]


def test_robots_denial_never_fetches_feed(session):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /feed\n")

    source = eligible_source()
    session.add(source)
    session.flush()
    result = poll_source(session, source, transport=httpx.MockTransport(respond),
                         resolver=lambda host, port: ["93.184.215.14"])
    assert result.state == "failed" and "Robots" in result.reason
    assert calls == ["/robots.txt"]


def test_new_eligible_candidate_activates_without_manual_approval(session):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, content=FEED)

    candidate = eligible_source()
    candidate.enabled = False
    candidate.endpoint_verified_at = None
    candidate.can_process_ai = True
    session.add(candidate)
    session.flush()
    result = verify_candidate(session, candidate,
                              now=datetime(2026, 10, 9, 10, tzinfo=timezone.utc),
                              transport=httpx.MockTransport(respond),
                              resolver=lambda host, port: ["93.184.215.14"])
    assert result.state == "active" and candidate.enabled
    assert candidate.endpoint_verified_at and requested == ["/robots.txt", "/feed"]
    assert session.scalar(select(AuditEntry).where(AuditEntry.action == "source_activated"))


def test_candidate_without_grant_or_with_operator_pause_never_fetches(session):
    candidate = eligible_source()
    candidate.enabled = False
    candidate.endpoint_verified_at = None
    candidate.can_process_ai = True
    session.add(candidate)
    session.flush()
    never = httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("network used")))
    candidate.permission_basis = ""
    assert verify_candidate(session, candidate, transport=never).state == "blocked"
    candidate.permission_basis = "Original authorized feed"
    candidate.operator_disabled = True
    assert verify_candidate(session, candidate, transport=never).state == "blocked"


def test_repeated_poll_failures_disable_source_for_automatic_reverification(session):
    source = eligible_source()
    source.can_process_ai = True
    session.add(source)
    session.flush()
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    for _ in range(5):
        source.next_poll_at = None
        assert poll_source(session, source, transport=transport,
                           resolver=lambda host, port: ["93.184.215.14"]).state == "failed"
    assert not source.enabled and source.consecutive_failures == 5
    assert session.scalar(select(AuditEntry).where(AuditEntry.action == "source_auto_disabled"))


def test_catalog_discovers_candidates_idempotently_without_making_grants(tmp_path, session):
    catalog = tmp_path / "candidates.json"
    catalog.write_text('{"sources":[{"name":"Community candidate",'
                       '"canonical_domain":"community.example",'
                       '"feed_url":"https://community.example/rss",'
                       '"coverage_ids":["fixture-2026-demo:r-west-greece"]}]}',
                       encoding="utf-8")
    assert import_catalog(session, catalog) == 1
    assert import_catalog(session, catalog) == 0
    source = session.scalar(select(Source).where(Source.name == "Community candidate"))
    assert not source.enabled and not source.can_process_ai and not source.permission_basis
    assert source.coverage_ids == ["fixture-2026-demo:r-west-greece"]
    assert "rights: unverified" in source.verification_note
    assert session.scalar(select(AuditEntry).where(
        AuditEntry.action == "source_discovered",
    )).detail["rights_status"] == "unverified"


def test_catalog_cannot_claim_permission_without_documented_grant():
    with pytest.raises(ValueError, match="documented grant"):
        CatalogCandidate(name="Unknown rights", canonical_domain="local.example",
                         feed_url="https://local.example/rss", can_fetch=True)


def test_catalog_upgrades_only_untouched_homepage_candidates(tmp_path, session):
    path = tmp_path / "sources.json"
    names = {"Untouched": "untouched.example", "Configured": "configured.example",
             "Paused": "paused.example"}
    path.write_text(json.dumps({"sources": [
        {"name": name, "canonical_domain": domain, "feed_url": f"https://{domain}/",
         "source_type": "html_index", "extraction_strategy": "requires_site_plugin"}
        for name, domain in names.items()
    ]}), encoding="utf-8")
    assert import_catalog(session, path) == 3
    configured = session.scalar(select(Source).where(Source.name == "Configured"))
    configured.permission_basis = "Operator-reviewed use"
    configured.can_fetch = True
    paused = session.scalar(select(Source).where(Source.name == "Paused"))
    paused.operator_disabled = True
    path.write_text(json.dumps({"sources": [
        {"name": name, "canonical_domain": domain,
         "feed_url": f"https://{domain}/feed/"} for name, domain in names.items()
    ]}), encoding="utf-8")
    assert import_catalog(session, path) == 0
    assert import_catalog(session, path) == 0
    untouched = session.scalar(select(Source).where(Source.name == "Untouched"))
    assert untouched.feed_url == "https://untouched.example/feed/"
    assert untouched.source_type == "rss" and not untouched.enabled and not untouched.can_fetch
    assert configured.feed_url == "https://configured.example/" and configured.can_fetch
    assert paused.feed_url == "https://paused.example/" and paused.operator_disabled
    assert session.scalar(select(func.count()).select_from(Source)) == 3
    assert session.scalar(select(func.count()).select_from(AuditEntry).where(
        AuditEntry.action == "source_endpoint_discovered")) == 1


def test_bundled_source_catalog_covers_every_region_without_granting_rights(session):
    path = Path(__file__).resolve().parents[1] / "fixtures" / "source_catalog.json"
    catalog = SourceCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    assert len(catalog.sources) > 60
    assert len({candidate.name for candidate in catalog.sources}) == len(catalog.sources)
    assert len({candidate.canonical_domain for candidate in catalog.sources}) == len(catalog.sources)
    assert len({candidate.feed_url for candidate in catalog.sources}) == len(catalog.sources)
    regions = {
        "attica", "central_macedonia", "west_macedonia", "east_macedonia_thrace",
        "epirus", "thessaly", "ionian_islands", "west_greece", "central_greece",
        "peloponnese", "north_aegean", "south_aegean", "crete",
    }
    assert {candidate.target_region for candidate in catalog.sources} >= regions | {"national", "global"}
    feed_regions = {candidate.target_region for candidate in catalog.sources
                    if candidate.source_type in {"rss", "atom"}}
    assert feed_regions >= regions | {"national", "global"}, regions - feed_regions
    assert {"Ta Nea", "Skai", "CNN Greece", "Proto Thema", "To Vima",
            "Efsyn"} <= {candidate.name for candidate in catalog.sources}
    assert import_catalog(session, path) == len(catalog.sources)
    assert import_catalog(session, path) == 0
    assert all(not any((source.enabled, source.can_discover, source.can_fetch,
                        source.can_store, source.can_process_ai, source.can_reuse_text,
                        source.can_reuse_images))
               and source.coverage_ids == [] for source in session.scalars(select(Source)))