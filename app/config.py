from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Bootstrap config — only what's needed before Redis is available.
    Set these as environment variables (or in .env for local dev).

    Everything written by the setup endpoint (API keys, plan codes, Paddle
    credentials, etc.) is stored in Redis via app.utils.config_store.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Redis — required for the config store, Celery broker, and idempotency
    redis_url: str = "redis://localhost:6379/0"

    # HTTP server
    port: int = 3000
    middleware_url: str = "http://localhost:3000"  # public URL, used by setup UI default

    # Logging / concurrency
    log_level: str = "info"
    worker_concurrency: int = 10


settings = Settings()
