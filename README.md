# Greek news backend

Permission-gated, evidence-backed Greek news backend. Eligible, source-linked stories can be drafted, validated and published by the worker without an editorial click. The included demo is **offline and fictional**; the bundled source inventory is disabled and does not provide licensed, nationwide live coverage.

## Local pilot

Requires Python 3.12-3.14. SQLite is for local tests and single-process exploration; deploy with PostgreSQL.

```sh
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/greek-news seed-demo
NEWS_FIXTURE_MODE=true .venv/bin/greek-news run-once
NEWS_FIXTURE_MODE=true NEWS_ADMIN_TOKEN=local-admin-token NEWS_EDITOR_TOKEN=local-editor-token \
  .venv/bin/uvicorn greek_news.api:app --host 127.0.0.1 --port 8000
```

`GET /health`, `GET /ready`, and `/docs` are then available at `http://127.0.0.1:8000`. With `NEWS_FIXTURE_MODE=true`, the fictional notices can publish on the tick and appear in `GET /articles`; without it, fictional content is hidden from the public API. The fixture register contains 13 regions but only a small illustrative set of units and municipalities. Its place IDs and relations are **not** an official administrative register.

Use `GET /admin/stories` and `GET /admin/articles` with `Authorization: Bearer local-editor-token` to inspect work and optionally edit or correct articles. The worker schedules generation and publication automatically for eligible evidence; `POST /admin/stories/{id}/generate` can also queue a draft. `greek-news run-once --no-schedule` processes existing jobs. See [the operator runbook](docs/OPERATIONS.md) for optional controls, jobs, backups, and VPS setup. The deployed stack is defined in [compose.yaml](compose.yaml).

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests alembic
.venv/bin/alembic check
```

## Data and safeguards

- PostgreSQL owns sources, immutable revisions, event groups, article versions, outbox records, audit events, leases, and budget reservations. Redis is only the Celery broker; one Beat process schedules database-backed jobs.
- The worker schedules an idempotent hourly sync of the bundled [source inventory](fixtures/source_catalog.json). It includes 68 distinct publishers: 21 national (including Ta Nea, Skai and CNN Greece), 43 regional/local across all 13 Greek regions, and 4 international. Of these, 48 have technically verified RSS endpoints and 20 are homepage-only research leads; all have permissions off and stay inactive. A verified RSS response is not a reuse license. Sources need documented permission for discovery, fetch, storage and AI processing before the worker can verify and activate a compatible feed; text/image reuse requires separate permissions. Feed polling observes `robots.txt`, checks HTTPS/public addresses at connection time, limits redirects/size/time, uses conditional requests, and backs off on errors.
- Curated claims and place roles are required for drafting. RSS metadata alone is **not** a verified fact extraction pipeline; live feed items without curated evidence are stored but do not become stories. The fictional fixture supplies its own source-linked claims.
- The geography importer accepts versioned, reviewed JSON. It distinguishes affected/occurred/background places and refuses ambiguous matches. A national story is not automatically attached to every municipality.
- Draft validation checks structure, references, evidence IDs, place IDs/roles, numbers, intervals, and risk flags; it cannot prove semantic truth or full headline-body fidelity. No editor approval is required for a valid, eligible version. Conflicting or ambiguous evidence, insufficiently corroborated sensitive claims, and expired service notices cannot auto-publish.
- Publication and generation are unpaused by default, with optional configuration and database pause controls. Explicit source/topic policy entries can deny automatic publication; they are not required to opt in. The default fake adapter accepts fictional, curated evidence only; live drafting needs configured `openai_compatible` credentials. Fixture content is visible only with explicit `NEWS_FIXTURE_MODE=true`.
- Public API returns only published versions with source links, a correction note, disclosure, UTC and Athens timestamps, and role-specific geographic filters. Editor changes create new versions; a valid, eligible correction can publish automatically after revalidation. Withdrawal removes the article from public queries while keeping its audit history.

The [source review](docs/SOURCES.md) distinguishes verified RSS endpoints from homepage-only leads and explains why none are enabled. Real production ingestion still needs rights clearance, a reviewed authoritative geography register, source-specific evidence extraction/curation, configured live-model credentials, and operational verification. The locality hints in the source inventory are research leads, **not** official place IDs or evidence for auto-geotagging. Cost-per-article data is unavailable until actual provider pricing and usage are configured. No live-source or PostgreSQL multi-worker claim is made from the SQLite-only local tests.