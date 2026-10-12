"""Offline, attributed GeoNames gazetteer; cities are never mislabeled as municipalities."""

import gettext
import geonamescache
import pycountry
from sqlalchemy import select

from greek_news.geography import RegisterInput, PlaceInput, import_register, normalize_name
from greek_news.models import Place, PlaceAlias

REGISTER = "world-geonames-3.0.2"
REGIONS = {
    "ESYE11": ("GR-A", "Ανατολική Μακεδονία και Θράκη"),
    "ESYE12": ("GR-B", "Κεντρική Μακεδονία"),
    "ESYE13": ("GR-C", "Δυτική Μακεδονία"),
    "ESYE14": ("GR-E", "Θεσσαλία"),
    "ESYE21": ("GR-D", "Ήπειρος"),
    "ESYE22": ("GR-F", "Ιόνια Νησιά"),
    "ESYE23": ("GR-G", "Δυτική Ελλάδα"),
    "ESYE24": ("GR-H", "Στερεά Ελλάδα"),
    "ESYE25": ("GR-J", "Πελοπόννησος"),
    "ESYE31": ("GR-I", "Αττική"),
    "ESYE41": ("GR-K", "Βόρειο Αιγαίο"),
    "ESYE42": ("GR-L", "Νότιο Αιγαίο"),
    "ESYE43": ("GR-M", "Κρήτη"),
}
EXTRA_ALIASES = {
    264371: ["Αθήνα", "Αθήνας", "Athens"], 734077: ["Θεσσαλονίκη", "Θεσσαλονίκης"],
    255683: ["Πάτρα", "Πάτρας", "Patras"], 261745: ["Ηράκλειο", "Ηρακλείου", "Heraklion"],
    260114: ["Λάρισα", "Λάρισας"], 251833: ["Βόλος", "Βόλου"],
}


def bootstrap_geography(session) -> int:
    translator = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["el"], fallback=True)
    countries = geonamescache.GeonamesCache().get_countries()
    places = [PlaceInput(official_id=row["iso"], kind="country",
                        name_el=translator.gettext(row["name"]),
                        aliases=[row["name"], row["iso"], row["iso3"]])
              for row in countries.values()]
    for code, (official, name) in REGIONS.items():
        subdivision = pycountry.subdivisions.get(code=official)
        places.append(PlaceInput(official_id=official, kind="region", name_el=name,
                                 parent_official_id="GR", aliases=[subdivision.name] if subdivision else []))
    cache = geonamescache.GeonamesCache(min_city_population=500)
    for row in cache.get_cities().values():
        if row["countrycode"] != "GR" and row["population"] < 15000:
            continue
        if row["countrycode"] not in countries:
            continue
        alternatives = row.get("alternatenames", [])
        greek = [name for name in alternatives if any("\u0370" <= char <= "\u03ff" for char in name)]
        name = next((x for x in EXTRA_ALIASES.get(row["geonameid"], []) if "\u0370" <= x[0] <= "\u03ff"), None) or (greek[0] if greek else row["name"])
        aliases = list(dict.fromkeys([row["name"], *greek, *EXTRA_ALIASES.get(row["geonameid"], []),
                                    *alternatives[:12]]))[:24]
        parent = REGIONS.get(row["admin1code"], (row["countrycode"],))[0] if row["countrycode"] == "GR" else row["countrycode"]
        places.append(PlaceInput(official_id=f"geonames-{row['geonameid']}", kind="city",
                                 name_el=name, parent_official_id=parent, aliases=aliases))
    register = RegisterInput(id=REGISTER, version="geonamescache-3.0.2",
                             provenance="GeoNames via geonamescache 3.0.2, CC BY 4.0 (https://www.geonames.org/about.html); ISO names via pycountry. Greece cities population >=500; world cities >=15000. Greek regions mapped from GeoNames admin1 codes. Gazetteer, not an official municipality register.",
                             is_fixture=False, places=places)
    count = import_register(session, register)
    # Backfill any legacy registers once without changing their identity.
    indexed = set(session.scalars(select(PlaceAlias.place_id).distinct()))
    for row in session.scalars(select(Place)):
        if row.id in indexed:
            continue
        for name in {normalize_name(row.name_el), *(normalize_name(x) for x in row.aliases)}:
            if name and len(name) <= 200:
                session.add(PlaceAlias(place_id=row.id, normalized_name=name))
    session.flush()
    return count
