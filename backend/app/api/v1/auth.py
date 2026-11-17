from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.dependencies import (
    get_clock,
    get_redis,
    get_session,
    get_settings,
    get_sms_provider,
    require_trusted_origin,
)
from app.core.errors import Unauthenticated
from app.core.rate_limit import (
    RateLimiter,
    get_rate_limiter,
    login_ip_rate_limit,
    rate_limit,
)
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    OtpRequest,
    OtpVerify,
    RegisterRequest,
    RegisterResponse,
    UserOut,
)
from app.services import auth as auth_service
from app.services.auth import AuthResult
from app.services.notifications import SmsProvider

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/v1/auth"

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Callable[[], float], Depends(get_clock)]
RedisDep = Annotated[Redis, Depends(get_redis)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
SmsDep = Annotated[SmsProvider, Depends(get_sms_provider)]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE)]


def _set_refresh_cookie(response: Response, settings: Settings, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_ttl_days * 86400,
        path=COOKIE_PATH,
        domain=settings.cookie_domain or None,
        secure=settings.app_env != "local",
        httponly=True,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    # Browsers only delete a cookie when path, domain, secure and samesite match the original.
    response.delete_cookie(
        REFRESH_COOKIE,
        path=COOKIE_PATH,
        domain=settings.cookie_domain or None,
        secure=settings.app_env != "local",
        httponly=True,
        samesite="lax",
    )


def _auth_response(response: Response, settings: Settings, result: AuthResult) -> AuthResponse:
    _set_refresh_cookie(response, settings, result.refresh_token)
    return AuthResponse(access_token=result.access_token, user=UserOut.model_validate(result.user))


@router.post(
    "/otp/request",
    status_code=202,
    dependencies=[Depends(rate_limit("otp_request_ip", limit=20, window_sec=3600))],
)
async def request_otp(
    body: OtpRequest,
    redis: RedisDep,
    limiter: LimiterDep,
    sms: SmsDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> dict[str, str]:
    await auth_service.request_otp(redis, limiter, sms, settings, clock, body.phone)
    return {}


@router.post(
    "/otp/verify",
    dependencies=[Depends(rate_limit("otp_verify_ip", limit=30, window_sec=600))],
)
async def verify_otp(
    body: OtpVerify,
    response: Response,
    session: SessionDep,
    redis: RedisDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> AuthResponse:
    result = await auth_service.verify_otp(
        session, redis, settings, clock, body.phone, body.code, body.name
    )
    return _auth_response(response, settings, result)


@router.post(
    "/register",
    status_code=201,
    dependencies=[Depends(rate_limit("register_ip", limit=10, window_sec=3600))],
)
async def register(body: RegisterRequest, session: SessionDep) -> RegisterResponse:
    user = await auth_service.register_owner(session, body.name, body.email, body.password)
    return RegisterResponse(user=UserOut.model_validate(user))


@router.post(
    "/login",
    dependencies=[Depends(login_ip_rate_limit)],
)
async def login(
    body: LoginRequest,
    response: Response,
    session: SessionDep,
    limiter: LimiterDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> AuthResponse:
    result = await auth_service.login(session, limiter, settings, clock, body.email, body.password)
    return _auth_response(response, settings, result)


@router.post("/refresh", dependencies=[Depends(require_trusted_origin)])
async def refresh(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    refresh_token: RefreshCookie = None,
) -> AuthResponse:
    try:
        if refresh_token is None:
            raise Unauthenticated("Invalid refresh token")
        result = await auth_service.refresh_session(session, settings, clock, refresh_token)
    except Unauthenticated as error:
        cleared = Response()
        _clear_refresh_cookie(cleared, settings)
        error.headers["Set-Cookie"] = cleared.headers["set-cookie"]
        raise
    return _auth_response(response, settings, result)


@router.post("/logout", status_code=204, dependencies=[Depends(require_trusted_origin)])
async def logout(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
    refresh_token: RefreshCookie = None,
) -> None:
    if refresh_token is not None:
        await auth_service.revoke_session(
            session, refresh_token, datetime.fromtimestamp(clock(), UTC)
        )
    _clear_refresh_cookie(response, settings)
