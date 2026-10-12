"""Parse public HTML without executing JavaScript or treating feeds as news input."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse
from lxml import html
import trafilatura

from greek_news.ingestion import SourceInput, canonical_url

ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle", "AnalysisNewsArticle"}
EXCLUDED = re.compile(r"(?:^|/)(?:feed|rss|atom|wp-json|api|login|sign-in|subscribe|privacy|terms|tag|author|search)(?:/|$)|\.(?:xml|rss|atom|pdf|jpe?g|png|gif|svg|zip|mp4)(?:$|\?)", re.I)
INDEX = re.compile(r"/(?:category|section|region|perifereia)(?:/[^/]+)?/?$|/(?:news|latest|local|world|politics|sports|epikairotita)/?$", re.I)


@dataclass(frozen=True)
class PageLink:
    url: str
    title: str
    kind: str
    score: int


def document(content: bytes, content_type: str = ""):
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind and kind not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("News input must be HTML, never RSS, XML or a news API response")
    if re.match(rb"\s*(?:<\?xml[^>]*>\s*)?<(?:rss|feed|urlset|sitemapindex)\b", content, re.I):
        raise ValueError("Feeds and XML sitemaps are not supported news inputs")
    if not content.strip():
        raise ValueError("Empty HTML")
    return html.fromstring(content, parser=html.HTMLParser(encoding="utf-8", no_network=True))


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def metadata(root) -> dict:
    result = {}
    for node in root.xpath("//meta[@content]"):
        key = node.get("property") or node.get("name") or ""
        if key:
            result[key.lower()] = clean_text(node.get("content", ""))[:5000]
    return result


def json_articles(root) -> list[dict]:
    found = []

    def walk(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, list):
            for child in value[:50]:
                walk(child, depth + 1)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if ARTICLE_TYPES.intersection(types):
                found.append(value)
            if "@graph" in value:
                walk(value["@graph"], depth + 1)
            if "mainEntity" in value:
                walk(value["mainEntity"], depth + 1)

    for node in root.xpath("//script[contains(@type, 'ld+json')]")[:20]:
        value = node.text or ""
        if len(value) > 250000:
            continue
        try:
            walk(json.loads(value))
        except (ValueError, TypeError, RecursionError):
            continue
    return found


def parse_links(content: bytes, base_url: str, host: str, *, limit: int = 50,
                article_pattern: str | None = None, index_pattern: str | None = None,
                selector: str | None = None, external: bool = False) -> list[PageLink]:
    root = document(content)
    nodes = root.cssselect(selector) if selector else root.xpath("//a[@href]")
    links = {}
    for node in nodes[:5000]:
        if not node.get("href"):
            continue
        title = clean_text(" ".join(node.itertext()) or node.get("title", ""))[:1000]
        try:
            url = canonical_url(urljoin(base_url, node.get("href")))
            parts = urlsplit(url)
        except (TypeError, ValueError, UnicodeError):
            continue
        if len(url) > 2000 or EXCLUDED.search(parts.path) or parts.query:
            continue
        if parts.hostname != host and not external:
            continue
        if parts.hostname != host and (len(title) < 25 or len(title.split()) < 4):
            continue
        path = parts.path
        containers = node.xpath("ancestor::article | ancestor::h1 | ancestor::h2 | ancestor::h3 | ancestor::h4")
        heading = bool(containers) or bool(node.xpath(".//h2 | .//h3 | .//h4"))
        index = bool(re.search(index_pattern, path)) if index_pattern else bool(INDEX.fullmatch(path))
        explicit = bool(re.search(article_pattern, path)) if article_pattern else False
        if path == "/" or not title:
            continue
        if not index and not explicit and not heading and (len(title) < 22 or len(title.split()) < 4):
            continue
        kind = "index" if index and not explicit else "article"
        score = int(explicit) * 8 + int(heading) * 5 + int(bool(re.search(r"\d{4}/\d{1,2}/\d{1,2}|\d{5,}|\.html$", path))) * 3
        previous = links.get(url)
        if previous is None or score > previous.score:
            links[url] = PageLink(url, title, kind, score)
    return sorted(links.values(), key=lambda row: -row.score)[:limit]


def timestamp(value, timezone_name: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = isoparse(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(timezone_name))
        return result
    except (ValueError, TypeError, OverflowError):
        return None


def parse_article(content: bytes, url: str, host: str, *, content_type: str = "",
                  title_hint: str = "", timezone_name: str = "UTC") -> tuple[SourceInput, dict]:
    root = document(content, content_type)
    meta = metadata(root)
    structured = json_articles(root)
    detail = structured[0] if structured else {}
    h1 = root.xpath("//h1")
    title = clean_text(str(detail.get("headline") or meta.get("og:title") or
                           (h1[0].text_content() if h1 else title_hint)))[:1000]
    if not title:
        raise ValueError("Article has no identifiable headline")
    body = trafilatura.extract(content, include_comments=False, include_tables=False,
                               favor_precision=True, deduplicate=True, url=url)
    if not body and detail.get("articleBody"):
        body = clean_text(str(detail["articleBody"]))
    if not body:
        article = root.xpath("//article")
        if article:
            body = "\n".join(clean_text(p.text_content()) for p in article[0].xpath(".//p")
                             if clean_text(p.text_content()))
    if not body or len(body) < 120:
        raise ValueError("Article has too little readable text; index, paywall or script-only page")
    if not (structured or meta.get("og:type") == "article" or h1 and root.xpath("//article")):
        raise ValueError("Page is not an identifiable article")
    if re.search(r"enable javascript|checking your browser|verify you are human|access denied", body[:400], re.I):
        raise ValueError("Access challenge is not an article")
    published = detail.get("datePublished") or meta.get("article:published_time") or meta.get("datepublished") or meta.get("date")
    updated = detail.get("dateModified") or meta.get("article:modified_time") or meta.get("datemodified")
    if not published:
        times = root.xpath("//article//time[@datetime] | //time[@itemprop='datePublished'][@datetime]")
        published = times[0].get("datetime") if times else None
    canonical = url
    declared = root.xpath("//link[contains(concat(' ', @rel, ' '), ' canonical ')]/@href")
    if declared:
        try:
            candidate = canonical_url(urljoin(url, declared[0]))
            if urlsplit(candidate).hostname == host:
                canonical = candidate
        except ValueError:
            pass
    extra = {"date_precision": "day" if isinstance(published, str) and len(published) == 10 else "timestamp" if published else "unknown",
             "publication_time_verified": bool(timestamp(published, timezone_name)),
             "original_language": root.get("lang", ""), "author": detail.get("author"),
             "source_headline": title, "declared_canonical": canonical}
    return SourceInput(url=canonical, title=title, text=body[:100000],
                       published_at=timestamp(published, timezone_name),
                       updated_at=timestamp(updated, timezone_name)), extra
