from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str
    db_password: str

    # Redis
    redis_url: str

    # Flight Provider
    flight_provider: str = "cascade"
    serpapi_api_key: str = ""
    amadeus_api_key: str = ""
    amadeus_api_secret: str = ""
    apify_api_token: str = ""

    # LLM Provider
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    mistral_api_key: str = ""

    # App
    app_env: str = "development"
    allowed_origins: str = "http://localhost:3000"
    cache_ttl_hours: int = 6
    llm_cache_ttl_hours: int = 24
    max_airports_search: int = 300

    # Per-IP hourly rate limits on the public search endpoints.
    # Smart Multi-City is far more expensive (LLM call + many provider calls),
    # so its limit is stricter.
    rate_limit_reverse_hourly: int = 30
    rate_limit_smart_hourly: int = 10

    model_config = SettingsConfigDict(env_file=".env")


# Istanza globale usata in tutto il progetto
settings = Settings()
