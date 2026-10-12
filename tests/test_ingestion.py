from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from greek_news.db import Base
from greek_news.ingestion import EvidenceInput, SourceInput, canonical_url, ingest_item
from greek_news.models import Source, SourceItem, SourceRevision


def fixture_source():
    return Source(
        name="Synthetic notices", canonical_domain="fixtures.invalid",
        feed_url="https://fixtures.invalid/feed", source_type="fixture", enabled=True,
        can_discover=True, can_fetch=True, can_store=True, can_process_ai=True,
        permission_basis="Synthetic texts created for offline testing",
        permission_reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def notice(title="Πάτρα: διακοπή νερού"):
    return SourceInput(
        url="https://fixtures.invalid/notice?utm_source=test", title=title,
        published_at=datetime(2026, 10, 8, 9, tzinfo=timezone.utc),
        text=f"{title}. Η ανακοίνωση αφορά τρεις δήμους.",
        claims=[EvidenceInput(text="Η ανακοίνωση αφορά τρεις δήμους.",
                              excerpt="Η ανακοίνωση αφορά τρεις δήμους.")],
    )


def test_reimports_and_changed_facts_make_immutable_revisions(session):
    source = fixture_source()
    session.add(source)
    session.flush()
    initial = ingest_item(session, source, notice())
    again = ingest_item(session, source, notice())
    revised = ingest_item(session, source, notice("Πάτρα: ενημερωμένη διακοπή νερού"))
    assert (initial.state, again.state, revised.state) == ("new", "unchanged", "revised")
    assert initial.item_id == revised.item_id
    assert session.scalar(select(func.count()).select_from(SourceItem)) == 1
    assert session.scalar(select(func.count()).select_from(SourceRevision)) == 2
    assert session.get(SourceRevision, initial.revision_id).title == "Πάτρα: διακοπή νερού"
    assert ingest_item(session, source, notice()).state == "revised"
    assert session.get(SourceItem, initial.item_id).current_revision == 3
    assert session.scalar(select(func.count()).select_from(SourceRevision)) == 3


def test_refuses_unreviewed_permissions_and_naive_dates(session):
    source = fixture_source()
    source.can_store = False
    session.add(source)
    session.flush()
    with pytest.raises(PermissionError):
        ingest_item(session, source, notice())
    with pytest.raises(ValueError, match="timezone"):
        SourceInput(url="https://fixtures.invalid/one", title="Title",
                    published_at=datetime(2026, 10, 8))
    assert canonical_url("https://fixtures.invalid/one/?gclid=a&utm_source=b#fragment") == (
        "https://fixtures.invalid/one"
    )


def test_concurrent_imports_keep_one_item_and_revision(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = fixture_source()
        session.add(source)
        session.commit()
        source_id = source.id

    def worker(_):
        with Session(engine) as session:
            result = ingest_item(session, session.get(Source, source_id), notice())
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(8)))
    with Session(engine) as session:
        assert len({result.item_id for result in results}) == 1
        assert session.scalar(select(func.count()).select_from(SourceItem)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRevision)) == 1
    engine.dispose()