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
    matches = []
    for s in states:
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
</header>

<div class="grid">
  <div class="card"><div class="card-title">Tennis Live</div><div class="card-value" id="stat-matches">&mdash;</div><div class="card-sub">matches now</div></div>
  <div class="card"><div class="card-title">Football Live</div><div class="card-value" id="stat-fb-matches">&mdash;</div><div class="card-sub">matches now</div></div>
  <div class="card"><div class="card-title">Signals (24h)</div><div class="card-value" id="stat-signals">&mdash;</div><div class="card-sub">tennis + football</div></div>
  <div class="card"><div class="card-title">Uptime</div><div class="card-value" id="stat-uptime">&mdash;</div><div class="card-sub">since restart</div></div>
</div>

<div class="tab-bar">
  <button class="tab-btn active" data-tab="tennis" onclick="switchTab('tennis')">🎾 Tennis</button>
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

<footer>Auto-refreshes every 30s &middot; <span id="last-updated">&mdash;</span></footer>

<script>
const SURFACE_CLASS={clay:'surface-clay',grass:'surface-grass',hard:'surface-hard',indoor_hard:'surface-indoor_hard'};
const SURFACE_DOT={clay:'🟤',grass:'🟢',hard:'🔵',indoor_hard:'🔵'};
const SIG_EMOJI={momentum:'⚡',odds_value:'📉',serve_degradation:'🎯',set_pattern:'📊',fatigue:'😤',ml_value:'🤖',endgame:'⏱',break_momentum:'💥',second_set_fade:'🔄'};
const SIG_NAME={momentum:'Momentum Surge',odds_value:'Odds Value',serve_degradation:'Serve Degradation',set_pattern:'Set Pattern',fatigue:'Fatigue',ml_value:'ML Value',endgame:'Endgame Scalp',break_momentum:'Break Momentum',second_set_fade:'Second Set Fade'};
const MKT_LABEL={match_winner:'Match Winner',next_game:'Next Game',next_set:'Next Set',set_winner_set2:'Set 2 Winner'};

function fmtUptime(s){if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m';}
function fmtTime(iso){const d=new Date(iso+'Z');return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}

// ── MATCHES ───────────────────────────────────────────────────────────────────
function renderMatches(matches){
  const el=document.getElementById('matches');
  if(!matches.length){el.innerHTML='<div class="empty">No live matches tracked right now</div>';return;}
  el.innerHTML=matches.map(renderMatch).join('');
}

function renderMatch(m){
  const surf=SURFACE_CLASS[m.surface]||'';
  const surfLabel=m.surface.replace('_',' ');
  const hasOdds=m.odds_p1>1.01&&m.odds_p2>1.01;
  const hasScore=m.sets_p1>0||m.sets_p2>0||m.games_p1>0||m.games_p2>0;
  const sets=m.set_scores||[];
  const numSets=sets.length;

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

  return `<div class="match-card">${header}${players}${scoreboard}${probBar}${oddsRow}</div>`;
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
  const fs=st.flashscore||{}, espn=st.espn||{}, sc=st.sofascore||{}, oa=st.odds_api||{}, ba=st.bets_api||{};
  const sources=[
    {name:'ESPN',ok:true,detail:'Live scores (always on)'},
    {name:'BetsAPI',ok:ba.token_set,detail:ba.token_set?`Live odds · ${ba.consecutive_failures||0} failures`:'No token — add BETS_API_TOKEN'},
    {name:'Sofascore',ok:!sc.blocked,detail:sc.blocked?'Blocked on cloud IP':'Available (serve stats)'},
    {name:'Flashscore',ok:fs.http_ok,detail:fs.http_ok?'OK':`${fs.consecutive_failures||0} failures`},
    {name:'Odds API',ok:oa.key_set,detail:oa.key_set?`Every ${oa.poll_interval_secs}s`:'No API key'},
  ];
  document.getElementById('sources').innerHTML=sources.map(s=>`
    <div class="status-card">
      <div class="status-name"><span class="dot ${s.ok?'dot-green':'dot-red'}"></span>${s.name}</div>
      <div class="status-val">${s.detail}</div>
    </div>`).join('');
  document.getElementById('stat-odds').textContent=oa.key_set?'✓ Set':'✗ Missing';
  document.getElementById('stat-odds-sub').textContent=oa.key_set?`every ${oa.poll_interval_secs}s`:'Add ODDS_API_KEY';
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
  const d=new Date(iso);
  return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})+' ('+d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'})+')';
}

function minsUntil(iso){
  if(!iso) return null;
  const diff=Math.round((new Date(iso)-Date.now())/60000);
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

// ── MAIN ──────────────────────────────────────────────────────────────────────
async function refresh(){
  try{
    const [status,matches,signals,fbMatches,fbSignals]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/matches').then(r=>r.json()),
      fetch('/api/signals').then(r=>r.json()),
      fetch('/api/football/matches').then(r=>r.json()),
      fetch('/api/football/signals').then(r=>r.json()),
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
    document.getElementById('last-updated').textContent='Updated: '+new Date().toLocaleTimeString();
    document.getElementById('refresh-label').textContent='Next in 30s';
  }catch(e){
    document.getElementById('refresh-label').textContent='Error — retrying…';
  }
}
refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""


async def make_app(runner) -> web.Application:
    app = web.Application()
    app.router.add_get("/", lambda req: _dashboard(req))
    app.router.add_get("/health", lambda req: _health(runner, req))
    app.router.add_get("/api/status", lambda req: _api_status(runner, req))
    app.router.add_get("/api/matches", lambda req: _api_matches(runner, req))
    app.router.add_get("/api/signals", lambda req: _api_signals(runner, req))
    app.router.add_get("/api/football/matches", lambda req: _api_football_matches(runner, req))
    app.router.add_get("/api/football/signals", lambda req: _api_football_signals(runner, req))
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
