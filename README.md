# HopCraft

**Intelligent flight search for explorers who don't know where to go (yet).**

HopCraft solves two problems no mainstream flight aggregator addresses well:

- **Reverse Search** — pick a destination, see every cheap flight heading there from all of Europe on a map.
- **Smart Multi-City** — give it a budget, a trip length, and your home airport; AI suggests complete multi-city itineraries with real verified prices.

> Portfolio project · Python / FastAPI · React / Leaflet · PostgreSQL · Redis · Docker · Railway

---

## Live Demo

| | |
|---|---|
| 🌍 **App** | https://frontend-production-3477.up.railway.app |
| ⚙️ **API (Swagger)** | https://backend-production-f37c.up.railway.app/docs |
| ❤️ **Health** | https://backend-production-f37c.up.railway.app/api/v1/health |
| ▶️ **2-minute video** | [Watch on LinkedIn](https://www.linkedin.com/posts/salvo-lombardo_hi-in-the-past-few-weeks-ive-been-working-ugcPost-7435751911275827200-Z_xv?utm_source=share&utm_medium=member_ios&rcm=ACoAAE5CKlgBgeWzwZ_yF3GSZ8lanRZ-oJnRwXE) |

> The app runs entirely on free tiers (flight data, LLMs, hosting). External API quotas are limited and protected by per-IP rate limits — if a search returns a "quota exhausted" message, that's the cost control doing its job, not a bug.

---

## Features

### Reverse Search
Enter a destination (e.g. Catania) and a date range. HopCraft queries all active European airports and shows you the cheapest one-way fares on an interactive map — markers coloured by price tier, sortable list below.

Optional geographic filter: restrict origins to airports within a given radius (useful for hub destinations like London or Dubai that would otherwise scan 1 000+ airports).

### Smart Multi-City (AI-powered)
Enter your home airport, trip duration (5–25 days), and budget per person. A 5-step async pipeline:

1. **Area calculation** — estimates the reachable radius from trip length (5 days → ~1 000 km, 25 days → ~3 500 km).
2. **AI itinerary generation** — sends the shortlist of reachable airports to Gemini 2.5 Flash (with Groq and Mistral as automatic fallbacks) and gets back 8–10 geographically sensible multi-city routes as JSON.
3. **Real price verification** — fetches actual flight prices for every leg of every candidate itinerary (async, parallel, provider cascade).
4. **Budget filtering + ranking** — drops itineraries over budget; ranks the rest by total cost.
5. **Top 5 results** — displayed on map with polylines connecting the stops, per-leg prices, and AI travel notes.

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Backend | FastAPI (Python 3.12) | Async, ideal for parallel API calls |
| Database | PostgreSQL 16 | Airports, flight cache (TTL 6 h), search history |
| Cache / Rate limiting | Redis 7 | LLM response cache, quota tracking, circuit breaker, per-IP limits |
| Flight data (primary) | SerpAPI — Google Flights | 250 req/month free, covers Wizz Air, easyJet |
| Flight data (fallback) | Amadeus Self-Service | 2 000 req/month free, major carriers only |
| Flight data (last resort) | Apify — Google Flights scraper | ~$5 free credits/month, low-cost carriers |
| LLM (primary) | Google Gemini 2.5 Flash | 250 req/day free, no credit card |
| LLM (fallback 1) | Groq — Llama 3.3 70B | Free, >300 tok/sec |
| LLM (fallback 2) | Mistral | 1B tokens/month free |
| Frontend | React 18 + Vite + react-leaflet | Interactive map with routes and price markers |
| Hosting | Railway | 4 services: backend, frontend, PostgreSQL, Redis |
| CI | GitHub Actions | Lint (ruff) → test (pytest, 115 tests); Railway auto-deploys on push |

> **Previously deployed on AWS** (EC2 + S3 + CloudFront, provisioned with Terraform). The migration to Railway was a deliberate cost decision when the AWS Free Tier expired — the full reasoning, the original architecture, and the target production design are documented as an architecture decision record in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#infrastructure-an-architecture-decision-record). The original Terraform IaC lives in a separate private repository (available on request).

---

## Production Hardening

Because every external dependency runs on a limited free tier, controlling API spend is a first-class concern in the design:

- **Two-level flight cache** — PostgreSQL JSONB per route/date (TTL 6 h); cache hits cost zero API calls.
- **LLM response cache** — identical Smart Multi-City searches (same origin/duration/budget bucket/season) reuse the AI suggestions from Redis for 24 h instead of burning LLM quota. Prices are still verified live.
- **Quota-aware provider cascade** — monthly per-provider counters in Redis (with a safety margin under each free-tier limit); exhausted providers are skipped automatically.
- **Circuit breaker** — a provider failing repeatedly (outage, not quota) is taken out of the cascade for a cooldown window instead of slowing every search with doomed calls.
- **Per-IP rate limiting** — hourly limits on the public search endpoints so a single client can't drain the monthly quotas.
- **Retries with exponential backoff + jitter** on all external HTTP calls — with one deliberate exception: a rate-limited LLM is *not* retried, because falling through to the next LLM in the chain is faster than waiting out a backoff.
- **Structured JSON logging with correlation IDs** — every request gets an ID (honouring incoming `X-Request-ID`) propagated through the whole async pipeline via contextvars; per-request events log which provider/LLM was used and the latency of every step.

---

## Quick Start (local)

**Prerequisites:** Docker + Docker Compose, plus API keys (see below).

```bash
# 1. Clone
git clone https://github.com/SalvoLombardo/HopCraft-railway-.git
cd HopCraft-railway-

# 2. Configure environment
cp .env.example .env
# Edit .env and fill in your API keys (see reference below)

# 3. Start everything
docker compose up --build

# 4. Seed the airport database (~1 174 European airports)
docker compose exec backend python -m app.db.seed_airports

# 5. Open the app
open http://localhost:3000

# API docs (Swagger UI)
open http://localhost:8000/docs
```

---

## Environment Variables Reference

All keys come from free tiers — no credit card required anywhere.

### Required

| Variable | Example / Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://hopcraft:pwd@db:5432/hopcraft` | Async PostgreSQL DSN. Plain `postgresql://` URLs (as provided by Railway) are normalised automatically. |
| `DB_PASSWORD` | — | PostgreSQL password (used by docker-compose's `db` service). |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string. |
| `GEMINI_API_KEY` | — | [aistudio.google.com](https://aistudio.google.com) — primary LLM. |
| `SERPAPI_API_KEY` | — | [serpapi.com](https://serpapi.com) — primary flight provider. |
| `AMADEUS_API_KEY` / `AMADEUS_API_SECRET` | — | [developers.amadeus.com](https://developers.amadeus.com) — fallback flight provider. |

### Optional

| Variable | Default | Description |
|---|---|---|
| `FLIGHT_PROVIDER` | `cascade` | Cascade order: `cascade` (auto by quota), `serpapi`, `amadeus`, `apify`. |
| `APIFY_API_TOKEN` | *(empty)* | [apify.com](https://apify.com) — last-resort scraper; empty = excluded from cascade. |
| `LLM_PROVIDER` | `gemini` | Start of the LLM fallback chain: `gemini`, `groq`, `mistral`. |
| `GROQ_API_KEY` | *(empty)* | [console.groq.com](https://console.groq.com) — first LLM fallback. |
| `MISTRAL_API_KEY` | *(empty)* | [console.mistral.ai](https://console.mistral.ai) — second LLM fallback. |
| `APP_ENV` | `development` | `production` enables JSON logging and INFO level. |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins — set to the frontend's public URL in production. |
| `CACHE_TTL_HOURS` | `6` | Flight cache validity. |
| `LLM_CACHE_TTL_HOURS` | `24` | AI itinerary suggestion cache validity. |
| `RATE_LIMIT_REVERSE_HOURLY` | `30` | Per-IP hourly limit on Reverse Search. |
| `RATE_LIMIT_SMART_HOURLY` | `10` | Per-IP hourly limit on Smart Multi-City (more expensive: LLM + many provider calls). |
| `MAX_AIRPORTS_SEARCH` | `300` | Max airports considered per search. |

### Frontend (build-time)

| Variable | Description |
|---|---|
| `VITE_API_URL` | Public backend URL, baked into the static bundle at build time. Changing it requires a frontend rebuild. |

---

## Deploying on Railway

The project runs as four Railway services in one project:

1. **PostgreSQL** and **Redis** — Railway database plugins; they expose `DATABASE_URL` / `REDIS_URL` automatically.
2. **backend** — from this GitHub repo, **Root Directory `backend`** (uses `backend/Dockerfile`). Set `PORT=8000` plus all backend variables from the reference above (`DATABASE_URL=${{Postgres.DATABASE_URL}}`, `REDIS_URL=${{Redis.REDIS_URL}}`, `APP_ENV=production`, `ALLOWED_ORIGINS=<frontend public URL>`, and the API keys). Health check path: `/api/v1/health` — it verifies actual DB and Redis connectivity, not just process liveness.
3. **frontend** — same repo, **Root Directory `frontend`**. One variable: `VITE_API_URL=<backend public URL>` (with `https://`).

One-time after the first deploy — seed the airports table (from your machine, using the Postgres service's **public** `DATABASE_PUBLIC_URL`):

```bash
cd backend
DATABASE_URL="<DATABASE_PUBLIC_URL>" DB_PASSWORD=x REDIS_URL=redis://localhost:6379/0 \
  python3 -m app.db.seed_airports
```

Railway auto-deploys both services on every push to `main`; CI (lint + 115 tests) runs in parallel on GitHub Actions.

---

## Project Structure

```
HopCraft/
├── backend/
│   ├── app/
│   │   ├── api/v1/routes/       # search.py, airports.py
│   │   ├── services/
│   │   │   ├── providers/       # Flight Provider Layer (Strategy Pattern)
│   │   │   │   ├── base.py      # FlightProvider ABC, FlightOffer, Leg
│   │   │   │   ├── google_flights.py  # SerpAPI (primary)
│   │   │   │   ├── amadeus.py   # Amadeus (fallback)
│   │   │   │   ├── apify.py     # Apify scraper (last resort)
│   │   │   │   └── factory.py   # Cascade: quota + circuit breaker aware
│   │   │   ├── llm/             # LLM Provider Layer (Strategy Pattern)
│   │   │   │   ├── gemini.py    # Gemini 2.5 Flash (primary)
│   │   │   │   ├── groq.py      # Llama 3.3 70B (fallback)
│   │   │   │   ├── mistral.py   # Mistral (fallback)
│   │   │   │   └── factory.py   # generate_with_fallback()
│   │   │   ├── search_engine.py     # Reverse search logic
│   │   │   ├── area_calculator.py   # Radius from trip duration
│   │   │   └── itinerary_engine.py  # Smart Multi-City 5-step pipeline
│   │   ├── models/              # SQLAlchemy models + Pydantic schemas
│   │   ├── db/                  # DB connection, Redis, flight cache, seed
│   │   └── utils/               # geo, rate limiter, circuit breaker,
│   │                            # LLM cache, HTTP retry, per-IP limits,
│   │                            # JSON logging + correlation IDs
│   └── tests/                   # 115 tests (all external services mocked)
├── frontend/
│   └── src/
│       ├── components/          # SearchForm, SmartSearchForm, Map, ResultsList, ItineraryCard
│       └── services/api.js      # HTTP client
├── docker-compose.yml           # Local dev (backend, frontend, postgres, redis)
└── docs/                        # ARCHITECTURE.md (incl. ADR), SETUP.md, API.md
```

---

## Architecture Overview

```
Railway project
  ├── frontend  (nginx, React SPA — *.up.railway.app, HTTPS)
  │       │  fetch → VITE_API_URL
  │       ▼
  ├── backend   (FastAPI / uvicorn — *.up.railway.app, HTTPS)
  │       ├──► PostgreSQL  (airports, flight_cache)
  │       └──► Redis       (quotas, circuit breaker, LLM cache, per-IP limits)
  │
  ├── PostgreSQL (Railway plugin, private network)
  └── Redis      (Railway plugin, private network)

Flight data cascade:  SerpAPI → Amadeus → Apify   (quota + circuit-breaker aware)
LLM fallback chain:   Gemini  → Groq → Mistral
```

For the full architectural breakdown — including the AWS → Railway migration decision record — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, Strategy Pattern, cascade logic, database schema, caching, observability — plus the infrastructure ADR (AWS original design, Railway migration, target architecture) |
| [docs/SETUP.md](docs/SETUP.md) | Local dev setup, env variables reference, Railway deploy, database operations |
| [docs/API.md](docs/API.md) | Complete API reference with request/response examples |

---

## Running Tests

```bash
cd backend
pip install -r requirements.txt
pytest tests/ -v
# 115/115 PASSED — all external APIs are mocked
```

---

## Roadmap

- [ ] Alembic migrations (currently `create_all()` on startup)
- [ ] Additional flight providers (Kiwi Tequila if B2B access becomes available, RapidAPI aggregators)
- [ ] Expand airport database beyond Europe (North Africa already seeded)
- [ ] Radius filter UI in Reverse Search form (browser geolocation)

---

## License

MIT
