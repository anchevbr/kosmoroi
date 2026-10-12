import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from greek_news.geography import AmbiguousPlace, RegisterInput, import_register, resolve_place
from greek_news.models import Place


def load_fixture():
    return RegisterInput.model_validate_json(
        (Path(__file__).parents[1] / "fixtures" / "places.json").read_text(encoding="utf-8")
    )


def test_import_is_versioned_and_explicitly_incomplete(session):
    fixture = load_fixture()
    assert fixture.is_fixture and "not an authoritative" in fixture.provenance
    assert import_register(session, fixture) == len(fixture.places)
    assert import_register(session, fixture) == 0
    assert session.scalar(select(func.count()).select_from(Place).where(Place.kind == "region")) == 13
    changed = fixture.model_copy(deep=True)
    changed.places[0].name_el = "Different"
    with pytest.raises(ValueError, match="cannot be changed"):
        import_register(session, changed)


def test_heraklion_needs_context_and_background_does_not_resolve_it(session):
    import_register(session, load_fixture())
    with pytest.raises(AmbiguousPlace):
        resolve_place(session, "Ηράκλειο", kind="municipality")
    crete = resolve_place(session, "Ηράκλειο", kind="municipality", context="Κρήτη")
    attica = resolve_place(session, "Ηράκλειο", kind="municipality", context="Αττική")
    assert crete.id != attica.id
    assert resolve_place(session, "Patra", kind="municipality").name_el == "Πάτρα"


def test_rejects_bad_hierarchy():
    raw = json.loads((Path(__file__).parents[1] / "fixtures" / "places.json").read_text())
    raw["places"][13]["parent_official_id"] = "m-patra"
    with pytest.raises(ValueError, match="Invalid parent"):
        RegisterInput.model_validate(raw)