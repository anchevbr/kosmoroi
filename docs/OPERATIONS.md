# Operator runbook

## Deploy on a VPS

Use a supported Docker Engine and Compose plugin, a firewall, TLS reverse proxy, and a private filesystem. The Compose stack binds the API to `127.0.0.1:8000`; do not expose it directly or publish PostgreSQL/Redis ports. Restrict `/admin` to trusted operators, preferably behind a VPN or allowlist as well as its bearer tokens.

```sh
cp .env.example .env
chmod 600 .env
# Set different random admin/editor tokens, a URL-safe PostgreSQL password,
# the real HTTPS public URL, and reviewed runtime settings in .env.
docker compose up --build -d
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
docker compose logs migrate worker beat
```

`migrate` applies Alembic revisions before the API/worker/Beat processes start. `/health` checks the process; `/ready` confirms an accessible database with an Alembic revision, not that sources, workers, or licensing are ready. Only one `beat` service should run. The image runs as an unprivileged user and excludes `.env` and local database files from its build context. Rotate tokens and use managed secret storage for real deployments. URL-encode non-URL-safe database passwords or use a URL-safe generated value.

For an **offline demo only**, set `NEWS_FIXTURE_MODE=true`, recreate the API/worker/Beat and run `docker compose exec api greek-news seed-demo`; this imports fictional sources and the partial fixture register. In production, leave fixture mode off and do not seed the demo. The worker imports the bundled [source inventory](../fixtures/source_catalog.json) on its first tick and synchronizes it hourly by default; `NEWS_SOURCE_CATALOG_PATH` can override the path or be blank to turn off sync. All catalog candidates remain disabled without permission. `docker compose exec api greek-news seed-candidates` adds separate, inactive legacy suggestions, but is not needed for catalog sync. Before any live import, supply a provenance-checked administrative register through `greek-news import-geo /path/to/register.json`, set `NEWS_GEO_REGISTER_ID` to its ID and recreate API/worker/Beat. The included register is not ELSTAT-derived and does not substantiate full Greek coverage.

## Editorial workflow

Use `/docs` for schemas. Send `Authorization: Bearer <editor token>` for editorial routes; source changes, policies, pause switches and retries require the separate admin token. Start with `GET /admin/sources`, `/admin/stories`, `/admin/articles`, `/admin/jobs` and `/admin/metrics` (which marks empty regions as gaps and labels the fixture register). Reasons are required for manual grouping, correction, approval, rejection, publication and withdrawal.

1. For an authorized feed, record a dated permission grant for discovery, fetch, storage and AI processing; review reuse rights separately. Set the matching source flags and supported RSS/Atom extraction strategy. The worker verifies robots and the endpoint, activates eligible candidates, polls, groups items with curated evidence, drafts, validates and publishes eligible versions without an editor click. RSS metadata alone contains no verified claims or place roles, so merely turning on a source does not create articles. Homepage-only candidates need a supported feed and adapter before activation.
2. Optional interventions: inspect `GET /admin/articles/{id}` and its frozen evidence/validation errors, use `POST /admin/articles/{id}/versions` for an evidence-validated revision, or approve/reject a version with a reason. Published corrections require `correction_note`; legacy pre-snapshot versions must be regenerated. `POST /admin/stories/{id}/generate` can queue a draft; the normal worker schedules eligible work without it. `greek-news run-once` runs a scheduled tick locally; `run-once --no-schedule` processes queued jobs only.
3. Use `NEWS_DRAFT_ONLY=true` or `NEWS_PUBLICATION_PAUSED=true` in `.env` (then recreate API/workers) to stop publication, or use `PUT /admin/control` with `{"publication_paused":true,"generation_paused":true}` for immediate runtime pauses. The default for these switches is false. After resolving the cause, set them back to false. Manual `POST /admin/articles/{id}/publish` requires an approved version and a reason; an eligible automated version needs neither manual approval nor this request. Confirm via `GET /articles/{id}`. Withdrawal is `POST /admin/articles/{id}/withdraw` with a reason.

Do **not** grant source permissions just to start the worker. Optional `/admin/policies` entries can disable automatic publication for a source/topic; `pilot_approved_at` is not a publication prerequisite. The fake adapter handles fictional evidence only, and fixture data is visible publicly only in explicit fixture mode. For live drafting configure `NEWS_LLM_PROVIDER=openai_compatible` with approved HTTPS endpoint, model and credentials. Automatic validation checks provenance, permissions, conflict/risk flags and expiry, but does not prove semantic truth; continue auditing published results. A queued record is rechecked against current evidence at publish time; changed evidence or an expired interval blocks it. Refresh the evidence and regenerate a version before publishing again.

## Recovery, monitoring and backups

Inspect `/admin/jobs?state=failed` and `/admin/metrics` for failures, stale source fetches, budget usage and regional gaps. Only admin-approved jobs with no uncertain model call and fewer than three attempts can be retried via `/admin/jobs/{id}/retry`. A worker lease expiry recovers uncommitted jobs; a reserved but uncertain model call is **not** sent again without manual reconciliation. Paused publication fails closed. `greek-news run-once` is an explicit single-process recovery option if Beat/Redis is unavailable; it still uses database leases.

Take encrypted, access-controlled PostgreSQL backups outside the application host and test restoration regularly. For a manual snapshot:

```sh
docker compose exec -T db pg_dump -U news -Fc greek_news > backup.dump
```

On a **separate restore target** (or during planned downtime after stopping `api`, `worker`, and `beat`), restore from a trusted backup and apply any newer migrations before restarting:

```sh
docker compose exec -T db pg_restore --clean --if-exists --no-owner -U news -d greek_news < backup.dump
docker compose run --rm migrate
docker compose up -d api worker beat
```

The PostgreSQL backup is authoritative for articles, jobs, revisions, policies and budgets. Redis broker state and Beat's local schedule are disposable: the database scanner rebuilds due work. Preserve `.env`/secrets separately and do not confuse an unchanged `/health` result with restored correctness. PostgreSQL-specific concurrency, live feed operation, container start-up and restore have not been exercised in this offline workspace.