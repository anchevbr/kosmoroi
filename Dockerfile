FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY fixtures/ ./fixtures/

RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 news
USER news

EXPOSE 8000
CMD ["uvicorn", "greek_news.api:app", "--host", "0.0.0.0", "--port", "8000"]