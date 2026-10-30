import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Codes for Starlette's own HTTP errors (unknown route, wrong method, ...).
HTTP_ERROR_CODES = {
    401: ("UNAUTHENTICATED", "Unauthenticated"),
    403: ("FORBIDDEN", "Forbidden"),
    404: ("NOT_FOUND", "Not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
}


class DomainError(Exception):
    """Base for typed service errors; each subclass maps to one stable code (R4.1)."""

    code = "INTERNAL_ERROR"
    status = 500
    title = "Internal error"

    def __init__(self, detail: str | None = None, **extra: object) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra
        self.headers: dict[str, str] = {}


class Unauthenticated(DomainError):
    code = "UNAUTHENTICATED"
    status = 401
    title = "Unauthenticated"


class Forbidden(DomainError):
    code = "FORBIDDEN"
    status = 403
    title = "Forbidden"


class NotFound(DomainError):
    code = "NOT_FOUND"
    status = 404
    title = "Not found"


class ValidationFailed(DomainError):
    """A rule the schema cannot express (e.g. a duplicate email); same shape as a 422."""

    code = "VALIDATION_ERROR"
    status = 422
    title = "Validation error"

    def __init__(self, loc: list[str], msg: str) -> None:
        super().__init__("Request validation failed", errors=[{"loc": loc, "msg": msg}])


class RateLimited(DomainError):
    code = "RATE_LIMITED"
    status = 429
    title = "Too many requests"

    def __init__(self, retry_after_sec: int) -> None:
        super().__init__()
        self.headers["Retry-After"] = str(retry_after_sec)


class IdempotencyConflict(DomainError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409
    title = "Idempotency conflict"

    def __init__(self, detail: str, *, in_progress: bool = False) -> None:
        super().__init__(detail, **({"in_progress": True} if in_progress else {}))
        if in_progress:
            self.headers["Retry-After"] = "1"


class ServiceUnavailable(DomainError):
    code = "SERVICE_UNAVAILABLE"
    status = 503
    title = "Service unavailable"

    def __init__(self, detail: str | None = None, *, retry_after_sec: int | None = None) -> None:
        super().__init__(detail)
        if retry_after_sec is not None:
            self.headers["Retry-After"] = str(retry_after_sec)


class SlotTaken(DomainError):
    code = "SLOT_TAKEN"
    status = 409
    title = "Slot taken"

    def __init__(self, next_slots: list[dict[str, object]]) -> None:
        super().__init__("That time was just booked", next_slots=next_slots)


class InvalidTransition(DomainError):
    code = "INVALID_TRANSITION"
    status = 409
    title = "Invalid transition"


class BookingRestricted(DomainError):
    code = "BOOKING_RESTRICTED"
    status = 403
    title = "Booking restricted"


class BookingLimitReached(DomainError):
    code = "BOOKING_LIMIT_REACHED"
    status = 409
    title = "Booking limit reached"


class OtpInvalid(DomainError):
    code = "OTP_INVALID"
    status = 400
    title = "Invalid code"


class OtpExpired(DomainError):
    code = "OTP_EXPIRED"
    status = 400
    title = "Code expired"


def problem_response(
    status: int,
    title: str,
    code: str,
    detail: str,
    extra: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        **(extra or {}),
    }
    return JSONResponse(
        body, status_code=status, media_type="application/problem+json", headers=headers
    )


async def handle_domain_error(request: Request, error: DomainError) -> JSONResponse:
    return problem_response(
        error.status, error.title, error.code, error.detail, error.extra, error.headers
    )


async def handle_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    # Pydantic's `input` is dropped on purpose: it would echo submitted passwords and codes back.
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in error.errors()]
    return problem_response(
        422, "Validation error", "VALIDATION_ERROR", "Request validation failed", {"errors": errors}
    )


async def handle_http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code, title = HTTP_ERROR_CODES.get(
        error.status_code,
        ("VALIDATION_ERROR", "Bad request")
        if error.status_code < 500
        else ("INTERNAL_ERROR", "Internal error"),
    )
    detail = str(error.detail) if error.status_code < 500 else title
    return problem_response(error.status_code, title, code, detail, headers=error.headers)


async def handle_unhandled_error(request: Request, error: Exception) -> JSONResponse:
    # The response never carries the exception text, SQL or a stack trace (R4.6). The request id
    # is a placeholder until the Phase 7 middleware assigns one.
    request_id = request.headers.get("x-request-id", "-")
    logger.error(
        "unhandled error request_id=%s %s %s",
        request_id,
        request.method,
        request.url.path,
        exc_info=error,
    )
    return problem_response(500, "Internal error", "INTERNAL_ERROR", "An unexpected error occurred")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, handle_domain_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unhandled_error)
