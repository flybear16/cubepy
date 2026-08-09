"""Application settings loaded from environment / .env (CUBEPY_ prefix)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CUBEPY_", env_file=".env", extra="ignore"
    )

    # Auth
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    # PEM public key for RS256 verification (required when jwt_algorithm == "RS256").
    jwt_public_key: str | None = None

    # Data sources. Hologres speaks the Postgres wire protocol -> asyncpg driver.
    pg_dsn: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/cubepy"
    # Generic DSN override (any SQLAlchemy URL, e.g. duckdb:///path.duckdb).
    db_dsn: str | None = None

    # Cache
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 300

    # Subscribe default polling interval (seconds)
    default_refresh_every: int = 30

    # Pre-aggregation: off by default — rollup tables must be built (Phase 3) and
    # the matcher is fail-closed, so flipping this on never yields wrong results,
    # only falls back to the base cube if a rollup is missing.
    preagg_enabled: bool = False
    # When pre-aggregation is enabled, build every rollup once on startup before
    # serving, then refresh each on its refresh_key.every interval.
    preagg_refresh_on_start: bool = True

    log_level: str = "INFO"


settings = Settings()
