"""Idempotent fictional development scenarios and inactive live-source candidates."""

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.db import utc_now
from greek_news.geography import RegisterInput, import_register
from greek_news.ingestion import SourceInput, ingest_item
from greek_news.models import Source, SourceItem
from greek_news.stories import group_item


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CHECKED = datetime(2026, 10, 9, tzinfo=timezone.utc)


def seed_candidates(session: Session) -> int:
    candidates = [
        dict(
            name="Civil Protection press RSS - rights review required",
            canonical_domain="civilprotection.gov.gr",
            feed_url="https://civilprotection.gov.gr/deltia-tupou.rss",
            source_type="rss", organization="Greek Civil Protection Ministry",
            trust_category="official_statement", poll_minutes=30,
            permission_basis="RSS advertised for feed readers; site terms exclude commercial reuse under CC BY-NC-SA 4.0. Permissions for this product not established.",
            terms_url="https://civilprotection.gov.gr/oroi-xrisis",
            permission_reviewed_at=CHECKED, endpoint_verified_at=CHECKED,
            verification_note="RSS responded 2026-10-09; duplicate entries and malformed GUIDs observed; not cleared for commercial AI processing.",
        ),
        dict(
            name="ERTNews regional page - inactive candidate",
            canonical_domain="www.ertnews.gr",
            feed_url="https://www.ertnews.gr/news/perifereiakoi-stathmoi/",
            source_type="html_index", extraction_strategy="requires_site_plugin",
            organization="ERT", trust_category="publisher", poll_minutes=30,
            verification_note="Regional reporting page responded 2026-10-09; no feed endpoint or commercial AI rights verified.",
            endpoint_verified_at=CHECKED,
        ),
        dict(
            name="Civil Protection maps RSS - inactive candidate",
            canonical_domain="civilprotection.gov.gr",
            feed_url="https://civilprotection.gov.gr/cp_hartes.rss",
            source_type="rss", organization="Greek Civil Protection Ministry",
            trust_category="official_statement", poll_minutes=60,
            verification_note="Advertised in RSS directory; endpoint itself and rights not checked.",
        ),
    ]
    count = 0
    for values in candidates:
        if session.scalar(select(Source).where(Source.feed_url == values["feed_url"])):
            continue
        session.add(Source(**values))
        count += 1
    session.flush()
    return count


def seed_demo(session: Session) -> dict[str, int]:
    geo = RegisterInput.model_validate_json((FIXTURES / "places.json").read_text(encoding="utf-8"))
    places = import_register(session, geo)
    scenarios = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
    source_count = item_count = revision_count = 0
    for spec in scenarios["sources"]:
        owner = session.scalar(select(Source).where(Source.feed_url == spec["feed_url"]))
        if owner is None:
            owner = Source(
                **spec, source_type="fixture", enabled=True, can_discover=True,
                can_fetch=True, can_store=True, can_process_ai=True, can_reuse_text=True,
                permission_basis="Original fictional text created for offline tests only",
                permission_reviewed_at=utc_now(),
            )
            session.add(owner)
            session.flush()
            source_count += 1
    for row in scenarios["items"]:
        source = session.scalar(select(Source).where(Source.name == row["source"]))
        result = ingest_item(session, source, SourceInput.model_validate(row["input"]))
        item_count += result.state == "new"
        revision_count += result.state == "revised"
        group_item(session, session.get(SourceItem, result.item_id), geo.id)
    return dict(places=places, sources=source_count, items=item_count, revisions=revision_count)