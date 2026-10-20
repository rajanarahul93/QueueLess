from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Required values have no default so a missing variable fails at startup."""

    database_url: str
    redis_url: str
