"""Read-only published-article API with role-specific geographic editions."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from greek_news.article_schema import ArticleDraft
from greek_news.config import Settings, get_settings
from greek_news.db import get_session, utc_date
from greek_news.geography import ancestors, region_of
from greek_news.models import (
    Article, ArticleVersion, GeoRegister, Place, Source, SourceItem, Story, StoryPlace,
)
from greek_news.validation import HIGH_RISK


router = APIRouter(tags=["published articles"])
ATHENS = ZoneInfo("Europe/Athens")


def article_visible(session: Session, article: Article, settings: Settings) -> bool:
    if article.status != "published":
        return False
    if settings.fixture_mode:
        return True
    fixture_source = session.scalar(select(Source.id).join(
        SourceItem, SourceItem.source_id == Source.id,
    ).where(SourceItem.story_id == article.story_id, Source.source_type == "fixture").limit(1))
    fixture_place = session.scalar(select(GeoRegister.id).join(
        Place, Place.register_id == GeoRegister.id,
    ).join(StoryPlace, StoryPlace.place_id == Place.id).where(
        StoryPlace.story_id == article.story_id, GeoRegister.is_fixture.is_(True),
    ).limit(1))
    return fixture_source is None and fixture_place is None


def _relevant_places(session: Session, story_id: str) -> list[dict]:
    result = []
    for link in session.scalars(select(StoryPlace).where(StoryPlace.story_id == story_id)):
        place = session.get(Place, link.place_id)
        result.append({
            "id": place.id, "name_el": place.name_el, "kind": place.kind,
            "parent_id": place.parent_id, "region_id": region_of(session, place).id,
            "role": link.role,
        })
    return sorted(result, key=lambda row: (row["role"], row["id"]))


def serialize_article(session: Session, article: Article, settings: Settings) -> dict:
    version = session.scalar(select(ArticleVersion).where(
        ArticleVersion.article_id == article.id,
        ArticleVersion.number == article.published_version,
    ))
    if version is None:
        raise ValueError("Published article has no published version")
    draft = ArticleDraft.model_validate(version.content)
    story = session.get(Story, article.story_id)
    places = _relevant_places(session, story.id)
    affected = [row["id"] for row in places if row["role"] == "affected"]
    register_ids = {session.get(Place, row["id"]).register_id for row in places}
    registers = [session.get(GeoRegister, register_id) for register_id in sorted(register_ids)]
    sources = [{
        "source_id": ref.source_id, "revision_id": ref.revision_id, "url": ref.url,
        "organization": session.get(Source, ref.source_id).organization,
    } for ref in draft.source_references]
    return {
        "id": article.id, "slug": article.slug,
        "canonical_url": f"{settings.public_base_url.rstrip('/')}/articles/{article.id}/{article.slug}",
        "title": draft.selected_title, "summary": draft.summary,
        "body_sections": [{"heading": section.heading, "text": section.text}
                          for section in draft.body_sections],
        "seo_description": draft.seo_description,
        "topic": story.topic, "category": draft.category, "topic_tags": draft.topic_tags,
        "audience_scope": story.audience_scope, "geography": places,
        "geo_registers": [{"id": row.id, "version": row.version, "fixture": row.is_fixture}
                          for row in registers],
        "ad_context": {"eligible": bool(affected) and not set(story.risk_flags) & HIGH_RISK,
                       "affected_place_ids": affected},
        "sources": sources, "correction_note": version.correction_note,
        "author": article.author_name, "editor": article.editor_name,
        "automation_disclosure": article.automation_disclosure,
        "datePublished": article.published_at.isoformat(),
        "dateModified": article.modified_at.isoformat() if article.modified_at else None,
        "athensPublished": article.published_at.astimezone(ATHENS).isoformat(),
        "athensModified": article.modified_at.astimezone(ATHENS).isoformat()
        if article.modified_at else None,
    }


def _matches_place(session: Session, story_id: str, place_id: str, expected_kind: str) -> bool:
    target = session.get(Place, place_id)
    if not target or target.kind != expected_kind:
        return False
    for link in session.scalars(select(StoryPlace).where(
        StoryPlace.story_id == story_id, StoryPlace.role.in_(["occurred", "affected"]),
    )):
        place = session.get(Place, link.place_id)
        if place_id in {ancestor.id for ancestor in ancestors(session, place)}:
            return True
    return False


@router.get("/articles")
def list_articles(
    region: str | None = None, regional_unit: str | None = None,
    municipality: str | None = None, topic: str | None = None,
    category: str | None = None, published_after: datetime | None = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    session: Session = Depends(get_session), settings: Settings = Depends(get_settings),
) -> dict:
    if published_after:
        try:
            published_after = utc_date(published_after)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    query = select(Article).where(Article.status == "published")
    if published_after:
        query = query.where(Article.published_at >= published_after)
    articles = session.scalars(query.order_by(Article.published_at.desc(), Article.id)).all()
    matches = []
    for article in articles:
        if not article_visible(session, article, settings):
            continue
        story = session.get(Story, article.story_id)
        if topic and story.topic != topic:
            continue
        if any(not _matches_place(session, story.id, place_id, kind) for place_id, kind in (
            (place_id, kind) for place_id, kind in (
                (region, "region"), (regional_unit, "regional_unit"),
                (municipality, "municipality"),
            ) if place_id
        )):
            continue
        if category and ArticleDraft.model_validate(session.scalar(select(ArticleVersion).where(
            ArticleVersion.article_id == article.id,
            ArticleVersion.number == article.published_version,
        )).content).category != category:
            continue
        matches.append(article)
    return {"total": len(matches), "limit": limit, "offset": offset,
            "items": [serialize_article(session, article, settings)
                      for article in matches[offset:offset + limit]]}


@router.get("/articles/{article_id}")
def get_article(
    article_id: str, session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    article = session.get(Article, article_id)
    if not article or not article_visible(session, article, settings):
        raise HTTPException(status_code=404, detail="Published article not found")
    return serialize_article(session, article, settings)


@router.get("/articles/{article_id}/{slug}")
def get_canonical_article(
    article_id: str, slug: str, session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    article = session.get(Article, article_id)
    if not article or not article_visible(session, article, settings) or article.slug != slug:
        raise HTTPException(status_code=404, detail="Published article not found")
    return serialize_article(session, article, settings)