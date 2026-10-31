from sqlalchemy.exc import DBAPIError


def driver_error(error: DBAPIError) -> tuple[str | None, str | None]:
    """(SQLSTATE, constraint name) from the database driver's error, never from message text."""
    cause = error.orig.__cause__ if error.orig is not None else None
    return getattr(cause, "sqlstate", None), getattr(cause, "constraint_name", None)
