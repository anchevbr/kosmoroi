"""HTML collector behavior, source policy, backoff, detail extraction and idempotency."""

import json
import socket
from datetime import timedelta
from pathlib import Path
import httpx
import pytest
from sqlalchemy import func, select
from greek_news.config import Settings
from greek_news.db import utc_now, use_clock
from greek_news.discovery import CatalogCandidate, SourceCatalog, import_catalog, verify_candidate
from greek_news.html_pages import document, parse_article
from greek_news.models import CrawlURL, Source, SourceItem, SourceRevision
from greek_news.polling import fetch_frontier, poll_source

INDEX = b'''<html><body><main><div><h2><a href="/news/local-notice">Local water service notice in Patras</a></h2></div><article><a href="/news/another-report">Second useful local community report</a></article><nav><a href="/about">About</a><a href="/feed/">RSS</a></nav></main></body></html>'''
ARTICLE = '''<html lang="el"><head><meta property="og:type" content="article"><script type="application/ld+json">{"@type":"NewsArticle","headline":"Πάτρα: Προγραμματισμένη διακοπή νερού","datePublished":"2026-10-09T12:00:00+03:00","dateModified":"2026-10-09T12:30:00+03:00"}</script></head><body><article><h1>Πάτρα: Προγραμματισμένη διακοπή νερού</h1><p>Στην Πάτρα ανακοινώθηκε προγραμματισμένη διακοπή νερού λόγω εργασιών στο δίκτυο ύδρευσης. Η υπηρεσία ενημερώνει τους κατοίκους για τις εργασίες.</p><p>Οι κάτοικοι μπορούν να διαβάσουν τη σχετική ανακοίνωση για την ενημέρωσή τους. Νεότερη ανακοίνωση θα δημοσιευτεί αν προκύψει αλλαγή στο πρόγραμμα.</p></article></body></html>'''
def RESOLVER(host, port):
    return ["93.184.215.14"]


def eligible_source():
    now = utc_now()
    return Source(name="HTML test", canonical_domain="source.example", feed_url="https://source.example/",
                  source_type="html", extraction_strategy="article_html", enabled=True,
                  can_discover=True, can_fetch=True, can_store=True, can_process_ai=True,
                  permission_basis="Synthetic public HTML for this test", permission_reviewed_at=now - timedelta(days=1),
                  endpoint_verified_at=now - timedelta(hours=1))


def transport_for(body=INDEX, *, etag=None, robots="User-agent: *\nAllow: /\n", seen=None):
    def respond(request):
        if seen is not None:
            seen.append((request.url.path, request.headers.get("If-None-Match")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if etag and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else body,
                              headers={"Content-Type": "text/html", **({"ETag": etag} if etag else {})})
    return httpx.MockTransport(respond)


def test_indexes_find_links_outside_article_tags_and_never_store_menu_text(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    result = poll_source(session, source, transport=transport_for(), resolver=RESOLVER)
    assert result.state == "ok" and result.new == 2
    rows = session.scalars(select(CrawlURL)).all()
    assert {row.url for row in rows} == {"https://source.example/news/local-notice", "https://source.example/news/another-report"}
    assert all(row.kind == "article" for row in rows)
    assert session.scalar(select(func.count()).select_from(SourceItem)) == 0


def test_index_conditional_get_does_not_redownload_known_pages(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    seen = []
    transport = transport_for(etag='"v1"', seen=seen)
    assert poll_source(session, source, transport=transport, resolver=RESOLVER).new == 2
    with use_clock(utc_now() + timedelta(minutes=31)):
        assert poll_source(session, source, transport=transport, resolver=RESOLVER).state == "unchanged"
    assert ("/", '"v1"') in seen
    assert len([path for path, _ in seen if path == "/robots.txt"]) == 1


def test_detail_html_extracts_content_dates_and_keeps_revisions_idempotent(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    assert poll_source(session, source, transport=transport_for(), resolver=RESOLVER).state == "ok"
    row = session.scalar(select(CrawlURL).where(CrawlURL.url.endswith("local-notice")))
    with use_clock(utc_now() + timedelta(seconds=3)):
        result = fetch_frontier(session, row, Settings(_env_file=None), transport=transport_for(ARTICLE), resolver=RESOLVER)
    assert result.new == 1
    item = session.get(SourceItem, row.source_item_id)
    revision = session.scalar(select(SourceRevision))
    assert item.source_published_at.hour == 9 and revision.permitted_text
    assert revision.evidence_claims == [] and revision.analysis_state == "pending"
    assert revision.analysis["html_metadata"]["publication_time_verified"]
    with use_clock(utc_now() + timedelta(hours=7)):
        result = fetch_frontier(session, row, Settings(_env_file=None), transport=transport_for(ARTICLE), resolver=RESOLVER)
    assert result.new == result.revised == 0
    assert session.scalar(select(func.count()).select_from(SourceRevision)) == 1


def test_robots_denial_never_fetches_html(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    seen = []
    result = poll_source(session, source, transport=transport_for(robots="User-agent: *\nDisallow: /\n", seen=seen), resolver=RESOLVER)
    assert result.state == "failed" and "Robots" in result.reason
    assert seen == [("/robots.txt", None)]


def test_automatic_verification_accepts_html_and_no_editor_click(session):
    source = eligible_source()
    source.enabled, source.endpoint_verified_at = False, None
    session.add(source)
    session.flush()
    result = verify_candidate(session, source, transport=transport_for(), resolver=RESOLVER)
    assert result.state == "active" and source.enabled and source.endpoint_verified_at


def test_automatic_verification_rejects_rss_even_when_mislabeled_html(session):
    source = eligible_source()
    source.enabled, source.endpoint_verified_at = False, None
    session.add(source)
    session.flush()
    result = verify_candidate(session, source, transport=transport_for(b'<rss version="2.0"><channel/></rss>'), resolver=RESOLVER)
    assert result.state == "deferred" and not source.enabled
    with pytest.raises(ValueError, match="HTML"):
        document(b'{"news": []}', "application/json")


def test_dns_failure_is_a_bounded_health_failure(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    def unavailable(*args):
        raise socket.gaierror("DNS failure")
    result = poll_source(session, source, transport=transport_for(), resolver=unavailable)
    assert result.state == "failed" and source.next_poll_at > utc_now()
    assert source.consecutive_failures == 1


def test_repeated_failures_trigger_automatic_reverification(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    for _ in range(5):
        source.next_poll_at = source.next_request_at = None
        assert poll_source(session, source, transport=transport, resolver=RESOLVER).state == "failed"
    assert not source.enabled and source.endpoint_verified_at is None
    assert source.next_poll_at > utc_now()


def test_host_cadence_defers_articles_instead_of_sleeping(session):
    source = eligible_source()
    session.add(source)
    session.flush()
    poll_source(session, source, transport=transport_for(), resolver=RESOLVER)
    row = session.scalar(select(CrawlURL))
    never = httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("No early fetch")))
    assert fetch_frontier(session, row, Settings(_env_file=None), transport=never, resolver=RESOLVER).state == "deferred"
    assert row.next_fetch_at > utc_now()


def test_html_metadata_and_canonical_cannot_move_to_another_host():
    content = ARTICLE.replace('<head>', '<head><link rel="canonical" href="https://evil.example/copied">')
    item, meta = parse_article(content.encode(), "https://source.example/news/one", "source.example")
    assert item.url == "https://source.example/news/one"
    assert item.updated_at > item.published_at and meta["date_precision"] == "timestamp"


def test_script_only_or_paywalled_page_is_skipped():
    with pytest.raises(ValueError, match="too little"):
        parse_article(b'<html><h1>Subscribe for this news</h1><script>load()</script></html>',
                      "https://source.example/story", "source.example")


def test_public_fact_catalog_is_automatic_without_claiming_reuse_license(session, tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": [{"name": "Community", "canonical_domain": "town.example",
                                           "feed_url": "https://town.example/", "rights_status": "public_facts"}]}))
    settings = Settings(_env_file=None, public_html_enabled=True)
    assert import_catalog(session, path, settings) == 1
    assert import_catalog(session, path, settings) == 0
    source = session.scalar(select(Source))
    assert not source.enabled and source.can_fetch and source.can_process_ai
    assert not source.can_reuse_text and not source.can_reuse_images
    assert "not a redistribution license" in source.permission_basis


def test_verbatim_or_image_license_cannot_be_inferred_from_public_html():
    with pytest.raises(ValueError, match="documented grant"):
        CatalogCandidate(name="Reuse", canonical_domain="town.example", feed_url="https://town.example/", can_reuse_images=True)
    with pytest.raises(ValueError):
        CatalogCandidate(name="Feed", canonical_domain="town.example", feed_url="https://town.example/", source_type="rss")


def test_catalog_covers_every_greek_region_and_international_sources():
    data = SourceCatalog.model_validate_json(Path("fixtures/source_catalog.json").read_text())
    regions = {"attica", "central_macedonia", "west_macedonia", "east_macedonia_thrace", "epirus", "thessaly",
               "ionian_islands", "west_greece", "central_greece", "peloponnese", "north_aegean", "south_aegean", "crete"}
    assert {row.target_region for row in data.sources} >= regions | {"national", "global"}
    assert len(data.sources) >= 90 and all(row.source_type == "html" for row in data.sources)
    assert len({row.feed_url for row in data.sources}) == len(data.sources)
    assert all("/feed" not in row.feed_url and ".rss" not in row.feed_url for row in data.sources)
    assert len({row.country_code for row in data.sources}) >= 10
