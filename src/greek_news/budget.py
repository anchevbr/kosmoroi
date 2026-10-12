"""Atomic daily budget reservations committed before any external model call."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.config import Settings
from greek_news.db import utc_now
from greek_news.llm import DraftResult, PROMPT_VERSION
from greek_news.models import DailyBudget, LLMCall


class BudgetExceeded(ValueError):
    pass


def _pricing_available(settings: Settings) -> bool:
    return settings.input_cost_per_million is not None and settings.output_cost_per_million is not None


def reserve_generation(
    session: Session, job_id: str, settings: Settings, *, now: datetime | None = None,
) -> LLMCall:
    existing = session.query(LLMCall).filter_by(job_id=job_id).one_or_none()
    if existing:
        raise BudgetExceeded("A prior model attempt exists; reconcile before retrying")
    now = now or utc_now()
    if settings.max_tokens_per_call < 1:
        raise BudgetExceeded("A positive per-call token cap is required")
    day = now.date()
    priced = _pricing_available(settings)
    reserved_cost = (settings.max_tokens_per_call * max(
        settings.input_cost_per_million, settings.output_cost_per_million,
    ) / 1_000_000) if priced else None
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(DailyBudget)
    elif dialect == "sqlite":
        statement = sqlite_insert(DailyBudget)
    else:
        raise ValueError("Unsupported budget database")
    session.execute(statement.values(
        day=day, calls_reserved=0, tokens_reserved=0, generations_reserved=0,
        input_tokens_used=0, output_tokens_used=0, cost_reserved_usd=0.0,
        cost_usd=0.0 if priced else None,
    ).on_conflict_do_nothing(index_elements=[DailyBudget.day]))
    conditions = [
        DailyBudget.day == day,
        DailyBudget.calls_reserved < settings.daily_call_limit,
        DailyBudget.generations_reserved < settings.daily_generation_limit,
        DailyBudget.tokens_reserved + settings.max_tokens_per_call <= settings.daily_token_limit,
    ]
    if reserved_cost is not None and settings.daily_spend_limit_usd is not None:
        conditions.append(DailyBudget.cost_reserved_usd + reserved_cost <= settings.daily_spend_limit_usd)
    claimed = session.execute(update(DailyBudget).where(*conditions).values(
        calls_reserved=DailyBudget.calls_reserved + 1,
        generations_reserved=DailyBudget.generations_reserved + 1,
        tokens_reserved=DailyBudget.tokens_reserved + settings.max_tokens_per_call,
        cost_reserved_usd=DailyBudget.cost_reserved_usd + (reserved_cost or 0.0),
    ))
    if claimed.rowcount != 1:
        raise BudgetExceeded("Daily calls, tokens, generations or priced spend limit reached")
    usage = LLMCall(
        job_id=job_id, model=settings.llm_model or "deterministic-fixture",
        prompt_version=PROMPT_VERSION, reserved_tokens=settings.max_tokens_per_call,
        reserved_cost_usd=reserved_cost, status="reserved", created_at=now,
    )
    session.add(usage)
    session.flush()
    return usage


def complete_generation(
    session: Session, job_id: str, result: DraftResult, settings: Settings,
    article_id: str,
) -> LLMCall:
    usage = session.query(LLMCall).filter_by(job_id=job_id).with_for_update().one()
    if usage.status != "reserved":
        raise ValueError("Usage is not an open reservation")
    priced = _pricing_available(settings)
    actual_cost = ((result.input_tokens * settings.input_cost_per_million
                    + result.output_tokens * settings.output_cost_per_million) / 1_000_000
                   if priced else None)
    day = session.get(DailyBudget, usage.created_at.date(), with_for_update=True)
    day.input_tokens_used += result.input_tokens
    day.output_tokens_used += result.output_tokens
    if actual_cost is not None:
        day.cost_usd = (day.cost_usd or 0.0) + actual_cost
    usage.input_tokens = result.input_tokens
    usage.output_tokens = result.output_tokens
    usage.estimated_cost_usd = actual_cost
    usage.latency_ms = result.latency_ms
    usage.retry_count = result.retry_count
    usage.model = result.model
    usage.article_id = article_id
    usage.status = "completed"
    return usage


def mark_uncertain(session: Session, job_id: str) -> None:
    usage = session.query(LLMCall).filter_by(job_id=job_id).with_for_update().one_or_none()
    if usage and usage.status == "reserved":
        usage.status = "uncertain"