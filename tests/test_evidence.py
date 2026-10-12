import pytest
from sqlalchemy import select

from greek_news.evidence import freeze_evidence
from greek_news.models import Source, Story
from greek_news.seeds import seed_demo


def test_evidence_packet_has_real_revisions_separate_roles_and_stable_hash(session):
    seed_demo(session)
    story = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    packet = freeze_evidence(session, story)
    assert packet.fixture_only and packet.independent_origins == 2
    assert len(packet.sources) == 3
    assert len([place for place in packet.places if place.role == "affected"]) == 3
    assert len([place for place in packet.places if place.role == "background"]) == 1
    assert all(claim.revision_id in {source.revision_id for source in packet.sources}
               for claim in packet.claims)
    assert packet.fingerprint() == freeze_evidence(session, story).fingerprint()


def test_revoking_ai_rights_stops_drafting(session):
    seed_demo(session)
    story = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    owner = session.scalar(select(Source).where(Source.name == "Fictional agency copy"))
    owner.can_process_ai = False
    with pytest.raises(PermissionError, match="AI processing"):
        freeze_evidence(session, story)