# Kosmoroi backend

Automatic Greek local/national and worldwide news from public **HTML pages**. No publisher news API, RSS/Atom feed, XML sitemap input, or browser scraping of ChatGPT is required. An AI model analyzes downloaded articles, writes original Greek coverage, and separately verifies every generated headline and body section. Optional OpenAI web search discovers additional article URLs; those URLs must still pass HTML fetching and extraction.

## Start

```bash
cp .env.example .env
mkdir -p models
# Edit .env: database password, random admin/editor tokens, AI key and public URL.
docker compose up --build -d
docker compose logs -f bootstrap worker
```

Set `NEWS_LLM_API_KEY` (or `OPENAI_API_KEY`) to a funded AI account key. This is the AI service key, not a news API key. `NEWS_LLM_MODEL=gpt-5.4-mini` is the initial configurable model. Local inference is also supported: install a multilingual instruction GGUF in `models/news.gguf`, set `INSTALL_LOCAL_AI=true`, `NEWS_LLM_PROVIDER=llama_cpp`, `NEWS_LLM_MODEL` to its name, and `NEWS_WEB_SEARCH_ENABLED=false`. Local inference uses the Python llama.cpp binding in process and requires enough RAM/CPU or a compatible GPU build.

Compose migrates PostgreSQL, imports the offline GeoNames gazetteer and 93 HTML source candidates, and starts the API, worker and scheduler. API binds to localhost:8000; place an HTTPS reverse proxy in front for public access. No editorial approval, source-count quorum, or per-source activation click is required. Sources are verified automatically and normal low-risk local news can proceed from one usable source.

```bash
docker compose exec api greek-news doctor
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` reports process liveness. `/ready` and `doctor` report migration, geography, AI configuration, source activation, worker activity and authentication prerequisites. Configuration readiness does not prove successful AI billing, complete regional coverage, or perfect factual accuracy. Watch actual source health and completed jobs in `/admin/metrics` and `/admin/jobs` using the admin bearer token.

## Automatic workflow

1. Import HTML source entries, discover linked sections/articles, and optionally search for additional news using the AI provider's web-search tool.
2. Verify public HTTPS, robots rules, response size/type and host boundaries. Keep a durable URL frontier, conditional requests, per-source request cadence, retry/backoff and recent-article watches.
3. Extract article text, JSON-LD/headline/date metadata and canonical URL. Navigation, XML feeds, API JSON, access challenges and unreadable pages are rejected.
4. Extract exact source excerpts, classify categories and geographic roles, resolve places, and group related events. Publisher coverage is never used as proof of an event location.
5. Rank local/national/international candidates in separate capacity lanes. Write an original article with three headline candidates and sentence evidence references.
6. Apply structural checks and a separate AI semantic check for names, numbers, dates, attribution, allegations, causation and negation. Failed drafts retry up to three attempts with feedback, then skip automatically. Sensitive claims need independent corroboration; contradictory evidence waits for changed evidence rather than human review.
7. Publish through a durable outbox, rechecking current evidence and automatic policy. Revised sources refresh story geography and generate updates to the same article ID/URL. The public API reads the published snapshot until a new version actually publishes.

AI calls for extraction, writing, verification and search are recorded, budgeted before execution, and reconciled against usage. Completed responses can be reused after worker interruption; uncertain attempts retain a conservative reservation and have bounded automatic recovery. Default limits are 100 drafts, 600 AI calls, 4M tokens and $10/day; whichever limit is reached first pauses additional AI work until the next day. These are caps, not promised article output or actual cost. Update configured prices when changing models or provider pricing.

## Frontend contract and SEO

| Endpoint | Purpose |
| --- | --- |
| `/taxonomy` | Stable edition, category, subcategory, format and scope IDs |
| `/places` | Country/region/city place IDs and hierarchy; supports country, parent, kind and text filters |
| `/articles` | SQL-filtered published articles with edition, country, place/region/municipality, category, subcategory, topic, entity, language, text, date and pagination filters |
| `/articles/{id}` | JSON article, sources, country/geography roles, tags, entities, ad context, correction note and verification status |
| `/articles/{id}/related` | Related published articles for internal links |
| `/articles/{id}/{slug}` | Crawlable HTML with canonical, description, social metadata and NewsArticle JSON-LD; JSON with `Accept: application/json` |
| `/sitemap.xml` | Sitemap index |
| `/sitemaps/articles-1.xml` | Paginated article URLs and genuine modification dates |
| `/sitemaps/news.xml` | Recent news sitemap (two days, maximum 1,000 URLs) |

There is no RSS input or export. The XML sitemaps above are **outputs for our own site's SEO**, not collection inputs. Headlines are factual and specific; the backend does not promise rankings or traffic. No images are republished merely because they appeared in downloaded HTML. Media is currently an empty list, ready for a separately licensed media workflow.

GeoNames data is attributed under CC BY 4.0. The production gazetteer includes countries, Greece's 13 mapped regions, Greek cities/settlements with population >=500 and world cities >=15,000. It is not a complete official Greek municipality register: cities remain cities. Import an authoritative municipality register with `greek-news import-geo path.json` for exact municipality coverage. Ambiguous or unknown places stay unresolved and are not invented for ad targeting.

## Source configuration

The catalog targets all 13 Greek regions plus national and international reporting, sports, government and community notices. Live activation is determined by actual HTML/robots checks; entries are candidates rather than a claim that every publisher works. Script-only, paywalled, denied or changed sites are skipped/backed off and may need a different HTML entry or selector. No paywall, login or CAPTCHA bypass is implemented.

`NEWS_PUBLIC_HTML_ENABLED=true` applies the configured factual-reporting access policy to public candidates. This policy permits internal analysis and original attributed reporting in this application; it does **not** manufacture publisher licenses. Known restricted catalog entries remain excluded. Source-owned wording and photos are not licensed for republication. Raw full article text expires after seven days; evidence excerpts, content hashes, revisions, generated content and audit records remain traceable. Operators can exclude a publisher or topic through configuration without introducing article-review queues.

Add HTML sections using `entry_urls` and optional `link_selector`, `article_url_pattern`, `index_url_pattern`. The source API accepts `entry_url`; the database's legacy `feed_url` field is retained for migration compatibility and stores an HTML entry URL. Old feed entries are never polled and matching catalog entries migrate to HTML automatically.

## Local development and validation

```bash
python -m venv .venv
. .venv/bin/activate
pip install '.[dev]'
alembic upgrade head
greek-news bootstrap
greek-news run-once
ruff check src tests alembic
pytest -q
```

PostgreSQL is the production target. SQLite supports development and tests with one worker. Run one Celery Beat and the supplied worker configuration; PostgreSQL also uses a pipeline advisory lock and row locks. The CI workflow runs tests, PostgreSQL migration checks, Compose validation and a Docker build. Supply `NEWS_TEST_POSTGRES_URL` to run PostgreSQL concurrency tests locally.

Offline fictional scenarios require explicit `NEWS_FIXTURE_MODE=true` and `NEWS_LLM_PROVIDER=fake`. Fixture content is hidden from production public queries. Tests inject HTML and model boundaries; they do not establish the factual quality of a real model or availability of live publishers. A live server, funded AI key (or model file) and successful source checks are required to establish live operation.

See [operations](docs/OPERATIONS.md), [sources](docs/SOURCES.md), and [AI provider reference](https://developers.openai.com/api/docs/guides/tools-web-search).
