"""Conservative deterministic checks; passing is not proof of semantic truth."""

import re
from datetime import datetime
from difflib import SequenceMatcher

from pydantic import BaseModel

from greek_news.article_schema import ArticleDraft
from greek_news.db import utc_now
from greek_news.evidence import EvidenceClaim, EvidencePacket
from greek_news.geography import normalize_name


NUMBERS = re.compile(r"(?<!\w)\d+(?:[.,:/]\d+)*(?!\w)")
QUOTES = re.compile(r"«([^»]+)»")
HTML = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.IGNORECASE)
MONTHS = {
    "ιανουαριου", "φεβρουαριου", "μαρτιου", "απριλιου", "μαιου", "ιουνιου",
    "ιουλιου", "αυγουστου", "σεπτεμβριου", "οκτωβριου", "νοεμβριου", "δεκεμβριου",
}
SENSATIONAL = {"χαοσ", "σοκ", "πανικοσ", "αποκαλυψη"}
HIGH_RISK = {"death", "minors", "crime", "allegation", "medical", "political_accusation",
             "active_emergency", "conflicting_reports"}


class ValidationReport(BaseModel):
    valid: bool
    errors: list[str]
    warnings: list[str]
    semantic_checked: bool = False
    semantic_passed: bool = False
    semantic_verification: dict | None = None


def _check_text(
    label: str, text: str, linked_ids: list[str], claims: dict[str, EvidenceClaim],
    errors: list[str], *, headline: bool = False,
) -> None:
    linked = [claims[id_value] for id_value in linked_ids if id_value in claims]
    if not linked or len(linked) != len(linked_ids):
        errors.append(f"{label}: missing or invented evidence reference")
        return
    evidence = " ".join(f"{claim.text} {claim.excerpt}" for claim in linked)
    missing_numbers = set(NUMBERS.findall(text)) - set(NUMBERS.findall(evidence))
    if missing_numbers:
        errors.append(f"{label}: unsupported numbers {sorted(missing_numbers)}")
    text_months = set(normalize_name(text).split()) & MONTHS
    if text_months - set(normalize_name(evidence).split()):
        errors.append(f"{label}: unsupported event month")
    if HTML.search(text):
        errors.append(f"{label}: HTML is not permitted")
    if headline and set(normalize_name(text).split()) & SENSATIONAL:
        errors.append(f"{label}: unsupported sensational wording")
    for quotation in QUOTES.findall(text):
        if not any(quotation in claim.excerpt and claim.speaker for claim in linked):
            errors.append(f"{label}: unattributed or invented quotation")


def validate_draft(
    draft: ArticleDraft, packet: EvidencePacket, now: datetime | None = None,
) -> ValidationReport:
    now = now or utc_now()
    errors: list[str] = []
    warnings: list[str] = []
    claims = {claim.id: claim for claim in packet.claims}
    expected_sources = {(source.source_id, source.revision_id, source.url)
                        for source in packet.sources}
    actual_sources = [(ref.source_id, ref.revision_id, ref.url)
                      for ref in draft.source_references]
    if set(actual_sources) != expected_sources or len(actual_sources) != len(expected_sources):
        errors.append("Source references do not match the frozen evidence")
    affected = [place.id for place in packet.places if place.role == "affected"]
    occurred = [place.id for place in packet.places if place.role == "occurred"]
    expected_geography = affected or occurred
    if (set(draft.geographic_ids) != set(expected_geography)
            or len(draft.geographic_ids) != len(expected_geography)):
        errors.append("Geography IDs differ from affected/occurred places")
    if draft.audience_scope != packet.audience_scope or draft.category != packet.topic:
        errors.append("Audience scope or topic does not match the story")
    if not set(packet.risk_flags).issubset(draft.risk_flags):
        errors.append("Risk flags from evidence were omitted")
    if len(set(draft.title_candidates)) != 3:
        errors.append("Three distinct headlines are required")
    allowed_title_names = {normalize_name(place.name_el) for place in packet.places
                           if place.role in {"affected", "occurred"}}
    for index, title in enumerate(draft.title_candidates):
        label = f"title option {index + 1}"
        _check_text(label, title, draft.title_evidence_ids, claims, errors, headline=True)
        for place in packet.places:
            name = normalize_name(place.name_el)
            if (place.role == "background" and name not in allowed_title_names
                    and re.search(rf"\b{re.escape(name)}\b", normalize_name(title))):
                errors.append(f"{label}: background place presented as a headline location")
    _check_text("summary", draft.summary, draft.summary_evidence_ids, claims, errors)
    _check_text("SEO", draft.seo_description, draft.seo_evidence_ids, claims, errors)
    for index, section in enumerate(draft.body_sections):
        _check_text(f"body section {index + 1}", section.text, section.evidence_ids, claims, errors)
        if section.heading:
            _check_text(f"body heading {index + 1}", section.heading, section.evidence_ids,
                        claims, errors)
    for index, link in enumerate(draft.claim_links):
        _check_text(f"claim link {index + 1}", link.statement, link.evidence_ids, claims, errors)
    for section in draft.body_sections:
        for claim_id in section.evidence_ids:
            claim = claims.get(claim_id)
            if claim and not next(
                source.text_reuse_permitted for source in packet.sources
                if source.source_id == claim.source_id
            ) and len(claim.excerpt.split()) >= 12:
                if SequenceMatcher(None, section.text, claim.excerpt).ratio() > 0.85:
                    errors.append("Body text is suspiciously close to source-owned prose")
    if packet.event_until and packet.event_until < now and packet.event_type in {
        "water_notice", "road_closure", "power_notice", "transport_notice",
    }:
        errors.append("Effective service interval has ended; status cannot be assumed active")
    if packet.conflict:
        warnings.append("Conflicting source claims require further evidence")
    if set(packet.risk_flags) & HIGH_RISK:
        warnings.append("Sensitive claims require risk-proportionate corroboration")
    if any(source.published_at is None for source in packet.sources):
        warnings.append("At least one source lacks a verified publication time")
    warnings.append("Deterministic checks cannot establish semantic support or truth")
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)
