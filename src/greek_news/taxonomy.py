"""Stable frontend identifiers, separate editions and categories, validated subcategories."""

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

CATEGORIES = {
    "politics": ("Πολιτική", ["government", "elections", "diplomacy", "eu"]),
    "business": ("Οικονομία", ["markets", "companies", "employment", "consumer", "energy"]),
    "sports": ("Αθλητισμός", ["football", "basketball", "tennis", "motorsport", "other-sports"]),
    "technology": ("Τεχνολογία", ["ai", "internet", "gadgets", "cybersecurity"]),
    "science": ("Επιστήμη", ["research", "space", "nature"]),
    "health": ("Υγεία", ["public-health", "health-services", "research"]),
    "environment": ("Περιβάλλον", ["climate", "wildlife", "conservation"]),
    "weather": ("Καιρός", ["forecasts", "warnings"]),
    "education": ("Παιδεία", ["schools", "universities", "exams"]),
    "culture": ("Πολιτισμός", ["arts", "books", "cinema", "heritage"]),
    "entertainment": ("Ψυχαγωγία", ["television", "music", "celebrities"]),
    "travel": ("Ταξίδια", ["destinations", "tourism", "aviation"]),
    "lifestyle": ("Ζωή", ["food", "family", "community"]),
    "services": ("Χρηστικές ειδήσεις", ["water", "power", "public-services"]),
    "transport": ("Μεταφορές", ["roads", "public-transport", "shipping"]),
    "events": ("Εκδηλώσεις", ["local-events", "festivals", "sport-events"]),
    "crime": ("Αστυνομικό", ["courts", "investigations", "public-safety"]),
    "general": ("Επικαιρότητα", ["society", "breaking", "human-interest"]),
}


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=150)
    kind: Literal["person", "organization", "team", "product", "event"]
    evidence_excerpt: str = Field(min_length=1, max_length=1000)

    @property
    def key(self) -> str:
        from greek_news.geography import normalize_name
        return f"{self.kind}:{normalize_name(self.name).replace(' ', '-')[:160]}"


def topic_id(value: str) -> str:
    value = value.lower().strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 80:
        raise ValueError("Topic tags must be stable ASCII identifiers")
    return value


def taxonomy_contract() -> dict:
    return {"version": "1", "editions": [{"id": "greece", "name_el": "Ελλάδα"},
                                          {"id": "world", "name_el": "Κόσμος"}],
            "categories": [{"id": key, "name_el": value[0], "subcategories": value[1]}
                           for key, value in CATEGORIES.items()],
            "audience_scopes": ["local", "regional", "national", "international", "mixed", "unknown"],
            "geography_roles": ["occurred", "affected", "background"],
            "formats": ["news", "explainer", "service", "event", "update"]}
