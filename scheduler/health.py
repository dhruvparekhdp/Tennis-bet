"""Aiohttp web server: dashboard UI + JSON API endpoints."""
from __future__ import annotations

import json
from datetime import datetime

from aiohttp import web

_start_time = datetime.utcnow()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reconstruct_sets(
    game_log: list[int],
    sets_p1: int,
    sets_p2: int,
    games_p1: int,
    games_p2: int,
) -> list[dict]:
    """Reconstruct per-set game scores from game_log."""
    sets: list[dict] = []
    log = list(game_log)
    total_completed = sets_p1 + sets_p2

    for _ in range(total_completed):
        sp1, sp2 = 0, 0
        while log:
            w = log.pop(0)
            if w == 1:
                sp1 += 1
            else:
                sp2 += 1
            if (sp1 >= 6 or sp2 >= 6) and abs(sp1 - sp2) >= 2:
                break
            if sp1 == 7 or sp2 == 7:  # tiebreak
                break
        sets.append({"p1": sp1, "p2": sp2, "current": False})

    # Current set in progress
    sets.append({"p1": games_p1, "p2": games_p2, "current": True})
    return sets


# ── JSON API ──────────────────────────────────────────────────────────────────

async def _api_status(runner, request: web.Request) -> web.Response:
    status = runner.get_status()
    count = await runner.store.count()
    uptime = int((datetime.utcnow() - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({"uptime_seconds": uptime, "matches_tracked": count, **status}),
        content_type="application/json",
    )


async def _api_matches(runner, request: web.Request) -> web.Response:
    from analysis.win_probability import compute_win_probability
    states = await runner.store.get_all()
    # Live first, upcoming sorted by start_time
    live = [s for s in states if not s.is_scheduled]
    soon = sorted(
        [s for s in states if s.is_scheduled],
        key=lambda s: s.start_time or datetime.utcnow(),
    )
    matches = []
    for s in live + soon:
        try:
            win_p1, win_p2 = compute_win_probability(s)
        except Exception:
            win_p1, win_p2 = 0.0, 0.0

        set_scores = _reconstruct_sets(
            s.game_log, s.sets_p1, s.sets_p2,
            s.games_in_set_p1, s.games_in_set_p2,
        )
        odds_history = [
            {"odds_p1": p.odds_p1, "odds_p2": p.odds_p2,
             "timestamp": p.timestamp.isoformat()}
            for p in s.odds_history[-20:]
        ]
        matches.append({
            "match_id": s.match_id,
            "player1": s.player1_name,
            "player2": s.player2_name,
            "tournament": s.tournament,
            "surface": s.surface,
            "sets_p1": s.sets_p1,
            "sets_p2": s.sets_p2,
            "games_p1": s.games_in_set_p1,
            "games_p2": s.games_in_set_p2,
            "current_set": s.current_set,
            "is_tiebreak": s.is_tiebreak,
            "odds_p1": s.odds_p1,
            "odds_p2": s.odds_p2,
            "win_prob_p1": round(win_p1 * 100, 1),
            "win_prob_p2": round(win_p2 * 100, 1),
            "set_scores": set_scores,
            "odds_history": odds_history,
            "game_log": s.game_log[-20:],
            "duration_mins": s.match_duration_mins,
            "source": s.match_id.split("_")[0],
            "is_upcoming": s.is_scheduled,
            "start_time": s.start_time.isoformat() if s.start_time else None,
        })
    return web.Response(text=json.dumps(matches), content_type="application/json")


async def _api_football_matches(runner, request: web.Request) -> web.Response:
    states = await runner.football_store.get_all()
    # Live first, then upcoming sorted by kickoff
    live = [s for s in states if not s.is_scheduled]
    soon = sorted(
        [s for s in states if s.is_scheduled],
        key=lambda s: s.kickoff_time or datetime.utcnow(),
    )
    matches = []
    for s in live + soon:
        matches.append({
            "match_id": s.match_id,
            "home_team": s.home_team,
            "away_team": s.away_team,
            "tournament": s.tournament,
            "league_key": s.league_key,
            "minute": s.minute,
            "home_score": s.home_score,
            "away_score": s.away_score,
            "home_odds": s.home_odds,
            "draw_odds": s.draw_odds,
            "away_odds": s.away_odds,
            "home_red_cards": s.home_red_cards,
            "away_red_cards": s.away_red_cards,
            "is_halftime": s.is_halftime,
            "is_extra_time": s.is_extra_time,
            "period": s.period,
            "is_scheduled": s.is_scheduled,
            "kickoff_time": s.kickoff_time.isoformat() if s.kickoff_time else None,
        })
    return web.Response(text=json.dumps(matches), content_type="application/json")


async def _api_football_signals(runner, request: web.Request) -> web.Response:
    sigs = runner.football_engine.get_recent_signals(hours=24)
    result = [
        {
            "match_id": s.match_id,
            "signal_type": s.signal_type,
            "team_to_back": s.team_to_back,
            "opponent": s.opponent,
            "tournament": s.tournament,
            "is_home": s.is_home,
            "market": s.market,
            "current_odds": s.current_odds,
            "fair_odds": s.fair_odds,
            "edge_pct": round(s.edge_pct * 100, 1),
            "confidence": round(s.confidence * 100),
            "stake_pct": round(s.stake_pct * 100, 1),
            "trigger": s.trigger_description,
            "score_summary": s.score_summary,
            "minute": s.minute,
            "timestamp": s.timestamp.isoformat(),
        }
        for s in reversed(sigs)  # newest first
    ]
    return web.Response(text=json.dumps(result), content_type="application/json")


async def _api_signals(runner, request: web.Request) -> web.Response:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.get_recent_signals(hours=24)
    signals = [
        {
            "match_id": r.match_id,
            "signal_type": r.signal_type,
            "player_name": r.player_name or "",
            "opponent_name": r.opponent_name or "",
            "tournament": r.tournament or "",
            "surface": r.surface or "hard",
            "trigger": r.trigger_description,
            "confidence": round(r.confidence * 100),
            "market": r.recommended_market,
            "odds": r.current_odds,
            "fair_odds": r.fair_odds,
            "edge_pct": round(r.edge_pct * 100, 1),
            "stake_pct": round(r.stake_pct * 100, 1),
            "model_win_prob": round(getattr(r, "model_win_prob", 0) * 100, 1),
            "score_at_signal": getattr(r, "score_at_signal", ""),
            "outcome": getattr(r, "outcome", "pending"),
            "timestamp": r.timestamp.isoformat(),
        }
        for r in rows
    ]
    return web.Response(text=json.dumps(signals), content_type="application/json")


async def _api_scalping(runner, request: web.Request) -> web.Response:
    """Sure-shot / scalping opportunities across all live tennis matches."""
    from analysis.scalping import scan_all
    from config.settings import settings
    states = await runner.store.get_all()
    opps = scan_all(
        states,
        min_win_prob=settings.scalp_min_win_prob,
        lock_win_prob=settings.scalp_lock_win_prob,
        max_odds=settings.scalp_max_odds,
        lock_max_odds=settings.scalp_lock_max_odds,
    )
    result = [
        {
            "match_id": o.match_id,
            "player_name": o.player_name,
            "opponent_name": o.opponent_name,
            "tournament": o.tournament,
            "surface": o.surface,
            "source": o.source,
            "score_summary": o.score_summary,
            "win_prob": round(o.win_prob * 100, 1),
            "market_odds": o.market_odds,
            "market_implied": round(o.market_implied * 100, 1),
            "edge_pct": o.edge_pct,
            "ev_pct": o.ev_pct,
            "tier": o.tier,
            "reasons": o.reasons,
            "scalp_window": o.scalp_window,
            "is_serving": o.is_serving,
            "timestamp": o.timestamp.isoformat(),
        }
        for o in opps
    ]
    return web.Response(text=json.dumps(result), content_type="application/json")


async def _api_h2h(runner, request: web.Request) -> web.Response:
    p1 = request.query.get("p1", "")
    p2 = request.query.get("p2", "")
    surface = request.query.get("surface", None)
    if not p1 or not p2:
        return web.Response(text=json.dumps({"error": "p1 and p2 required"}),
                            content_type="application/json", status=400)
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        h2h = await repo.get_h2h(p1, p2, surface)
        p1_form = await repo.get_player_form(p1, surface)
        p2_form = await repo.get_player_form(p2, surface)
    return web.Response(
        text=json.dumps({"h2h": h2h, "p1_form": p1_form, "p2_form": p2_form}),
        content_type="application/json",
    )


async def _api_ingest(runner, request: web.Request) -> web.Response:
    """Receive match states pushed from a laptop-based Flashscore scraper."""
    import structlog as _log

    from analysis.match_state import MatchState, ServeStats
    from config.settings import settings

    key = request.headers.get("X-Ingest-Key", "")
    if settings.ingest_api_key and key != settings.ingest_api_key:
        return web.Response(
            text=json.dumps({"error": "unauthorized"}),
            content_type="application/json",
            status=401,
        )

    try:
        data = await request.json()
    except Exception:
        return web.Response(
            text=json.dumps({"error": "invalid JSON"}),
            content_type="application/json",
            status=400,
        )

    matches = data.get("matches", [])
    count = 0
    pushed_ids: set[str] = set()
    for m in matches:
        try:
            ts = datetime.fromisoformat(m["timestamp"]) if m.get("timestamp") else datetime.utcnow()
            st = datetime.fromisoformat(m["start_time"]) if m.get("start_time") else None
            sp1 = m.get("serve_stats_p1", {})
            sp2 = m.get("serve_stats_p2", {})
            state = MatchState(
                match_id=m["match_id"],
                player1_name=m["player1_name"],
                player2_name=m["player2_name"],
                surface=m["surface"],
                tournament=m["tournament"],
                current_server=m.get("current_server", 0),
                sets_p1=m.get("sets_p1", 0),
                sets_p2=m.get("sets_p2", 0),
                games_in_set_p1=m.get("games_in_set_p1", 0),
                games_in_set_p2=m.get("games_in_set_p2", 0),
                current_set=m.get("current_set", 1),
                is_tiebreak=m.get("is_tiebreak", False),
                serve_stats_p1=ServeStats(
                    first_serve_pct=sp1.get("first_serve_pct", 0.6),
                    aces=sp1.get("aces", 0),
                    double_faults=sp1.get("double_faults", 0),
                ),
                serve_stats_p2=ServeStats(
                    first_serve_pct=sp2.get("first_serve_pct", 0.6),
                    aces=sp2.get("aces", 0),
                    double_faults=sp2.get("double_faults", 0),
                ),
                odds_p1=m.get("odds_p1", 0.0),
                odds_p2=m.get("odds_p2", 0.0),
                game_log=m.get("game_log", []),
                match_duration_mins=m.get("match_duration_mins", 0),
                timestamp=ts,
                is_scheduled=m.get("is_scheduled", False),
                start_time=st,
            )
            await runner.store.update(state)
            pushed_ids.add(state.match_id)
            count += 1
        except Exception as exc:
            _log.get_logger().warning("ingest_match_failed", error=str(exc))

    # Remove stale pushed matches that are no longer in the push payload.
    # Pushed sources are laptop-scraped: Flashscore (fs_) and Parimatch (pm_).
    for s in await runner.store.get_all():
        if (s.match_id.startswith("fs_") or s.match_id.startswith("pm_")) \
                and s.match_id not in pushed_ids:
            await runner.store.remove(s.match_id)

    _log.get_logger().info("ingest_received", count=count)
    return web.Response(
        text=json.dumps({"ok": True, "count": count}),
        content_type="application/json",
    )


async def _api_debug(runner, request: web.Request) -> web.Response:
    """Diagnostic endpoint — returns collector state, all stored match IDs, and timing."""
    states = await runner.store.get_all()
    fb_states = await runner.football_store.get_all()
    uptime = int((datetime.utcnow() - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({
            "uptime_seconds": uptime,
            "tennis_matches": [
                {
                    "match_id": s.match_id,
                    "players": f"{s.player1_name} vs {s.player2_name}",
                    "tournament": s.tournament,
                    "score": f"{s.sets_p1}-{s.sets_p2} ({s.games_in_set_p1}-{s.games_in_set_p2})",
                    "has_odds": s.odds_p1 > 1.01,
                    "source": s.match_id.split("_")[0],
                }
                for s in states
            ],
            "football_matches": [
                {
                    "match_id": s.match_id,
                    "teams": f"{s.home_team} vs {s.away_team}",
                    "tournament": s.tournament,
                    "minute": s.minute,
                    "is_scheduled": s.is_scheduled,
                }
                for s in fb_states
            ],
            "collector_status": runner.get_status(),
        }),
        content_type="application/json",
    )


async def _health(runner, request: web.Request) -> web.Response:
    count = await runner.store.count()
    uptime = int((datetime.utcnow() - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({"status": "ok", "matches_tracked": count, "uptime_seconds": uptime}),
        content_type="application/json",
    )


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tennis Bet Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:18px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
.badge{background:#0ea5e9;color:#fff;font-size:11px;padding:2px 8px;border-radius:9999px;font-weight:600}
.refresh{font-size:12px;color:#64748b}
.nav-btn{margin-left:12px;background:#0ea5e9;color:#fff;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap}
.nav-btn:hover{background:#0284c7}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;padding:16px 20px 0}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:6px}
.card-value{font-size:26px;font-weight:700;color:#f1f5f9}
.card-sub{font-size:11px;color:#94a3b8;margin-top:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dot-green{background:#22c55e}.dot-red{background:#ef4444}.dot-yellow{background:#f59e0b}.dot-gray{background:#475569}
section{padding:16px 20px}
section h2{font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.status-card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:10px}
.status-name{font-size:11px;font-weight:600;color:#94a3b8;margin-bottom:4px}
.status-val{font-size:12px;color:#f1f5f9}
footer{text-align:center;padding:16px;color:#334155;font-size:11px;border-top:1px solid #1e293b;margin-top:4px}
.empty{color:#475569;font-size:13px;padding:20px 0;text-align:center}

/* ── Match card — Fairplay style ── */
.match-card{background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden;margin-bottom:12px}
.mc-header{display:flex;align-items:center;gap:6px;padding:7px 12px;background:#162032;border-bottom:1px solid #1e3a5f;font-size:11px;color:#64748b;flex-wrap:wrap}
.source-tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#1e3a5f;color:#7dd3fc;font-weight:700;letter-spacing:.04em}
.surface-clay{color:#f97316}.surface-grass{color:#22c55e}.surface-hard{color:#38bdf8}.surface-indoor_hard{color:#818cf8}
/* Player row */
.mc-players{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:14px 12px 10px}
.mc-player{display:flex;flex-direction:column;gap:3px}
.mc-player.right{align-items:flex-end;text-align:right}
.mc-name{font-size:15px;font-weight:700;color:#f1f5f9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px}
.mc-sets-won{font-size:32px;font-weight:900;color:#f1f5f9;line-height:1}
.mc-leading{font-size:10px;padding:2px 6px;border-radius:4px;background:#14532d;color:#4ade80;font-weight:700;margin-top:2px;display:inline-block}
/* Center score */
.mc-center{display:flex;flex-direction:column;align-items:center;gap:4px;padding:0 14px;min-width:100px}
.mc-set-label{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:#475569;font-weight:600}
.mc-game-score{font-size:30px;font-weight:900;color:#f1f5f9;letter-spacing:3px;line-height:1}
.mc-tb-tag{font-size:9px;background:#7c3aed;color:#ddd6fe;padding:1px 5px;border-radius:3px;font-weight:700}
.mc-duration{font-size:10px;color:#64748b;background:#0f172a;padding:2px 7px;border-radius:4px}
/* Scoreboard — Fairplay style */
.mc-scoreboard{background:#0f172a;border-top:1px solid #1e3a5f;padding:8px 12px}
.sb-table{width:100%;border-collapse:collapse;font-size:12px}
.sb-table th{color:#475569;font-weight:600;text-transform:uppercase;font-size:9px;letter-spacing:.06em;padding:4px 6px;text-align:center;border-bottom:1px solid #1e293b}
.sb-table th.pname{text-align:left}
.sb-table td{padding:5px 6px;text-align:center;font-size:13px;font-weight:700}
.sb-table td.pname{text-align:left;font-size:11px;color:#94a3b8;font-weight:500}
.sb-won{color:#38bdf8}
.sb-lost{color:#475569}
.sb-cur{color:#f1f5f9;position:relative}
.sb-cur::after{content:'▸';font-size:8px;color:#f59e0b;position:absolute;top:-1px;right:-2px}
.sb-sets-total{font-size:16px;font-weight:900;color:#f1f5f9}
.sb-sets-won{color:#38bdf8}
/* Win probability bar */
.mc-prob{background:#0f172a;border-top:1px solid #1e293b;padding:8px 12px;display:flex;align-items:center;gap:8px;font-size:11px}
.prob-name{color:#94a3b8;white-space:nowrap;font-size:10px;min-width:70px;overflow:hidden;text-overflow:ellipsis}
.prob-name.right{text-align:right;min-width:70px}
.prob-bar-wrap{flex:1;height:8px;background:#1e293b;border-radius:4px;overflow:hidden;display:flex}
.prob-bar-p1{height:100%;background:#38bdf8;transition:width .4s}
.prob-bar-p2{height:100%;background:#f97316;transition:width .4s}
.prob-pct{font-weight:700;color:#f1f5f9;white-space:nowrap;font-size:11px;min-width:36px}
.prob-pct.right{text-align:right}
/* Odds */
.mc-odds-row{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#0f172a;border-top:1px solid #334155}
.mc-odds-box{padding:10px 12px;text-align:center;background:#1e293b;cursor:pointer;transition:background .15s}
.mc-odds-box:hover{background:#243554}
.mc-odds-label{font-size:9px;color:#64748b;margin-bottom:3px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.mc-odds-val{font-size:24px;font-weight:900;line-height:1}
.mc-odds-val.fav{color:#34d399}
.mc-odds-val.dog{color:#38bdf8}
.mc-odds-val.none{color:#334155;font-size:16px}
.mc-odds-hint{font-size:9px;color:#475569;margin-top:2px}

/* ── Signal cards — clearer BET ON ── */
.signal-card{background:#1e293b;border:1px solid #334155;border-radius:10px;overflow:hidden;margin-bottom:10px}
.sc-header{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#0f172a;border-bottom:1px solid #334155}
.sc-type{font-size:11px;font-weight:700;padding:3px 8px;border-radius:5px;white-space:nowrap}
.sc-momentum{background:#1d4ed8;color:#bfdbfe}
.sc-odds_value{background:#7c3aed;color:#ddd6fe}
.sc-serve_degradation{background:#b45309;color:#fde68a}
.sc-set_pattern{background:#065f46;color:#a7f3d0}
.sc-fatigue{background:#9f1239;color:#fecdd3}
.sc-ml_value{background:#155e75;color:#a5f3fc}
.sc-endgame{background:#713f12;color:#fef08a}
.sc-break_momentum{background:#7f1d1d;color:#fca5a5}
.sc-second_set_fade{background:#312e81;color:#c7d2fe}
.sc-time{font-size:11px;color:#475569;margin-left:auto}
.sc-outcome-won{font-size:10px;background:#14532d;color:#4ade80;padding:2px 7px;border-radius:4px;font-weight:700}
.sc-outcome-lost{font-size:10px;background:#7f1d1d;color:#fca5a5;padding:2px 7px;border-radius:4px;font-weight:700}
.sc-outcome-pending{font-size:10px;background:#1e293b;color:#64748b;padding:2px 7px;border-radius:4px}
/* BET ON banner */
.sc-bet-banner{background:#0c2e1a;border-bottom:1px solid #14532d;padding:10px 12px;display:flex;align-items:center;gap:10px}
.sc-bet-arrow{font-size:20px}
.sc-bet-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#4ade80;font-weight:700}
.sc-bet-player{font-size:18px;font-weight:900;color:#f1f5f9;line-height:1.1}
.sc-bet-market{font-size:11px;color:#86efac;margin-top:2px}
.sc-bet-odds{margin-left:auto;text-align:right}
.sc-bet-odds-val{font-size:22px;font-weight:900;color:#34d399}
.sc-bet-odds-fair{font-size:10px;color:#4ade80}
/* Body */
.sc-body{padding:10px 12px}
.sc-match{font-size:12px;color:#94a3b8;margin-bottom:6px}
.sc-match strong{color:#e2e8f0}
.sc-why{font-size:11px;color:#94a3b8;line-height:1.5;margin-bottom:8px}
/* Probability comparison */
.sc-probs{background:#0f172a;border-radius:6px;padding:8px 10px;margin-bottom:8px}
.sc-prob-row{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.sc-prob-row:last-child{margin-bottom:0}
.sc-prob-lbl{font-size:10px;color:#64748b;width:46px;font-weight:600}
.sc-prob-bar-wrap{flex:1;height:7px;background:#1e293b;border-radius:3px;overflow:hidden}
.sc-prob-bar{height:100%;border-radius:3px}
.sc-prob-bar-model{background:#38bdf8}
.sc-prob-bar-market{background:#94a3b8}
.sc-prob-pct{font-size:11px;font-weight:700;color:#f1f5f9;width:36px;text-align:right}
/* Footer row */
.sc-footer{display:flex;align-items:center;gap:10px;padding:8px 12px;background:#0f172a;border-top:1px solid #1e293b;flex-wrap:wrap}
.sc-conf{font-size:13px;font-weight:800;color:#f1f5f9}
.sc-conf-bar{font-size:13px;color:#334155;letter-spacing:1px}
.sc-edge{font-size:11px;color:#22c55e;font-weight:700}
.sc-stake{font-size:10px;background:#1d4736;color:#34d399;padding:2px 8px;border-radius:4px;font-weight:600;margin-left:auto}

/* ── Tab bar ── */
.tab-bar{display:flex;gap:0;padding:0 20px;background:#1e293b;border-bottom:2px solid #0f172a}
.tab-btn{background:none;border:none;border-bottom:3px solid transparent;color:#64748b;font-size:13px;font-weight:600;padding:11px 20px;cursor:pointer;transition:all .15s;margin-bottom:-2px}
.tab-btn.active{color:#f1f5f9;border-bottom-color:#0ea5e9}
.tab-btn:hover:not(.active){color:#94a3b8}
.tab-content{display:none}
.tab-content.active{display:block}
.tab-badge{display:inline-block;background:#dc2626;color:#fff;font-size:10px;font-weight:800;padding:0 6px;border-radius:9999px;margin-left:4px;vertical-align:middle}

/* ── Scalping ── */
.scalp-intro{font-size:11px;color:#94a3b8;line-height:1.6;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px 12px;margin-bottom:14px}
.scalp-intro strong{color:#e2e8f0}
.scalp-card{background:#1e293b;border:1px solid #334155;border-left:4px solid #475569;border-radius:12px;overflow:hidden;margin-bottom:12px}
.scalp-card.tier-lock{border-left-color:#22c55e;box-shadow:0 0 0 1px rgba(34,197,94,.25)}
.scalp-card.tier-strong{border-left-color:#0ea5e9}
.scalp-card.tier-watch{border-left-color:#f59e0b}
.scalp-head{display:flex;align-items:center;gap:8px;padding:9px 12px;background:#162032;border-bottom:1px solid #1e3a5f;flex-wrap:wrap}
.scalp-tier{font-size:10px;font-weight:900;letter-spacing:.08em;padding:2px 9px;border-radius:5px}
.scalp-tier.tier-lock{background:#14532d;color:#4ade80}
.scalp-tier.tier-strong{background:#0c4a6e;color:#7dd3fc}
.scalp-tier.tier-watch{background:#78350f;color:#fcd34d}
.scalp-tourney{font-size:11px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scalp-src{font-size:9px;padding:1px 6px;border-radius:4px;background:#1e3a5f;color:#7dd3fc;font-weight:700;text-transform:uppercase}
.scalp-window{font-size:10px;background:#3b0764;color:#e9d5ff;padding:2px 8px;border-radius:5px;font-weight:700;margin-left:auto;animation:fbpulse 1.2s infinite}
.scalp-body{padding:12px}
.scalp-bet{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.scalp-bet-info{flex:1;min-width:0}
.scalp-bet-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#4ade80;font-weight:700}
.scalp-player{font-size:19px;font-weight:900;color:#f1f5f9;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scalp-vs{font-size:11px;color:#64748b;margin-top:2px}
.scalp-odds{text-align:right}
.scalp-odds-val{font-size:24px;font-weight:900;color:#34d399}
.scalp-odds-val.none{color:#475569}
.scalp-odds-cap{font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
.scalp-score{font-size:13px;font-weight:800;color:#f59e0b;margin-bottom:8px}
.scalp-prob-wrap{height:9px;background:#0f172a;border-radius:5px;overflow:hidden;margin-bottom:4px;position:relative}
.scalp-prob-bar{height:100%;background:linear-gradient(90deg,#0ea5e9,#22c55e);border-radius:5px}
.scalp-prob-lbls{display:flex;justify-content:space-between;font-size:10px;color:#64748b;margin-bottom:8px}
.scalp-prob-lbls b{color:#f1f5f9}
.scalp-reasons{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.scalp-reason{font-size:10px;background:#0f172a;color:#cbd5e1;padding:2px 8px;border-radius:9999px;border:1px solid #1e293b}
.scalp-foot{display:flex;align-items:center;gap:12px;padding:8px 12px;background:#0f172a;border-top:1px solid #1e293b;flex-wrap:wrap}
.scalp-stat{font-size:11px;color:#94a3b8}
.scalp-stat b{color:#f1f5f9}
.scalp-ev-pos{color:#22c55e;font-weight:700}
.scalp-ev-neg{color:#f87171;font-weight:700}

/* ── Football match card ── */
.fb-card{background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden;margin-bottom:12px}
.fb-header{display:flex;align-items:center;gap:6px;padding:7px 12px;background:#1a1f2e;border-bottom:1px solid #2d3748;font-size:11px;color:#64748b;flex-wrap:wrap}
.fb-league-tag{font-size:10px;padding:1px 7px;border-radius:4px;background:#166534;color:#86efac;font-weight:700;letter-spacing:.04em}
.fb-minute{font-size:12px;font-weight:800;color:#f59e0b;margin-left:auto;display:flex;align-items:center;gap:4px}
.fb-live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ef4444;animation:fbpulse .9s infinite}
@keyframes fbpulse{0%,100%{opacity:1}50%{opacity:.25}}
.fb-ht-badge{font-size:10px;background:#78350f;color:#fde68a;padding:1px 6px;border-radius:3px;font-weight:700}
.fb-et-badge{font-size:10px;background:#7c3aed;color:#ddd6fe;padding:1px 6px;border-radius:3px;font-weight:700}
/* Score area */
.fb-score-row{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:18px 14px 14px}
.fb-team{display:flex;flex-direction:column;gap:5px}
.fb-team.right{align-items:flex-end;text-align:right}
.fb-team-name{font-size:14px;font-weight:700;color:#f1f5f9;max-width:145px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-red-cards{display:flex;gap:2px}
.fb-red-card{width:11px;height:15px;background:#ef4444;border-radius:2px}
.fb-leading-badge{font-size:10px;padding:2px 6px;border-radius:4px;background:#14532d;color:#4ade80;font-weight:700}
/* Center */
.fb-score-center{text-align:center;padding:0 18px;min-width:90px}
.fb-score{font-size:46px;font-weight:900;color:#f1f5f9;letter-spacing:6px;line-height:1}
.fb-score-sub{font-size:10px;color:#64748b;margin-top:4px}
/* Odds 3-way */
.fb-odds-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:#0f172a;border-top:1px solid #334155}
.fb-odds-box{padding:10px 6px;text-align:center;background:#1e293b}
.fb-odds-label{font-size:9px;color:#64748b;margin-bottom:3px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-odds-val{font-size:20px;font-weight:900;color:#38bdf8}
.fb-odds-val.fav{color:#34d399}
.fb-odds-val.draw{color:#94a3b8}
.fb-odds-val.none{color:#334155;font-size:14px}

/* ── Football signal card ── */
.fb-sig-card{background:#1e293b;border:1px solid #334155;border-radius:10px;overflow:hidden;margin-bottom:10px}
.fb-sig-header{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#0f172a;border-bottom:1px solid #334155}
.fb-sig-type{font-size:11px;font-weight:700;padding:3px 8px;border-radius:5px;white-space:nowrap}
.fb-sig-late_lead{background:#065f46;color:#a7f3d0}
.fb-sig-heavy_fav_dominating{background:#1d4ed8;color:#bfdbfe}
.fb-sig-late_draw_fade{background:#312e81;color:#c7d2fe}
.fb-sig-red_card_advantage{background:#7f1d1d;color:#fca5a5}
.fb-sig-clean_sheet_likely{background:#134e4a;color:#99f6e4}
.fb-sig-time{font-size:11px;color:#475569;margin-left:auto}
</style>
</head>
<body>
<header>
  <h1>&#127934; Tennis Bet Monitor <span class="badge" id="live-count">0 live</span></h1>
  <span class="refresh" id="refresh-label">Loading&hellip;</span>
  <a class="nav-btn" href="/settings">⚙️ Settings</a>
  <a class="nav-btn" href="/data">🗄️ History</a>
</header>

<div class="grid">
  <div class="card"><div class="card-title">Tennis Live</div><div class="card-value" id="stat-matches">&mdash;</div><div class="card-sub">matches now</div></div>
  <div class="card"><div class="card-title">Football Live</div><div class="card-value" id="stat-fb-matches">&mdash;</div><div class="card-sub">matches now</div></div>
  <div class="card"><div class="card-title">Signals (24h)</div><div class="card-value" id="stat-signals">&mdash;</div><div class="card-sub">tennis + football</div></div>
  <div class="card"><div class="card-title">Uptime</div><div class="card-value" id="stat-uptime">&mdash;</div><div class="card-sub">since restart</div></div>
</div>

<div class="tab-bar">
  <button class="tab-btn active" data-tab="tennis" onclick="switchTab('tennis')">🎾 Tennis</button>
  <button class="tab-btn" data-tab="scalping" onclick="switchTab('scalping')">🎯 Scalping <span id="scalp-count-badge" class="tab-badge" style="display:none">0</span></button>
  <button class="tab-btn" data-tab="football" onclick="switchTab('football')">⚽ Football</button>
</div>

<div id="tab-tennis" class="tab-content active">
  <section>
    <h2>Data Sources</h2>
    <div class="status-grid" id="sources"></div>
  </section>
  <section>
    <h2>Live Tennis Matches</h2>
    <div id="matches"><div class="empty">No live matches tracked</div></div>
  </section>
  <section>
    <h2>Tennis Signals (last 24h)</h2>
    <div id="signals"><div class="empty">No signals fired yet</div></div>
  </section>
</div>

<div id="tab-scalping" class="tab-content">
  <section>
    <h2>🎯 Sure-Shot / Scalping Opportunities</h2>
    <div class="scalp-intro">Near-certain in-play winners — favourite holds a decisive lead <em>and</em> is priced short. <strong>LOCK</strong> = highest conviction. A <strong>scalp window</strong> means odds drifted up after a dropped game (better entry now). Fixed-odds books carry risk — no result is ever 100%.</div>
    <div id="scalp-list"><div class="empty">No sure-shot opportunities right now</div></div>
  </section>
</div>

<div id="tab-football" class="tab-content">
  <section>
    <h2>Live Football Matches</h2>
    <div id="fb-matches"><div class="empty">No live football matches tracked</div></div>
  </section>
  <section>
    <h2>Football Signals (last 24h)</h2>
    <div id="fb-signals"><div class="empty">No football signals fired yet</div></div>
  </section>
</div>

<footer>Auto-refreshes every 30s &middot; <span id="last-updated">&mdash;</span> &middot; <a href="/data" style="color:#38bdf8;text-decoration:none">🗄️ DB Dump</a> &middot; <a href="/settings" style="color:#3fb950;text-decoration:none">⚙️ Settings</a> &middot; <a href="/api/debug/collectors" style="color:#a78bfa;text-decoration:none">🔬 Debug</a></footer>

<script>
const SURFACE_CLASS={clay:'surface-clay',grass:'surface-grass',hard:'surface-hard',indoor_hard:'surface-indoor_hard'};
const SURFACE_DOT={clay:'🟤',grass:'🟢',hard:'🔵',indoor_hard:'🔵'};
const SIG_EMOJI={momentum:'⚡',odds_value:'📉',serve_degradation:'🎯',set_pattern:'📊',fatigue:'😤',ml_value:'🤖',endgame:'⏱',break_momentum:'💥',second_set_fade:'🔄'};
const SIG_NAME={momentum:'Momentum Surge',odds_value:'Odds Value',serve_degradation:'Serve Degradation',set_pattern:'Set Pattern',fatigue:'Fatigue',ml_value:'ML Value',endgame:'Endgame Scalp',break_momentum:'Break Momentum',second_set_fade:'Second Set Fade'};
const MKT_LABEL={match_winner:'Match Winner',next_game:'Next Game',next_set:'Next Set',set_winner_set2:'Set 2 Winner'};

function fmtUptime(s){if(s==null||isNaN(s))return '—';if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m';}
const _IST={timeZone:'Asia/Kolkata'};
function fmtTime(iso){
  const d=new Date(iso.endsWith('Z')||iso.includes('+')?iso:iso+'Z');
  return d.toLocaleTimeString('en-IN',{..._IST,hour:'2-digit',minute:'2-digit'})+ ' IST';
}

// ── MATCHES ───────────────────────────────────────────────────────────────────
function renderMatches(matches){
  const el=document.getElementById('matches');
  if(!matches.length){
    el.innerHTML='<div class="empty">No live matches tracked right now.<br><span style="font-size:11px;color:#334155">ESPN updates every 30s · BetsAPI covers all tours if token is set · Odds API shows in-play matches</span></div>';
    return;
  }
  el.innerHTML=matches.map(renderMatch).join('');
}

// H2H cache so we don't re-fetch on every render
const _h2hCache={};
async function loadH2H(matchId,p1,p2,surface){
  const key=matchId;
  if(_h2hCache[key]) return _h2hCache[key];
  try{
    const r=await fetch(`/api/h2h?p1=${encodeURIComponent(p1)}&p2=${encodeURIComponent(p2)}&surface=${encodeURIComponent(surface||'')}`);
    const d=await r.json();
    _h2hCache[key]=d;
    return d;
  }catch(e){return null;}
}

function toggleH2H(matchId,p1,p2,surface){
  const panel=document.getElementById('h2h-'+matchId);
  if(!panel) return;
  const isOpen=panel.style.display!=='none';
  if(isOpen){panel.style.display='none';return;}
  panel.style.display='block';
  if(panel.dataset.loaded) return;
  panel.innerHTML='<div style="padding:16px;color:#64748b;text-align:center;font-size:12px">Loading H2H data…</div>';
  loadH2H(matchId,p1,p2,surface).then(data=>{
    if(!data){panel.innerHTML='<div style="padding:12px;color:#475569;font-size:11px;text-align:center">No H2H data in database yet</div>';return;}
    panel.innerHTML=renderH2HPanel(data,p1,p2);
    panel.dataset.loaded='1';
  });
}

function renderH2HPanel(data,p1,p2){
  const h=data.h2h||{};
  const f1=data.p1_form||{};
  const f2=data.p2_form||{};
  const p1s=p1.split(' ').pop();
  const p2s=p2.split(' ').pop();
  const total=h.total_meetings||0;

  // H2H header
  let h2hHeader='<div style="padding:10px 12px;background:#0f172a;border-bottom:1px solid #1e293b">';
  if(total===0){
    h2hHeader+='<div style="color:#475569;font-size:12px;text-align:center">No historical H2H found in database</div>';
  } else {
    const p1pct=total>0?Math.round(h.p1_wins/total*100):50;
    const p2pct=100-p1pct;
    h2hHeader+=`<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:13px;font-weight:800;color:#f1f5f9">${h.p1_wins}</span>
      <span style="font-size:10px;color:#64748b;font-weight:600;flex:1;text-align:center">H2H · ${total} meetings</span>
      <span style="font-size:13px;font-weight:800;color:#f1f5f9">${h.p2_wins}</span>
    </div>
    <div style="display:flex;height:6px;border-radius:3px;overflow:hidden;margin-bottom:4px">
      <div style="width:${p1pct}%;background:#38bdf8"></div>
      <div style="width:${p2pct}%;background:#f97316"></div>
    </div>`;
    if(h.surface_meetings>0){
      h2hHeader+=`<div style="display:flex;justify-content:space-between;font-size:10px;color:#64748b;margin-top:4px">
        <span>${esc(p1s)} ${h.p1_surface_wins}-${h.p2_surface_wins} ${esc(p2s)} on surface</span>
        <span>${h.surface_meetings} matches</span>
      </div>`;
    }
  }
  h2hHeader+='</div>';

  // Last meetings
  let meetings='';
  if((h.last_meetings||[]).length>0){
    meetings='<div style="padding:8px 12px;border-bottom:1px solid #1e293b">';
    meetings+=`<div style="font-size:10px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Recent Meetings</div>`;
    for(const m of h.last_meetings){
      const isP1Win=m.winner==='p1';
      const surfColor={clay:'#f97316',grass:'#22c55e',hard:'#38bdf8',indoor_hard:'#818cf8'}[m.surface]||'#94a3b8';
      meetings+=`<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #0f172a;font-size:11px">
        <span style="color:#475569;width:36px;flex-shrink:0">${m.year}</span>
        <span style="color:${surfColor};font-size:9px;width:8px;flex-shrink:0">●</span>
        <span style="color:#64748b;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.tournament)}</span>
        <span style="font-size:9px;color:#475569;width:28px;text-align:center">${m.round||''}</span>
        <span style="font-weight:700;color:${isP1Win?'#38bdf8':'#f97316'};width:60px;text-align:right;flex-shrink:0">${esc(isP1Win?p1s:p2s)}</span>
        <span style="color:#334155;width:4px">·</span>
        <span style="color:#94a3b8;width:70px;flex-shrink:0;font-size:10px">${esc(m.score)}</span>
      </div>`;
    }
    meetings+='</div>';
  }

  // Form blocks
  function formBubbles(form){
    return (form.recent||[]).map(f=>`<span style="display:inline-block;width:18px;height:18px;border-radius:3px;background:${f.won?'#166534':'#7f1d1d'};color:${f.won?'#4ade80':'#fca5a5'};font-size:9px;font-weight:800;line-height:18px;text-align:center" title="${f.won?'W':'L'} vs ${f.opponent} (${f.tournament})">${f.won?'W':'L'}</span>`).join('');
  }

  function statRow(label,v1,v2,higherIsBetter=true){
    const n1=parseFloat(v1)||0, n2=parseFloat(v2)||0;
    const p1b=higherIsBetter?(n1>n2):(n1<n2);
    const p2b=higherIsBetter?(n2>n1):(n2<n1);
    return `<tr>
      <td style="font-size:12px;font-weight:${p1b?'800':'500'};color:${p1b?'#38bdf8':'#94a3b8'};padding:4px 0;text-align:left">${v1}</td>
      <td style="font-size:10px;color:#475569;text-align:center;padding:4px 8px">${label}</td>
      <td style="font-size:12px;font-weight:${p2b?'800':'500'};color:${p2b?'#f97316':'#94a3b8'};padding:4px 0;text-align:right">${v2}</td>
    </tr>`;
  }

  const stats=`<div style="padding:8px 12px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div>
        <div style="font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${esc(p1s)} FORM (last ${f1.total||0})</div>
        <div style="display:flex;gap:2px;flex-wrap:wrap">${formBubbles(f1)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:10px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${esc(p2s)} FORM (last ${f2.total||0})</div>
        <div style="display:flex;gap:2px;flex-wrap:wrap;justify-content:flex-end">${formBubbles(f2)}</div>
      </div>
    </div>
    <table style="width:100%;border-collapse:collapse">
      ${statRow('Win Rate',`${f1.win_rate||0}%`,`${f2.win_rate||0}%`)}
      ${statRow('1st Serve %',`${f1.avg_first_serve_pct||0}%`,`${f2.avg_first_serve_pct||0}%`)}
      ${statRow('Aces/Match',f1.avg_aces||0,f2.avg_aces||0)}
      ${statRow('BP Save %',`${f1.bp_save_pct||0}%`,`${f2.bp_save_pct||0}%`)}
      ${f1.surface_win_rate!=null?statRow('Surface Win %',`${f1.surface_win_rate}%`,`${f2.surface_win_rate||0}%`):''}
    </table>
  </div>`;

  return h2hHeader+meetings+stats;
}

function renderMatch(m){
  if(m.is_upcoming) return renderTennisUpcoming(m);
  const surf=SURFACE_CLASS[m.surface]||'';
  const surfLabel=m.surface.replace('_',' ');
  const hasOdds=m.odds_p1>1.01&&m.odds_p2>1.01;
  const hasScore=m.sets_p1>0||m.sets_p2>0||m.games_p1>0||m.games_p2>0;
  const isOddsOnly=m.source==='odds';
  const sets=m.set_scores||[];
  const numSets=sets.length;

  // Odds-only match (no live score data from ESPN/BetsAPI)
  if(isOddsOnly){
    const favP1=hasOdds&&m.odds_p1<m.odds_p2;
    const impliedP1=hasOdds?Math.round(100/m.odds_p1)+'%':'—';
    const impliedP2=hasOdds?Math.round(100/m.odds_p2)+'%':'—';
    const mid2=m.match_id.replace(/[^a-z0-9]/gi,'_');
    return `<div class="match-card">
      <div class="mc-header">
        <span class="source-tag" style="background:#1a2e1a;color:#86efac">LIVE</span>
        <span class="${surf}">${surfLabel}</span>
        <span>&middot; ${esc(m.tournament)}</span>
        <span style="margin-left:auto;font-size:10px;color:#475569">odds only · no score feed</span>
      </div>
      <div class="mc-players">
        <div class="mc-player">
          <div class="mc-name">${esc(m.player1)}</div>
          <div style="font-size:11px;color:#64748b;margin-top:3px">impl. ${impliedP1}</div>
        </div>
        <div class="mc-center">
          <div class="mc-set-label">IN PLAY</div>
          <div style="font-size:20px;font-weight:900;color:#475569;letter-spacing:2px">vs</div>
        </div>
        <div class="mc-player right">
          <div class="mc-name">${esc(m.player2)}</div>
          <div style="font-size:11px;color:#64748b;margin-top:3px">impl. ${impliedP2}</div>
        </div>
      </div>
      <div class="mc-odds-row">
        <div class="mc-odds-box">
          <div class="mc-odds-label">Back ${esc(m.player1.split(' ').pop())}</div>
          <div class="mc-odds-val ${hasOdds?(favP1?'fav':'dog'):'none'}">${hasOdds?m.odds_p1.toFixed(2):'—'}</div>
        </div>
        <div class="mc-odds-box">
          <div class="mc-odds-label">Back ${esc(m.player2.split(' ').pop())}</div>
          <div class="mc-odds-val ${hasOdds?(!favP1?'fav':'dog'):'none'}">${hasOdds?m.odds_p2.toFixed(2):'—'}</div>
        </div>
      </div>
      <div onclick="toggleH2H('${mid2}','${esc(m.player1)}','${esc(m.player2)}','${m.surface}')"
        style="padding:8px 12px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;background:#0c1929;border-top:1px solid #1e293b">
        <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em">H2H &amp; Player Stats</span>
        <span style="font-size:14px;color:#475569">▾</span>
      </div>
      <div id="h2h-${mid2}" style="display:none;border-top:1px solid #1e293b"></div>
    </div>`;
  }

  // Header
  const tbBadge=m.is_tiebreak?'<span style="background:#7c3aed;color:#ddd6fe;font-size:9px;padding:1px 5px;border-radius:3px;font-weight:700;margin-left:auto">TIEBREAK</span>':'';
  const dur=m.duration_mins>0?`<span>${m.duration_mins} min</span>`:'';
  const header=`<div class="mc-header">
    <span class="source-tag">${m.source.toUpperCase()}</span>
    <span class="${surf}">${surfLabel}</span>
    <span>&middot; ${esc(m.tournament)}</span>
    ${dur}
    ${tbBadge}
  </div>`;

  // Player row
  const p1Lead=m.sets_p1>m.sets_p2||(!m.sets_p1&&!m.sets_p2&&m.games_p1>m.games_p2);
  const p2Lead=m.sets_p2>m.sets_p1||(!m.sets_p1&&!m.sets_p2&&m.games_p2>m.games_p1);
  const p1LeadBadge=p1Lead?'<span class="mc-leading">LEADING</span>':'';
  const p2LeadBadge=p2Lead?'<span class="mc-leading">LEADING</span>':'';

  const gameScore=hasScore?`${m.games_p1} : ${m.games_p2}`:'— : —';
  const setLabel=`Set ${m.current_set}${m.is_tiebreak?' · TB':''}`;

  const players=`<div class="mc-players">
    <div class="mc-player">
      <div class="mc-name">${esc(m.player1)}</div>
      ${hasScore?`<div class="mc-sets-won">${m.sets_p1}</div>`:''}
      ${p1LeadBadge}
    </div>
    <div class="mc-center">
      <div class="mc-set-label">${setLabel}</div>
      <div class="mc-game-score">${gameScore}</div>
      ${m.duration_mins>0?`<div class="mc-duration">${m.duration_mins} min</div>`:''}
    </div>
    <div class="mc-player right">
      <div class="mc-name">${esc(m.player2)}</div>
      ${hasScore?`<div class="mc-sets-won">${m.sets_p2}</div>`:''}
      ${p2LeadBadge}
    </div>
  </div>`;

  // Fairplay-style scoreboard
  let scoreboard='';
  if(numSets>0){
    const p1Short=m.player1.split(' ').pop();
    const p2Short=m.player2.split(' ').pop();
    const setHeaders=sets.map((_,i)=>`<th>S${i+1}</th>`).join('');
    const p1Cells=sets.map((s,i)=>{
      if(s.current) return `<td class="sb-cur">${s.p1}</td>`;
      return `<td class="${s.p1>s.p2?'sb-won':'sb-lost'}">${s.p1}</td>`;
    }).join('');
    const p2Cells=sets.map((s,i)=>{
      if(s.current) return `<td class="sb-cur">${s.p2}</td>`;
      return `<td class="${s.p2>s.p1?'sb-won':'sb-lost'}">${s.p2}</td>`;
    }).join('');
    scoreboard=`<div class="mc-scoreboard"><table class="sb-table">
      <thead><tr>
        <th class="pname"></th>${setHeaders}
        <th>Sets</th>
      </tr></thead>
      <tbody>
        <tr>
          <td class="pname">${esc(p1Short)}</td>${p1Cells}
          <td class="${m.sets_p1>=m.sets_p2?'sb-sets-total sb-won':'sb-sets-total sb-lost'}">${m.sets_p1}</td>
        </tr>
        <tr>
          <td class="pname">${esc(p2Short)}</td>${p2Cells}
          <td class="${m.sets_p2>=m.sets_p1?'sb-sets-total sb-won':'sb-sets-total sb-lost'}">${m.sets_p2}</td>
        </tr>
      </tbody>
    </table></div>`;
  }

  // Win probability bar
  let probBar='';
  if(m.win_prob_p1>0||m.win_prob_p2>0){
    const p1w=Math.max(5,Math.min(95,m.win_prob_p1));
    const p2w=Math.max(5,Math.min(95,m.win_prob_p2));
    const p1Short=m.player1.split(' ').pop();
    const p2Short=m.player2.split(' ').pop();
    probBar=`<div class="mc-prob">
      <span class="prob-name">${esc(p1Short)}</span>
      <span class="prob-pct">${m.win_prob_p1}%</span>
      <div class="prob-bar-wrap">
        <div class="prob-bar-p1" style="width:${p1w}%"></div>
        <div class="prob-bar-p2" style="width:${p2w}%"></div>
      </div>
      <span class="prob-pct right">${m.win_prob_p2}%</span>
      <span class="prob-name right">${esc(p2Short)}</span>
    </div>`;
  }

  // Odds
  const favP1=hasOdds&&m.odds_p1<m.odds_p2;
  const oddsRow=`<div class="mc-odds-row">
    <div class="mc-odds-box">
      <div class="mc-odds-label">Back ${esc(m.player1.split(' ').pop())}</div>
      <div class="mc-odds-val ${hasOdds?(favP1?'fav':'dog'):'none'}">${hasOdds?m.odds_p1.toFixed(2):'—'}</div>
      ${!hasOdds?'<div class="mc-odds-hint">No live odds yet</div>':''}
    </div>
    <div class="mc-odds-box">
      <div class="mc-odds-label">Back ${esc(m.player2.split(' ').pop())}</div>
      <div class="mc-odds-val ${hasOdds?(!favP1?'fav':'dog'):'none'}">${hasOdds?m.odds_p2.toFixed(2):'—'}</div>
    </div>
  </div>`;

  const mid=m.match_id.replace(/[^a-z0-9]/gi,'_');
  const h2hBtn=`<div onclick="toggleH2H('${mid}','${esc(m.player1)}','${esc(m.player2)}','${m.surface}')"
    style="padding:8px 12px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;background:#0c1929;border-top:1px solid #1e293b">
    <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em">H2H &amp; Player Stats</span>
    <span style="font-size:14px;color:#475569">▾</span>
  </div>
  <div id="h2h-${mid}" style="display:none;border-top:1px solid #1e293b"></div>`;

  return `<div class="match-card">${header}${players}${scoreboard}${probBar}${oddsRow}${h2hBtn}</div>`;
}

// ── TENNIS UPCOMING ───────────────────────────────────────────────────────────
function renderTennisUpcoming(m){
  const surf=SURFACE_CLASS[m.surface]||'';
  const surfLabel=m.surface.replace('_',' ');
  const hasOdds=m.odds_p1>1.01&&m.odds_p2>1.01;
  const until=minsUntil(m.start_time);
  const kt=fmtKickoff(m.start_time);
  const favP1=hasOdds&&m.odds_p1<m.odds_p2;
  return `<div class="match-card" style="opacity:.82">
    <div class="mc-header">
      <span class="source-tag" style="background:#1a2e1a;color:#6ee7b7">UPCOMING</span>
      <span class="${surf}">${surfLabel}</span>
      <span>&middot; ${esc(m.tournament)}</span>
      <span style="margin-left:auto;font-size:11px;color:#6ee7b7;font-weight:700">⏰ ${esc(until||kt||'')}</span>
    </div>
    <div class="mc-players">
      <div class="mc-player">
        <div class="mc-name">${esc(m.player1)}</div>
        ${hasOdds?`<div style="font-size:11px;color:#64748b;margin-top:3px">impl. ${Math.round(100/m.odds_p1)}%</div>`:''}
      </div>
      <div class="mc-center">
        <div style="font-size:13px;color:#64748b;font-weight:700">vs</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:4px">${esc(kt)}</div>
      </div>
      <div class="mc-player right">
        <div class="mc-name">${esc(m.player2)}</div>
        ${hasOdds?`<div style="font-size:11px;color:#64748b;margin-top:3px">impl. ${Math.round(100/m.odds_p2)}%</div>`:''}
      </div>
    </div>
    ${hasOdds?`<div class="mc-odds-row">
      <div class="mc-odds-box">
        <div class="mc-odds-label">Back ${esc(m.player1.split(' ').pop())}</div>
        <div class="mc-odds-val ${favP1?'fav':'dog'}">${m.odds_p1.toFixed(2)}</div>
      </div>
      <div class="mc-odds-box">
        <div class="mc-odds-label">Back ${esc(m.player2.split(' ').pop())}</div>
        <div class="mc-odds-val ${!favP1?'fav':'dog'}">${m.odds_p2.toFixed(2)}</div>
      </div>
    </div>`:''}
  </div>`;
}

// ── SIGNALS ───────────────────────────────────────────────────────────────────
function renderSignals(signals){
  const el=document.getElementById('signals');
  if(!signals.length){el.innerHTML='<div class="empty">No signals in the last 24 hours</div>';return;}
  el.innerHTML=signals.map(renderSignal).join('');
}

function renderSignal(s){
  const emoji=SIG_EMOJI[s.signal_type]||'🎾';
  const name=SIG_NAME[s.signal_type]||s.signal_type.replace(/_/g,' ');
  const mkt=MKT_LABEL[s.market]||s.market;
  const surf=SURFACE_DOT[s.surface]||'⚪';

  // Outcome badge
  let outcomeBadge='';
  if(s.outcome==='won') outcomeBadge='<span class="sc-outcome-won">✓ WON</span>';
  else if(s.outcome==='lost') outcomeBadge='<span class="sc-outcome-lost">✗ LOST</span>';
  else outcomeBadge='<span class="sc-outcome-pending">Pending</span>';

  // Win probability comparison
  const modelProb=s.model_win_prob||0;
  const marketProb=s.odds>1?Math.round(100/s.odds*10)/10:0;
  const probHtml=`<div class="sc-probs">
    <div class="sc-prob-row">
      <span class="sc-prob-lbl">Model</span>
      <div class="sc-prob-bar-wrap"><div class="sc-prob-bar sc-prob-bar-model" style="width:${Math.min(100,modelProb)}%"></div></div>
      <span class="sc-prob-pct">${modelProb}%</span>
    </div>
    <div class="sc-prob-row">
      <span class="sc-prob-lbl">Market</span>
      <div class="sc-prob-bar-wrap"><div class="sc-prob-bar sc-prob-bar-market" style="width:${Math.min(100,marketProb)}%"></div></div>
      <span class="sc-prob-pct">${marketProb}%</span>
    </div>
  </div>`;

  const confBar='█'.repeat(Math.round(s.confidence/10))+'░'.repeat(10-Math.round(s.confidence/10));
  const scoreNote=s.score_at_signal?`<span style="color:#64748b;font-size:10px"> · ${esc(s.score_at_signal)}</span>`:'';

  return `<div class="signal-card">
    <div class="sc-header">
      <span class="sc-type sc-${s.signal_type}">${emoji} ${name}</span>
      ${outcomeBadge}
      <span class="sc-time">${fmtTime(s.timestamp)}</span>
    </div>

    <div class="sc-bet-banner">
      <span class="sc-bet-arrow">🎯</span>
      <div>
        <div class="sc-bet-label">Bet on</div>
        <div class="sc-bet-player">${esc(s.player_name)}</div>
        <div class="sc-bet-market">${surf} ${esc(s.tournament)} &middot; ${mkt}</div>
      </div>
      <div class="sc-bet-odds">
        <div class="sc-bet-odds-val">${s.odds.toFixed(2)}</div>
        <div class="sc-bet-odds-fair">fair: ${s.fair_odds.toFixed(2)}</div>
      </div>
    </div>

    <div class="sc-body">
      <div class="sc-match">vs <strong>${esc(s.opponent_name)}</strong>${scoreNote}</div>
      <div class="sc-why">${esc(s.trigger)}</div>
      ${probHtml}
    </div>

    <div class="sc-footer">
      <span class="sc-conf">${s.confidence}%</span>
      <span class="sc-conf-bar">${confBar}</span>
      <span class="sc-edge">+${s.edge_pct}% edge</span>
      <span class="sc-stake">Stake ${s.stake_pct}%</span>
    </div>
  </div>`;
}

// ── STATUS ────────────────────────────────────────────────────────────────────
function renderStatus(st){
  const fs=st.flashscore||{}, espn=st.espn||{}, sc=st.sofascore||{}, oa=st.odds_api||{}, ba=st.bets_api||{}, sr=st.sportradar||{}, as_=st.api_sports||{};
  const sources=[
    {name:'ESPN',ok:true,detail:'Live scores (always on)'},
    {name:'API-Sports',ok:as_.key_set,detail:as_.key_set?`${as_.last_live||0} live · ${as_.last_scheduled||0} upcoming · ${as_.quota_remaining!=null?as_.quota_remaining+' req left today':'checking...'} · every ${as_.poll_interval_secs}s`:'No key — add API_SPORTS_KEY (100 req/day FREE)'},
    {name:'Sportradar',ok:sr.key_set,detail:sr.key_set?`All tours+leagues · every ${sr.poll_interval_secs}s`:'No key — add SPORTRADAR_API_KEY (free trial)'},
    {name:'BetsAPI',ok:ba.token_set,detail:ba.token_set?`Live odds · ${ba.consecutive_failures||0} failures`:'No token — add BETS_API_TOKEN'},
    {name:'Sofascore',ok:!sc.blocked,detail:sc.blocked?'Blocked on cloud IP':'Available (serve stats)'},
    {name:'Flashscore',ok:fs.http_ok,detail:fs.http_ok?'OK':`${fs.consecutive_failures||0} failures`},
    {name:'Odds API',ok:oa.key_set,detail:oa.key_set?`${oa.last_events_fetched||0} events · ${oa.quota_remaining!=null?oa.quota_remaining+' credits left':'checking...'} · every ${oa.poll_interval_secs}s`:'No API key — add ODDS_API_KEY'},
  ];
  document.getElementById('sources').innerHTML=sources.map(s=>`
    <div class="status-card">
      <div class="status-name"><span class="dot ${s.ok?'dot-green':'dot-red'}"></span>${s.name}</div>
      <div class="status-val">${s.detail}</div>
    </div>`).join('');
}

function esc(s){
  const d=document.createElement('div');
  d.textContent=s||'';
  return d.innerHTML;
}

// ── TAB SWITCHING ─────────────────────────────────────────────────────────────
function switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.toggle('active',c.id==='tab-'+tab));
}

// ── FOOTBALL MATCHES ──────────────────────────────────────────────────────────
const FB_SIG_NAME={
  late_lead:'⏱ Late Lead',
  heavy_fav_dominating:'💪 Fav Dominating',
  late_draw_fade:'🔄 Draw Fade',
  red_card_advantage:'🟥 Red Card Edge',
  clean_sheet_likely:'🧤 Clean Sheet',
};
const FB_MKT={match_winner:'Match Winner',draw_no_bet:'Draw No Bet',asian_handicap_0:'AH 0'};

function renderFootballMatches(matches){
  const el=document.getElementById('fb-matches');
  if(!matches.length){el.innerHTML='<div class="empty">No live or upcoming football matches in next 3 hours</div>';return;}
  el.innerHTML=matches.map(renderFootballMatch).join('');
}

function fmtKickoff(iso){
  if(!iso) return '';
  const d=new Date(iso.endsWith('Z')||iso.includes('+')?iso:iso+'Z');
  return d.toLocaleTimeString('en-IN',{..._IST,hour:'2-digit',minute:'2-digit'})
    +' IST ('+d.toLocaleDateString('en-IN',{..._IST,weekday:'short',month:'short',day:'numeric'})+')';
}

function minsUntil(iso){
  if(!iso) return null;
  const d=new Date(iso.endsWith('Z')||iso.includes('+')?iso:iso+'Z');
  const diff=Math.round((d-Date.now())/60000);
  if(diff<=0) return 'Starting now';
  if(diff<60) return `in ${diff} min`;
  const h=Math.floor(diff/60),m=diff%60;
  return `in ${h}h${m>0?' '+m+'m':''}`;
}

function renderFootballMatch(m){
  if(m.is_scheduled) return renderFootballUpcoming(m);

  const hasOdds=m.home_odds>1.01&&m.away_odds>1.01&&m.draw_odds>1.01;
  const favHome=hasOdds&&m.home_odds<=m.away_odds&&m.home_odds<=m.draw_odds;
  const favAway=hasOdds&&m.away_odds<m.home_odds&&m.away_odds<=m.draw_odds;
  const homeLeads=m.home_score>m.away_score;
  const awayLeads=m.away_score>m.home_score;

  const leagueLabel=m.league_key.replace(/\./g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  const htBadge=m.is_halftime?'<span class="fb-ht-badge">HT</span>':'';
  const etBadge=m.is_extra_time?'<span class="fb-et-badge">ET</span>':'';
  const minDisplay=m.is_halftime?'HT':(m.minute>0?m.minute+"'":"?'");
  const header=`<div class="fb-header">
    <span class="fb-league-tag">${esc(leagueLabel)}</span>
    <span>${esc(m.tournament)}</span>
    ${htBadge}${etBadge}
    <span class="fb-minute"><span class="fb-live-dot"></span>${minDisplay}</span>
  </div>`;

  const homeRC=Array(m.home_red_cards).fill('<span class="fb-red-card"></span>').join('');
  const awayRC=Array(m.away_red_cards).fill('<span class="fb-red-card"></span>').join('');
  const scoreRow=`<div class="fb-score-row">
    <div class="fb-team">
      <div class="fb-team-name">${esc(m.home_team)}</div>
      ${homeRC?`<div class="fb-red-cards">${homeRC}</div>`:''}
      ${homeLeads?'<span class="fb-leading-badge">LEADING</span>':''}
    </div>
    <div class="fb-score-center">
      <div class="fb-score">${m.home_score}&nbsp;:&nbsp;${m.away_score}</div>
    </div>
    <div class="fb-team right">
      <div class="fb-team-name">${esc(m.away_team)}</div>
      ${awayRC?`<div class="fb-red-cards" style="justify-content:flex-end">${awayRC}</div>`:''}
      ${awayLeads?'<span class="fb-leading-badge">LEADING</span>':''}
    </div>
  </div>`;

  const oddsRow=`<div class="fb-odds-row">
    <div class="fb-odds-box">
      <div class="fb-odds-label">1 · ${esc(m.home_team.split(' ').slice(-1)[0])}</div>
      <div class="fb-odds-val ${hasOdds?(favHome?'fav':''):'none'}">${hasOdds?m.home_odds.toFixed(2):'—'}</div>
    </div>
    <div class="fb-odds-box">
      <div class="fb-odds-label">X · Draw</div>
      <div class="fb-odds-val draw">${hasOdds?m.draw_odds.toFixed(2):'—'}</div>
    </div>
    <div class="fb-odds-box">
      <div class="fb-odds-label">2 · ${esc(m.away_team.split(' ').slice(-1)[0])}</div>
      <div class="fb-odds-val ${hasOdds?(favAway?'fav':''):'none'}">${hasOdds?m.away_odds.toFixed(2):'—'}</div>
    </div>
  </div>`;

  return `<div class="fb-card">${header}${scoreRow}${oddsRow}</div>`;
}

function renderFootballUpcoming(m){
  const leagueLabel=m.league_key.replace(/\./g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  const until=minsUntil(m.kickoff_time);
  const kt=fmtKickoff(m.kickoff_time);
  return `<div class="fb-card" style="opacity:.82">
    <div class="fb-header">
      <span class="fb-league-tag" style="background:#1e3a2e;color:#6ee7b7">${esc(leagueLabel)}</span>
      <span>${esc(m.tournament)}</span>
      <span class="fb-minute" style="color:#6ee7b7">⏰ ${esc(until||'')}</span>
    </div>
    <div class="fb-score-row">
      <div class="fb-team"><div class="fb-team-name">${esc(m.home_team)}</div></div>
      <div class="fb-score-center">
        <div style="font-size:13px;color:#64748b;font-weight:700">UPCOMING</div>
        <div style="font-size:12px;color:#94a3b8;margin-top:4px">${esc(kt)}</div>
      </div>
      <div class="fb-team right"><div class="fb-team-name">${esc(m.away_team)}</div></div>
    </div>
  </div>`;
}

// ── FOOTBALL SIGNALS ──────────────────────────────────────────────────────────
function renderFootballSignals(signals){
  const el=document.getElementById('fb-signals');
  if(!signals.length){el.innerHTML='<div class="empty">No football signals in the last 24 hours</div>';return;}
  el.innerHTML=signals.map(renderFootballSignal).join('');
}

function renderFootballSignal(s){
  const name=FB_SIG_NAME[s.signal_type]||s.signal_type.replace(/_/g,' ');
  const mkt=FB_MKT[s.market]||s.market;
  const side=s.is_home?'🏠 HOME':'✈️ AWAY';
  const confBar='█'.repeat(Math.round(s.confidence/10))+'░'.repeat(10-Math.round(s.confidence/10));
  const modelPct=s.fair_odds>1?Math.round(100/s.fair_odds):0;
  const mktPct=s.current_odds>1?Math.round(100/s.current_odds):0;
  const modelBar='▓'.repeat(Math.round(modelPct/5))+'░'.repeat(20-Math.round(modelPct/5));
  const mktBarStr='▓'.repeat(Math.round(mktPct/5))+'░'.repeat(20-Math.round(mktPct/5));

  return `<div class="fb-sig-card">
    <div class="fb-sig-header">
      <span class="fb-sig-type fb-sig-${s.signal_type}">${name}</span>
      <span class="fb-sig-time">${fmtTime(s.timestamp)}</span>
    </div>
    <div class="sc-bet-banner">
      <span class="sc-bet-arrow">🎯</span>
      <div>
        <div class="sc-bet-label">Bet on</div>
        <div class="sc-bet-player">${esc(s.team_to_back)}</div>
        <div class="sc-bet-market">${side} · ${mkt} · ${esc(s.tournament)}</div>
      </div>
      <div class="sc-bet-odds">
        <div class="sc-bet-odds-val">${s.current_odds.toFixed(2)}</div>
        <div class="sc-bet-odds-fair">fair: ${s.fair_odds.toFixed(2)}</div>
      </div>
    </div>
    <div class="sc-body">
      <div class="sc-match">vs <strong>${esc(s.opponent)}</strong> · <span style="color:#f59e0b">${esc(s.score_summary)}</span></div>
      <div class="sc-why">${esc(s.trigger)}</div>
      <div class="sc-probs">
        <div class="sc-prob-row">
          <span class="sc-prob-lbl">Model</span>
          <div class="sc-prob-bar-wrap"><div class="sc-prob-bar sc-prob-bar-model" style="width:${Math.min(100,modelPct)}%"></div></div>
          <span class="sc-prob-pct">${modelPct}%</span>
        </div>
        <div class="sc-prob-row">
          <span class="sc-prob-lbl">Market</span>
          <div class="sc-prob-bar-wrap"><div class="sc-prob-bar sc-prob-bar-market" style="width:${Math.min(100,mktPct)}%"></div></div>
          <span class="sc-prob-pct">${mktPct}%</span>
        </div>
      </div>
    </div>
    <div class="sc-footer">
      <span class="sc-conf">${s.confidence}%</span>
      <span class="sc-conf-bar">${confBar}</span>
      <span class="sc-edge">+${s.edge_pct}% edge</span>
      <span class="sc-stake">Stake ${s.stake_pct}%</span>
    </div>
  </div>`;
}

// ── SCALPING ────────────────────────────────────────────────────────────────────
const TIER_LABEL={lock:'🔒 LOCK',strong:'✅ STRONG',watch:'👀 WATCH'};
function renderScalping(opps){
  const el=document.getElementById('scalp-list');
  const badge=document.getElementById('scalp-count-badge');
  const locks=opps.filter(o=>o.tier==='lock').length;
  if(badge){
    if(opps.length){badge.style.display='inline-block';badge.textContent=opps.length;
      badge.style.background=locks?'#16a34a':'#0ea5e9';}
    else{badge.style.display='none';}
  }
  if(!opps.length){
    el.innerHTML='<div class="empty">No sure-shot opportunities right now.<br><span style="font-size:11px;color:#334155">Appears when a favourite leads decisively & is priced ≤'+'1.25. Needs live odds (Odds API / Parimatch push) for best accuracy.</span></div>';
    return;
  }
  el.innerHTML=opps.map(renderScalpCard).join('');
}

function renderScalpCard(o){
  const oddsTxt=o.market_odds>1.01?o.market_odds.toFixed(2):'—';
  const hasOdds=o.market_odds>1.01;
  const evClass=o.ev_pct>=0?'scalp-ev-pos':'scalp-ev-neg';
  const evTxt=(o.ev_pct>=0?'+':'')+o.ev_pct+'%';
  const win=o.win_prob;
  const reasons=(o.reasons||[]).map(r=>`<span class="scalp-reason">${esc(r)}</span>`).join('');
  const window=o.scalp_window?'<span class="scalp-window">⚡ SCALP WINDOW</span>':'';
  const serving=o.is_serving?' 🎾 serving':'';
  return `<div class="scalp-card tier-${o.tier}">
    <div class="scalp-head">
      <span class="scalp-tier tier-${o.tier}">${TIER_LABEL[o.tier]||o.tier}</span>
      <span class="scalp-src">${esc(o.source)}</span>
      <span class="scalp-tourney">${esc(o.tournament)}</span>
      ${window}
    </div>
    <div class="scalp-body">
      <div class="scalp-bet">
        <div class="scalp-bet-info">
          <div class="scalp-bet-label">Back to win${serving}</div>
          <div class="scalp-player">${esc(o.player_name)}</div>
          <div class="scalp-vs">vs ${esc(o.opponent_name)}</div>
        </div>
        <div class="scalp-odds">
          <div class="scalp-odds-val ${hasOdds?'':'none'}">${oddsTxt}</div>
          <div class="scalp-odds-cap">${hasOdds?'back odds':'no odds'}</div>
        </div>
      </div>
      <div class="scalp-score">${esc(o.score_summary)}</div>
      <div class="scalp-prob-wrap"><div class="scalp-prob-bar" style="width:${Math.min(100,win)}%"></div></div>
      <div class="scalp-prob-lbls"><span>Model win prob</span><span><b>${win}%</b>${hasOdds?' · market '+o.market_implied+'%':''}</span></div>
      ${reasons?`<div class="scalp-reasons">${reasons}</div>`:''}
    </div>
    <div class="scalp-foot">
      ${hasOdds?`<span class="scalp-stat">Edge <b>${(o.edge_pct>=0?'+':'')+o.edge_pct}%</b></span>`:''}
      ${hasOdds?`<span class="scalp-stat">EV <span class="${evClass}">${evTxt}</span></span>`:''}
      <span class="scalp-stat" style="margin-left:auto">${esc(o.surface)}</span>
    </div>
  </div>`;
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
// Fetch JSON that never rejects — a single failing endpoint must not blank the whole dashboard.
function jget(url,fallback){return fetch(url).then(r=>r.ok?r.json():fallback).catch(()=>fallback);}
async function refresh(){
  try{
    const [status,matches,signals,fbMatches,fbSignals,scalps]=await Promise.all([
      jget('/api/status',{}),
      jget('/api/matches',[]),
      jget('/api/signals',[]),
      jget('/api/football/matches',[]),
      jget('/api/football/signals',[]),
      jget('/api/scalping',[]),
    ]);
    document.getElementById('stat-matches').textContent=matches.length;
    document.getElementById('stat-fb-matches').textContent=fbMatches.length;
    document.getElementById('stat-signals').textContent=signals.length+fbSignals.length;
    document.getElementById('stat-uptime').textContent=fmtUptime(status.uptime_seconds);
    document.getElementById('live-count').textContent=matches.length+' tennis';
    renderStatus(status);
    renderMatches(matches);
    renderSignals(signals);
    renderFootballMatches(fbMatches);
    renderFootballSignals(fbSignals);
    renderScalping(scalps||[]);
    document.getElementById('last-updated').textContent='Updated: '+new Date().toLocaleTimeString('en-IN',_IST)+' IST';
    document.getElementById('refresh-label').textContent='Next in 30s';
  }catch(e){
    console.error('refresh error:', e);
    document.getElementById('refresh-label').textContent='Error — retrying…';
  }
}
refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""


_DATA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Dump — Tennis Bet</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding-bottom:40px}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
header h1{font-size:17px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
header a.nav-btn{background:#0ea5e9;color:#fff;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap}
header a.nav-btn:hover{background:#0284c7}
.controls{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px;color:#94a3b8}
.controls input{width:70px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:4px 8px}
.controls button{background:#0ea5e9;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-weight:600;cursor:pointer}
.controls button:hover{background:#0284c7}
.toc{padding:12px 20px;display:flex;flex-wrap:wrap;gap:8px}
.toc a{font-size:12px;background:#1e293b;border:1px solid #334155;color:#cbd5e1;padding:4px 10px;border-radius:9999px;text-decoration:none}
.toc a:hover{border-color:#0ea5e9;color:#fff}
.toc a b{color:#38bdf8}
section{padding:8px 20px 20px}
.tbl-head{display:flex;align-items:baseline;gap:10px;margin:18px 0 8px;border-bottom:1px solid #334155;padding-bottom:6px}
.tbl-head h2{font-size:15px;font-weight:700;color:#f1f5f9}
.tbl-head .count{font-size:12px;color:#64748b}
.tbl-head .count b{color:#22c55e}
.scroll{overflow-x:auto;border:1px solid #334155;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{border:1px solid #1e293b;padding:5px 9px;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}
th{background:#1e293b;color:#94a3b8;position:sticky;top:0;font-weight:600}
tr:nth-child(even) td{background:#172033}
td.null{color:#475569;font-style:italic}
.empty{color:#475569;font-size:13px;padding:14px 0}
.err{color:#f87171;font-size:12px;padding:8px 0}
.note{color:#64748b;font-size:12px;padding:0 20px}
#status{color:#94a3b8;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>🗄️ Database Dump</h1>
  <a class="nav-btn" href="/">← Home</a>
  <a class="nav-btn" href="/settings">⚙️ Settings</a>
  <div class="controls">
    <span id="status">loading…</span>
    <label>rows/table
      <input id="limit" type="number" min="1" max="2000" value="100">
    </label>
    <button onclick="load()">Reload</button>
  </div>
</header>
<div class="toc" id="toc"></div>
<div class="note">Newest rows first (by primary key). Increase rows/table to dump more — capped at 2000 per table.</div>
<div id="tables"></div>
<script>
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cell(v){
  if(v===null||v===undefined) return '<td class="null">NULL</td>';
  return '<td title="'+esc(v)+'">'+esc(v)+'</td>';
}
function renderTable(t){
  const head='<div class="tbl-head" id="t_'+esc(t.name)+'">'
    +'<h2>'+esc(t.name)+'</h2>'
    +'<span class="count">showing <b>'+t.shown+'</b> of '+t.total.toLocaleString()+' rows</span></div>';
  if(t.error) return head+'<div class="err">error: '+esc(t.error)+'</div>';
  if(!t.rows.length) return head+'<div class="empty">— empty —</div>';
  let h='<div class="scroll"><table><thead><tr>';
  for(const c of t.columns) h+='<th>'+esc(c)+'</th>';
  h+='</tr></thead><tbody>';
  for(const row of t.rows){
    h+='<tr>';
    for(const v of row) h+=cell(v);
    h+='</tr>';
  }
  h+='</tbody></table></div>';
  return head+h;
}
async function load(){
  const lim=Math.max(1,Math.min(2000,parseInt(document.getElementById('limit').value)||100));
  document.getElementById('status').textContent='loading…';
  try{
    const data=await fetch('/api/tables?limit='+lim).then(r=>r.json());
    const tables=data.tables||[];
    document.getElementById('toc').innerHTML=tables.map(t=>
      '<a href="#t_'+esc(t.name)+'">'+esc(t.name)+' <b>'+t.total.toLocaleString()+'</b></a>').join('');
    document.getElementById('tables').innerHTML=
      tables.map(t=>'<section>'+renderTable(t)+'</section>').join('');
    const totRows=tables.reduce((a,t)=>a+t.total,0);
    document.getElementById('status').textContent=
      tables.length+' tables · '+totRows.toLocaleString()+' rows total';
  }catch(e){
    document.getElementById('status').textContent='Error: '+e;
    document.getElementById('tables').innerHTML='<div class="err" style="padding:20px">Failed to load: '+esc(e)+'</div>';
  }
}
load();
</script>
</body>
</html>"""


async def _api_collectors_debug(runner, request: web.Request) -> web.Response:
    """
    Live diagnostic: fires every cloud-safe collector once and returns raw results.
    Helps diagnose why the dashboard shows 0 matches.
    GET /api/debug/collectors
    """
    import traceback
    from datetime import timezone

    from config.settings import settings

    out: dict = {
        "generated_at_ist": (
            datetime.utcnow().replace(tzinfo=timezone.utc)
            .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
            .strftime("%Y-%m-%d %H:%M:%S IST")
        ),
        "collectors": {},
    }

    # ── Odds API ──────────────────────────────────────────────────────────────
    if settings.odds_api_key:
        import httpx as _httpx
        try:
            key = settings.odds_api_key
            async with _httpx.AsyncClient(timeout=12) as c:
                sports_resp = await c.get(
                    "https://api.the-odds-api.com/v4/sports/",
                    params={"apiKey": key, "all": "true"})
                all_sports = sports_resp.json() if sports_resp.status_code == 200 else []
                tennis_keys = [s["key"] for s in all_sports if "tennis" in s.get("key","")]
                active_tennis = [s for s in all_sports
                                 if "tennis" in s.get("key","") and s.get("active")]

                # Fetch odds for active keys + Grand Slam fallbacks
                from datetime import timedelta, timezone as _tz
                _now = datetime.now(_tz.utc)
                _from = (_now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
                _to = (_now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
                sample_events: list[dict] = []
                keys_tried: list[dict] = []
                try_keys = [s["key"] for s in active_tennis] or [
                    "tennis_atp_french_open", "tennis_wta_french_open",
                    "tennis_atp_wimbledon", "tennis_atp", "tennis_wta"]
                for sk in try_keys[:6]:
                    r = await c.get(
                        f"https://api.the-odds-api.com/v4/sports/{sk}/odds/",
                        params={"apiKey": key, "regions": "eu",
                                "markets": "h2h", "oddsFormat": "decimal",
                                "commenceTimeFrom": _from, "commenceTimeTo": _to})
                    q_rem = r.headers.get("x-requests-remaining", "?")
                    keys_tried.append({"key": sk, "status": r.status_code,
                                       "events": len(r.json()) if r.status_code == 200 else 0,
                                       "quota_remaining": q_rem})
                    if r.status_code == 200:
                        for e in r.json()[:5]:
                            home = e.get("home_team", "")
                            away = e.get("away_team", "")
                            bks = e.get("bookmakers", [])
                            # Match odds by name (correct way)
                            h_p = a_p = 0.0
                            for bm in bks:
                                for mkt in bm.get("markets", []):
                                    if mkt.get("key") == "h2h":
                                        for oc in mkt.get("outcomes", []):
                                            nm = oc.get("name","").lower()
                                            pr = float(oc.get("price", 0))
                                            if nm == home.lower() and pr > h_p:
                                                h_p = pr
                                            elif nm == away.lower() and pr > a_p:
                                                a_p = pr
                            mins_u = int(
                                (datetime.fromisoformat(e["commence_time"].replace("Z","+00:00")) - _now
                                 ).total_seconds() / 60) if e.get("commence_time") else None
                            sample_events.append({
                                "sport": sk,
                                "match": f"{home} vs {away}",
                                "commence_time": e.get("commence_time"),
                                "mins_until": mins_u,
                                "bookmakers": len(bks),
                                "odds_home": h_p, "odds_away": a_p,
                            })

            out["collectors"]["odds_api"] = {
                "status": "ok",
                "all_tennis_keys": tennis_keys,
                "active_tennis_keys": [s.get("key") for s in active_tennis],
                "keys_tried": keys_tried,
                "quota_remaining": runner.odds_api.quota_remaining,
                "sample_events": sample_events,
            }
        except Exception:
            out["collectors"]["odds_api"] = {"status": "error", "detail": traceback.format_exc()[-400:]}
    else:
        out["collectors"]["odds_api"] = {"status": "no_key"}

    # ── Sportradar ────────────────────────────────────────────────────────────
    if settings.sportradar_api_key:
        import httpx as _httpx
        key = settings.sportradar_api_key
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            async with _httpx.AsyncClient(timeout=12) as c:
                live_r = await c.get(
                    "https://api.sportradar.com/tennis/trial/v3/en/schedules/live/summaries.json",
                    headers={"x-api-key": key})
                sched_r = await c.get(
                    f"https://api.sportradar.com/tennis/trial/v3/en/schedules/{today}/schedule.json",
                    headers={"x-api-key": key})

            def _sr_sample(data, key_name):
                items = data.get(key_name, []) if isinstance(data, dict) else []
                out = []
                for item in items[:5]:
                    ev = item.get("sport_event", item)
                    comps = ev.get("competitors", [])
                    p1 = comps[0].get("name", "?") if comps else "?"
                    p2 = comps[1].get("name", "?") if len(comps) > 1 else "?"
                    st = (item.get("sport_event_status") or {}).get("status", ev.get("status","?"))
                    out.append({"match": f"{p1} vs {p2}", "status": st,
                                "start": ev.get("start_time") or ev.get("scheduled")})
                return out

            out["collectors"]["sportradar"] = {
                "live_status": live_r.status_code,
                "schedule_status": sched_r.status_code,
                "live_count": len(live_r.json().get("summaries", [])) if live_r.status_code == 200 else 0,
                "schedule_count": len(sched_r.json().get("sport_events", [])) if sched_r.status_code == 200 else 0,
                "live_sample": _sr_sample(live_r.json() if live_r.status_code == 200 else {}, "summaries"),
                "schedule_sample": _sr_sample(sched_r.json() if sched_r.status_code == 200 else {}, "sport_events"),
            }
        except Exception:
            out["collectors"]["sportradar"] = {"status": "error", "detail": traceback.format_exc()[-400:]}
    else:
        out["collectors"]["sportradar"] = {"status": "no_key"}

    # ── API-Sports Tennis ─────────────────────────────────────────────────────
    if settings.api_sports_key:
        try:
            async with _httpx.AsyncClient(
                timeout=8,
                headers={
                    "x-apisports-key": settings.api_sports_key,
                    "x-apisports-host": "v1.tennis.api-sports.io",
                },
            ) as c:
                r = await c.get("https://v1.tennis.api-sports.io/games",
                                params={"live": "all"})
            quota = r.headers.get("x-ratelimit-requests-remaining", "?")
            games = r.json().get("response", []) if r.status_code == 200 else []
            out["collectors"]["api_sports"] = {
                "status_code": r.status_code,
                "live_games": len(games),
                "quota_remaining": quota,
                "sample": [
                    f"{g.get('teams',{}).get('home',{}).get('name','?')} vs "
                    f"{g.get('teams',{}).get('away',{}).get('name','?')}"
                    for g in games[:5]
                ],
            }
        except Exception:
            out["collectors"]["api_sports"] = {"status": "error", "detail": traceback.format_exc()[-400:]}
    else:
        out["collectors"]["api_sports"] = {"status": "no_key", "note": "Add API_SPORTS_KEY — 100 req/day free at api-sports.io"}

    # ── ESPN (cloud-safe check — tests the same URLs as the real collector) ──────
    import httpx as _httpx
    _ESPN_TEST_URLS = [
        "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/tennis/french-open/scoreboard",
    ]
    espn_results = []
    try:
        async with _httpx.AsyncClient(timeout=8) as c:
            for url in _ESPN_TEST_URLS:
                try:
                    r = await c.get(url, params={"limit": "20"})
                    events = r.json().get("events", []) if r.status_code == 200 else []
                    statuses: dict[str, int] = {}
                    for e in events:
                        s = e.get("status", {}).get("type", {}).get("name", "?")
                        statuses[s] = statuses.get(s, 0) + 1
                    espn_results.append({
                        "url": url.split("/sports/tennis/")[1],
                        "status_code": r.status_code,
                        "events": len(events),
                        "statuses": statuses,
                        "sample": [
                            e.get("name", "?") for e in events[:3]
                        ],
                    })
                except Exception as _e:
                    espn_results.append({"url": url, "error": str(_e)})
        out["collectors"]["espn"] = {
            "endpoints": espn_results,
            "blocked": all(x.get("status_code") == 403 for x in espn_results),
            "total_events": sum(x.get("events", 0) for x in espn_results),
        }
    except Exception:
        out["collectors"]["espn"] = {"status": "error", "detail": traceback.format_exc()[-200:]}

    # ── In-memory store ───────────────────────────────────────────────────────
    all_states = await runner.store.get_all()
    out["store"] = {
        "total": len(all_states),
        "live": sum(1 for s in all_states if not s.is_scheduled),
        "scheduled": sum(1 for s in all_states if s.is_scheduled),
        "matches": [
            {"id": s.match_id, "p1": s.player1_name, "p2": s.player2_name,
             "tournament": s.tournament, "scheduled": s.is_scheduled}
            for s in all_states[:20]
        ],
    }

    return web.Response(text=json.dumps(out, default=str, indent=2),
                        content_type="application/json")


async def _api_tables(runner, request: web.Request) -> web.Response:
    """Dump every table in the database (reflected, so it covers all tables).

    Query params:
      limit — rows per table (default 100, max 2000)
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from storage.database import engine

    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 2000))
    except ValueError:
        limit = 100

    out: dict = {"generated_at": datetime.utcnow().isoformat() + "Z", "limit": limit,
                 "tables": []}

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names()
        )
        pk_map = await conn.run_sync(
            lambda sync_conn: {
                t: sa_inspect(sync_conn).get_pk_constraint(t).get("constrained_columns", [])
                for t in table_names
            }
        )

        for name in sorted(table_names):
            try:
                total = (await conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"'))).scalar() or 0
            except Exception:
                total = 0

            # Newest-first when there's a single-column primary key, else natural order.
            pks = pk_map.get(name) or []
            order = f' ORDER BY "{pks[0]}" DESC' if len(pks) == 1 else ""
            cols: list[str] = []
            rows: list[list] = []
            error = None
            try:
                result = await conn.execute(
                    text(f'SELECT * FROM "{name}"{order} LIMIT :lim'), {"lim": limit})
                cols = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
            except Exception as exc:
                error = str(exc)

            out["tables"].append({
                "name": name,
                "columns": cols,
                "rows": rows,
                "shown": len(rows),
                "total": total,
                "error": error,
            })

    return web.Response(text=json.dumps(out, default=str),
                        content_type="application/json")


async def _data_page(request: web.Request) -> web.Response:
    return web.Response(text=_DATA_HTML, content_type="text/html")


async def _settings_page(request: web.Request) -> web.Response:
    return web.Response(text=_SETTINGS_HTML, content_type="text/html")


async def _api_collector_toggle(runner, request: web.Request) -> web.Response:
    """POST /api/settings/toggle  body: {"collector": "sportradar", "enabled": true}"""
    try:
        body = await request.json()
        collector = str(body.get("collector", ""))
        enabled = bool(body.get("enabled", True))
        if collector not in runner.collector_enabled:
            return web.Response(
                text=json.dumps({"error": f"unknown collector: {collector}"}),
                content_type="application/json", status=400,
            )
        runner.collector_enabled[collector] = enabled
        _log.get_logger().info("collector_toggled", collector=collector, enabled=enabled)
        return web.Response(
            text=json.dumps({"collector": collector, "enabled": enabled, "ok": True}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json", status=500,
        )


async def _api_collector_states(runner, request: web.Request) -> web.Response:
    """GET /api/settings — returns collector enabled/disabled states with quota info."""
    from config.settings import settings as _settings
    states = {}
    for name, enabled in runner.collector_enabled.items():
        states[name] = {"enabled": enabled}

    # Enrich with quota / key info
    states["sportradar"].update({
        "key_set": bool(_settings.sportradar_api_key),
        "poll_interval_secs": _settings.sportradar_poll_interval_seconds,
        "quota_total": 1000,
        "calls_per_poll": 3,
        "polls_per_day": round(86400 / _settings.sportradar_poll_interval_seconds, 1),
        "est_calls_per_month": round(3 * 86400 / _settings.sportradar_poll_interval_seconds * 30),
    })
    states["odds_api"].update({
        "key_set": bool(_settings.odds_api_key),
        "poll_interval_secs": _settings.odds_poll_interval_seconds,
        "quota_remaining": runner.odds_api.quota_remaining,
        "quota_used": runner.odds_api.quota_used,
    })
    states["api_sports"].update({
        "key_set": bool(_settings.api_sports_key),
        "poll_interval_secs": _settings.api_sports_poll_interval_seconds,
        "quota_remaining": runner.api_sports.quota_remaining,
    })
    states["espn"].update({"key_set": True, "poll_interval_secs": _settings.sofascore_poll_interval})
    states["bets_api"].update({"key_set": bool(_settings.bets_api_token)})

    return web.Response(text=json.dumps(states), content_type="application/json")


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Collector Settings — Tennis Bet</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:16px}
.topbar a{color:#58a6ff;text-decoration:none;font-size:14px;padding:6px 12px;border-radius:6px;border:1px solid #30363d}
.topbar a:hover{background:#21262d}
.topbar h1{font-size:16px;font-weight:600;color:#e6edf3;margin-left:8px}
.container{max-width:760px;margin:32px auto;padding:0 16px}
h2{font-size:20px;font-weight:700;margin-bottom:6px}
.subtitle{color:#8b949e;font-size:13px;margin-bottom:28px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px 24px;margin-bottom:16px;transition:border-color .2s}
.card.active{border-color:#238636}
.card.paused{border-color:#f85149;opacity:.85}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-title{display:flex;align-items:center;gap:10px}
.card-name{font-size:16px;font-weight:600}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.badge-green{background:#0d4429;color:#3fb950}
.badge-red{background:#3d0a0a;color:#f85149}
.badge-yellow{background:#3d2b00;color:#e3b341}
.card-meta{color:#8b949e;font-size:13px;line-height:1.6}
.meta-row{display:flex;justify-content:space-between;margin-top:6px}
.meta-label{color:#8b949e}
.meta-value{color:#e6edf3;font-weight:500}
.quota-bar{height:6px;background:#21262d;border-radius:3px;margin-top:10px;overflow:hidden}
.quota-fill{height:100%;border-radius:3px;transition:width .4s}
.quota-fill.safe{background:#238636}
.quota-fill.warn{background:#e3b341}
.quota-fill.danger{background:#f85149}
/* Toggle */
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle-label{font-size:13px;color:#8b949e;min-width:44px;text-align:right}
.toggle{position:relative;width:44px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#30363d;border-radius:24px;transition:.3s}
.slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#e6edf3;border-radius:50%;transition:.3s}
input:checked+.slider{background:#238636}
input:checked+.slider:before{transform:translateX(20px)}
.warn-box{background:#2d1f00;border:1px solid #e3b341;border-radius:8px;padding:12px 16px;font-size:13px;color:#e3b341;margin-top:14px;display:none}
.warn-box.show{display:block}
.save-btn{background:#238636;border:none;color:#fff;font-size:14px;font-weight:600;padding:10px 24px;border-radius:8px;cursor:pointer;margin-top:24px;width:100%;transition:background .2s}
.save-btn:hover{background:#2ea043}
.toast{position:fixed;bottom:24px;right:24px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;font-size:14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999}
.toast.show{opacity:1}
.toast.err{background:#f85149}
</style>
</head>
<body>

<div class="topbar">
  <a href="/">← Home</a>
  <a href="/data">📊 History</a>
  <h1>⚙️ Collector Settings</h1>
</div>

<div class="container">
  <h2>Data Source Controls</h2>
  <p class="subtitle">Toggle collectors on/off to manage API quota. Changes take effect immediately — no redeploy needed.</p>

  <div id="cards">Loading...</div>
</div>

<div class="toast" id="toast"></div>

<script>
const _IST = {timeZone:'Asia/Kolkata'};

const SOURCES = [
  {
    id: 'sportradar',
    name: 'Sportradar Tennis',
    icon: '🎾',
    desc: 'Live + scheduled matches, all tours (ATP, WTA, ITF, Challengers)',
    quota_label: 'Trial quota',
    quota_total: 1000,
    can_toggle: true,
    warning: 'At 5-min interval, Sportradar uses ~720 calls/month. Trial limit is 1,000. Toggle OFF when not actively monitoring to save credits.',
  },
  {
    id: 'odds_api',
    name: 'Odds API',
    icon: '💰',
    desc: 'Pre-match odds for French Open, ATP, WTA (free tier: 500 req/month)',
    quota_label: 'Monthly quota',
    quota_total: 500,
    can_toggle: true,
    warning: 'Free tier has 500 requests/month. Toggle OFF when quota is low to preserve remaining credits.',
  },
  {
    id: 'espn',
    name: 'ESPN',
    icon: '📡',
    desc: 'Live scores backup, always cloud-safe, unlimited',
    quota_label: 'Unlimited',
    quota_total: null,
    can_toggle: true,
    warning: null,
  },
  {
    id: 'bets_api',
    name: 'BetsAPI',
    icon: '📈',
    desc: 'Live in-play odds (requires paid token)',
    quota_label: 'Paid plan',
    quota_total: null,
    can_toggle: true,
    warning: null,
  },
  {
    id: 'api_sports',
    name: 'API-Sports',
    icon: '🏆',
    desc: 'Live scores (100 req/day free)',
    quota_label: 'Daily quota',
    quota_total: 100,
    can_toggle: true,
    warning: null,
  },
];

let states = {};

async function load() {
  try {
    const r = await fetch('/api/settings');
    states = await r.json();
    render();
  } catch(e) {
    document.getElementById('cards').innerHTML = '<p style="color:#f85149">Failed to load settings</p>';
  }
}

function render() {
  const el = document.getElementById('cards');
  el.innerHTML = SOURCES.map(src => {
    const st = states[src.id] || {};
    const enabled = st.enabled !== false;
    const keySet = st.key_set !== false;

    // Quota calc
    let quotaHtml = '';
    if (src.id === 'sportradar' && st.est_calls_per_month != null) {
      const used = st.est_calls_per_month;
      const total = src.quota_total;
      const pct = Math.min(100, Math.round(used / total * 100));
      const cls = pct < 60 ? 'safe' : pct < 85 ? 'warn' : 'danger';
      quotaHtml = `
        <div class="meta-row"><span class="meta-label">Interval</span><span class="meta-value">${st.poll_interval_secs}s (${Math.round(st.poll_interval_secs/60)}min)</span></div>
        <div class="meta-row"><span class="meta-label">Est. calls/month</span><span class="meta-value">${used} / ${total} (${pct}%)</span></div>
        <div class="quota-bar"><div class="quota-fill ${cls}" style="width:${pct}%"></div></div>`;
    } else if (src.id === 'odds_api') {
      const used = st.quota_used != null ? st.quota_used : '?';
      const rem = st.quota_remaining != null ? st.quota_remaining : '?';
      const pct = st.quota_used != null ? Math.min(100, Math.round(st.quota_used / src.quota_total * 100)) : 0;
      const cls = pct < 60 ? 'safe' : pct < 85 ? 'warn' : 'danger';
      quotaHtml = `
        <div class="meta-row"><span class="meta-label">Used this month</span><span class="meta-value">${used} / ${src.quota_total}</span></div>
        <div class="meta-row"><span class="meta-label">Remaining</span><span class="meta-value">${rem} credits</span></div>
        <div class="quota-bar"><div class="quota-fill ${cls}" style="width:${pct}%"></div></div>`;
    } else if (src.id === 'api_sports') {
      const rem = st.quota_remaining != null ? st.quota_remaining : '?';
      const pct = rem !== '?' ? Math.min(100, Math.round((src.quota_total - rem) / src.quota_total * 100)) : 0;
      const cls = pct < 60 ? 'safe' : pct < 85 ? 'warn' : 'danger';
      quotaHtml = `
        <div class="meta-row"><span class="meta-label">Remaining today</span><span class="meta-value">${rem} / ${src.quota_total} req</span></div>
        <div class="quota-bar"><div class="quota-fill ${cls}" style="width:${pct}%"></div></div>`;
    } else if (src.id === 'espn') {
      quotaHtml = `<div class="meta-row"><span class="meta-label">Quota</span><span class="meta-value" style="color:#3fb950">Unlimited ✓</span></div>`;
    } else if (src.id === 'bets_api') {
      quotaHtml = `<div class="meta-row"><span class="meta-label">Status</span><span class="meta-value">${keySet ? 'Token active' : 'No token set'}</span></div>`;
    }

    const warnShow = enabled && src.warning ? 'show' : '';
    const cardCls = enabled ? 'card active' : 'card paused';
    const badgeTxt = enabled ? 'ACTIVE' : 'PAUSED';
    const badgeCls = enabled ? 'badge badge-green' : 'badge badge-red';
    const noKey = !keySet ? '<span class="badge badge-yellow">NO KEY</span>' : '';

    return `
    <div class="${cardCls}" id="card-${src.id}">
      <div class="card-header">
        <div class="card-title">
          <span style="font-size:22px">${src.icon}</span>
          <div>
            <div class="card-name">${src.name} ${noKey}</div>
            <div style="font-size:12px;color:#8b949e;margin-top:2px">${src.desc}</div>
          </div>
        </div>
        <div class="toggle-wrap">
          <span class="toggle-label" id="lbl-${src.id}">${enabled ? 'ON' : 'OFF'}</span>
          <label class="toggle">
            <input type="checkbox" id="tog-${src.id}" ${enabled ? 'checked' : ''} onchange="toggle('${src.id}')">
            <span class="slider"></span>
          </label>
        </div>
      </div>
      <div class="card-meta">
        <span class="${badgeCls}">${badgeTxt}</span>
        ${quotaHtml}
      </div>
      <div class="warn-box ${warnShow}" id="warn-${src.id}">${src.warning || ''}</div>
    </div>`;
  }).join('');
}

async function toggle(id) {
  const cb = document.getElementById('tog-' + id);
  const enabled = cb.checked;
  try {
    const r = await fetch('/api/settings/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collector: id, enabled}),
    });
    const data = await r.json();
    if (data.ok) {
      states[id] = {...(states[id] || {}), enabled};
      render();
      showToast(enabled ? id + ' enabled ✓' : id + ' paused — saving quota', !enabled);
    } else {
      showToast('Error: ' + (data.error || 'unknown'), true);
      cb.checked = !enabled;
    }
  } catch(e) {
    showToast('Network error', true);
    cb.checked = !enabled;
  }
}

function showToast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(() => t.className = 'toast', 2800);
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


async def make_app(runner) -> web.Application:
    app = web.Application()
    app.router.add_get("/", lambda req: _dashboard(req))
    app.router.add_get("/data", lambda req: _data_page(req))
    app.router.add_get("/api/tables", lambda req: _api_tables(runner, req))
    app.router.add_get("/health", lambda req: _health(runner, req))
    app.router.add_get("/api/status", lambda req: _api_status(runner, req))
    app.router.add_get("/api/matches", lambda req: _api_matches(runner, req))
    app.router.add_get("/api/signals", lambda req: _api_signals(runner, req))
    app.router.add_get("/api/football/matches", lambda req: _api_football_matches(runner, req))
    app.router.add_get("/api/football/signals", lambda req: _api_football_signals(runner, req))
    app.router.add_get("/api/debug", lambda req: _api_debug(runner, req))
    app.router.add_get("/api/debug/collectors", lambda req: _api_collectors_debug(runner, req))
    app.router.add_get("/api/h2h", lambda req: _api_h2h(runner, req))
    app.router.add_get("/api/scalping", lambda req: _api_scalping(runner, req))
    app.router.add_post("/api/ingest", lambda req: _api_ingest(runner, req))
    app.router.add_get("/settings", lambda req: _settings_page(req))
    app.router.add_get("/api/settings", lambda req: _api_collector_states(runner, req))
    app.router.add_post("/api/settings/toggle", lambda req: _api_collector_toggle(runner, req))
    return app


async def _dashboard(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def start_health_server(runner, port: int = 8080) -> web.AppRunner:
    app = await make_app(runner)
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", port)
    await site.start()
    return web_runner
