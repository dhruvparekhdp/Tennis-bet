from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


def _make_url(raw: str) -> tuple[str, dict]:
    """
    Normalise a database URL for SQLAlchemy asyncpg:
    - Convert postgres:// / postgresql:// → postgresql+asyncpg://
    - Strip sslmode= query param (asyncpg rejects it) and convert to connect_args ssl=True
    Returns (url, connect_args).
    """
    connect_args: dict = {}

    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgresql://") and "+asyncpg" not in raw:
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)

    # asyncpg doesn't accept sslmode — strip it and pass ssl via connect_args
    if "sslmode=" in raw:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query, keep_blank_values=True)
        sslmode = params.pop("sslmode", ["require"])[0]
        if sslmode in ("require", "verify-ca", "verify-full"):
            connect_args["ssl"] = True
        new_query = urlencode({k: v[0] for k, v in params.items()})
        raw = urlunparse(parsed._replace(query=new_query))

    return raw, connect_args


_url, _connect_args = _make_url(settings.database_url)

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        yield session
