FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG INSTALL_LOCAL_AI=false
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY fixtures/ ./fixtures/
RUN if [ "$INSTALL_LOCAL_AI" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends build-essential cmake && \
      CMAKE_ARGS="-DGGML_NATIVE=OFF" pip install --no-cache-dir '.[local-ai]' && \
      apt-get purge -y --auto-remove build-essential cmake && rm -rf /var/lib/apt/lists/*; \
    else pip install --no-cache-dir .; fi && \
    useradd --create-home --uid 10001 news && mkdir -p /app/state && chown news:news /app/state
USER news
EXPOSE 8000
CMD ["uvicorn", "greek_news.api:app", "--host", "0.0.0.0", "--port", "8000"]
