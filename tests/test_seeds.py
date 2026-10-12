from sqlalchemy import func, select

from greek_news.models import Source, SourceItem, Story
from greek_news.seeds import seed_candidates, seed_demo
from greek_news.stories import independent_origins


def test_seed_demo_idempotent_and_candidates_stay_disabled(session):
    first = seed_demo(session)
    second = seed_demo(session)
    assert first["places"] > 13 and first["items"] == 5
    assert second == {"places": 0, "sources": 0, "items": 0, "revisions": 0}
    assert session.scalar(select(func.count()).select_from(SourceItem)) == 5
    assert session.scalar(select(func.count()).select_from(Story)) == 3
    main = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    assert independent_origins(session, main.id) == 2
    assert seed_candidates(session) == 3
    assert seed_candidates(session) == 0
    for candidate in session.scalars(select(Source).where(Source.source_type != "fixture")):
        assert not candidate.enabled and not candidate.can_fetch and not candidate.can_process_ai