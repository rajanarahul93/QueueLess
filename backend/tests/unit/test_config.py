import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import LOCAL_JWT_SECRET, LOCAL_STREAM_TOKEN_SECRET, Settings
from app.main import create_app
from tests.helpers import TEST_JWT_SECRET, TEST_STREAM_SECRET, make_settings

REQUIRED = {"database_url", "redis_url", "jwt_secret", "stream_token_secret"}


def test_missing_required_env_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name.upper(), raising=False)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert {e["loc"][0] for e in error.value.errors()} == REQUIRED


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("STREAM_TOKEN_SECRET", TEST_STREAM_SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    settings = Settings()
    assert settings.redis_url == "redis://h:6379/0"


def test_app_env_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("STREAM_TOKEN_SECRET", TEST_STREAM_SECRET)
    # Only production refuses the mock OTP provider, so this proves the default behaviourally.
    with pytest.raises(ValidationError, match="not allowed in production"):
        Settings(_env_file=None)


@pytest.mark.parametrize("field", ["jwt_secret", "stream_token_secret"])
@pytest.mark.parametrize("secret", ["", "x" * 31])
def test_short_or_blank_secret_is_rejected(field: str, secret: str) -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make_settings(**{field: secret})


def test_secrets_must_differ() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        make_settings(stream_token_secret=TEST_JWT_SECRET)


@pytest.mark.parametrize("app_env", ["production", "test"])
@pytest.mark.parametrize("field", ["jwt_secret", "stream_token_secret"])
def test_placeholder_secret_is_rejected_outside_local(app_env: str, field: str) -> None:
    placeholder = LOCAL_JWT_SECRET if field == "jwt_secret" else LOCAL_STREAM_TOKEN_SECRET
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(app_env=app_env, **{field: placeholder})


def test_placeholders_are_accepted_locally() -> None:
    settings = make_settings(
        app_env="local",
        jwt_secret=LOCAL_JWT_SECRET,
        stream_token_secret=LOCAL_STREAM_TOKEN_SECRET,
    )
    assert settings.app_env == "local"


def test_test_dummies_are_not_the_placeholders() -> None:
    assert {TEST_JWT_SECRET, TEST_STREAM_SECRET}.isdisjoint(
        {LOCAL_JWT_SECRET, LOCAL_STREAM_TOKEN_SECRET}
    )


def test_compose_passes_the_same_placeholders_as_settings() -> None:
    compose = (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()
    assert f"JWT_SECRET:-{LOCAL_JWT_SECRET}}}" in compose
    assert f"STREAM_TOKEN_SECRET:-{LOCAL_STREAM_TOKEN_SECRET}}}" in compose
    assert "APP_ENV: local" in compose


def test_every_published_compose_port_is_loopback_only() -> None:
    # The placeholder secrets are public, so no service may be reachable from the network.
    # Text parsing (no YAML dependency) is strict: an unrecognised ports form fails the test.
    compose = (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()
    ports: list[str] = []
    for line in compose.splitlines():
        if not re.match(r"^\s*ports:", line):
            continue
        inline = re.fullmatch(r'\s*ports:\s*\[("[^"]+"(?:\s*,\s*"[^"]+")*)\]\s*', line)
        assert inline, f"unrecognised ports form, extend this test: {line!r}"
        ports += re.findall(r'"([^"]+)"', inline.group(1))
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), ports
    assert {p.split(":")[1] for p in ports} >= {"8000", "3000"}  # api and web
    assert sum(":5432" in p for p in ports) == 1 and sum(":6379" in p for p in ports) == 1


def test_production_with_placeholder_secret_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:1/d")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("JWT_SECRET", LOCAL_JWT_SECRET)
    monkeypatch.setenv("STREAM_TOKEN_SECRET", LOCAL_STREAM_TOKEN_SECRET)
    with pytest.raises(ValidationError, match="placeholder"):
        create_app()  # no settings passed: read from the environment when the app is created


def test_cors_origins_are_comma_separated_and_never_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(cors_origins="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]
    assert make_settings(cors_origins="").cors_origins == []
    with pytest.raises(ValidationError, match="not \\*"):
        make_settings(cors_origins="*")


def test_mock_otp_provider_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="not allowed in production"):
        make_settings(app_env="production")


def test_pool_and_booking_limit_defaults_and_validation() -> None:
    settings = make_settings()
    assert (settings.db_pool_size, settings.booking_rate_limit_per_hour) == (10, 20)
    for name in ("db_pool_size", "booking_rate_limit_per_hour"):
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                make_settings(**{name: bad})
