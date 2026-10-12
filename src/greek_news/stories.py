"""Conservative event grouping, role-specific geography and explainable ranking."""

import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from greek_news.db import utc_now
from greek_news.geography import AmbiguousPlace, country_of, normalize_name, region_of, resolve_place
from greek_news.ingestion import PlaceHint
from greek_news.models import Place, Source, SourceItem, SourceRevision, Story, StoryPlace


ATHENS = ZoneInfo("Europe/Athens")
STOPWORDS = {"και", "στη", "στην", "στο", "στον", "της", "των", "του", "για", "απο", "σε"}


def _tokens(title: str, places: list[Place]) -> set[str]:
    place_words = {word for place in places for word in normalize_name(place.name_el).split()}
    return {token for token in normalize_name(title).split()
            if len(token) > 2 and token not in STOPWORDS and token not in place_words}


def _revision(session: Session, item: SourceItem) -> SourceRevision:
    return session.scalar(select(SourceRevision).where(
        SourceRevision.source_item_id == item.id,
        SourceRevision.number == item.current_revision,
    ))


def _resolve_hints(
    session: Session, revision: SourceRevision, register_id: str,
) -> tuple[list[tuple[Place, str]], bool]:
    resolved = []
    ambiguous = False
    for raw in revision.place_hints:
        hint = PlaceHint.model_validate(raw)
        try:
            place = resolve_place(session, hint.name, kind=hint.kind, context=hint.context,
                                  register_id=register_id)
            resolved.append((place, hint.role))
        except AmbiguousPlace:
            ambiguous = True
    return resolved, ambiguous


def _scope(session: Session, item: SourceItem, resolved: list[tuple[Place, str]]) -> str:
    if item.audience_scope == "international":
        return "international"
    if any((country_of(session, place) is not None
            and country_of(session, place).official_id != "GR")
           for place, role in resolved if role in {"affected", "occurred"}):
        return "international"
    if item.audience_scope == "national":
        return "national"
    relevant = [place for place, role in resolved if role in {"occurred", "affected"}]
    if not relevant:
        return item.audience_scope
    if len({region_of(session, place).id for place in relevant}) > 1:
        return "mixed"
    if all(place.kind in {"region", "regional_unit"} for place in relevant):
        return "regional"
    return "local"


def independent_origins(session: Session, story_id: str) -> int:
    rows = session.execute(select(Source).join(SourceItem, SourceItem.source_id == Source.id).where(
        SourceItem.story_id == story_id,
    )).scalars().all()
    return len({source.syndication_group or source.organization or source.id for source in rows})


def _conflicts(session: Session, story_id: str) -> bool:
    values: dict[str, set[str]] = {}
    items = session.scalars(select(SourceItem).where(SourceItem.story_id == story_id)).all()
    for item in items:
        for claim in _revision(session, item).evidence_claims:
            if claim.get("fact_key") and claim.get("value") is not None:
                values.setdefault(claim["fact_key"], set()).add(claim["value"])
    return any(len(options) > 1 for options in values.values())


def _matches(
    session: Session, story: Story, item: SourceItem, tokens: set[str],
    place_ids: set[str], scope: str,
) -> bool:
    previous_places = {row.place_id for row in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story.id, StoryPlace.role.in_(["occurred", "affected"]),
    ))}
    if not (place_ids & previous_places or scope == story.audience_scope == "national"):
        return False
    previous_items = session.scalars(select(SourceItem).where(SourceItem.story_id == story.id)).all()
    for previous in previous_items:
        if item.event_at and previous.event_at:
            if abs((item.event_at - previous.event_at).total_seconds()) > 7200:
                continue
            threshold = 0.6
        else:
            if not item.source_published_at or not previous.source_published_at:
                continue
            if abs((item.source_published_at - previous.source_published_at).total_seconds()) > 43200:
                continue
            threshold = 0.85
        earlier_places = [session.get(Place, place_id) for place_id in previous_places]
        earlier_tokens = _tokens(_revision(session, previous).title, earlier_places)
        shared = len(tokens & earlier_tokens)
        if shared >= 2 and shared / max(1, min(len(tokens), len(earlier_tokens))) >= threshold:
            return True
    return False


def refresh_story(session: Session, story: Story, register_id: str) -> None:
    items = session.scalars(select(SourceItem).where(SourceItem.story_id == story.id)).all()
    locations: dict[tuple[str, str], Place] = {}
    risks: set[str] = set()
    for current in items:
        revision = _revision(session, current)
        resolved, ambiguous = _resolve_hints(session, revision, register_id)
        locations.update({(place.id, role): place for place, role in resolved})
        risks.update(revision.risk_flags)
        if ambiguous:
            risks.add("ambiguous_geography")
    for existing in session.scalars(select(StoryPlace).where(StoryPlace.story_id == story.id)):
        session.delete(existing)
    session.flush()
    for place_id, role in sorted(locations):
        session.add(StoryPlace(story_id=story.id, place_id=place_id, role=role))
    session.flush()
    story.conflict = _conflicts(session, story.id)
    story.risk_flags = sorted(risks)
    if items:
        story.audience_scope = _scope(session, items[0], [
            (place, role) for (place_id, role), place in locations.items()
        ])
        event_starts = {item.event_at for item in items if item.event_at}
        event_ends = {item.event_until for item in items if item.event_until}
        story.event_at = next(iter(event_starts)) if len(event_starts) == 1 else None
        story.event_until = next(iter(event_ends)) if len(event_ends) == 1 else None
        if len(event_starts) > 1 or len(event_ends) > 1:
            story.conflict = True
        story.topic = items[0].topic
        story.event_type = items[0].event_type
    story.status = "skipped" if story.conflict or "ambiguous_geography" in risks else "new"
    story.updated_at = utc_now()


def group_item(session: Session, item: SourceItem, register_id: str) -> Story | None:
    if item.story_id:
        story = session.get(Story, item.story_id)
        refresh_story(session, story, register_id)
        return story
    reference_time = item.event_at or item.source_published_at
    if reference_time is None and revision_ready(session, item):
        reference_time = item.discovered_at
    if reference_time is None:
        return None
    anchor_day = reference_time.astimezone(ATHENS).date()
    revision = _revision(session, item)
    resolved, ambiguous = _resolve_hints(session, revision, register_id)
    scope = _scope(session, item, resolved)
    places = [place for place, _ in resolved]
    relevant_ids = {place.id for place, role in resolved if role in {"occurred", "affected"}}
    tokens = _tokens(revision.title, places)
    signature = json.dumps({
        "day": anchor_day.isoformat(), "time": item.event_at.isoformat() if item.event_at else None,
        "event_type": item.event_type, "topic": item.topic, "places": sorted(relevant_ids),
        "tokens": sorted(tokens), "unresolved": item.id if ambiguous or scope == "unknown" else None,
    }, sort_keys=True)
    fingerprint = hashlib.sha256(signature.encode()).hexdigest()
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:bucket))"), {
            "bucket": f"{anchor_day}:{item.event_type}:{item.topic}:{scope}",
        })
    story = None
    if not ambiguous and scope != "unknown" and item.event_type != "unclassified":
        for candidate in session.scalars(select(Story).where(
            Story.anchor_day == anchor_day, Story.event_type == item.event_type,
            Story.topic == item.topic,
        ).with_for_update()):
            if _matches(session, candidate, item, tokens, relevant_ids, scope):
                story = candidate
                break
    if story is None:
        story = Story(
            fingerprint=fingerprint, anchor_day=anchor_day, event_at=item.event_at,
            event_until=item.event_until, event_type=item.event_type, topic=item.topic,
            audience_scope=scope, status="skipped" if ambiguous else "new",
            risk_flags=["ambiguous_geography"] if ambiguous else revision.risk_flags,
        )
        session.add(story)
        session.flush()
    item.story_id = story.id
    session.flush()
    for place, role in resolved:
        exists = session.scalar(select(StoryPlace).where(
            StoryPlace.story_id == story.id, StoryPlace.place_id == place.id,
            StoryPlace.role == role,
        ))
        if not exists:
            session.add(StoryPlace(story_id=story.id, place_id=place.id, role=role))
    session.flush()
    story.conflict = _conflicts(session, story.id)
    story.risk_flags = sorted(set(story.risk_flags or []) | set(revision.risk_flags)
                              | ({"ambiguous_geography"} if ambiguous else set()))
    story.status = ("skipped" if story.conflict or "ambiguous_geography" in story.risk_flags
                    else "new")
    story.updated_at = utc_now()
    return story


def revision_ready(session, item):
    revision = _revision(session, item)
    return bool(revision and revision.analysis_state == "completed" and revision.analysis)


def rank_story(session: Session, story: Story, now: datetime | None = None) -> tuple[int, str]:
    now = now or utc_now()
    items = session.scalars(select(SourceItem).where(SourceItem.story_id == story.id)).all()
    dated = [item.source_published_at or item.discovered_at for item in items if item.source_published_at or revision_ready(session, item)]
    age_hours = max(0, (now - max(dated)).total_seconds() / 3600) if dated else 10000
    freshness = 3 if age_hours <= 24 else 1 if age_hours <= 72 else 0
    evidence = sum(len(_revision(session, item).evidence_claims) for item in items)
    affected = session.scalar(select(StoryPlace).where(
        StoryPlace.story_id == story.id, StoryPlace.role == "affected",
    ).limit(1)) is not None
    useful = 3 if story.topic in {"services", "transport", "education", "weather"} and affected else 0
    score = freshness + useful + min(evidence, 3) + min(independent_origins(session, story.id), 2)
    reason = f"freshness={freshness}, local_usefulness={useful}, evidence={min(evidence, 3)}, independent_origins={min(independent_origins(session, story.id), 2)}"
    story.score, story.selection_reason = score, reason
    return score, reason


def select_candidates(
    session: Session, *, now: datetime | None = None,
    national_max: int = 5, regional_max: int = 2, minimum_score: int = 4,
    international_max: int = 10, general_max: int = 3,
) -> list[Story]:
    now = now or utc_now()
    national: list[Story] = []
    international: list[Story] = []
    general: list[Story] = []
    regional: dict[str, list[Story]] = {}
    for story in session.scalars(select(Story).where(Story.status == "new")):
        items = session.scalars(select(SourceItem).where(SourceItem.story_id == story.id)).all()
        dated = [item.source_published_at or item.discovered_at for item in items if item.source_published_at or revision_ready(session, item)]
        if (not dated or story.conflict or "ambiguous_geography" in story.risk_flags
                or story.event_until and story.event_until <= now
                or not story.event_at and max(dated) < now - timedelta(days=3)):
            continue
        if not any(_revision(session, item).evidence_claims for item in items):
            continue
        if rank_story(session, story, now)[0] < minimum_score:
            continue
        if max(dated) > now + timedelta(minutes=5):
            continue
        if story.audience_scope == "international":
            international.append(story)
        elif story.audience_scope == "national":
            national.append(story)
        else:
            links = session.scalars(select(StoryPlace).where(
                StoryPlace.story_id == story.id, StoryPlace.role.in_(["affected", "occurred"]),
            ))
            region_ids = {region_of(session, session.get(Place, link.place_id)).id for link in links}
            if not region_ids:
                general.append(story)
            for region_id in region_ids:
                regional.setdefault(region_id, []).append(story)
    chosen = sorted(national, key=lambda story: (-story.score, story.id))[:max(0, national_max)]
    chosen += sorted(international, key=lambda story: (-story.score, story.id))[:max(0, international_max)]
    chosen += sorted(general, key=lambda story: (-story.score, story.id))[:max(0, general_max)]
    selected_ids = {story.id for story in chosen}
    for region_id in sorted(regional):
        for story in sorted(regional[region_id], key=lambda row: (-row.score, row.id))[:
            max(0, regional_max)
        ]:
            if story.id not in selected_ids:
                chosen.append(story)
                selected_ids.add(story.id)
    return chosen
