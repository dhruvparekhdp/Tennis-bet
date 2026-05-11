from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


def _make_url(raw: str) -> str:
    """
    Render (and most PaaS) provide DATABASE_URL as postgres:// or postgresql://.
    SQLAlchemy async needs postgresql+asyncpg://.
    SQLite stays as-is (sqlite+aiosqlite://...).
    """
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgresql://") and "+asyncpg" not in raw:
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return raw


_url = _make_url(settings.database_url)

engine = create_async_engine(
    _url,
    echo=False,
    # PostgreSQL: use a connection pool; SQLite: single connection
    pool_pre_ping=True,
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
