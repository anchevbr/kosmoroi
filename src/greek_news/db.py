"""Database/session lifecycle and UTC timestamp handling."""

from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from greek_news.config import get_settings


_clock: ContextVar[datetime | None] = ContextVar("news_clock", default=None)


def utc_now() -> datetime:
    return _clock.get() or datetime.now(timezone.utc)


@contextmanager
def use_clock(value: datetime):
    """One coherent clock for a worker tick or an offline scenario."""
    token = _clock.set(utc_date(value))
    try:
        yield
    finally:
        _clock.reset(token)


def utc_date(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A source timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        return utc_date(value) if value else None

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else utc_date(value)


class Base(DeclarativeBase):
    pass


@lru_cache
def engine_for(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True, connect_args={"timeout": 30} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def get_session() -> Session:
    with sessionmaker(engine_for(get_settings().database_url), expire_on_commit=False)() as session:
        yield session
