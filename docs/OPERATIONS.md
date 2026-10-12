# Operations

Use PostgreSQL, one scheduler, and the Compose worker configuration. Set real secrets, the public HTTPS base URL, and either a funded AI key/model or an installed local GGUF. `docker compose up --build -d` runs migrations and the idempotent bootstrap before serving or scheduling work. Bootstrap is offline; source verification and collection happen in the worker.

The normal flow is fully automatic. Low-risk articles do not wait for multiple sources. Sensitive claims need independent corroboration and conflicting evidence waits for source changes. Bad drafts retry with validation feedback, then skip; there is no compulsory human-review queue. Operator routes exist for exclusions, corrections, withdrawal and emergency pause.

`greek-news doctor` exits 1 while prerequisites are missing. `/ready` returns 503 with the missing checks. `/health` only establishes process liveness. Source discovery records actual robots/HTML failures with bounded backoff; five index failures disable the source temporarily for automatic re-verification. Article failures are tracked individually rather than disabling a healthy publisher. DNS/network errors are recorded instead of crashing a tick.

Read `/admin/sources`, `/admin/jobs`, `/admin/articles/{id}` and `/admin/metrics` with a bearer admin/editor token. Check actual successful fetch/extraction times, pending raw revisions, completed model calls, published articles and regional coverage gaps. Gazetteer coverage, catalog targets and working news coverage are different measurements.

AI budgets include extraction, drafting, verification and search. Calls reserve worst-case input/output capacity before execution. Usage reconciles tokens/spend after responses; uncertain transport/worker outcomes retain their reservation. Interrupted jobs recover with bounded new attempts and reuse completed responses for matching requests. Limits defer work to the next UTC day. A pause retries after a minute; missing model configuration retries after 15 minutes. Limits are configurable in .env.

Keep only one active SQLite worker. In PostgreSQL, a tick uses an advisory lock; job claiming uses row locks with SKIP LOCKED. Generation uses bounded model output. Increase model/crawler capacity deliberately after observing latency and source health. Use valid source selectors and robots-permitted HTML sections to fix a changed layout; no login, CAPTCHA or paywall bypass is attempted.

Back up PostgreSQL and retain the source catalog and geographic register versions. Migrations preserve existing records. Legacy RSS sources are not fetched; bootstrap upgrades matching catalog entries to HTML. Back up before downgrading: old schemas cannot represent multiple model stages or reverted-content revisions without losing records.

Raw full source text expires after the configured retention period. Exact extracted evidence, hashes, article snapshots, revisions and audits remain available. Public articles always use the published snapshot, including original source references and geography. Later source updates trigger new versions with stable article identity and first publication time.

Publish the API and crawlable HTML routes behind an HTTPS proxy; the Compose port binds to localhost. Host your publisher logo if you configure its URL. Configure the future frontend to use stable taxonomy/place IDs and the supplied canonical URLs. Add licensed photos through a separate media workflow; public HTML does not grant photo rights.
