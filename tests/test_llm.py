from sqlalchemy import select

from greek_news.config import Settings
from greek_news.evidence import freeze_evidence
from greek_news.llm import FakeAdapter, configured_adapter
from greek_news.models import Story
from greek_news.seeds import seed_demo


def test_fake_draft_is_structured_and_never_grants_publication(session):
    seed_demo(session)
    story = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    packet = freeze_evidence(session, story)
    result = FakeAdapter().draft(packet)
    assert len(set(result.draft.title_candidates)) == 3
    assert result.draft.geographic_ids == [place.id for place in packet.places
                                           if place.role == "affected"]
    assert {ref.revision_id for ref in result.draft.source_references} == {
        source.revision_id for source in packet.sources
    }
    assert "status" not in result.draft.model_dump()
    assert result.input_tokens > 0 and result.output_tokens > 0


def test_live_adapter_requires_explicit_provider_configuration():
    assert isinstance(configured_adapter(Settings(_env_file=None)), FakeAdapter)
    try:
        configured_adapter(Settings(_env_file=None, llm_provider="openai"))
    except RuntimeError as exc:
        assert "NEWS_LLM_API_KEY" in str(exc)
    else:
        raise AssertionError("Missing live credentials must not succeed")