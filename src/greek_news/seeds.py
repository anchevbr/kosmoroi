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
    candidates = [dict(name=name, canonical_domain=host, feed_url=url,
                       source_type="html", extraction_strategy="article_html",
                       organization=name, verification_note="HTML example; policy not configured")
                  for name, host, url in [
                      ("ERT Regional HTML example", "www.ertnews.gr", "https://www.ertnews.gr/news/perifereiakoi-stathmoi/"),
                      ("Civil Protection HTML example", "civilprotection.gov.gr", "https://civilprotection.gov.gr/"),
                      ("UN News HTML example", "news.un.org", "https://news.un.org/en/")]]
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