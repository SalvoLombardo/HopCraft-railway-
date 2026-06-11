# HopCraft — Architecture

## Table of Contents

1. [System Overview](#system-overview)
2. [Backend Structure](#backend-structure)
3. [Flight Provider Layer](#flight-provider-layer)
4. [LLM Provider Layer](#llm-provider-layer)
5. [Smart Multi-City Pipeline](#smart-multi-city-pipeline)
6. [Reverse Search Logic](#reverse-search-logic)
7. [Cost Control & Resilience](#cost-control--resilience)
8. [Database Schema](#database-schema)
9. [Observability](#observability)
10. [CI/CD Pipeline](#cicd-pipeline)
11. [Infrastructure: an Architecture Decision Record](#infrastructure-an-architecture-decision-record)

---

## System Overview

```
┌─────────────────────────┐
│   React + Leaflet       │
│   (Vite build, nginx)   │
│                         │
│  SearchForm             │
│  SmartSearchForm        │
│  Map (Leaflet)          │
│  ResultsList            │
│  ItineraryCard          │
│  ProviderBadge          │
└────────────┬────────────┘
             │ HTTPS (VITE_API_URL, CORS-restricted)
             ▼
┌─────────────────────────┐
│   FastAPI Backend       │
│   (uvicorn, $PORT)      │
│                         │
│  /api/v1/search/reverse │──► search_engine.py
│  /api/v1/search/smart-  │──► itinerary_engine.py
│  /api/v1/airports       │──► airports.py
│  /api/v1/health         │──► checks DB + Redis connectivity
└──────┬──────────────────┘
       │
       ├──► Flight Provider Layer (SerpAPI → Amadeus → Apify)
       ├──► LLM Provider Layer    (Gemini → Groq → Mistral)
       ├──► PostgreSQL            (airports, flight_cache, search_history)
       └──► Redis                 (quota counters, circuit breaker,
                                   LLM response cache, per-IP rate limits)
```

Both Strategy layers share the same design idea: the application code never knows
which concrete provider it is talking to. Selection, fallback, quota accounting,
and failure isolation all live in the factory.

---

## Backend Structure

```
backend/app/
├── main.py              # FastAPI app, CORS, correlation-ID middleware,
│                        # lifespan (create_all tables, Redis ping), /health
├── config.py            # pydantic-settings: typed config, reads .env
├── api/v1/
│   ├── router.py        # Aggregates all routes
│   └── routes/
│       ├── search.py    # GET /search/reverse, POST /search/smart-multi
│       └── airports.py  # GET /airports, GET /airports/in-radius
├── services/
│   ├── providers/       # Flight Provider Layer (see below)
│   ├── llm/             # LLM Provider Layer (see below)
│   ├── search_engine.py     # Reverse search core logic
│   ├── area_calculator.py   # Reachable area from trip duration
│   └── itinerary_engine.py  # Smart Multi-City 5-step pipeline
├── models/
│   ├── airport.py       # SQLAlchemy model: Airport
│   ├── flight_cache.py  # SQLAlchemy model: FlightCache
│   └── schemas.py       # Pydantic schemas (all API input/output)
├── db/
│   ├── database.py      # Async SQLAlchemy engine + session factory
│   ├── redis.py         # Redis connection (redis.asyncio)
│   ├── cache.py         # Flight cache read/write helpers
│   └── seed_airports.py # Populates airports from OpenFlights CSV
└── utils/
    ├── geo.py             # haversine_km, estimate_radius_km, estimate_stops
    ├── rate_limiter.py    # Monthly provider quota counters (Redis)
    ├── user_rate_limit.py # Per-IP hourly limits on public endpoints
    ├── circuit_breaker.py # Per-provider failure isolation (Redis)
    ├── llm_cache.py       # Redis cache for AI itinerary suggestions
    ├── http_retry.py      # Shared retry helper (backoff + jitter)
    └── logging_config.py  # JSON logging + correlation-ID contextvar
```

---

## Flight Provider Layer

### Design: Strategy Pattern + Automatic Cascade

The application code (`search_engine.py`, `itinerary_engine.py`) never knows which flight provider it is using. The layer exposes a single abstract interface; providers are selected and rotated automatically at runtime based on remaining quota **and** recent failure history (circuit breaker).

```
providers/
├── base.py            # FlightProvider (ABC), FlightOffer, Leg
├── google_flights.py  # GoogleFlightsProvider — SerpAPI (primary)
├── amadeus.py         # AmadeusProvider — Amadeus Self-Service (fallback)
├── apify.py           # ApifyProvider — Google Flights scraper (last resort)
└── factory.py         # get_providers_in_order(), get_provider_quotas()
```

### Abstract Interface (`base.py`)

```python
@dataclass
class Leg:
    origin: str        # IATA code
    destination: str
    date: date

@dataclass
class FlightOffer:
    origin: str
    destination: str
    departure: str     # ISO datetime string
    price_eur: float
    airline: str
    direct: bool
    duration_minutes: int

class FlightProvider(ABC):
    @abstractmethod
    async def search_one_way(
        self, origin, destination, date_from, date_to,
        direct_only=False, max_results=50
    ) -> list[FlightOffer]: ...

    @abstractmethod
    async def search_multi_city(
        self, legs: list[Leg]
    ) -> list[FlightOffer]: ...
```

### Cascade Logic (`factory.py`)

```python
PROVIDER_LIMITS = {
    "serpapi":  230,   # free tier 250, safety margin 20
    "amadeus":  1800,  # free tier 2000, safety margin 200
    "apify":    180,   # ~$5 free credits, conservative estimate
}

async def get_providers_in_order() -> list[tuple[str, FlightProvider]]:
    """
    Returns providers in cascade order, excluding any that:
      - have exhausted their monthly quota (Redis counter), or
      - have an OPEN circuit breaker (repeated recent failures).
    """
```

Monthly quotas are tracked in Redis (`serpapi:monthly`, …) on a rolling 30-day TTL. Each successful provider call increments the counter; at the limit the provider is skipped until the window resets.

#### Forcing a provider via `FLIGHT_PROVIDER`

| `FLIGHT_PROVIDER` | Effective order | Typical use |
|---|---|---|
| `cascade` | SerpAPI → Amadeus → Apify (auto by quota) | Production |
| `amadeus` | Amadeus → SerpAPI → Apify | Local dev — preserves the 250 SerpAPI req/month |
| `serpapi` | SerpAPI → Amadeus → Apify | Force SerpAPI explicitly |
| `apify` | Apify → SerpAPI → Amadeus | Testing the scraper path |

The forced provider only changes the *starting order* — quota and circuit-breaker checks still apply, and exhausted providers fall through to the next one.

### Provider Characteristics

| Provider | Coverage | Quota | Notes |
|---|---|---|---|
| SerpAPI (GoogleFlightsProvider) | Wizz Air, easyJet, Ryanair (partial) | 250 req/month | Primary. Structured JSON from Google Flights. Parallel per-date calls with retry. |
| Amadeus (AmadeusProvider) | Major carriers only (no EU low-cost) | 2 000 req/month | Fallback. OAuth2 token cached ~30 min; concurrency capped by semaphore; exponential backoff on 429/5xx. When Amadeus is the only active provider, the LLM prompt receives a `provider_hint` steering it toward hub airports. |
| Apify (ApifyProvider) | Low-cost carriers incl. Ryanair | ~180 runs/month | Last resort. Actor runs cost credits and take up to 120 s — deliberately **no retry**: a failure should cascade, not double the spend. |

### ProviderStatus in every response

```json
{
  "provider_status": {
    "active_provider": "serpapi",
    "serpapi_remaining": 187,
    "amadeus_remaining": 1800,
    "note": "Results from Google Flights (SerpAPI) — includes Wizz Air, easyJet…"
  }
}
```

The frontend renders this as a coloured badge, so quota degradation is visible to the user instead of silently changing result quality.

---

## LLM Provider Layer

### Design: Strategy Pattern + Automatic Fallback

Used in Step 2 of the Smart Multi-City pipeline to generate candidate itineraries.

```
llm/
├── base.py      # LLMProvider (ABC), SuggestedItinerary, system prompt, parse_itineraries()
├── gemini.py    # GeminiProvider — Gemini 2.5 Flash (primary)
├── groq.py      # GroqProvider  — Llama 3.3 70B (fast fallback)
├── mistral.py   # MistralProvider — Mistral (volume fallback)
└── factory.py   # generate_with_fallback()
```

### Fallback Factory (`factory.py`)

```python
_FALLBACK_ORDER = ["gemini", "groq", "mistral"]

async def generate_with_fallback(...) -> list[SuggestedItinerary]:
    start = _FALLBACK_ORDER.index(settings.llm_provider)
    for name in _FALLBACK_ORDER[start:]:
        try:
            return await _PROVIDERS[name]().generate_itineraries(...)
        except Exception:
            continue  # try next provider
    raise RuntimeError("All LLM providers failed")
```

`LLM_PROVIDER` is a **start index** into the fixed chain: `LLM_PROVIDER=groq` means Gemini is never tried for that run. Every successful call emits a structured `llm_call` log event with the provider chosen, its position in the fallback chain, and latency.

**Retry policy (deliberate asymmetry):** each LLM provider retries once on 5xx, but a **429 is never retried** — when an LLM is rate-limited, falling through to the next provider in the chain is faster and cheaper than waiting out a backoff.

### Prompt Engineering

The prompt is defined once in `base.py` and used identically across all providers. A structured system prompt requests JSON-only output; `parse_itineraries()` handles both raw JSON and markdown-wrapped code blocks.

The `provider_hint` parameter is injected into the user prompt when Amadeus is the only active flight provider, guiding the AI away from secondary airports (BGY, STN, …) that Amadeus cannot price.

### LLM Provider Limits (free tier, as of early 2026)

| Provider | Model | Free Limit | Card required |
|---|---|---|---|
| Gemini | 2.5 Flash | 250 req/day | No |
| Groq | Llama 3.3 70B | ~14 400 req/day (1M tok/min) | No |
| Mistral | mistral-small-latest | ~1B tokens/month | No |

---

## Smart Multi-City Pipeline

Implemented in `itinerary_engine.py` → `run_smart_multi()`.

```
Step 0 (guard): if NO flight provider is available (quota/circuit),
        fail fast with a clear 503 — before spending an LLM call.

Step 1: calculate_area(session, origin, trip_duration_days)
        ├─ Queries DB for all active airports
        ├─ Computes Haversine distance from origin to each
        └─ Returns AreaResult: radius_km, num_stops, sorted airport list

Step 2: generate_with_fallback(...)  [LLM]
        ├─ Redis cache first: same origin/duration/budget-bucket/season/stops
        │  → reuse cached suggestions for 24 h (llm_cache.py), zero LLM cost
        ├─ On miss: Gemini (→ Groq → Mistral on error)
        └─ 8–10 JSON itineraries: { route, reasoning, difficulty, best_season }

Step 3: _price_itinerary() × N  (asyncio.gather, semaphore=3 concurrent)
        ├─ Validates route structure (starts/ends at origin, no duplicate stops)
        ├─ Distributes departure dates evenly across the trip
        ├─ Prices every leg through the provider cascade
        └─ Records circuit-breaker success/failure per provider call

Step 4: Budget filter + rank
        ├─ Drop itineraries where sum(leg prices) > budget_per_person_eur
        ├─ Sort by total per person ascending
        └─ Keep top 5; count n_no_data / n_over_budget for diagnostics

Step 5: Build SmartMultiOut
        └─ ItineraryOut × 5: rank, route, prices, legs, ai_notes,
           suggested_days_per_stop, provider_status
```

Partial failures are never silent: every dropped route is counted (`routes_no_data`, `routes_over_budget`) and logged in the `smart_multi_timing` event; if nothing survives, the user gets a message explaining *which* filter killed the results.

### Radius / Stops Estimation

```python
def estimate_radius_km(days: int) -> int:
    if days <= 7:   return days * 200           # up to 1 400 km
    if days <= 15:  return 1400 + (days-7)*150  # up to 2 600 km
    return min(2600 + (days-15)*100, 5000)      # capped at 5 000 km

def estimate_stops(days: int) -> int:
    if days <= 7:   return min(2, days // 3)
    if days <= 15:  return min(3, days // 4)
    return min(4, days // 5)
```

---

## Reverse Search Logic

Implemented in `search_engine.py` → `reverse_search()`.

```
1. Load all active airports from DB (exclude destination)
2. Optional: filter by radius from origin_lat/origin_lon (Haversine)
3. Build date list: date_from → date_to (max 7 days)
4. Batch query flight_cache for valid entries (fetched_at within TTL)
   → cache_best: {origin: (cheapest_offer, fetched_at)}
5. Identify missing_origins (no cache hit)
   → take first _MAX_NEW_CALLS_PER_SEARCH = 50
6. For each missing origin: asyncio.gather(_fetch, return_exceptions=True)
   _fetch: provider cascade; per-call circuit-breaker bookkeeping;
   one origin failing entirely never aborts the whole search
   → saves new results to cache
7. Merge cache results + fresh results
8. Sort by price_eur, cap at max_results
9. Emit structured "reverse_search" event (cache hits, provider usage, latency)
10. Attach provider_status
```

---

## Cost Control & Resilience

Every external dependency runs on a limited free tier, so API spend control is an
architectural concern, not an afterthought. The mechanisms compose in layers:

| Layer | Mechanism | Where |
|---|---|---|
| Don't call at all | Flight cache (PostgreSQL JSONB, TTL 6 h) | `db/cache.py` |
| Don't call at all | LLM suggestion cache (Redis, TTL 24 h, budget bucketed to €25) | `utils/llm_cache.py` |
| Don't let one user spend the month | Per-IP hourly limits (30/h reverse, 10/h smart-multi), fail-open on Redis errors | `utils/user_rate_limit.py` |
| Stop at the budget | Monthly quota counters with safety margin per provider | `utils/rate_limiter.py` |
| Don't pay for outages | Circuit breaker: 3 failures/120 s → provider skipped for 5 min | `utils/circuit_breaker.py` |
| Pay once, not thrice | Retry with exponential backoff + jitter on transient errors only | `utils/http_retry.py` |
| Fail fast | No providers available → 503 before the LLM call is spent | `itinerary_engine.py` |

Two deliberate asymmetries worth noting:

- **User rate limiting fails open, the circuit breaker fails closed.** If Redis hiccups, a missing rate-limit check should not take the API down (fail open); a missing circuit check just means one extra provider attempt (fail closed = circuit treated as closed). Both errors are logged.
- **LLMs are not retried on 429.** The flight providers back off and retry on 429 because there is no alternative source for the same data mid-search. The LLM chain has interchangeable alternatives, so the fastest recovery is falling through.

---

## Database Schema

### `airports`

```sql
CREATE TABLE airports (
    iata_code   VARCHAR(3)   PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    city        VARCHAR(255) NOT NULL,
    country     VARCHAR(100) NOT NULL,
    continent   VARCHAR(2),            -- ISO: EU, AF, AS, NA, SA, OC
    latitude    FLOAT        NOT NULL,
    longitude   FLOAT        NOT NULL,
    is_active   BOOLEAN      DEFAULT TRUE
);
CREATE INDEX idx_airports_coords ON airports (latitude, longitude);
```

Seeded from OpenFlights (`airports.dat`), filtered to Europe + North Africa (~1 174 airports). `seed_airports.py` is idempotent (`on_conflict_do_update`).

### `flight_cache`

```sql
CREATE TABLE flight_cache (
    id                    SERIAL PRIMARY KEY,
    origin                VARCHAR(3)   NOT NULL,
    destination           VARCHAR(3)   NOT NULL,
    departure_date        DATE         NOT NULL,
    price_eur             DECIMAL(10,2),
    airline               VARCHAR(100),
    direct_flight         BOOLEAN,
    flight_duration_minutes INTEGER,
    fetched_at            TIMESTAMP    DEFAULT NOW(),
    raw_response          JSONB,       -- full list of FlightOffer dicts
    UNIQUE(origin, destination, departure_date)
);
CREATE INDEX idx_cache_lookup ON flight_cache (destination, departure_date, fetched_at);
CREATE INDEX idx_cache_expiry ON flight_cache (fetched_at);
```

TTL is controlled by `CACHE_TTL_HOURS` (default 6). The full raw response is stored as JSONB so the same cache entry can be re-parsed and re-filtered.

### Migrations

No Alembic yet. Tables are created via `create_all()` in the FastAPI lifespan handler — adequate for the current single-table-additions history, flagged in the roadmap as the next hardening step. New columns currently require a manual `ALTER TABLE` (see `SETUP.md`).

---

## Observability

### Structured JSON logging + correlation IDs

In production (`APP_ENV=production`) every log line is a single JSON object:

```json
{"ts": "2026-06-11T12:40:26", "level": "INFO", "logger": "app.services.search_engine",
 "correlation_id": "6d410c8025bc", "message": "{\"event\": \"reverse_search\", ...}"}
```

- An HTTP middleware assigns each request a **correlation ID** (honouring an incoming `X-Request-ID` header, returning the ID in the response). The ID lives in a `contextvar`, so it propagates automatically through the entire async pipeline — including `asyncio.gather` fan-outs to providers — with no manual parameter threading.
- In development the same information is rendered human-readable.

### Structured events

| Event | Emitted by | Key fields |
|---|---|---|
| `smart_multi_timing` | `itinerary_engine` (1/request) | per-step latency (`step_area_ms`, `step_llm_ms`, `step_pricing_ms`), `llm_cache_hit`, route counts (suggested / no_data / over_budget / returned), active provider |
| `reverse_search` | `search_engine` (1/request) | origins from cache vs fetched, per-provider usage counts, total latency |
| `llm_call` | `llm/factory` (1/LLM success) | provider name, `fallback_position` (0 = primary worked), latency, itinerary count |

Events are written even on failure paths (e.g. zero results), so failed requests are as observable as successful ones.

### Where to look

Railway's log explorer supports full-text filtering on these JSON lines — filter by
`correlation_id` to follow one request end-to-end, by `event` to build ad-hoc metrics
(e.g. fallback frequency: `llm_call` with `fallback_position > 0`), or by `level=WARNING`
to watch provider failures and circuit-breaker trips. Railway also supports
webhook/email alerts on deploy failures and resource limits.

---

## CI/CD Pipeline

`.github/workflows/ci.yml` — triggered on every push and PR to `main`.

```
Job: lint  (ruff)
  └─ ruff check --select E,F --line-length 150 backend/app backend/tests

Job: test  (pytest)  — needs: lint
  └─ pytest tests/ -v
     All 115 tests pass with mocked external services
     (DB, Redis, SerpAPI, Amadeus, Apify, Gemini, Groq, Mistral)
```

**Deployment is not part of CI:** Railway watches the GitHub repo and rebuilds/deploys
both services (backend, frontend) automatically on every push to `main`. The previous
AWS deploy job (GHCR build → S3 sync → CloudFront invalidation → SSH into EC2) was
removed during the migration — see the decision record below.

---

## Infrastructure: an Architecture Decision Record

This section documents *why* the deployment looks the way it does, in ADR form.
The infrastructure decisions were deliberate at every step — including the decision
to simplify.

### ADR-001 — Original deployment on AWS (EC2 + S3 + CloudFront + Terraform)

**Status:** superseded by ADR-002 (June 2026)

**Context.** First production deployment of a portfolio project. Goals: learn and
demonstrate real-world infrastructure skills (IaC, networking, CDN, CI/CD to a cloud
target) while staying inside the AWS Free Tier.

**Decision.** Provision with Terraform, region `eu-south-1` (Milan):

```
Internet users
      │
      ▼
CloudFront (*.cloudfront.net — free HTTPS, no custom domain needed)
  ├─ GET /*       → S3 bucket (React SPA, versioned deploys)
  └─ GET /api/*   → EC2 :80 (Nginx)
                          └─ FastAPI :8000

EC2 t3.micro (docker-compose.prod.yml)
  ├─ nginx       :80  → backend:8000
  ├─ backend     :8000
  ├─ postgres    (persistent EBS volume)
  └─ redis       (persistent volume)
```

- **CloudFront as the single entry point** solved HTTPS for free (no certificate or
  domain purchase) and let one origin-routing rule split static assets (S3) from API
  traffic (EC2).
- **Single EC2 with docker-compose** instead of ECS/RDS/ElastiCache: at portfolio
  scale, managed services added cost and complexity without changing what the
  application code demonstrates. Postgres and Redis as containers with persistent
  volumes were a conscious trade-off (acceptable RPO for a demo, zero managed-DB cost).
- **GitHub Actions deploy job:** build backend image → push to GHCR → SSH into EC2 →
  `docker compose up -d`; frontend build → S3 sync → CloudFront invalidation.
- **CloudWatch Logs** via the Docker `awslogs` driver for centralized logging.

**Consequences.** Full control and a realistic ops surface (IAM, security groups,
SSH hardening, log shipping) at $0/month — until the Free Tier clock ran out.
Operating it meant owning patching, disk space, and certificate-free HTTPS quirks.

### ADR-002 — Migration to Railway

**Status:** accepted (June 2026) — current deployment

**Context.** The AWS Free Tier (12 months) expired. Keeping the EC2+CloudFront stack
running would have cost real money every month for a portfolio project with sporadic
traffic. The alternative — Railway's usage-based free/hobby tier — could run the same
four containers for ≈ $0.

**Decision.** Migrate to Railway as four services in one project: **backend**
(Dockerfile build, root `backend/`), **frontend** (Dockerfile build, root
`frontend/`, nginx serving the Vite bundle), **PostgreSQL** and **Redis** (managed
plugins, private networking). Railway auto-deploys on every push to `main`; CI keeps
running lint + tests on GitHub Actions.

The application code is **identical** between the two environments — the migration
touched only:
- the deploy job (deleted from CI — Railway watches the repo),
- `DATABASE_URL` normalisation (Railway provides `postgresql://`, SQLAlchemy async
  needs `postgresql+asyncpg://` — handled in code),
- the backend Dockerfile binding to Railway's injected `$PORT`,
- `VITE_API_URL` / `ALLOWED_ORIGINS` pointing at the Railway domains.

**Trade-off accepted.**

| Gained | Lost |
|---|---|
| ~$0/month again | Terraform IaC (Railway config is dashboard/CLI state) |
| Zero infra management (no patching, disks, SSH) | Granular network control (security groups, private subnets) |
| Deploy on push, build logs, instant rollbacks | CloudFront-class CDN in front of the SPA |
| Managed Postgres/Redis with backups | The "I run my own boxes" ops surface |

**Consequences.** The trade was cost and simplicity over control — the right trade
for a portfolio demo, and explicitly **not** presented as the production ceiling
(see ADR-003). The original Terraform code was removed from this repository to avoid
documenting infrastructure that no longer matches reality; it is preserved in a
separate **private repository** together with the AWS deployment history, available
on request.

### ADR-003 — Target production architecture (if cost were no constraint)

**Status:** documented as design intent — not deployed

If this product had real traffic and a budget, the AWS deployment would evolve to:

```
Route 53 (custom domain) ── ACM certificates
      │
      ▼
CloudFront ── WAF (rate limiting at the edge)
  ├─ /*     → S3 (SPA)
  └─ /api/* → ALB → ECS Fargate (FastAPI, ≥2 tasks, multi-AZ, autoscaling)
                      ├─ RDS PostgreSQL (Multi-AZ, automated backups, PITR)
                      ├─ ElastiCache Redis (replication group)
                      └─ Secrets Manager (API keys, rotation)

Observability: CloudWatch Logs + Container Insights, X-Ray traces,
               alarms → SNS. IaC: the same Terraform, extended per module.
CI/CD: GitHub Actions → ECR → ECS rolling deploy (blue/green via CodeDeploy).
```

The jump from ADR-001 is deliberate and incremental: containers move from one EC2 to
Fargate (no hosts to manage, real autoscaling), the data layer moves to managed
Multi-AZ services (real RPO/RTO), secrets move out of env files, and the edge gains
WAF protection — none of which changes a line of application code, because the app
was built twelve-factor from the start (config via env, stateless containers,
externalized state in Postgres/Redis).

That last property is the actual point of these three ADRs read together: **the
application is deployment-agnostic by design.** EC2-compose, Railway, and Fargate are
three points on the same cost/control curve, and moving between them has so far
required zero application changes.
