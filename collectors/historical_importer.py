"""
Historical data importer — Jeff Sackmann's tennis_atp / tennis_wta CSV files.

Downloads match CSVs from GitHub (no API key needed) and populates:
  1. player_stats  — per-player, per-surface aggregated stats (used by signal analyzers)
  2. match_records — individual match rows with full stats (used for ML training)

Data source: https://github.com/JeffSackmann/tennis_atp
             https://github.com/JeffSackmann/tennis_wta
License: CC BY-NC-SA 4.0 — non-commercial use.

Covers 2000–2024 by default (25 years, ~140k ATP + ~100k WTA matches).
Runs once on startup if tables are empty, then weekly.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import MatchRecord, PlayerStats

log = structlog.get_logger()

_GITHUB_RAW = "https://raw.githubusercontent.com/JeffSackmann"

# 2000-2024 — 25 years of quality data; pre-2000 stats are incomplete
_YEARS = list(range(2000, 2025))

_ATP_URLS = [
    (f"{_GITHUB_RAW}/tennis_atp/master/atp_matches_{y}.csv", "atp", y)
    for y in _YEARS
]
_WTA_URLS = [
    (f"{_GITHUB_RAW}/tennis_wta/master/wta_matches_{y}.csv", "wta", y)
    for y in _YEARS
]

_SURFACE_MAP = {
    "Hard": "hard",
    "Clay": "clay",
    "Grass": "grass",
    "Carpet": "indoor_hard",
}


@dataclass
class _PlayerSurface:
    """Accumulator for aggregated player stats on one surface."""
    matches_played: int = 0
    matches_won: int = 0
    first_set_losses: int = 0
    first_set_loss_wins: int = 0
    first_serve_pct_sum: float = 0.0
    first_serve_pct_count: int = 0
    aces_sum: float = 0.0
    dfs_sum: float = 0.0
    svc_games_sum: float = 0.0


def _winner_took_first_set(score: str) -> bool:
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


def _safe_int(val: str, default: int = 0) -> int:
    try:
        return int(float(val)) if val and val.strip() else default
    except (ValueError, AttributeError):
        return default


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val) if val and val.strip() else default
    except (ValueError, AttributeError):
        return default


def _process_row(
    row: dict,
    tour: str,
    year: int,
    accum: dict[tuple[str, str], _PlayerSurface],
) -> MatchRecord | None:
    surface_raw = row.get("surface", "")
    surface = _SURFACE_MAP.get(surface_raw, "hard")
    winner = row.get("winner_name", "").strip()
    loser = row.get("loser_name", "").strip()
    score = row.get("score", "")

    if not winner or not loser:
        return None
    if "W/O" in score or "RET" in score or "DEF" in score:
        return None
    if "/" in winner or "/" in loser:
        return None  # skip doubles

    winner_won_set1 = _winner_took_first_set(score)

    # Update aggregates
    wk = (winner, surface)
    w = accum[wk]
    w.matches_played += 1
    w.matches_won += 1
    if not winner_won_set1:
        w.first_set_losses += 1
        w.first_set_loss_wins += 1

    w_svpt = _safe_float(row.get("w_svpt", ""))
    w_1stIn = _safe_float(row.get("w_1stIn", ""))
    w_SvGms = _safe_float(row.get("w_SvGms", ""))
    if w_svpt > 0 and w_SvGms > 0:
        w.first_serve_pct_sum += w_1stIn / w_svpt
        w.first_serve_pct_count += 1
        w.aces_sum += _safe_float(row.get("w_ace", ""))
        w.dfs_sum += _safe_float(row.get("w_df", ""))
        w.svc_games_sum += w_SvGms

    lk = (loser, surface)
    l = accum[lk]
    l.matches_played += 1
    if winner_won_set1:
        l.first_set_losses += 1

    l_svpt = _safe_float(row.get("l_svpt", ""))
    l_1stIn = _safe_float(row.get("l_1stIn", ""))
    l_SvGms = _safe_float(row.get("l_SvGms", ""))
    if l_svpt > 0 and l_SvGms > 0:
        l.first_serve_pct_sum += l_1stIn / l_svpt
        l.first_serve_pct_count += 1
        l.aces_sum += _safe_float(row.get("l_ace", ""))
        l.dfs_sum += _safe_float(row.get("l_df", ""))
        l.svc_games_sum += l_SvGms

    return MatchRecord(
        tour=tour,
        year=year,
        tourney_id=row.get("tourney_id", ""),
        tourney_name=row.get("tourney_name", ""),
        surface=surface,
        tourney_level=row.get("tourney_level", ""),
        round=row.get("round", ""),
        best_of=_safe_int(row.get("best_of", "3"), 3),
        winner_name=winner,
        loser_name=loser,
        winner_rank=_safe_int(row.get("winner_rank", "")),
        loser_rank=_safe_int(row.get("loser_rank", "")),
        score=score,
        minutes=_safe_int(row.get("minutes", "")),
        w_ace=_safe_int(row.get("w_ace", "")),
        w_df=_safe_int(row.get("w_df", "")),
        w_svpt=_safe_int(row.get("w_svpt", "")),
        w_1st_in=_safe_int(row.get("w_1stIn", "")),
        w_1st_won=_safe_int(row.get("w_1stWon", "")),
        w_2nd_won=_safe_int(row.get("w_2ndWon", "")),
        w_svc_games=_safe_int(row.get("w_SvGms", "")),
        w_bp_saved=_safe_int(row.get("w_bpSaved", "")),
        w_bp_faced=_safe_int(row.get("w_bpFaced", "")),
        l_ace=_safe_int(row.get("l_ace", "")),
        l_df=_safe_int(row.get("l_df", "")),
        l_svpt=_safe_int(row.get("l_svpt", "")),
        l_1st_in=_safe_int(row.get("l_1stIn", "")),
        l_1st_won=_safe_int(row.get("l_1stWon", "")),
        l_2nd_won=_safe_int(row.get("l_2ndWon", "")),
        l_svc_games=_safe_int(row.get("l_SvGms", "")),
        l_bp_saved=_safe_int(row.get("l_bpSaved", "")),
        l_bp_faced=_safe_int(row.get("l_bpFaced", "")),
    )


async def _download_csv(client: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        resp = await client.get(url, timeout=60.0)
        if resp.status_code == 404:
            log.debug("historical_csv_not_found", url=url.split("/")[-1])
            return []
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    except Exception:
        log.exception("historical_csv_download_failed", url=url.split("/")[-1])
        return []


async def run_import(session: AsyncSession, force: bool = False) -> None:
    """
    Download Sackmann CSVs and upsert player_stats + insert match_records.
    Skips if match_records already has data (unless force=True).
    """
    if not force:
        try:
            result = await session.execute(
                text("SELECT COUNT(*) FROM match_records LIMIT 1")
            )
            count = result.scalar() or 0
            if count > 0:
                log.info("historical_import_skipped",
                         reason="match_records already populated", rows=count)
                return
        except Exception:
            pass  # table may not exist yet on very first run

    log.info("historical_import_starting", years=f"{_YEARS[0]}-{_YEARS[-1]}")
    accum: dict[tuple[str, str], _PlayerSurface] = defaultdict(_PlayerSurface)
    total_records = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "tennis-bet-data-import/1.0"},
        follow_redirects=True,
    ) as client:
        for url, tour, year in _ATP_URLS + _WTA_URLS:
            rows = await _download_csv(client, url)
            if not rows:
                continue

            batch: list[MatchRecord] = []
            for row in rows:
                record = _process_row(row, tour, year, accum)
                if record:
                    batch.append(record)

            if batch:
                session.add_all(batch)
                await session.flush()  # write each year without holding all in memory
                total_records += len(batch)

            log.info("historical_csv_processed",
                     file=url.split("/")[-1], rows=len(rows), records=len(batch))

    # Upsert player_stats from accumulated data
    inserted_stats = 0
    for (name, surface), stats in accum.items():
        if stats.matches_played < 5:
            continue

        fsp = (stats.first_serve_pct_sum / stats.first_serve_pct_count
               if stats.first_serve_pct_count > 0 else 0.62)
        avg_aces = (stats.aces_sum / stats.svc_games_sum
                    if stats.svc_games_sum > 0 else 0.5)
        avg_dfs = (stats.dfs_sum / stats.svc_games_sum
                   if stats.svc_games_sum > 0 else 0.2)

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
            inserted_stats += 1
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
             match_records=total_records,
             player_stats_upserted=inserted_stats,
             unique_player_surfaces=len(accum))
