from datetime import datetime, timezone

from sqlalchemy import select

from greek_news.evidence import freeze_evidence
from greek_news.llm import FakeAdapter
from greek_news.models import Story
from greek_news.seeds import seed_demo
from greek_news.validation import validate_draft


NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)


def setup_draft(session):
    seed_demo(session)
    story = session.scalar(select(Story).where(Story.event_type == "water_notice"))
    packet = freeze_evidence(session, story)
    return packet, FakeAdapter().draft(packet).draft


def test_fake_article_is_structurally_grounded_but_semantics_remain_unchecked(session):
    packet, draft = setup_draft(session)
    result = validate_draft(draft, packet, NOW)
    assert result.valid, result.errors
    assert not result.semantic_checked


def test_unsupported_number_quote_date_location_and_sensational_title_fail(session):
    packet, draft = setup_draft(session)
    unsupported = draft.model_copy(deep=True)
    unsupported.body_sections[0].text += " Η διακοπή θα διαρκέσει 99 ώρες."
    unsupported.body_sections[0].text += " Ο δήμαρχος είπε «Δεν υπάρχει πρόβλημα»."
    unsupported.title_candidates = (
        "Χάος στο Ηράκλειο στις 11 Νοεμβρίου 2026",
        draft.title_candidates[1], draft.title_candidates[2],
    )
    unsupported.selected_title = unsupported.title_candidates[0]
    unsupported.geographic_ids.append(next(place.id for place in packet.places
                                            if place.role == "background"))
    result = validate_draft(unsupported, packet, NOW)
    assert not result.valid
    assert any("99" in error for error in result.errors)
    assert any("quotation" in error for error in result.errors)
    assert any("event month" in error for error in result.errors)
    assert any("background place" in error for error in result.errors)
    assert any("sensational" in error for error in result.errors)
    assert any("Geography IDs" in error for error in result.errors)


def test_html_fake_reference_and_stale_notice_fail(session):
    packet, draft = setup_draft(session)
    modified = draft.model_copy(deep=True)
    modified.body_sections[0].text += "<script>alert(1)</script>"
    modified.source_references[0].revision_id = "not-a-real-revision"
    result = validate_draft(modified, packet, datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert not result.valid
    assert any("HTML" in error for error in result.errors)
    assert any("Source references" in error for error in result.errors)
    assert any("interval has ended" in error for error in result.errors)