"""Application settings loaded from environment / .env (CUBEPY_ prefix)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUBEPY_", env_file=".env", extra="ignore")

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

    # AI ask layer (M2). OpenAI-compatible endpoint: DeepSeek / DashScope(qwen) /
    # OpenAI / local vLLM are pure config, no code change.
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 30.0
    # Feature flag: the /cubepy/v1/ask router is only mounted when enabled
    # (or when a test injects an LLM explicitly).
    ask_enabled: bool = False
    # Second LLM pass: one-line insight over the result rows (degrades silently).
    ask_interpret: bool = True
    # Append-only JSONL audit trail for every ask (question/query/rows/latency).
    ask_audit_log: str | None = None
    # Glossary as a dotted "module.ATTR" path, resolved per request. Swap the
    # domain without code changes (e.g. cubepy.samples.glossary_trade.TRADE_GLOSSARY).
    ask_glossary: str = "cubepy.samples.glossary.SAMPLE_GLOSSARY"

    log_level: str = "INFO"


settings = Settings()
