# QueueLess

Multi-tenant live queue for service businesses: appointments and walk-in tokens in one queue, with real-time position and ETA.

## Local setup

Requires Docker. Optional for running checks outside Docker: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 24 with npm.

```text
cp .env.example .env            # fill in the blank POSTGRES_* values (and POSTGRES_HOST_PORT if 5432 is taken)
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up api worker web
# API: http://localhost:8000 (OpenAPI at /docs)   Web: http://localhost:3000
```

## Checks

```text
cd backend && uv sync && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest   # pytest needs Docker
cd web && npm ci && npm run lint && npm run typecheck && npm run build
```
