from app.core.config import Settings

TEST_JWT_SECRET = "test-jwt-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
TEST_STREAM_SECRET = "test-stream-secret-0123456789abcdef0123456789abcdef01234567"


def make_settings(**overrides: object) -> Settings:
    """Hermetic: model_validate skips the environment, so no .env is needed."""
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://test:test@localhost:1/test",
        "redis_url": "redis://localhost:1/0",
        "jwt_secret": TEST_JWT_SECRET,
        "stream_token_secret": TEST_STREAM_SECRET,
    }
    return Settings.model_validate(values | overrides)
