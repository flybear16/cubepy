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

    # Cache
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 300

    # Subscribe default polling interval (seconds)
    default_refresh_every: int = 30

    log_level: str = "INFO"


settings = Settings()
