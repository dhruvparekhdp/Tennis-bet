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
    # Import for the side effect of registering every model on Base.metadata.
    # Without it create_all sees an empty metadata and silently creates
    # nothing — the tables then appear to be missing at query time, which is
    # a confusing way to discover an import-order problem.
    import storage.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_columns(conn)


async def _migrate_columns(conn) -> None:
    """
    Idempotent column migrations — ADD COLUMN IF NOT EXISTS for every column
    added after the initial table creation. Safe to run on every startup.
    PostgreSQL 9.6+ supports IF NOT EXISTS on ADD COLUMN.
    """
    migrations = [
        # signal_log columns added in data-collection PR
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS model_win_prob FLOAT DEFAULT 0.0",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS score_at_signal VARCHAR DEFAULT ''",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS sets_p1_at_signal INTEGER DEFAULT 0",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS sets_p2_at_signal INTEGER DEFAULT 0",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS games_p1_at_signal INTEGER DEFAULT 0",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS games_p2_at_signal INTEGER DEFAULT 0",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS outcome VARCHAR DEFAULT 'pending'",
        "ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS match_winner INTEGER DEFAULT 0",
        # match_records table — bulk historical data from Sackmann ATP/WTA CSVs
        """CREATE TABLE IF NOT EXISTS match_records (
            id SERIAL PRIMARY KEY,
            tour VARCHAR, year INTEGER, tourney_id VARCHAR DEFAULT '',
            tourney_name VARCHAR, surface VARCHAR, tourney_level VARCHAR DEFAULT '',
            round VARCHAR DEFAULT '', best_of INTEGER DEFAULT 3,
            winner_name VARCHAR, loser_name VARCHAR,
            winner_rank INTEGER DEFAULT 0, loser_rank INTEGER DEFAULT 0,
            score VARCHAR DEFAULT '', minutes INTEGER DEFAULT 0,
            w_ace INTEGER DEFAULT 0, w_df INTEGER DEFAULT 0,
            w_svpt INTEGER DEFAULT 0, w_1st_in INTEGER DEFAULT 0,
            w_1st_won INTEGER DEFAULT 0, w_2nd_won INTEGER DEFAULT 0,
            w_svc_games INTEGER DEFAULT 0, w_bp_saved INTEGER DEFAULT 0,
            w_bp_faced INTEGER DEFAULT 0,
            l_ace INTEGER DEFAULT 0, l_df INTEGER DEFAULT 0,
            l_svpt INTEGER DEFAULT 0, l_1st_in INTEGER DEFAULT 0,
            l_1st_won INTEGER DEFAULT 0, l_2nd_won INTEGER DEFAULT 0,
            l_svc_games INTEGER DEFAULT 0, l_bp_saved INTEGER DEFAULT 0,
            l_bp_faced INTEGER DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS ix_mr_winner ON match_records (winner_name)",
        "CREATE INDEX IF NOT EXISTS ix_mr_loser ON match_records (loser_name)",
        "CREATE INDEX IF NOT EXISTS ix_mr_year_surface ON match_records (year, surface)",
        # slam_points table — point-by-point grand slam data
        """CREATE TABLE IF NOT EXISTS slam_points (
            id SERIAL PRIMARY KEY,
            slam VARCHAR, year INTEGER, match_id VARCHAR,
            player1 VARCHAR DEFAULT '', player2 VARCHAR DEFAULT '',
            set_no INTEGER, game_no INTEGER, point_no INTEGER,
            server INTEGER, point_winner INTEGER,
            p1_score VARCHAR DEFAULT '', p2_score VARCHAR DEFAULT '',
            p1_games INTEGER DEFAULT 0, p2_games INTEGER DEFAULT 0,
            p1_sets INTEGER DEFAULT 0, p2_sets INTEGER DEFAULT 0,
            is_break_point BOOLEAN DEFAULT FALSE,
            is_set_point BOOLEAN DEFAULT FALSE,
            is_match_point BOOLEAN DEFAULT FALSE,
            p1_ace BOOLEAN DEFAULT FALSE, p2_ace BOOLEAN DEFAULT FALSE,
            p1_double_fault BOOLEAN DEFAULT FALSE, p2_double_fault BOOLEAN DEFAULT FALSE,
            serve_no INTEGER DEFAULT 1, rally_length INTEGER DEFAULT 0,
            game_winner INTEGER DEFAULT 0, set_winner INTEGER DEFAULT 0,
            match_winner INTEGER DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sp_match ON slam_points (match_id)",
        "CREATE INDEX IF NOT EXISTS ix_sp_slam_year ON slam_points (slam, year)",
        # crypto_snapshots table
        """CREATE TABLE IF NOT EXISTS crypto_snapshots (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, price FLOAT, volume_24h FLOAT DEFAULT 0.0,
            rsi_14 FLOAT DEFAULT 50.0, macd_line FLOAT DEFAULT 0.0, macd_signal FLOAT DEFAULT 0.0,
            bollinger_upper FLOAT DEFAULT 0.0, bollinger_lower FLOAT DEFAULT 0.0,
            atr_14 FLOAT DEFAULT 0.0, sentiment_score FLOAT DEFAULT 0.0,
            price_30m_later FLOAT DEFAULT 0.0, price_1h_later FLOAT DEFAULT 0.0,
            price_4h_later FLOAT DEFAULT 0.0, price_1d_later FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_cs_symbol_ts ON crypto_snapshots (symbol, timestamp)",
        # commodity_snapshots table
        """CREATE TABLE IF NOT EXISTS commodity_snapshots (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, price FLOAT, rsi_14 FLOAT DEFAULT 50.0, atr_14 FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_comms_symbol_ts ON commodity_snapshots (symbol, timestamp)",
        # crypto_signal_log table
        """CREATE TABLE IF NOT EXISTS crypto_signal_log (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR, signal_type VARCHAR, direction VARCHAR,
            trigger_description TEXT, confidence FLOAT, current_price FLOAT,
            target_price FLOAT DEFAULT 0.0, stop_loss FLOAT DEFAULT 0.0,
            edge_pct FLOAT, stake_pct FLOAT, timeframe VARCHAR,
            sentiment_score FLOAT DEFAULT 0.0, indicators_summary VARCHAR DEFAULT '',
            outcome VARCHAR DEFAULT 'pending', pnl_pct FLOAT DEFAULT 0.0,
            timestamp TIMESTAMP
        )""",
        # crypto_watchlist table — symbols to stream, DB-backed instead of an env var
        """CREATE TABLE IF NOT EXISTS crypto_watchlist (
            symbol VARCHAR PRIMARY KEY,
            added_at TIMESTAMP
        )""",
        # Purge any legacy corrupted signals with invalid entry prices or astronomical moves
        """DELETE FROM crypto_signal_log 
           WHERE current_price <= 0.001 
              OR target_price <= 0 
              OR stop_loss <= 0 
              OR ABS(pnl_pct) > 500 
              OR ABS(target_price - current_price) / NULLIF(current_price, 0) > 2.0""",
    ]
    for sql in migrations:
        try:
            await conn.execute(__import__("sqlalchemy").text(sql))
        except Exception:
            pass  # column may already exist on fresh DBs — silently skip


async def get_session() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        yield session
