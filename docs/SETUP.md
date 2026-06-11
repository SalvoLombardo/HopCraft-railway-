# HopCraft — Setup Guide

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development](#local-development)
3. [Environment Variables Reference](#environment-variables-reference)
4. [Running Tests](#running-tests)
5. [Production Deploy (Railway)](#production-deploy-railway)
6. [Database Operations](#database-operations)
7. [Common Operations](#common-operations)

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker | ≥ 24 | With Docker Compose plugin |
| Git | any | |
| Python | 3.12 | Only needed for local tests outside Docker |
| Node.js | 20 | Only needed for frontend outside Docker |
| Railway CLI | ≥ 4 | Only for production deploy/ops (`brew install railway`) |

**API keys required (all free, no credit card):**

| Service | Sign up | Variable |
|---|---|---|
| SerpAPI | [serpapi.com](https://serpapi.com) | `SERPAPI_API_KEY` |
| Amadeus | [developers.amadeus.com](https://developers.amadeus.com) | `AMADEUS_API_KEY` + `AMADEUS_API_SECRET` |
| Google AI Studio | [aistudio.google.com](https://aistudio.google.com) | `GEMINI_API_KEY` |
| Groq | [console.groq.com](https://console.groq.com) | `GROQ_API_KEY` |
| Mistral | [console.mistral.ai](https://console.mistral.ai) | `MISTRAL_API_KEY` |
| Apify | [apify.com](https://apify.com) | `APIFY_API_TOKEN` (optional) |

> Groq, Mistral, and Apify are optional (fallbacks). With just Gemini + one flight
> provider key the app works fine for normal load.

---

## Local Development

### 1. Clone and configure

```bash
git clone https://github.com/SalvoLombardo/HopCraft-railway-.git
cd HopCraft-railway-

cp .env.example .env
# Fill in your API keys in .env (see reference below)
```

### 2. Start all services

```bash
docker compose up --build
```

This starts:
- `backend` — FastAPI on `localhost:8000`
- `frontend` — React (served by nginx) on `localhost:3000`
- `db` — PostgreSQL 16 on `localhost:5432`
- `redis` — Redis 7 on `localhost:6379`

The backend waits for PostgreSQL and Redis to be healthy before starting (health checks configured in `docker-compose.yml`). SQLAlchemy `create_all()` creates the tables on first startup.

### 3. Seed airports

```bash
docker compose exec backend python -m app.db.seed_airports
```

Loads ~1 174 European + North Africa airports from OpenFlights into the `airports` table. The script is idempotent — safe to run multiple times.

### 4. Open the app

- App: http://localhost:3000
- API docs (Swagger): http://localhost:8000/docs
- API docs (ReDoc): http://localhost:8000/redoc

### 5. Stop

```bash
docker compose down           # stop containers, keep volumes
docker compose down -v        # stop containers AND delete volumes (fresh start)
```

---

## Environment Variables Reference

Copy `.env.example` to `.env` and fill in the values below.

### Database

| Variable | Example | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://hopcraft:password@db:5432/hopcraft` | Full async DSN. The host `db` is the Docker service name. Plain `postgresql://` URLs (e.g. from Railway) are normalised to `+asyncpg` automatically. |
| `DB_PASSWORD` | `your_secure_password` | Used by the `db` service in `docker-compose.yml`. |

### Redis

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | The host `redis` is the Docker service name. |

### Flight Providers (cascade: SerpAPI → Amadeus → Apify)

| Variable | Default | Description |
|---|---|---|
| `FLIGHT_PROVIDER` | `cascade` | Controls provider order. See table below. |
| `SERPAPI_API_KEY` | — | SerpAPI key. |
| `AMADEUS_API_KEY` | — | Amadeus client ID. |
| `AMADEUS_API_SECRET` | — | Amadeus client secret. |
| `APIFY_API_TOKEN` | *(empty)* | Apify token. Empty = Apify excluded from the cascade. |

**`FLIGHT_PROVIDER` values:**

| Value | Cascade order | When to use |
|---|---|---|
| `cascade` | SerpAPI → Amadeus → Apify (auto by quota) | Production default |
| `serpapi` | SerpAPI first | Force SerpAPI (e.g. testing coverage) |
| `amadeus` | Amadeus first | **Recommended for local dev** — 2 000 req/month vs 250 for SerpAPI |
| `apify` | Apify first | Testing the scraper path only (slow) |

> Quota and circuit-breaker checks still apply in all modes. If the forced provider is exhausted or failing, the system falls through to the next one automatically.

### LLM Providers (fallback chain: Gemini → Groq → Mistral)

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | Starting point of the fallback chain. Options: `gemini`, `groq`, `mistral`. |
| `GEMINI_API_KEY` | — | Google AI Studio key. |
| `GROQ_API_KEY` | *(empty)* | Groq console key. |
| `MISTRAL_API_KEY` | *(empty)* | Mistral La Plateforme key. |

**`LLM_PROVIDER` values:**

| Value | Providers attempted in order | When to use |
|---|---|---|
| `gemini` | Gemini → Groq → Mistral | Default — Gemini is the highest quality option |
| `groq` | Groq → Mistral | Skip Gemini entirely |
| `mistral` | Mistral only | Last resort / testing Mistral specifically |

> Unlike `FLIGHT_PROVIDER`, this is a *start index* into the fixed chain — providers before the start are never tried.

### App Settings

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `production` switches to JSON logging at INFO level. |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins. Set to the frontend public URL in production. |
| `CACHE_TTL_HOURS` | `6` | How long flight cache entries stay valid. |
| `LLM_CACHE_TTL_HOURS` | `24` | How long AI itinerary suggestions are reused from Redis. |
| `RATE_LIMIT_REVERSE_HOURLY` | `30` | Per-IP hourly limit on Reverse Search. |
| `RATE_LIMIT_SMART_HOURLY` | `10` | Per-IP hourly limit on Smart Multi-City. |
| `MAX_AIRPORTS_SEARCH` | `300` | Max airports passed to the frontend airport list endpoint. |

### Frontend (build-time)

| Variable | Description |
|---|---|
| `VITE_API_URL` | Public backend URL. Baked into the static bundle at build time — changing it requires a rebuild, not just a restart. |

---

## Running Tests

Tests use pytest. All external services (DB, Redis, flight providers, LLMs) are mocked — no real API calls are made.

```bash
cd backend
pip install -r requirements.txt
pytest tests/ -v
```

Expected output: **115 passed**.

### Test files

| File | What it tests |
|---|---|
| `tests/test_geo.py` | Haversine distance, radius estimation, stop count estimation |
| `tests/test_search.py` | Reverse search flow: cache hits/misses, provider cascade, radius filter |
| `tests/test_itinerary.py` | Smart Multi-City pipeline, budget filtering, ranking, route validation |
| `tests/test_factories.py` | Both Strategy layers directly: cascade ordering, quota/circuit exclusion, LLM fallback order |
| `tests/test_api_integration.py` | Full HTTP → route → pipeline → JSON integration (ASGI transport) |
| `tests/test_llm_cache.py` | LLM suggestion cache: round-trip, budget bucketing, Redis-failure resilience |
| `tests/test_circuit_breaker.py` | Circuit open/close thresholds, per-IP rate limit dependency |
| `tests/test_http_retry.py` | Retry helper: backoff triggers, non-retryable statuses, exhaustion |

### Lint

```bash
ruff check --select E,F --line-length 150 backend/app backend/tests
```

---

## Production Deploy (Railway)

The production environment is a single Railway project with four services.
Why Railway (and what came before): see the decision record in
[ARCHITECTURE.md](ARCHITECTURE.md#infrastructure-an-architecture-decision-record).

### One-time project setup

1. **Databases** — in the Railway project: *+ New → Database →* **PostgreSQL**, then **Redis**.
2. **Backend service** — *+ New → GitHub Repo* → this repo.
   - Settings → **Root Directory**: `backend` (monorepo — picks up `backend/Dockerfile`)
   - Settings → Networking → **Generate Domain**
   - Settings → **Health Check Path**: `/api/v1/health`
   - Variables:
     ```
     PORT                = 8000
     DATABASE_URL        = ${{Postgres.DATABASE_URL}}
     DB_PASSWORD         = ${{Postgres.PGPASSWORD}}
     REDIS_URL           = ${{Redis.REDIS_URL}}
     APP_ENV             = production
     ALLOWED_ORIGINS     = https://<frontend-domain>.up.railway.app
     FLIGHT_PROVIDER     = cascade
     LLM_PROVIDER        = gemini
     SERPAPI_API_KEY     = <real key>
     AMADEUS_API_KEY     = <real key>
     AMADEUS_API_SECRET  = <real secret>
     GEMINI_API_KEY      = <real key>
     GROQ_API_KEY        = <real key or empty>
     MISTRAL_API_KEY     = <real key or empty>
     APIFY_API_TOKEN     = <real token or empty>
     ```
3. **Frontend service** — *+ New → GitHub Repo* → same repo.
   - Settings → **Root Directory**: `frontend`
   - Settings → Networking → **Generate Domain**
   - Variables: `VITE_API_URL = https://<backend-domain>.up.railway.app`

> ⚠️ Two values cross-reference each other: backend `ALLOWED_ORIGINS` must contain the
> frontend domain, and frontend `VITE_API_URL` must point at the backend domain — both
> with `https://`, no trailing slash. A wrong `ALLOWED_ORIGINS` shows up as CORS errors
> in the browser console; a schemeless `VITE_API_URL` silently produces relative URLs.

### Seed the airports table (one-time)

The Postgres service exposes a public proxy URL (`DATABASE_PUBLIC_URL` in its
Variables tab) — the internal `postgres.railway.internal` host is not reachable from
your machine.

```bash
cd backend
DATABASE_URL="<DATABASE_PUBLIC_URL>" DB_PASSWORD=x REDIS_URL=redis://localhost:6379/0 \
  python3 -m app.db.seed_airports
```

### Deploys

Railway watches `main`: every push rebuilds and redeploys both services. There is no
deploy step in GitHub Actions — CI only runs lint + tests.

Useful CLI commands (after `railway login` and `railway link`):

```bash
railway status                          # project / services overview
railway logs --service backend          # live logs (JSON events in production)
railway variables --service backend     # inspect env vars
railway redeploy --service backend      # manual redeploy
```

### Verify a deploy

```bash
curl https://<backend-domain>/api/v1/health
# → {"status":"ok","env":"production","db":"ok","redis":"ok"}
```

The health endpoint actually checks DB and Redis connectivity and returns 503 with
per-dependency detail if either is down.

---

## Database Operations

### Re-run seed (idempotent)

```bash
# Local
docker compose exec backend python -m app.db.seed_airports

# Production: same command with DATABASE_URL=<DATABASE_PUBLIC_URL> (see above)
```

### Add a new column (no Alembic)

Since the project uses `create_all()` instead of Alembic, new columns need a manual ALTER:

```bash
docker compose exec db psql -U hopcraft -d hopcraft -c \
    "ALTER TABLE airports ADD COLUMN IF NOT EXISTS my_column VARCHAR(50);"
```

Then update the SQLAlchemy model and re-run the seed if the column needs populating.

### Connect directly to the database

```bash
# Local dev
docker compose exec db psql -U hopcraft -d hopcraft

# Production (Railway)
railway connect Postgres
```

### Inspect Redis state (quotas, circuit breaker)

```bash
# Local: docker compose exec redis redis-cli — Production: railway connect Redis
> GET serpapi:monthly          # calls used this 30-day window
> TTL serpapi:monthly          # seconds until window reset
> EXISTS circuit:serpapi:open  # 1 = circuit breaker currently open
> KEYS llm_itineraries:*       # cached LLM suggestions
```

---

## Common Operations

### Rebuild only the backend (local)

```bash
docker compose up --build backend
```

### View logs

```bash
# Local
docker compose logs -f backend

# Production
railway logs --service backend
```

### Reset flight cache (force fresh API calls)

```bash
docker compose exec db psql -U hopcraft -d hopcraft -c \
    "DELETE FROM flight_cache;"
```

### Reset provider quota counters (testing only)

```bash
docker compose exec redis redis-cli DEL serpapi:monthly amadeus:monthly apify:monthly
```
