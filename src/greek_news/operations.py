"""Truthful configuration and runtime readiness; no fake success with an idle pipeline."""

import importlib.util
import os
from pathlib import Path
from datetime import timedelta
from urllib.parse import urlsplit
from sqlalchemy import func, select, text
from greek_news.db import utc_now
from greek_news.models import Article, CrawlURL, GeoRegister, Job, LLMCall, Source, SourceRevision, WorkerHeartbeat
from greek_news.budget import model_prices

LATEST_REVISION = "c9e7a5b41201"


def diagnostics(session, settings):
    checks = {}
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        checks["migrations"] = revision == LATEST_REVISION
        if not checks["migrations"]:
            return {"ready": False, "checks": checks, "setup_required": ["Run alembic upgrade head"], "counts": {}}
    except Exception:
        return {"ready": False, "checks": {"migrations": False}, "setup_required": ["Run alembic upgrade head"], "counts": {}}
    register = session.get(GeoRegister, settings.geo_register_id)
    checks["geography"] = bool(register and (settings.fixture_mode or not register.is_fixture))
    endpoint = urlsplit(settings.llm_endpoint)
    if settings.llm_provider == "openai":
        checks["ai_configuration"] = bool((settings.llm_api_key or os.environ.get("OPENAI_API_KEY")) and settings.llm_model
                                            and endpoint.scheme == "https" and endpoint.hostname)
    elif settings.llm_provider == "llama_cpp":
        checks["ai_configuration"] = Path(settings.model_path).is_file() and importlib.util.find_spec("llama_cpp") is not None
    else:
        checks["ai_configuration"] = settings.llm_provider == "fake" and settings.fixture_mode
    checks["budget_configuration"] = settings.daily_spend_limit_usd is None or model_prices(settings) is not None
    sources = session.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True),
                            Source.can_process_ai.is_(True), Source.source_type.in_(["html", "html_index"])))
    checks["html_sources"] = bool(sources) or settings.fixture_mode
    heartbeat = session.scalar(select(WorkerHeartbeat).order_by(WorkerHeartbeat.updated_at.desc()).limit(1))
    checks["worker"] = bool(heartbeat and heartbeat.updated_at >= utc_now() - timedelta(minutes=10))
    checks["admin_authentication"] = len(settings.admin_token) >= 24 and settings.admin_token not in {"replace-with-unique-long-admin-token"}
    setup = {"migrations": "Run alembic upgrade head", "geography": "Run greek-news bootstrap or import a real gazetteer",
             "ai_configuration": "Configure a funded AI key/model or install a local GGUF and .[local-ai]",
             "budget_configuration": "Set input/output prices for the selected AI model",
             "html_sources": "Run the worker to automatically verify HTML entries; inspect source failures",
             "worker": "Start the worker and scheduler", "admin_authentication": "Set NEWS_ADMIN_TOKEN to a random secret of at least 24 characters"}
    counts = {"enabled_html_sources": sources,
              "crawl_pending": session.scalar(select(func.count()).select_from(CrawlURL).where(CrawlURL.state == "pending")),
              "unprocessed_articles": session.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.analysis_state == "pending")),
              "published": session.scalar(select(func.count()).select_from(Article).where(Article.status == "published")),
              "failed_jobs": session.scalar(select(func.count()).select_from(Job).where(Job.state == "failed")),
              "completed_ai_calls": session.scalar(select(func.count()).select_from(LLMCall).where(LLMCall.status == "completed"))}
    return {"ready": all(checks.values()), "checks": checks, "database_revision": revision,
            "setup_required": [setup[key] for key, ok in checks.items() if not ok], "counts": counts,
            "ai_model": settings.llm_model, "ai_provider": settings.llm_provider,
            "web_search": settings.web_search_enabled and settings.llm_provider == "openai",
            "last_worker_tick": heartbeat.updated_at.isoformat() if heartbeat else None,
            "note": "Configuration readiness does not certify source coverage or model accuracy; completed jobs and source health show actual operation."}
