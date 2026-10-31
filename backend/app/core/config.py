from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# docker-compose passes these so a fresh clone starts without real secrets. They are rejected
# unless app_env is "local".
LOCAL_JWT_SECRET = "local-dev-only-jwt-secret-not-for-production-use"
LOCAL_STREAM_TOKEN_SECRET = "local-dev-only-stream-secret-not-for-production-use"
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Required values have no default so a missing variable fails at startup."""

    # The strict value is the default, so forgetting APP_ENV cannot accept a dev placeholder.
    app_env: Literal["local", "test", "production"] = "production"
    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_access_ttl_min: int = Field(default=15, gt=0)
    refresh_ttl_days: int = Field(default=30, gt=0)
    stream_token_secret: str
    stream_token_ttl_sec: int = Field(default=60, gt=0)
    # Empty means no Domain attribute on the refresh cookie (local development).
    cookie_domain: str = ""
    # Comma-separated in the environment; NoDecode stops pydantic-settings expecting JSON.
    cors_origins: Annotated[list[str], NoDecode] = []
    otp_provider: Literal["mock"] = "mock"
    otp_ttl_sec: int = Field(default=300, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    otp_resend_cooldown_sec: int = Field(default=30, gt=0)
    idempotency_ttl_sec: int = Field(default=86400, gt=0)
    db_pool_size: int = Field(default=10, gt=0)
    booking_rate_limit_per_hour: int = Field(default=20, gt=0)
    join_rate_limit_per_hour: int = Field(default=20, gt=0)
    sse_heartbeat_sec: int = Field(default=15, gt=0)
    sse_max_conn_per_user: int = Field(default=5, gt=0)
    sse_max_conn_per_instance: int = Field(default=1000, gt=0)
    sse_max_stream_sec: int = Field(default=1800, gt=0)  # hard cap; the client reconnects

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def check_secrets(self) -> Self:
        secrets = {"jwt_secret": self.jwt_secret, "stream_token_secret": self.stream_token_secret}
        for name, value in secrets.items():
            if len(value) < MIN_SECRET_LENGTH:
                raise ValueError(f"{name} must be at least {MIN_SECRET_LENGTH} characters")
            if self.app_env != "local" and value in (LOCAL_JWT_SECRET, LOCAL_STREAM_TOKEN_SECRET):
                raise ValueError(f"{name} is a local-development placeholder")
        if self.jwt_secret == self.stream_token_secret:
            # Different secrets keep an access token from ever passing as a stream token.
            raise ValueError("jwt_secret and stream_token_secret must differ")
        if "*" in self.cors_origins:
            raise ValueError("cors_origins must list exact origins, not *")
        if self.sse_max_stream_sec < 2 * self.sse_heartbeat_sec:
            raise ValueError("sse_max_stream_sec must be at least twice sse_heartbeat_sec")
        if self.otp_provider == "mock" and self.app_env == "production":
            raise ValueError("otp_provider mock is not allowed in production")
        return self
