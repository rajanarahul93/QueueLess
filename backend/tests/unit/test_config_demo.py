"""APP_ENV=demo and the demo OTP provider: every environment x provider combination."""

import itertools
import secrets

import pytest
from pydantic import ValidationError

from app.core.config import LOCAL_JWT_SECRET, LOCAL_STREAM_TOKEN_SECRET, Settings
from tests.helpers import make_settings

ENVIRONMENTS = ["local", "test", "demo", "production"]
PROVIDERS = ["mock", "demo"]
# The mock provider prints codes, so only local and test may use it. The demo provider shows the
# code to the caller, so only demo may use it. Production accepts neither, which means a
# production start stays refused until a real provider exists.
ALLOWED = {("local", "mock"), ("test", "mock"), ("demo", "demo")}
ORIGINS = ["https://app.example.test"]


@pytest.mark.parametrize(("app_env", "provider"), list(itertools.product(ENVIRONMENTS, PROVIDERS)))
def test_every_environment_and_provider_combination(app_env: str, provider: str) -> None:
    def build() -> Settings:
        return make_settings(app_env=app_env, otp_provider=provider, cors_origins=ORIGINS)

    if (app_env, provider) in ALLOWED:
        assert build().otp_provider == provider
    else:
        with pytest.raises(ValidationError, match="otp_provider"):
            build()


def test_the_default_provider_is_not_valid_in_demo() -> None:
    # Demo needs OTP_PROVIDER=demo set on purpose; forgetting it can never show a code.
    with pytest.raises(ValidationError, match="otp_provider"):
        make_settings(app_env="demo", cors_origins=ORIGINS)


def test_production_still_refuses_to_start_with_either_provider() -> None:
    for provider in PROVIDERS:
        with pytest.raises(ValidationError, match="otp_provider"):
            make_settings(app_env="production", otp_provider=provider, cors_origins=ORIGINS)


@pytest.mark.parametrize("field", ["jwt_secret", "stream_token_secret"])
def test_demo_rejects_the_local_placeholder_secrets(field: str) -> None:
    placeholder = LOCAL_JWT_SECRET if field == "jwt_secret" else LOCAL_STREAM_TOKEN_SECRET
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(
            app_env="demo", otp_provider="demo", cors_origins=ORIGINS, **{field: placeholder}
        )


def test_demo_requires_explicit_cors_origins() -> None:
    with pytest.raises(ValidationError, match="cors_origins"):
        make_settings(app_env="demo", otp_provider="demo", cors_origins=[])


def test_demo_never_accepts_a_wildcard_origin() -> None:
    with pytest.raises(ValidationError, match="cors_origins"):
        make_settings(app_env="demo", otp_provider="demo", cors_origins=["*"])


@pytest.mark.parametrize(("field", "raised"), [("login_ip_limit", 21), ("login_email_limit", 6)])
def test_demo_cannot_raise_a_login_limit(field: str, raised: int) -> None:
    with pytest.raises(ValidationError, match="can only be lowered"):
        make_settings(app_env="demo", otp_provider="demo", cors_origins=ORIGINS, **{field: raised})


@pytest.mark.parametrize("field", ["login_ip_limit", "login_email_limit"])
def test_demo_may_lower_a_login_limit_like_production(field: str) -> None:
    settings = make_settings(
        app_env="demo", otp_provider="demo", cors_origins=ORIGINS, **{field: 2}
    )
    assert getattr(settings, field) == 2


def test_demo_keeps_every_other_default() -> None:
    settings = make_settings(app_env="demo", otp_provider="demo", cors_origins=ORIGINS)
    assert (settings.otp_ttl_sec, settings.otp_max_attempts, settings.otp_resend_cooldown_sec) == (
        300,
        5,
        30,
    )
    assert settings.login_ip_limit == 20 and settings.login_email_limit == 5


def test_the_demo_environment_reads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "APP_ENV": "demo",
        "OTP_PROVIDER": "demo",
        "CORS_ORIGINS": "https://app.example.test",
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "REDIS_URL": "redis://h/0",
        "JWT_SECRET": secrets.token_hex(32),
        "STREAM_TOKEN_SECRET": secrets.token_hex(32),
    }.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert (settings.app_env, settings.otp_provider) == ("demo", "demo")
