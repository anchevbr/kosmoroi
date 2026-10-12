from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from greek_news.budget import BudgetExceeded, reserve_generation
from greek_news.config import Settings
from greek_news.db import Base
from greek_news.models import DailyBudget, Job, LLMCall


def test_limits_are_reserved_atomically_across_workers(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, max_tokens_per_call=100, daily_token_limit=100,
                        daily_call_limit=1, daily_generation_limit=1)
    with Session(engine) as session:
        jobs = [Job(idempotency_key=f"draft:{index}", kind="generate") for index in range(2)]
        session.add_all(jobs)
        session.commit()
        ids = [job.id for job in jobs]

    def worker(job_id):
        with Session(engine) as session:
            try:
                reserve_generation(session, job_id, settings)
                session.commit()
                return True
            except BudgetExceeded:
                session.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ids))
    assert results.count(True) == results.count(False) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(LLMCall)) == 1
        day = session.scalar(select(DailyBudget))
        assert day.tokens_reserved == 100 and day.cost_usd is None
    engine.dispose()


def test_priced_spend_limit_and_retry_reservation_are_fail_closed(session):
    settings = Settings(
        _env_file=None, max_tokens_per_call=100, daily_call_limit=10,
        daily_token_limit=1000, daily_generation_limit=10,
        daily_spend_limit_usd=0.0001, input_cost_per_million=2.0,
        output_cost_per_million=2.0,
    )
    job = Job(idempotency_key="test:priced", kind="generate")
    session.add(job)
    session.flush()
    with __import__("pytest").raises(BudgetExceeded):
        reserve_generation(session, job.id, settings)
    assert session.scalar(select(func.count()).select_from(LLMCall)) == 0