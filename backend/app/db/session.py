from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    """Called once at startup and injected (R1.3); there is deliberately no module-level engine."""
    # hide_parameters: bound values (phones, emails, hashes) stay out of logged exception text.
    return create_async_engine(url, pool_size=pool_size, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: services read attributes after commit without a lazy load.
    return async_sessionmaker(engine, expire_on_commit=False)
