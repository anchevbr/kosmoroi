"""Model output contract; editorial state is intentionally not model-controlled."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BodySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str | None = None
    text: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(min_length=1)


class ClaimLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    revision_id: str
    url: str


class ArticleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_candidates: tuple[str, str, str]
    selected_title: str = Field(min_length=1, max_length=220)
    title_rationale: str
    title_evidence_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=700)
    summary_evidence_ids: list[str] = Field(min_length=1)
    body_sections: list[BodySection] = Field(min_length=1)
    claim_links: list[ClaimLink] = Field(min_length=1)
    category: str
    topic_tags: list[str]
    geographic_ids: list[str]
    audience_scope: str
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    seo_description: str = Field(min_length=1, max_length=300)
    seo_evidence_ids: list[str] = Field(min_length=1)
    source_references: list[SourceReference] = Field(min_length=1)
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("selected_title")
    @classmethod
    def title_is_option(cls, title: str, info) -> str:
        if "title_candidates" in info.data and title not in info.data["title_candidates"]:
            raise ValueError("Selected title must be one of the three candidates")
        return title