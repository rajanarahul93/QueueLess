import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.v1 import appointments, auth, businesses, queues, reports, streams, tokens
from app.core.config import SCRUBBED_ENVS, Settings, configure_logging
from app.core.errors import register_error_handlers
from app.core.idempotency import Replay, handle_replay
from app.core.log_scrub import install_log_scrub
from app.core.readiness import ReadyOut, check_ready
from app.core.request_id import RequestIdMiddleware
from app.core.timezones import zone_names
from app.db.session import create_engine, create_session_factory
from app.realtime.hub import Hub
from app.realtime.sse import RedactStreamToken
from app.services.notifications import create_sms_provider


def create_app(settings: Settings | None = None, clock: Callable[[], float] = time.time) -> FastAPI:
    """Settings are read when the app is created, not on import; run it with uvicorn --factory.

    `clock` is injectable so tests control OTP expiry and rate-limit windows without sleeping.
    """
    resolved = settings or Settings()
    configure_logging(resolved.log_level)
    if resolved.app_env in SCRUBBED_ENVS:
        install_log_scrub()
    zone_names()  # a broken tzdata install must stop the app at startup
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactStreamToken) for f in access_log.filters):
        access_log.addFilter(RedactStreamToken())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved.database_url, resolved.db_pool_size)
        redis = Redis.from_url(resolved.redis_url)
        app.state.settings = resolved
        app.state.clock = clock
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        app.state.sms = create_sms_provider(resolved)
        hub = Hub(
            resolved.redis_url,
            max_per_user=resolved.sse_max_conn_per_user,
            max_total=resolved.sse_max_conn_per_instance,
        )
        await hub.start()
        app.state.hub = hub
        try:
            yield
        finally:
            await hub.close()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="QueueLess", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        # EventSource sends Last-Event-ID on reconnect; the web app and API are different origins.
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
        # Without these the browser hides them from the web app (OTP countdown, replay notice).
        expose_headers=["Retry-After", "Idempotent-Replayed"],
        # Browsers cache a preflight per URL; each stream URL carries a new stream_token, so
        # stream reconnects still pay one preflight. Other calls reuse it for 10 minutes.
        max_age=600,
    )
    app.add_middleware(RequestIdMiddleware)  # after CORS, so preflight replies carry an id too
    register_error_handlers(app)
    app.add_exception_handler(Replay, handle_replay)  # type: ignore[arg-type]
    app.include_router(auth.router, prefix="/v1")
    app.include_router(businesses.router, prefix="/v1")
    app.include_router(appointments.router, prefix="/v1")
    app.include_router(queues.router, prefix="/v1")
    app.include_router(tokens.router, prefix="/v1")
    app.include_router(streams.router, prefix="/v1")
    app.include_router(reports.router, prefix="/v1")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=ReadyOut, responses={503: {"model": ReadyOut}})
    async def readyz(request: Request, response: Response) -> ReadyOut:
        """Liveness is /healthz; this also needs Postgres, Redis and the worker's relay."""
        state = request.app.state
        result = await check_ready(state.engine, state.redis, state.clock())
        if result.status != "ok":
            response.status_code = 503
        return result

    return app
