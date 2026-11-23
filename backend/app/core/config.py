import logging
import re
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

from app.core.request_id import RequestIdFilter, RequestIdFormatter

# docker-compose passes these so a fresh clone starts without real secrets. They are rejected
# unless app_env is "local".
LOCAL_JWT_SECRET = "local-dev-only-jwt-secret-not-for-production-use"
LOCAL_STREAM_TOKEN_SECRET = "local-dev-only-stream-secret-not-for-production-use"
MIN_SECRET_LENGTH = 32
# The login limits in force before they were configurable. Outside local and test a
# deployment may lower them, never raise them.
LOGIN_IP_LIMIT_CEILING = 20
LOGIN_EMAIL_LIMIT_CEILING = 5
LOGIN_LIMIT_WINDOW_SEC = 900
APP_LOG_HANDLER = "queueless"
# The one header name that must never be trusted for the client address: any client can send it.
UNTRUSTED_CLIENT_IP_HEADER = "x-forwarded-for"
# Which OTP provider each environment may use. The mock prints codes, so only local and test get
# it; the demo provider shows the code to the caller, so only demo gets it. Production has no
# entry: it stays unable to start until a real provider exists.
OTP_PROVIDER_FOR_ENV = {"local": "mock", "test": "mock", "demo": "demo"}
# Environments where logs must not carry OTP codes, phone numbers or emails. Local and test keep
# the mock provider's code line, which the end-to-end suite reads.
SCRUBBED_ENVS = ("demo", "production")


class Settings(BaseSettings):
    """Required values have no default so a missing variable fails at startup."""

    # The strict value is the default, so forgetting APP_ENV cannot accept a dev placeholder.
    app_env: Literal["local", "test", "demo", "production"] = "production"
    # Level of the app's own log lines (app.*); uvicorn and arq set their own. Any case accepted.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str
    redis_url: str
    jwt_secret: str
    jwt_access_ttl_min: int = Field(default=15, gt=0)
    refresh_ttl_days: int = Field(default=30, gt=0)
    # A rotated refresh token presented again this soon gets its successor again; 0 turns it off.
    refresh_reuse_grace_sec: int = Field(default=10, ge=0, le=30)
    stream_token_secret: str
    stream_token_ttl_sec: int = Field(default=60, gt=0)
    # Empty means no Domain attribute on the refresh cookie (local development).
    cookie_domain: str = ""
    # Comma-separated in the environment; NoDecode stops pydantic-settings expecting JSON.
    cors_origins: Annotated[list[str], NoDecode] = []
    # Empty means the address of the connection itself. Set it only behind a proxy that
    # OVERWRITES this header on every request, for example x-real-ip: a client can send any value
    # of a header the proxy passes through, and could then pick its own rate-limit bucket.
    client_ip_header: str = ""
    otp_provider: Literal["mock", "demo"] = "mock"
    otp_ttl_sec: int = Field(default=300, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    otp_resend_cooldown_sec: int = Field(default=30, gt=0)
    idempotency_ttl_sec: int = Field(default=86400, gt=0)
    db_pool_size: int = Field(default=10, gt=0)
    booking_rate_limit_per_hour: int = Field(default=20, gt=0)
    # Per IP and per email address, each over LOGIN_LIMIT_WINDOW_SEC.
    login_ip_limit: int = Field(default=LOGIN_IP_LIMIT_CEILING, gt=0)
    login_email_limit: int = Field(default=LOGIN_EMAIL_LIMIT_CEILING, gt=0)
    join_rate_limit_per_hour: int = Field(default=20, gt=0)
    sse_heartbeat_sec: int = Field(default=15, gt=0)
    sse_max_conn_per_user: int = Field(default=5, gt=0)
    sse_max_conn_per_instance: int = Field(default=1000, gt=0)
    sse_max_stream_sec: int = Field(default=1800, gt=0)  # hard cap; the client reconnects
    eta_cache_ttl_sec: int = Field(default=5, gt=0)  # every queue event also changes the key

    @field_validator("log_level", mode="before")
    @classmethod
    def upper_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("client_ip_header")
    @classmethod
    def plain_header_name(cls, value: str) -> str:
        if value == "":
            return value
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
            raise ValueError("client_ip_header must be a plain HTTP header name")
        if value.lower() == UNTRUSTED_CLIENT_IP_HEADER:
            raise ValueError("client_ip_header cannot be x-forwarded-for: any client can set it")
        return value.lower()

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
        if self.app_env == "demo" and not self.cors_origins:
            raise ValueError("cors_origins must name the web app's origin in demo")
        if self.sse_max_stream_sec < 2 * self.sse_heartbeat_sec:
            raise ValueError("sse_max_stream_sec must be at least twice sse_heartbeat_sec")
        if self.app_env not in ("local", "test"):
            ceilings = {
                "login_ip_limit": (self.login_ip_limit, LOGIN_IP_LIMIT_CEILING),
                "login_email_limit": (self.login_email_limit, LOGIN_EMAIL_LIMIT_CEILING),
            }
            for field, (configured, ceiling) in ceilings.items():
                if configured > ceiling:
                    raise ValueError(f"{field} can only be lowered outside local and test")
        if OTP_PROVIDER_FOR_ENV.get(self.app_env) != self.otp_provider:
            raise ValueError(f"otp_provider {self.otp_provider} is not allowed in {self.app_env}")
        return self


def configure_logging(level: str) -> None:
    """Prints the app's own log lines (app.*) on stderr, in one format for the API and the worker.

    Only the `app` logger gets a handler: uvicorn and arq keep theirs, so no line is printed
    twice. A second call in one process (tests, a reload) only changes the level. A line written
    while a request runs ends with its request id.
    """
    app_logger = logging.getLogger("app")
    if not any(handler.get_name() == APP_LOG_HANDLER for handler in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name(APP_LOG_HANDLER)
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(RequestIdFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(level)
