import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db.database import engine, Base
from app.db.redis import get_redis, close_redis
from app.api.v1.router import api_router
import app.models  # noqa: F401 — registra tutti i modelli con Base

logging.basicConfig(
    level=logging.DEBUG if settings.app_env == "development" else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)

###############---############
# REMEMBER TO SWITCH TO Alembic migrations IN PROD
###############---############
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    redis = await get_redis()
    await redis.ping()  # verifica connessione Redis all'avvio

    yield

    # Shutdown
    await close_redis()


app = FastAPI(
    title="HopCraft API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(api_router)


@app.get("/api/v1/health")
async def health(response: Response):
    db_ok = True
    redis_ok = True

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        redis_ok = False

    if not (db_ok and redis_ok):
        response.status_code = 503

    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "env": settings.app_env,
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
