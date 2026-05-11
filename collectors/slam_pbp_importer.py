"""
Slam point-by-point importer — Jeff Sackmann's tennis_slam_pointbypoint repo.

Downloads point-by-point CSVs for all four Grand Slams from 2011–2024.
Each row is one point with full context: server, score, break/set/match point,
aces, double faults, serve number, rally length.

Stores into the slam_points table — ~2–3 million rows total.
This is the richest dataset for momentum and psychological pattern analysis.

Data source: https://github.com/JeffSackmann/tennis_slam_pointbypoint
License: CC BY-NC-SA 4.0 — non-commercial use.
"""
from __future__ import annotations

import csv
import io

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import SlamPoint

log = structlog.get_logger()

_GITHUB_RAW = "https://raw.githubusercontent.com/JeffSackmann/tennis_slam_pointbypoint/master"

_SLAMS = {
    "ausopen":    "hard",
    "frenchopen": "clay",
    "wimbledon":  "grass",
    "usopen":     "hard",
}

# Point-by-point data starts from 2011
_YEARS = list(range(2011, 2025))


def _b(val: str) -> bool:
    """Parse a 0/1 string as bool."""
    try:
        return bool(int(val)) if val and val.strip() else False
    except (ValueError, TypeError):
        return False


def _i(val: str, default: int = 0) -> int:
    try:
        return int(float(val)) if val and val.strip() else default
    except (ValueError, TypeError):
        return default


def _parse_point(row: dict, slam: str, year: int,
                 player_map: dict[str, tuple[str, str]]) -> SlamPoint | None:
    match_id = row.get("match_id", "").strip()
    if not match_id:
        return None

    set_no = _i(row.get("SetNo", row.get("set_no", "0")))
    game_no = _i(row.get("GameNo", row.get("game_no", "0")))
    point_no = _i(row.get("PointNo", row.get("point_no", "0")))

    # Server: PointServer 1=player1, 2=player2
    server = _i(row.get("PointServer", row.get("server", "1")))

    # Who won: determine from P1Score/P2Score progression or GameWinner
    game_winner = _i(row.get("GameWinner", "0"))
    set_winner = _i(row.get("SetWinner", "0"))
    match_winner = _i(row.get("MatchWinner", row.get("match_winner", "0")))

    # Point winner — Sackmann uses 'PointWon' (0=server lost, 1=server won)
    # or sometimes directly a 'Winner' column
    point_won_by_server = _b(row.get("PointWon", row.get("won", "1")))
    point_winner = server if point_won_by_server else (2 if server == 1 else 1)

    p1_score = row.get("P1Score", row.get("p1_score", ""))
    p2_score = row.get("P2Score", row.get("p2_score", ""))
    p1_games = _i(row.get("P1GamesWon", row.get("game1", "0")))
    p2_games = _i(row.get("P2GamesWon", row.get("game2", "0")))
    p1_sets = _i(row.get("P1SetsWon", row.get("set1", "0")))
    p2_sets = _i(row.get("P2SetsWon", row.get("set2", "0")))

    # Break/set/match point flags
    p1_bp = _b(row.get("P1BreakPt", "0"))
    p2_bp = _b(row.get("P2BreakPt", "0"))
    is_break_point = p1_bp or p2_bp

    p1_sp = _b(row.get("P1SetPt", "0"))
    p2_sp = _b(row.get("P2SetPt", "0"))
    is_set_point = p1_sp or p2_sp

    p1_mp = _b(row.get("P1MatchPt", "0"))
    p2_mp = _b(row.get("P2MatchPt", "0"))
    is_match_point = p1_mp or p2_mp

    p1, p2 = player_map.get(match_id, ("", ""))

    return SlamPoint(
        slam=slam,
        year=year,
        match_id=match_id,
        player1=p1,
        player2=p2,
        set_no=set_no,
        game_no=game_no,
        point_no=point_no,
        server=server,
        point_winner=point_winner,
        p1_score=p1_score,
        p2_score=p2_score,
        p1_games=p1_games,
        p2_games=p2_games,
        p1_sets=p1_sets,
        p2_sets=p2_sets,
        is_break_point=is_break_point,
        is_set_point=is_set_point,
        is_match_point=is_match_point,
        p1_ace=_b(row.get("P1Ace", "0")),
        p2_ace=_b(row.get("P2Ace", "0")),
        p1_double_fault=_b(row.get("P1DoubleFault", "0")),
        p2_double_fault=_b(row.get("P2DoubleFault", "0")),
        serve_no=_i(row.get("ServeNo", row.get("serve_no", "1")), 1),
        rally_length=_i(row.get("RallyCount", row.get("rally_length", "0"))),
        game_winner=game_winner,
        set_winner=set_winner,
        match_winner=match_winner,
    )


async def _download(client: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        resp = await client.get(url, timeout=60.0)
        if resp.status_code == 404:
            log.debug("slam_pbp_not_found", url=url.split("/")[-1])
            return []
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    except Exception:
        log.exception("slam_pbp_download_failed", url=url.split("/")[-1])
        return []


async def _load_player_map(client: httpx.AsyncClient, slam: str, year: int) -> dict[str, tuple[str, str]]:
    """
    Load match metadata file to map match_id → (player1, player2).
    Sackmann names these {year}-{slam}-matches.csv.
    """
    url = f"{_GITHUB_RAW}/{year}-{slam}-matches.csv"
    rows = await _download(client, url)
    player_map: dict[str, tuple[str, str]] = {}
    for row in rows:
        mid = row.get("match_id", "").strip()
        # Column names vary slightly — try common variants
        p1 = (row.get("player1", "") or row.get("Player1", "") or "").strip()
        p2 = (row.get("player2", "") or row.get("Player2", "") or "").strip()
        if mid:
            player_map[mid] = (p1, p2)
    return player_map


async def run_slam_import(session: AsyncSession, force: bool = False) -> None:
    """
    Download slam point-by-point CSVs and insert into slam_points table.
    Skips if table already has data (unless force=True).
    """
    if not force:
        try:
            result = await session.execute(
                text("SELECT COUNT(*) FROM slam_points LIMIT 1")
            )
            count = result.scalar() or 0
            if count > 0:
                log.info("slam_pbp_import_skipped",
                         reason="slam_points already populated", rows=count)
                return
        except Exception:
            pass

    log.info("slam_pbp_import_starting",
             slams=list(_SLAMS.keys()), years=f"{_YEARS[0]}-{_YEARS[-1]}")
    total_points = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "tennis-bet-slam-import/1.0"},
        follow_redirects=True,
    ) as client:
        for slam in _SLAMS:
            for year in _YEARS:
                try:
                    player_map = await _load_player_map(client, slam, year)

                    url = f"{_GITHUB_RAW}/{year}-{slam}-points.csv"
                    rows = await _download(client, url)
                    if not rows:
                        continue

                    batch: list[SlamPoint] = []
                    for row in rows:
                        try:
                            pt = _parse_point(row, slam, year, player_map)
                            if pt:
                                batch.append(pt)
                        except Exception:
                            pass  # skip malformed points silently

                    if batch:
                        session.add_all(batch)
                        await session.flush()
                except Exception:
                    log.exception("slam_year_failed_non_fatal", slam=slam, year=year)
                    await session.rollback()  # clear bad state, continue
                    total_points += len(batch)

                log.info("slam_pbp_processed",
                         slam=slam, year=year,
                         rows=len(rows), points=len(batch))

    await session.commit()
    log.info("slam_pbp_import_done", total_points=total_points)
