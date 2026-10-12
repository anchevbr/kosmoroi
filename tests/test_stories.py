from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from greek_news.geography import import_register
from greek_news.ingestion import EvidenceInput, PlaceHint, SourceInput, ingest_item
from greek_news.models import Source, SourceItem, Story, StoryPlace
from greek_news.stories import group_item, independent_origins, rank_story, select_candidates
from test_geography import load_fixture


START = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
GEO = "fixture-2026-demo"


def source(name, domain, syndication=None):
    return Source(
        name=name, canonical_domain=domain, feed_url=f"https://{domain}/feed",
        source_type="fixture", enabled=True, can_discover=True, can_fetch=True,
        can_store=True, can_process_ai=True, permission_basis="Synthetic fixture",
        permission_reviewed_at=START - timedelta(days=1), syndication_group=syndication,
    )


def report(domain, suffix, title, *, when=START, value="4", places=None, scope="local"):
    return SourceInput(
        url=f"https://{domain}/{suffix}", title=title, published_at=when,
        event_at=when + timedelta(hours=1), event_type="water_notice", topic="services",
        audience_scope=scope, text=f"{title}. Η διακοπή θα διαρκέσει {value} ώρες.",
        claims=[EvidenceInput(text=f"Η διακοπή θα διαρκέσει {value} ώρες.",
                              excerpt=f"Η διακοπή θα διαρκέσει {value} ώρες.",
                              fact_key="duration_hours", value=value)],
        place_hints=places if places is not None else [
            PlaceHint(name="Πάτρα", kind="municipality", role="affected"),
        ],
    )


def test_group_one_event_but_not_repeated_day_and_count_syndication_once(session):
    import_register(session, load_fixture())
    sources = [source("Agency", "agency.example", "wire"),
               source("Syndicated", "copy.example", "wire"),
               source("Municipal", "town.example")]
    session.add_all(sources)
    session.flush()
    first = report("agency.example", "a", "Πάτρα: διακοπή νερού σε τρεις δήμους")
    second = report("copy.example", "b", "Διακοπή νερού σε τρεις δήμους της Πάτρας")
    third = report("town.example", "c", "Πάτρα: διακοπή νερού σε τρεις δήμους")
    stories = []
    for owner, data in zip(sources, [first, second, third]):
        outcome = ingest_item(session, owner, data)
        stories.append(group_item(session, session.get(SourceItem, outcome.item_id), GEO))
    assert len({story.id for story in stories}) == 1
    assert independent_origins(session, stories[0].id) == 2
    assert "independent_origins=2" in rank_story(session, stories[0], START)[1]
    later = ingest_item(session, sources[0], report(
        "agency.example", "next", first.title, when=START + timedelta(days=1),
    ))
    repeated = group_item(session, session.get(SourceItem, later.item_id), GEO)
    assert repeated.id != stories[0].id
    assert session.scalar(select(func.count()).select_from(Story)) == 2


def test_multiple_affected_places_background_and_ambiguous_names(session):
    import_register(session, load_fixture())
    owner = source("Notices", "notice.example")
    session.add(owner)
    session.flush()
    places = [PlaceHint(name=name, kind="municipality", role="affected")
              for name in ("Πάτρα", "Αιγιάλεια", "Δυτική Αχαΐα")]
    places.append(PlaceHint(name="Ηράκλειο", kind="municipality", context="Αττική",
                            role="background"))
    item = ingest_item(session, owner, report("notice.example", "three", "Διακοπή νερού στην Πάτρα",
                                             places=places))
    story = group_item(session, session.get(SourceItem, item.item_id), GEO)
    roles = [(row.role, row.place_id) for row in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story.id,
    ))]
    assert len([row for row in roles if row[0] == "affected"]) == 3
    assert len([row for row in roles if row[0] == "background"]) == 1
    ambiguous = ingest_item(session, owner, report(
        "notice.example", "unclear", "Διακοπή νερού στο Ηράκλειο",
        places=[PlaceHint(name="Ηράκλειο", kind="municipality", role="affected")],
    ))
    pending = group_item(session, session.get(SourceItem, ambiguous.item_id), GEO)
    assert pending.status == "skipped"
    assert "ambiguous_geography" in pending.risk_flags
    assert not session.scalars(select(StoryPlace).where(StoryPlace.story_id == pending.id)).all()


def test_national_report_is_not_attached_to_every_place(session):
    import_register(session, load_fixture())
    owner = source("National", "national.example")
    session.add(owner)
    session.flush()
    data = report("national.example", "one", "Εθνική ανακοίνωση για υπηρεσίες",
                  places=[], scope="national")
    item = ingest_item(session, owner, data)
    story = group_item(session, session.get(SourceItem, item.item_id), GEO)
    assert story.audience_scope == "national"
    assert session.scalar(select(func.count()).select_from(StoryPlace)) == 0


def test_conflicting_numbers_trigger_review(session):
    import_register(session, load_fixture())
    owners = [source("A", "a.example"), source("B", "b.example")]
    session.add_all(owners)
    session.flush()
    for owner, value in zip(owners, ("4", "5")):
        item = ingest_item(session, owner, report(owner.canonical_domain, "notice",
                           "Πάτρα: διακοπή νερού σε τρεις δήμους", value=value))
        story = group_item(session, session.get(SourceItem, item.item_id), GEO)
    assert story.conflict and story.status == "skipped"


def test_source_correction_rebuilds_affected_places_without_changing_story_id(session):
    import_register(session, load_fixture())
    owner = source("Corrections", "corrections.example")
    session.add(owner)
    session.flush()
    first = ingest_item(session, owner, report(
        owner.canonical_domain, "notice", "Πάτρα: διακοπή νερού σε τρεις δήμους",
    ))
    item = session.get(SourceItem, first.item_id)
    story = group_item(session, item, GEO)
    assert [link.place_id for link in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story.id, StoryPlace.role == "affected",
    ))] == [f"{GEO}:m-patra"]
    correction = ingest_item(session, owner, report(
        owner.canonical_domain, "notice", "Ηράκλειο: διακοπή νερού",
        places=[PlaceHint(name="Ηράκλειο", kind="municipality", context="Κρήτη",
                          role="affected")],
    ))
    assert correction.state == "revised"
    assert group_item(session, item, GEO).id == story.id
    assert [link.place_id for link in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story.id, StoryPlace.role == "affected",
    ))] == [f"{GEO}:m-heraklion-crete"]
    assert story.status == "new"
    assert story in select_candidates(session, now=START + timedelta(hours=1))


def test_national_and_regional_selection_budgets_do_not_create_quotas(session):
    from greek_news.seeds import seed_demo

    seed_demo(session)
    selected = select_candidates(session, now=START, national_max=1, regional_max=1)
    assert {story.audience_scope for story in selected} == {"national", "local"}
    assert len(selected) == 2
    assert not select_candidates(session, now=START, national_max=0, regional_max=0)
    assert len(select_candidates(session, now=START, national_max=10, regional_max=10)) == 2