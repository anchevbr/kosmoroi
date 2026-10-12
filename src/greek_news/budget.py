"""Atomic reservations and reconciliation for extraction, drafting, verification and search."""

from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from greek_news.config import Settings
from greek_news.db import utc_now
from greek_news.llm import PROMPT_VERSION
from greek_news.models import DailyBudget, Job, LLMCall


class BudgetExceeded(ValueError):
    pass


# Defaults are documented model prices; override when selecting another model.
KNOWN_PRICES = {"gpt-5.4-mini": (0.75, 4.5)}


def model_prices(settings: Settings) -> tuple[float, float] | None:
    if settings.input_cost_per_million is not None and settings.output_cost_per_million is not None:
        return (settings.input_cost_per_million, settings.output_cost_per_million)
    if settings.llm_provider in {"llama_cpp", "fake"}:
        return (0.0, 0.0)
    return KNOWN_PRICES.get(settings.llm_model)


def reserve_generation(session: Session, job_id: str, settings: Settings, *,
                       now: datetime | None = None, stage: str = "draft", attempt: int = 1,
                       input_cap: int = 0, output_cap: int | None = None) -> LLMCall:
    existing = session.scalar(select(LLMCall).where(LLMCall.job_id == job_id,
                              LLMCall.stage == stage, LLMCall.attempt == attempt))
    if existing:
        if existing.status == "completed" and existing.response:
            return existing
        raise BudgetExceeded("This model attempt is already reserved or exhausted")
    now = now or utc_now()
    total_cap = input_cap + (output_cap if output_cap is not None else settings.max_tokens_per_call)
    if total_cap < 1:
        raise BudgetExceeded("A positive token reservation is required")
    prices = model_prices(settings)
    if settings.daily_spend_limit_usd is not None and prices is None:
        raise RuntimeError("Set both model token prices to enforce spend for this model")
    extra_cost = settings.web_search_call_cost_usd if stage == "search" else 0
    cost = (input_cap * prices[0] + (total_cap - input_cap) * prices[1]) / 1_000_000 + extra_cost if prices else None
    statement = postgres_insert(DailyBudget) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(DailyBudget)
    session.execute(statement.values(day=now.date(), calls_reserved=0, tokens_reserved=0,
        generations_reserved=0, input_tokens_used=0, output_tokens_used=0,
        cost_reserved_usd=0.0, cost_usd=0.0 if prices else None,
    ).on_conflict_do_nothing(index_elements=[DailyBudget.day]))
    conditions = [DailyBudget.day == now.date(), DailyBudget.calls_reserved < settings.daily_call_limit,
                  DailyBudget.tokens_reserved + total_cap <= settings.daily_token_limit]
    generation = int(stage == "draft")
    if generation:
        conditions.append(DailyBudget.generations_reserved < settings.daily_generation_limit)
    if cost is not None and settings.daily_spend_limit_usd is not None:
        conditions.append(DailyBudget.cost_reserved_usd + cost <= settings.daily_spend_limit_usd)
    result = session.execute(update(DailyBudget).where(*conditions).values(
        calls_reserved=DailyBudget.calls_reserved + 1, tokens_reserved=DailyBudget.tokens_reserved + total_cap,
        generations_reserved=DailyBudget.generations_reserved + generation,
        cost_reserved_usd=DailyBudget.cost_reserved_usd + (cost or 0)))
    if result.rowcount != 1:
        raise BudgetExceeded("Daily AI call, token, generation or spend limit reached")
    call = LLMCall(job_id=job_id, stage=stage, attempt=attempt,
                   model=settings.llm_model, prompt_version=PROMPT_VERSION,
                   reserved_tokens=total_cap, reserved_cost_usd=cost,
                   created_at=now, status="reserved")
    session.add(call)
    session.flush()
    return call


def complete_generation(session: Session, job_id: str, result, settings: Settings,
                        article_id: str | None, *, stage="draft", attempt=1, response=None) -> LLMCall:
    usage = session.scalar(select(LLMCall).where(LLMCall.job_id == job_id, LLMCall.stage == stage,
                          LLMCall.attempt == attempt).with_for_update())
    if usage.status == "completed":
        return usage
    if usage.status != "reserved":
        raise ValueError("Usage is not an open reservation")
    prices = model_prices(settings)
    cost = (result.input_tokens * prices[0] + result.output_tokens * prices[1]) / 1_000_000 if prices else None
    if cost is not None and stage == "search":
        cost += settings.web_search_call_cost_usd
    day = session.get(DailyBudget, usage.created_at.date(), with_for_update=True)
    day.input_tokens_used += max(0, result.input_tokens)
    day.output_tokens_used += max(0, result.output_tokens)
    actual = max(0, result.input_tokens) + max(0, result.output_tokens)
    # Reservation balances contain completed usage plus outstanding worst-case reservations.
    day.tokens_reserved += actual - usage.reserved_tokens
    day.cost_reserved_usd += (cost or 0) - (usage.reserved_cost_usd or 0)
    if cost is not None:
        day.cost_usd = (day.cost_usd or 0) + cost
    usage.input_tokens, usage.output_tokens = result.input_tokens, result.output_tokens
    usage.estimated_cost_usd, usage.latency_ms = cost, result.latency_ms
    usage.retry_count, usage.model, usage.article_id = result.retry_count, result.model, article_id
    usage.response = response if response is not None else (result.draft.model_dump(mode="json") if hasattr(result, "draft") else result.data)
    usage.status = "completed"
    return usage


def mark_uncertain(session: Session, job_id: str) -> None:
    for usage in session.scalars(select(LLMCall).where(LLMCall.job_id == job_id, LLMCall.status == "reserved").with_for_update()):
        # Keep a worst-case charge for interrupted provider calls; future attempts have new IDs.
        usage.status = "uncertain"


def recorded_call(session_factory, job_id, settings, stage, system, payload, schema, output_cap, client, *, search=False):
    import json
    import hashlib
    from greek_news.llm import ModelFailure, ModelResult
    with session_factory() as session, session.begin():
        request_hash = hashlib.sha256(json.dumps([system, payload, schema, settings.llm_model, output_cap], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        job = session.get(Job, job_id)
        attempt = max(1, job.attempts)
        cached = session.scalar(select(LLMCall).where(LLMCall.job_id == job_id,
                                LLMCall.stage == stage, LLMCall.request_hash == request_hash,
                                LLMCall.status == "completed").order_by(LLMCall.attempt.desc()).limit(1))
        if cached and cached.response:
            return ModelResult(cached.response, cached.model, cached.input_tokens, cached.output_tokens, cached.latency_ms)
        # UTF-8 byte count is a conservative prompt bound, including schema and framing.
        input_cap = len(json.dumps(payload, ensure_ascii=False).encode()) + len(system.encode()) + len(json.dumps(schema or {}).encode()) + 512
        if search:
            input_cap += 16000  # Bound tool context; usage is reconciled after the response.
        usage = reserve_generation(session, job_id, settings, stage=stage, attempt=attempt,
                                   input_cap=input_cap, output_cap=output_cap)
        usage.request_hash = request_hash
    try:
        result = client.request(system, payload, schema, output_cap, search=search)
    except ModelFailure as exc:
        with session_factory() as session, session.begin():
            if exc.result:
                complete_generation(session, job_id, exc.result, settings, None, stage=stage, attempt=attempt)
            else:
                mark_uncertain(session, job_id)
        raise
    except Exception:
        with session_factory() as session, session.begin():
            mark_uncertain(session, job_id)
        raise
    with session_factory() as session, session.begin():
        complete_generation(session, job_id, result, settings, None, stage=stage, attempt=attempt)
    return result
