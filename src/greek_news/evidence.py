"""Immutable, traceable inputs for drafting and deterministic claim validation."""

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.db import utc_now
from greek_news.geography import country_of, region_of
from greek_news.ingestion import source_permits_storage
from greek_news.models import Place, Source, SourceItem, SourceRevision, Story, StoryPlace
from greek_news.stories import independent_origins


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source_id: str
    revision_id: str
    text: str
    excerpt: str
    fact_key: str | None = None
    value: str | None = None
    speaker: str | None = None


class EvidenceSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    revision_id: str
    url: str
    organization: str
    origin: str
    published_at: datetime | None
    updated_at: datetime | None
    text_reuse_permitted: bool
    country_code: str = "GR"
    language: str = "el"
    trust_category: str = "unreviewed"
    coverage_ids: tuple[str, ...] = ()


class EvidencePlace(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name_el: str
    kind: str
    region_id: str
    role: str
    country_code: str | None = None
    ancestor_ids: tuple[str, ...] = ()


class EvidencePacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    story_id: str
    event_type: str
    topic: str
    audience_scope: str
    event_at: datetime | None
    event_until: datetime | None
    sources: tuple[EvidenceSource, ...] = Field(min_length=1)
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1)
    places: tuple[EvidencePlace, ...]
    risk_flags: tuple[str, ...]
    conflict: bool
    independent_origins: int
    fixture_only: bool
    subcategory: str | None = None
    topic_tags: tuple[str, ...] = ()
    entities: tuple[dict, ...] = ()
    language: str = "el"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


def freeze_evidence(session: Session, story: Story, *, now: datetime | None = None) -> EvidencePacket:
    now = now or utc_now()
    sources: list[EvidenceSource] = []
    claims: list[EvidenceClaim] = []
    items = session.scalars(select(SourceItem).where(
        SourceItem.story_id == story.id,
    ).order_by(SourceItem.id)).all()
    for item in items:
        source = session.get(Source, item.source_id)
        if not source_permits_storage(source, now) or not source.can_process_ai:
            raise PermissionError("Every evidence source must permit storage and AI processing")
        revision = session.scalar(select(SourceRevision).where(
            SourceRevision.source_item_id == item.id,
            SourceRevision.number == item.current_revision,
        ))
        if revision is None:
            raise ValueError("Story references a missing source revision")
        sources.append(EvidenceSource(
            source_id=source.id, revision_id=revision.id, url=item.canonical_url,
            organization=source.organization or source.name,
            origin=source.syndication_group or source.organization or source.id,
            published_at=item.source_published_at, updated_at=item.source_updated_at,
            text_reuse_permitted=source.can_reuse_text,
            country_code=source.country_code, language=source.language,
            trust_category=source.trust_category,
            coverage_ids=tuple(source.coverage_ids),
        ))
        for index, raw in enumerate(revision.evidence_claims):
            if (revision.permitted_text is not None or revision.analysis_state != "completed") and raw["excerpt"] not in (revision.permitted_text or revision.title):
                raise ValueError("Stored evidence excerpt no longer matches its revision")
            claims.append(EvidenceClaim(
                id=f"{revision.id}:{index}", source_id=source.id, revision_id=revision.id,
                **raw,
            ))
    if not claims:
        raise ValueError("No verified excerpts are available for drafting")
    locations: list[EvidencePlace] = []
    for link in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story.id,
    ).order_by(StoryPlace.place_id, StoryPlace.role)):
        place = session.get(Place, link.place_id)
        country = country_of(session, place)
        from greek_news.geography import ancestors
        locations.append(EvidencePlace(
            id=place.id, name_el=place.name_el, kind=place.kind,
            region_id=region_of(session, place).id, role=link.role,
            country_code=country.official_id if country else "GR",
            ancestor_ids=tuple(row.id for row in ancestors(session, place)),
        ))
    analyses = [session.scalar(select(SourceRevision).where(
        SourceRevision.source_item_id == item.id,
        SourceRevision.number == item.current_revision,
    )).analysis or {} for item in items]
    topics = sorted({tag for analysis in analyses for tag in analysis.get("topic_tags", [])})
    entities = {entity.get("key", entity.get("name", "")): entity
                for analysis in analyses for entity in analysis.get("entities", [])}
    return EvidencePacket(
        story_id=story.id, event_type=story.event_type, topic=story.topic,
        audience_scope=story.audience_scope, event_at=story.event_at,
        event_until=story.event_until, sources=tuple(sources), claims=tuple(claims),
        places=tuple(locations), risk_flags=tuple(story.risk_flags),
        conflict=story.conflict, independent_origins=independent_origins(session, story.id),
        fixture_only=all(session.get(Source, item.source_id).source_type == "fixture" for item in items),
        subcategory=next((analysis.get("subcategory") for analysis in analyses if analysis.get("subcategory")), None),
        topic_tags=tuple(topics), entities=tuple(entities[key] for key in sorted(entities)),
    )
