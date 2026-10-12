"""Optional AI web search discovers URLs; it is never a substitute for fetching article HTML."""

import json
from pathlib import Path
from datetime import timedelta
from sqlalchemy import func, select
from greek_news.budget import recorded_call
from greek_news.db import utc_now
from greek_news.discovery import discover_publisher
from greek_news.ingestion import canonical_url
from greek_news.llm import configured_client
from greek_news.models import LLMCall
from greek_news.polling import add_frontier

SEARCH_SYSTEM = "Find recent news articles using web search. Prefer actual news publishers and primary official notices. Find both major events and small useful local stories. Search only the supplied query and date window. Return links to original article pages. Do not write our article. Search results are discovery leads; downloaded HTML is required before publication."


def search_queries():
    path = Path("fixtures/search_queries.json")
    return json.loads(path.read_text(encoding="utf-8"))["queries"]


def search_news(session_factory, job_id, query, settings, *, client=None):
    if not settings.web_search_enabled or settings.llm_provider != "openai":
        return {"state": "disabled", "urls": 0}
    now = utc_now()
    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(LLMCall).where(
            LLMCall.stage == "search", LLMCall.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)))
        if count >= settings.web_search_daily_limit:
            return {"state": "daily_limit", "urls": 0}
    client = client or configured_client(settings)
    payload = {"query": query, "from": (now - timedelta(days=1)).date().isoformat(),
               "until": now.date().isoformat(), "limit": 10}
    result = recorded_call(session_factory, job_id, settings, "search", SEARCH_SYSTEM,
                           payload, None, settings.extraction_max_tokens, client, search=True)
    added = 0
    with session_factory() as session, session.begin():
        for raw in result.data.get("urls", [])[:30]:
            try:
                url = canonical_url(raw)
            except ValueError:
                continue
            source = discover_publisher(session, url, None, settings)
            if source:
                added += add_frontier(session, source, url)
    return {"state": "completed", "urls": added}
