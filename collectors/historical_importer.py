"""
Historical data importer — Jeff Sackmann's tennis_atp / tennis_wta CSV files.

Downloads match CSVs from GitHub (no API key needed) and populates the
player_stats table with per-surface statistics. This immediately activates
the SetPatternAnalyzer which requires historical data to fire.

Data source: https://github.com/JeffSackmann/tennis_atp
             https://github.com/JeffSackmann/tennis_wta
License: CC BY-NC-SA 4.0 — non-commercial use.

Runs once on startup if player_stats table is empty, then weekly.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import PlayerStats

log = structlog.get_logger()

_GITHUB_RAW = "https://raw.githubusercontent.com/JeffSackmann"

# Last 3 years — balance between recency and volume
_ATP_URLS = [
    f"{_GITHUB_RAW}/tennis_atp/master/atp_matches_{y}.csv"
    for y in (2024, 2023, 2022)
]
_WTA_URLS = [
    f"{_GITHUB_RAW}/tennis_wta/master/wta_matches_{y}.csv"
    for y in (2024, 2023, 2022)
]

_SURFACE_MAP = {
    "Hard": "hard",
    "Clay": "clay",
    "Grass": "grass",
    "Carpet": "indoor_hard",
}


@dataclass
class _PlayerSurface:
    """Accumulator for one player on one surface."""
    matches_played: int = 0
    matches_won: int = 0
    first_set_losses: int = 0        # times they lost set 1
    first_set_loss_wins: int = 0     # times they won the match after losing set 1
    first_serve_pct_sum: float = 0.0
    first_serve_pct_count: int = 0
    aces_sum: float = 0.0
    dfs_sum: float = 0.0
    svc_games_sum: float = 0.0
    bp_saved_sum: float = 0.0
    bp_faced_sum: float = 0.0
    bp_won_sum: float = 0.0
    bp_opp_sum: float = 0.0


def _winner_took_first_set(score: str) -> bool:
    """Return True if the match winner (row winner) won the first set."""
    if not score:
        return True
    sets = score.strip().split()
    if not sets:
        return True
    parts = sets[0].split("-")
    if len(parts) != 2:
        return True
    try:
        w = int(parts[0].split("(")[0])
        l = int(parts[1].split("(")[0])
        return w > l
    except ValueError:
        return True


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val) if val.strip() else default
    except (ValueError, AttributeError):
        return default


def _process_row(row: dict, accum: dict[tuple[str, str], _PlayerSurface]) -> None:
    """Update accumulators for both winner and loser in a match row."""
    surface = _SURFACE_MAP.get(row.get("surface", ""), "hard")
    winner = row.get("winner_name", "").strip()
    loser = row.get("loser_name", "").strip()
    score = row.get("score", "")

    if not winner or not loser:
        return
    # Skip walkovers / retirements that have no real stats
    if "W/O" in score or "RET" in score or "DEF" in score:
        return

    winner_won_set1 = _winner_took_first_set(score)

    # Winner stats
    wk = (winner, surface)
    w = accum[wk]
    w.matches_played += 1
    w.matches_won += 1
    if not winner_won_set1:
        w.first_set_losses += 1
        w.first_set_loss_wins += 1  # won the match despite losing set 1

    w_svpt = _safe_float(row.get("w_svpt", ""))
    w_1stIn = _safe_float(row.get("w_1stIn", ""))
    w_SvGms = _safe_float(row.get("w_SvGms", ""))
    if w_svpt > 0 and w_SvGms > 0:
        w.first_serve_pct_sum += w_1stIn / w_svpt
        w.first_serve_pct_count += 1
        w.aces_sum += _safe_float(row.get("w_ace", ""))
        w.dfs_sum += _safe_float(row.get("w_df", ""))
        w.svc_games_sum += w_SvGms
        w.bp_saved_sum += _safe_float(row.get("w_bpSaved", ""))
        w.bp_faced_sum += _safe_float(row.get("w_bpFaced", ""))

    # Loser stats
    lk = (loser, surface)
    l = accum[lk]
    l.matches_played += 1
    if winner_won_set1:
        l.first_set_losses += 1  # loser lost set 1
        # loser did NOT win the match after losing set 1 (first_set_loss_wins unchanged)

    l_svpt = _safe_float(row.get("l_svpt", ""))
    l_1stIn = _safe_float(row.get("l_1stIn", ""))
    l_SvGms = _safe_float(row.get("l_SvGms", ""))
    if l_svpt > 0 and l_SvGms > 0:
        l.first_serve_pct_sum += l_1stIn / l_svpt
        l.first_serve_pct_count += 1
        l.aces_sum += _safe_float(row.get("l_ace", ""))
        l.dfs_sum += _safe_float(row.get("l_df", ""))
        l.svc_games_sum += l_SvGms
        l.bp_saved_sum += _safe_float(row.get("l_bpSaved", ""))
        l.bp_faced_sum += _safe_float(row.get("l_bpFaced", ""))


async def _download_csv(client: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        resp = await client.get(url, timeout=30.0)
        if resp.status_code == 404:
            log.info("historical_csv_not_found", url=url)
            return []
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    except Exception:
        log.exception("historical_csv_download_failed", url=url)
        return []


async def run_import(session: AsyncSession, force: bool = False) -> None:
    """
    Download Sackmann CSVs and upsert player_stats.
    Skips if table already has data (unless force=True).
    """
    if not force:
        result = await session.execute(select(PlayerStats).limit(1))
        if result.scalar_one_or_none() is not None:
            log.info("historical_import_skipped", reason="player_stats already populated")
            return

    log.info("historical_import_starting")
    accum: dict[tuple[str, str], _PlayerSurface] = defaultdict(_PlayerSurface)

    async with httpx.AsyncClient(
        headers={"User-Agent": "tennis-bet-data-import/1.0"},
        follow_redirects=True,
    ) as client:
        for url in _ATP_URLS + _WTA_URLS:
            rows = await _download_csv(client, url)
            for row in rows:
                _process_row(row, accum)
            log.info("historical_csv_processed", url=url.split("/")[-1], rows=len(rows))

    # Upsert into player_stats
    inserted = 0
    for (name, surface), stats in accum.items():
        if stats.matches_played < 5:
            continue  # skip players with too few matches

        fsp = (stats.first_serve_pct_sum / stats.first_serve_pct_count
               if stats.first_serve_pct_count > 0 else 0.62)
        avg_aces = (stats.aces_sum / stats.svc_games_sum
                    if stats.svc_games_sum > 0 else 0.5)
        avg_dfs = (stats.dfs_sum / stats.svc_games_sum
                   if stats.svc_games_sum > 0 else 0.2)

        # Check if row exists
        existing = await session.execute(
            select(PlayerStats)
            .where(PlayerStats.player_name == name)
            .where(PlayerStats.surface == surface)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(PlayerStats(
                player_name=name,
                surface=surface,
                matches_played=stats.matches_played,
                matches_won=stats.matches_won,
                first_set_losses=stats.first_set_losses,
                first_set_loss_wins=stats.first_set_loss_wins,
                avg_first_serve_pct=round(fsp, 4),
                avg_aces_per_game=round(avg_aces, 3),
                avg_dfs_per_game=round(avg_dfs, 3),
            ))
            inserted += 1
        else:
            row.matches_played = stats.matches_played
            row.matches_won = stats.matches_won
            row.first_set_losses = stats.first_set_losses
            row.first_set_loss_wins = stats.first_set_loss_wins
            row.avg_first_serve_pct = round(fsp, 4)
            row.avg_aces_per_game = round(avg_aces, 3)
            row.avg_dfs_per_game = round(avg_dfs, 3)

    await session.commit()
    log.info("historical_import_done",
             players=len(accum), rows_inserted=inserted)
