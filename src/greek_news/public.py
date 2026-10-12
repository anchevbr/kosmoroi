"""Published snapshot API, SQL-filtered editions, crawlable article HTML and SEO exports."""

import html
import json
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import exists, func, or_, select
from greek_news.article_schema import ArticleDraft
from greek_news.config import get_settings
from greek_news.db import get_session, utc_date, utc_now
from greek_news.evidence import EvidencePacket
from greek_news.models import Article, ArticleFacet, ArticleVersion, GeoRegister, Place
from greek_news.taxonomy import taxonomy_contract
from greek_news.validation import HIGH_RISK
from greek_news.stories import ATHENS

router = APIRouter(tags=["published articles"])


def visible_query(settings):
    statement = select(Article).where(Article.status == "published", Article.published_version.is_not(None))
    if not settings.fixture_mode:
        statement = statement.where(~exists(select(ArticleFacet.id).where(
            ArticleFacet.article_id == Article.id, ArticleFacet.kind == "fixture", ArticleFacet.value == "true")))
        # Old publications without snapshot facets are hidden until bootstrap backfills them.
        statement = statement.where(exists(select(ArticleFacet.id).where(ArticleFacet.article_id == Article.id)))
    return statement


def article_visible(session, article, settings):
    return bool(session.scalar(visible_query(settings).where(Article.id == article.id)))


def serialize_article(session, article, settings):
    version = session.scalar(select(ArticleVersion).where(ArticleVersion.article_id == article.id,
                            ArticleVersion.number == article.published_version))
    if not version or not version.evidence_packet:
        raise ValueError("Published article has no immutable evidence snapshot")
    draft = ArticleDraft.model_validate(version.content)
    packet = EvidencePacket.model_validate(version.evidence_packet)
    places = [{"id": place.id, "name_el": place.name_el, "kind": place.kind,
               "parent_id": (place.ancestor_ids[1] if len(place.ancestor_ids) > 1 else None),
               "region_id": place.region_id, "country_code": place.country_code,
               "role": place.role, "ancestor_ids": list(place.ancestor_ids)} for place in packet.places]
    relevant = [place for place in places if place["role"] == "affected"] or [place for place in places if place["role"] == "occurred"]
    affected = [place["id"] for place in places if place["role"] == "affected"]
    register_ids = {place.id.rsplit(":", 1)[0] for place in packet.places}
    registers = [session.get(GeoRegister, key) for key in sorted(register_ids)]
    canonical = f"{settings.public_base_url.rstrip('/')}/articles/{article.id}/{article.slug}"
    sources = [{"source_id": source.source_id, "revision_id": source.revision_id, "url": source.url,
                "organization": source.organization, "published_at": source.published_at.isoformat() if source.published_at else None,
                "publication_time_verified": source.published_at is not None} for source in packet.sources]
    entity_names = [{"id": entity.get("key"), "name": entity["name"], "kind": entity["kind"]} for entity in packet.entities]
    ad_eligible = bool(relevant) and not set(packet.risk_flags) & HIGH_RISK
    value = {
        "id": article.id, "slug": article.slug, "canonical_url": canonical,
        "title": draft.selected_title, "summary": draft.summary,
        "body_sections": [{"heading": section.heading, "text": section.text} for section in draft.body_sections],
        "seo_description": draft.seo_description, "language": draft.language,
        "edition": draft.edition, "category": draft.category, "subcategory": draft.subcategory,
        "topic": packet.topic, "topic_tags": draft.topic_tags, "entities": entity_names,
        "article_format": draft.article_format, "country_codes": draft.country_codes,
        "audience_scope": packet.audience_scope, "geography": places,
        "publisher_coverage_ids": draft.publisher_coverage_ids,
        "geo_registers": [{"id": row.id, "version": row.version, "fixture": row.is_fixture,
                           "provenance": row.provenance} for row in registers if row],
        "ad_context": {"eligible": ad_eligible, "affected_place_ids": affected,
                       "local_place_ids": [row["id"] for row in relevant] if ad_eligible else [],
                       "country_codes": draft.country_codes if ad_eligible else [],
                       "excluded_reason": "sensitive_content" if set(packet.risk_flags) & HIGH_RISK else None},
        "sources": sources, "correction_note": version.correction_note,
        "author": article.author_name, "editor": article.editor_name,
        "automation_disclosure": article.automation_disclosure,
        "datePublished": article.published_at.isoformat(),
        "dateModified": (article.modified_at or article.published_at).isoformat(),
        "athensPublished": article.published_at.astimezone(ATHENS).isoformat(),
        "athensModified": (article.modified_at or article.published_at).astimezone(ATHENS).isoformat(),
        "verification": {"structural": bool(version.validation.get("valid")),
                         "semantic": bool(version.validation.get("semantic_passed")),
                         "independent_origins": packet.independent_origins},
        "media": [],  # No third-party photo is implicitly licensed by a public HTML page.
    }
    publisher = {"@type": "Organization", "name": settings.publisher_name,
                 "url": settings.public_base_url.rstrip('/')}
    if settings.publisher_logo_url:
        publisher["logo"] = {"@type": "ImageObject", "url": settings.publisher_logo_url}
    value["structured_data"] = {"@context": "https://schema.org", "@type": "NewsArticle",
        "headline": value["title"], "description": value["seo_description"],
        "datePublished": value["datePublished"], "dateModified": value["dateModified"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "url": canonical, "inLanguage": draft.language, "articleSection": draft.category,
        "keywords": ", ".join(draft.topic_tags), "author": {"@type": "Organization", "name": article.author_name},
        "publisher": publisher, "citation": [source["url"] for source in sources],
        "articleBody": "\n\n".join(section.text for section in draft.body_sections)}
    return value


def _facet(statement, kind, value):
    return statement.where(exists(select(ArticleFacet.id).where(ArticleFacet.article_id == Article.id,
                                  ArticleFacet.kind == kind, ArticleFacet.value == value)))


@router.get("/taxonomy")
def get_taxonomy():
    return taxonomy_contract()


@router.get("/places")
def list_places(country: str | None = None, parent: str | None = None, kind: str | None = None,
                q: str | None = Query(None, min_length=2, max_length=80),
                limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                session=Depends(get_session), settings=Depends(get_settings)):
    statement = select(Place).join(GeoRegister)
    if not settings.fixture_mode:
        statement = statement.where(GeoRegister.is_fixture.is_(False))
    if kind:
        statement = statement.where(Place.kind == kind)
    if parent:
        statement = statement.where(Place.parent_id == parent)
    if country:
        # Countries are root ancestors; world imports use country-prefixed parent structure.
        from greek_news.geography import descendants
        ids = descendants(session, f"{settings.geo_register_id}:{country}")
        statement = statement.where(Place.id.in_(ids))
    if q:
        from greek_news.geography import normalize_name
        statement = statement.where(Place.normalized_name.contains(normalize_name(q), autoescape=True))
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(statement.order_by(Place.name_el, Place.id).offset(offset).limit(limit))
    return {"total": total, "items": [{"id": row.id, "name_el": row.name_el, "kind": row.kind,
             "parent_id": row.parent_id, "aliases": row.aliases} for row in rows]}


@router.get("/articles")
def list_articles(region: str | None = None, regional_unit: str | None = None,
                  municipality: str | None = None, place: str | None = None,
                  country: str | None = None, edition: str | None = None,
                  topic: str | None = None, category: str | None = None, subcategory: str | None = None,
                  entity: str | None = None, language: str | None = None,
                  q: str | None = Query(None, min_length=2, max_length=100), published_after: datetime | None = None,
                  limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                  session=Depends(get_session), settings=Depends(get_settings)):
    statement = visible_query(settings)
    if published_after:
        try:
            statement = statement.where(Article.published_at >= utc_date(published_after))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    for kind, value in (("edition", edition), ("country", country), ("category", category),
                        ("subcategory", subcategory), ("topic", topic), ("entity", entity), ("language", language),
                        ("place", region), ("place", regional_unit), ("place", municipality), ("place", place)):
        if value:
            statement = _facet(statement, kind, value)
    if q:
        # JSON text search is bounded in SQL; PostgreSQL can add a language-specific tsvector index.
        title = ArticleVersion.content["selected_title"].as_string()
        summary = ArticleVersion.content["summary"].as_string()
        statement = statement.join(ArticleVersion, (ArticleVersion.article_id == Article.id) &
                                   (ArticleVersion.number == Article.published_version)).where(
                                       or_(title.icontains(q, autoescape=True), summary.icontains(q, autoescape=True)))
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.scalars(statement.order_by(Article.published_at.desc(), Article.id).offset(offset).limit(limit)).all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [serialize_article(session, row, settings) for row in rows]}


def _article(session, article_id, settings):
    row = session.scalar(visible_query(settings).where(Article.id == article_id))
    if not row:
        raise HTTPException(404, "Published article not found")
    return row


@router.get("/articles/{article_id}")
def get_article(article_id: str, session=Depends(get_session), settings=Depends(get_settings)):
    return serialize_article(session, _article(session, article_id, settings), settings)


@router.get("/articles/{article_id}/related")
def related_articles(article_id: str, limit: int = Query(5, ge=1, le=20),
                     session=Depends(get_session), settings=Depends(get_settings)):
    article = _article(session, article_id, settings)
    facets = select(ArticleFacet.kind, ArticleFacet.value).where(ArticleFacet.article_id == article.id,
                          ArticleFacet.kind.in_(["entity", "place", "topic", "category"]))
    pairs = session.execute(facets).all()
    related = visible_query(settings).where(Article.id != article_id)
    if pairs:
        related = related.where(exists(select(ArticleFacet.id).where(ArticleFacet.article_id == Article.id,
                                or_(*[(ArticleFacet.kind == kind) & (ArticleFacet.value == value) for kind, value in pairs]))))
    rows = session.scalars(related.order_by(Article.published_at.desc()).limit(limit)).all()
    return {"items": [serialize_article(session, row, settings) for row in rows]}


@router.get("/articles/{article_id}/{slug}")
def get_canonical_article(article_id: str, slug: str, request: Request,
                          session=Depends(get_session), settings=Depends(get_settings)):
    article = _article(session, article_id, settings)
    if article.slug != slug:
        return RedirectResponse(f"/articles/{article.id}/{article.slug}", status_code=301)
    value = serialize_article(session, article, settings)
    if "application/json" in request.headers.get("accept", ""):
        return value
    esc = html.escape
    paragraphs = "".join((f"<h2>{esc(section['heading'])}</h2>" if section["heading"] else "") +
                         "".join(f"<p>{esc(paragraph)}</p>" for paragraph in section["text"].split("\n") if paragraph.strip())
                         for section in value["body_sections"])
    sources = "".join(f'<li><a href="{esc(source["url"], quote=True)}" rel="nofollow">{esc(source["organization"])}</a></li>' for source in value["sources"])
    structured = json.dumps(value["structured_data"], ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    correction = f'<p>{esc(value["correction_note"])}</p>' if value["correction_note"] else ""
    content = f'''<!doctype html><html lang="{esc(value['language'])}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(value['title'])} | {esc(settings.publisher_name)}</title><meta name="description" content="{esc(value['seo_description'], quote=True)}"><link rel="canonical" href="{esc(value['canonical_url'], quote=True)}"><meta property="og:type" content="article"><meta property="og:title" content="{esc(value['title'], quote=True)}"><meta property="og:description" content="{esc(value['seo_description'], quote=True)}"><meta property="og:url" content="{esc(value['canonical_url'], quote=True)}"><meta name="twitter:card" content="summary"><script type="application/ld+json">{structured}</script></head><body><main><article><h1>{esc(value['title'])}</h1><p>{esc(value['author'])} · <time datetime="{esc(value['datePublished'])}">{esc(value['athensPublished'])}</time></p><p>{esc(value['summary'])}</p>{paragraphs}{correction}<footer><p>{esc(value['automation_disclosure'])}</p><h2>Πηγές</h2><ul>{sources}</ul></footer></article></main></body></html>'''
    return HTMLResponse(content, headers={"Cache-Control": "public, max-age=60",
                         "ETag": f'"{article.id}-{article.published_version}"'})


@router.get("/robots.txt")
def robots(settings=Depends(get_settings)):
    value = f"User-agent: *\nAllow: /articles/\nDisallow: /admin/\nDisallow: /docs\nSitemap: {settings.public_base_url.rstrip('/')}/sitemap.xml\n"
    return Response(value, media_type="text/plain")


@router.get("/sitemap.xml")
def sitemap_index(session=Depends(get_session), settings=Depends(get_settings)):
    total = session.scalar(select(func.count()).select_from(visible_query(settings).subquery()))
    base = escape(settings.public_base_url.rstrip('/'))
    pages = max(1, (total + 9999) // 10000)
    body = "".join(f"<sitemap><loc>{base}/sitemaps/articles-{page}.xml</loc></sitemap>" for page in range(1, pages + 1))
    body += f"<sitemap><loc>{base}/sitemaps/news.xml</loc></sitemap>"
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>', media_type="application/xml")


@router.get("/sitemaps/articles-{page}.xml")
def articles_sitemap(page: int, session=Depends(get_session), settings=Depends(get_settings)):
    if page < 1:
        raise HTTPException(404, "Unknown sitemap page")
    rows = session.scalars(visible_query(settings).order_by(Article.id).offset((page - 1) * 10000).limit(10000))
    body = "".join(f"<url><loc>{escape(settings.public_base_url.rstrip('/'))}/articles/{row.id}/{escape(row.slug)}</loc><lastmod>{(row.modified_at or row.published_at).isoformat()}</lastmod></url>" for row in rows)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>', media_type="application/xml")


@router.get("/sitemaps/news.xml")
def news_sitemap(session=Depends(get_session), settings=Depends(get_settings)):
    rows = session.scalars(visible_query(settings).where(Article.published_at >= utc_now() - timedelta(days=2))
                           .order_by(Article.published_at.desc()).limit(1000)).all()
    body = ""
    for row in rows:
        value = serialize_article(session, row, settings)
        body += f'<url><loc>{escape(value["canonical_url"])}</loc><news:news><news:publication><news:name>{escape(settings.publisher_name)}</news:name><news:language>{escape(value["language"])}</news:language></news:publication><news:publication_date>{value["datePublished"]}</news:publication_date><news:title>{escape(value["title"])}</news:title></news:news></url>'
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{body}</urlset>', media_type="application/xml")
