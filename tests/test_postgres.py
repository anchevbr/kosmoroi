"""Optional production-dialect concurrency checks in an isolated temporary schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema
from greek_news.budget import BudgetExceeded, reserve_generation
from greek_news.config import Settings
from greek_news.db import Base, utc_now
from greek_news.jobs import claim_due, enqueue
from greek_news.models import DailyBudget


@pytest.fixture
def postgres():
    url = os.environ.get("NEWS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set NEWS_TEST_POSTGRES_URL to run PostgreSQL concurrency tests")
    base = create_engine(url)
    schema = "test_" + uuid4().hex
    with base.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = base.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        with base.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        base.dispose()


def test_postgres_workers_cannot_exceed_atomic_budget(postgres):
    now = utc_now()
    settings = Settings(_env_file=None, daily_token_limit=100, max_tokens_per_call=100,
                        daily_call_limit=1, daily_generation_limit=1)
    with Session(postgres) as session, session.begin():
        ids = [enqueue(session, "generate", {}, f"test:{i}").id for i in range(2)]
    def reserve(job_id):
        with Session(postgres) as session:
            try:
                reserve_generation(session, job_id, settings, now=now)
                session.commit()
                return True
            except BudgetExceeded:
                session.rollback()
                return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(reserve, ids)).count(True) == 1
    with Session(postgres) as session:
        assert session.scalar(select(DailyBudget)).tokens_reserved == 100


def test_postgres_skip_locked_claims_distinct_jobs(postgres):
    now = utc_now()
    with Session(postgres) as session, session.begin():
        ids = [enqueue(session, "poll", {}, f"poll:{i}", next_run_at=now).id for i in range(2)]
    barrier = Barrier(2)
    def claim(index):
        with Session(postgres) as session, session.begin():
            rows = claim_due(session, now=now, limit=1)
            barrier.wait(timeout=10)
            return rows
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert {row for result in results for row in result} == set(ids)
    assert all(len(result) == 1 for result in results)
