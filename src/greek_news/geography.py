"""Versioned place imports and conservative, contextual name resolution."""

import re
import unicodedata

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.models import GeoRegister, Place, PlaceRelation


KINDS = {"region", "regional_unit", "municipality", "settlement", "town", "island"}
PARENTS = {
    "region": {None},
    "regional_unit": {"region"},
    "municipality": {"regional_unit"},
    "town": {"municipality"},
    "settlement": {"municipality"},
    "island": {"region", "regional_unit"},
}


class PlaceInput(BaseModel):
    official_id: str = Field(min_length=1, max_length=100)
    kind: str
    name_el: str = Field(min_length=1)
    parent_official_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    island_official_ids: list[str] = Field(default_factory=list)


class RegisterInput(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]+$", max_length=100)
    version: str
    provenance: str
    is_fixture: bool
    places: list[PlaceInput] = Field(min_length=1)

    @model_validator(mode="after")
    def check_hierarchy(self) -> "RegisterInput":
        by_id = {place.official_id: place for place in self.places}
        if len(by_id) != len(self.places):
            raise ValueError("Duplicate official place IDs")
        for place in self.places:
            if place.kind not in KINDS:
                raise ValueError(f"Unknown place type: {place.kind}")
            parent = by_id.get(place.parent_official_id) if place.parent_official_id else None
            if place.parent_official_id and parent is None:
                raise ValueError(f"Missing parent for {place.official_id}")
            if (parent.kind if parent else None) not in PARENTS[place.kind]:
                raise ValueError(f"Invalid parent for {place.official_id}")
            if any(by_id.get(island_id, None) is None or by_id[island_id].kind != "island"
                   for island_id in place.island_official_ids):
                raise ValueError(f"Invalid island relation for {place.official_id}")
        return self


def normalize_name(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ς", "σ"))
    bare = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", bare, flags=re.UNICODE)).strip()


def place_id(register_id: str, official_id: str) -> str:
    return f"{register_id}:{official_id}"


def import_register(session: Session, data: RegisterInput) -> int:
    existing = session.get(GeoRegister, data.id)
    if existing:
        found = session.scalars(select(Place).where(Place.register_id == data.id)).all()
        expected = {
            place_id(data.id, place.official_id): place for place in data.places
        }
        if (existing.version != data.version or existing.provenance != data.provenance
                or existing.is_fixture != data.is_fixture or len(found) != len(expected)
                or any(
                    row.id not in expected
                    or row.name_el != expected[row.id].name_el
                    or row.kind != expected[row.id].kind
                    or row.parent_id != (
                        place_id(data.id, expected[row.id].parent_official_id)
                        if expected[row.id].parent_official_id else None
                    )
                    or row.aliases != expected[row.id].aliases
                    for row in found
                )):
            raise ValueError("A geographic register version cannot be changed in place")
        return 0
    session.add(GeoRegister(
        id=data.id, version=data.version, provenance=data.provenance,
        is_fixture=data.is_fixture,
    ))
    session.flush()
    for item in data.places:
        session.add(Place(
            id=place_id(data.id, item.official_id), official_id=item.official_id,
            register_id=data.id, kind=item.kind, name_el=item.name_el,
            normalized_name=normalize_name(item.name_el),
            parent_id=place_id(data.id, item.parent_official_id)
            if item.parent_official_id else None, aliases=item.aliases,
        ))
    session.flush()
    for item in data.places:
        for island_id in item.island_official_ids:
            session.add(PlaceRelation(
                place_id=place_id(data.id, item.official_id),
                related_id=place_id(data.id, island_id), relation="on_island",
            ))
    session.flush()
    return len(data.places)


class AmbiguousPlace(ValueError):
    pass


def ancestors(session: Session, place: Place) -> list[Place]:
    chain = [place]
    while chain[-1].parent_id:
        parent = session.get(Place, chain[-1].parent_id)
        if parent is None:
            raise ValueError("Broken geographic hierarchy")
        chain.append(parent)
    return chain


def resolve_place(
    session: Session, name: str, *, kind: str | None = None,
    context: str | None = None, register_id: str | None = None,
) -> Place:
    normalized = normalize_name(name)
    rows = session.scalars(select(Place).where(Place.register_id == register_id) if register_id
                           else select(Place)).all()
    candidates = [place for place in rows if (kind is None or place.kind == kind)
                  and (place.normalized_name == normalized
                       or normalized in {normalize_name(alias) for alias in place.aliases})]
    if context:
        region_context = normalize_name(context)
        candidates = [place for place in candidates if any(
            ancestor.kind == "region" and ancestor.normalized_name == region_context
            for ancestor in ancestors(session, place)
        )]
    if len(candidates) != 1:
        raise AmbiguousPlace(f"Expected one place for {name!r}, got {len(candidates)}")
    return candidates[0]


def region_of(session: Session, place: Place) -> Place:
    return next(ancestor for ancestor in ancestors(session, place) if ancestor.kind == "region")