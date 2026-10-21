from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from notifications.core.retry import parse_schedule


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", pattern="^(development|test|staging|production)$")
    log_level: str = "INFO"
    database_url: str = "sqlite:///./notifications.db"
    admin_api_key: str | None = None

    worker_enabled: bool = True
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_batch_size: int = Field(default=50, ge=1, le=1000)
    retry_schedule_seconds: str = "30,120,600,1800"
    max_attempts: int = Field(default=5, ge=1, le=50)
    digest_sweep_seconds: float = Field(default=30.0, gt=0)

    default_rate_limit_per_minute: int = Field(default=600, ge=1)
    default_timezone: str = "Asia/Dhaka"
    default_locale: str = Field(default="en", pattern="^(en|bn)$")

    email_providers: str = "console"
    sms_providers: str = "console"
    push_providers: str = "file"
    push_file_path: str = "./push-outbox.jsonl"
    provider_failure_threshold: int = Field(default=3, ge=1)
    provider_cooldown_seconds: float = Field(default=60.0, gt=0)

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@example.com"

    bd_sms_api_url: str | None = None
    bd_sms_api_key: str | None = None
    bd_sms_sender_id: str | None = None

    fcm_project_id: str | None = None
    fcm_access_token: str | None = None

    public_base_url: str = "http://localhost:8000"
    webhook_timeout_seconds: float = Field(default=5.0, gt=0)

    @field_validator("retry_schedule_seconds")
    @classmethod
    def _valid_schedule(cls, value: str) -> str:
        parse_schedule(value)
        return value

    @property
    def retry_schedule(self) -> tuple[int, ...]:
        return parse_schedule(self.retry_schedule_seconds)

    def providers_for(self, channel: str) -> list[str]:
        raw = {
            "email": self.email_providers,
            "sms": self.sms_providers,
            "push": self.push_providers,
        }.get(channel, "")
        return [p.strip() for p in raw.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
