import sqlite3
from sqlalchemy import event
from sqlalchemy.engine import Engine
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from greek_news import models


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database:
        yield database
    engine.dispose()


@pytest.fixture(autouse=True)
def scenario_clock(monkeypatch):
    """Fixtures live on a fixed date; production code shares a single injectable clock."""
    from datetime import datetime, timezone
    from greek_news.db import use_clock
    from greek_news.config import get_settings
    monkeypatch.setenv("NEWS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("NEWS_GEO_REGISTER_ID", "fixture-2026-demo")
    monkeypatch.setenv("NEWS_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("NEWS_PUBLIC_HTML_ENABLED", "false")
    get_settings.cache_clear()
    with use_clock(datetime(2026, 10, 9, 10, tzinfo=timezone.utc)):
        yield
    get_settings.cache_clear()


# Exercise the same foreign-key guarantees as production, including direct test engines.


@event.listens_for(Engine, "connect")
def test_foreign_keys(connection, record):
    if isinstance(connection, sqlite3.Connection):
        connection.execute("PRAGMA foreign_keys=ON")
