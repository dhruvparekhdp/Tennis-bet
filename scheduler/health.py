"""Aiohttp web server: dashboard UI + JSON API endpoints."""
from __future__ import annotations

import json
from datetime import datetime

from aiohttp import web

_start_time = datetime.utcnow()


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
    states = await runner.store.get_all()
    matches = []
    for s in states:
        odds_history = [
            {"odds_p1": p.odds_p1, "odds_p2": p.odds_p2,
             "timestamp": p.timestamp.isoformat()}
            for p in s.odds_history[-20:]  # last 20 points
        ]
        matches.append({
            "match_id": s.match_id,
            "player1": s.player1_name,
            "player2": s.player2_name,
            "tournament": s.tournament,
            "surface": s.surface,
            "score": f"{s.sets_p1}-{s.sets_p2}",
            "games": f"{s.games_in_set_p1}-{s.games_in_set_p2}",
            "current_set": s.current_set,
            "is_tiebreak": s.is_tiebreak,
            "odds_p1": s.odds_p1,
            "odds_p2": s.odds_p2,
            "odds_history": odds_history,
            "game_log": s.game_log[-20:],
            "duration_mins": s.match_duration_mins,
            "source": s.match_id.split("_")[0],
        })
    return web.Response(text=json.dumps(matches), content_type="application/json")


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

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tennis Bet Monitor</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
  header{background:#1e293b;border-bottom:1px solid #334155;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:20px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:10px}
  .badge{background:#0ea5e9;color:#fff;font-size:11px;padding:2px 8px;border-radius:9999px;font-weight:600}
  .refresh{font-size:12px;color:#64748b}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;padding:20px 24px 0}
  .card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px}
  .card-title{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:8px}
  .card-value{font-size:28px;font-weight:700;color:#f1f5f9}
  .card-sub{font-size:12px;color:#94a3b8;margin-top:4px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot-green{background:#22c55e}.dot-red{background:#ef4444}.dot-yellow{background:#f59e0b}.dot-gray{background:#475569}
  section{padding:20px 24px}
  section h2{font-size:14px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
  .match-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;margin-bottom:12px}
  .match-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}
  .players{font-size:17px;font-weight:700;color:#f1f5f9}
  .vs{color:#475569;font-weight:400;margin:0 6px}
  .tournament{font-size:12px;color:#64748b;margin-top:2px}
  .surface-clay{color:#f97316}.surface-grass{color:#22c55e}.surface-hard{color:#38bdf8}.surface-indoor_hard{color:#818cf8}
  .score-box{text-align:right}
  .score{font-size:22px;font-weight:800;color:#f1f5f9}
  .set-label{font-size:11px;color:#64748b}
  .odds-row{display:flex;gap:12px;margin-top:12px}
  .odds-box{flex:1;background:#0f172a;border-radius:8px;padding:10px;text-align:center}
  .odds-label{font-size:11px;color:#64748b;margin-bottom:4px}
  .odds-val{font-size:22px;font-weight:800;color:#38bdf8}
  .odds-val.no-odds{color:#475569;font-size:16px}
  .source-tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:4px;background:#1e3a5f;color:#7dd3fc;font-weight:600}
  .signal-row{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:8px;display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:start}
  .sig-left{display:flex;flex-direction:column;gap:6px;align-items:flex-start}
  .sig-type{font-size:12px;font-weight:700;padding:3px 8px;border-radius:6px;white-space:nowrap}
  .sig-momentum{background:#1d4ed8;color:#bfdbfe}
  .sig-odds_value{background:#7c3aed;color:#ddd6fe}
  .sig-serve_degradation{background:#b45309;color:#fde68a}
  .sig-set_pattern{background:#065f46;color:#a7f3d0}
  .sig-fatigue{background:#9f1239;color:#fecdd3}
  .sig-ml_value{background:#155e75;color:#a5f3fc}
  .sig-body{min-width:0}
  .sig-match{font-size:14px;font-weight:600;color:#f1f5f9}
  .sig-match .vs{color:#475569;font-weight:400;margin:0 4px;font-size:12px}
  .sig-tourn{font-size:11px;color:#64748b;margin-top:3px}
  .sig-desc{font-size:11px;color:#94a3b8;margin-top:4px;line-height:1.4}
  .sig-meta{text-align:right;white-space:nowrap}
  .sig-conf{font-size:20px;font-weight:800;color:#f1f5f9}
  .sig-edge{font-size:11px;color:#22c55e;margin-top:2px;font-weight:600}
  .sig-odds-display{font-size:11px;color:#94a3b8;margin-top:2px}
  .sig-stake{display:inline-block;font-size:10px;background:#1d4736;color:#34d399;padding:2px 6px;border-radius:4px;margin-top:4px;font-weight:600}
  .sig-time{font-size:11px;color:#475569}
  .empty{color:#475569;font-size:14px;padding:24px 0;text-align:center}
  .status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
  .status-card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px}
  .status-name{font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:6px}
  .status-val{font-size:13px;color:#f1f5f9;font-weight:500}
  footer{text-align:center;padding:20px;color:#334155;font-size:12px;border-top:1px solid #1e293b;margin-top:8px}
  @media(max-width:600px){.odds-row{flex-direction:column}.match-header{flex-direction:column;gap:8px}}
</style>
</head>
<body>
<header>
  <h1>&#127934; Tennis Bet Monitor <span class="badge" id="live-count">0 live</span></h1>
  <span class="refresh" id="refresh-label">Refreshing&hellip;</span>
</header>

<div class="grid">
  <div class="card"><div class="card-title">Live Matches</div><div class="card-value" id="stat-matches">&mdash;</div><div class="card-sub">tracked now</div></div>
  <div class="card"><div class="card-title">Signals (24h)</div><div class="card-value" id="stat-signals">&mdash;</div><div class="card-sub">alerts fired</div></div>
  <div class="card"><div class="card-title">Uptime</div><div class="card-value" id="stat-uptime">&mdash;</div><div class="card-sub">since last restart</div></div>
  <div class="card"><div class="card-title">Odds API</div><div class="card-value" id="stat-odds">&mdash;</div><div class="card-sub" id="stat-odds-sub"></div></div>
</div>

<section>
  <h2>Data Sources</h2>
  <div class="status-grid" id="sources"></div>
</section>

<section>
  <h2>Live Matches</h2>
  <div id="matches"><div class="empty">No live matches tracked</div></div>
</section>

<section>
  <h2>Recent Signals (last 24h)</h2>
  <div id="signals"><div class="empty">No signals fired yet</div></div>
</section>

<footer>Auto-refreshes every 30s &middot; <span id="last-updated">&mdash;</span></footer>

<script>
const SURFACE_CLASS = {clay:'surface-clay',grass:'surface-grass',hard:'surface-hard',indoor_hard:'surface-indoor_hard'};
const SIG_EMOJI = {momentum:'&#9889;',odds_value:'&#128201;',serve_degradation:'&#127919;',set_pattern:'&#128202;',fatigue:'&#128548;',ml_value:'&#129302;'};

function fmtUptime(s){
  if(s<60) return s+'s';
  if(s<3600) return Math.floor(s/60)+'m '+( s%60)+'s';
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h+'h '+m+'m';
}
function fmtTime(iso){
  const d=new Date(iso+'Z');
  return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function fmtOdds(v){return v>1.01?v.toFixed(2):'&mdash;';}

function renderMatches(matches){
  const el=document.getElementById('matches');
  if(!matches.length){el.innerHTML='<div class="empty">No live matches tracked right now</div>';return;}
  el.innerHTML=matches.map(m=>{
    const surf=SURFACE_CLASS[m.surface]||'';
    const oddsP1=fmtOdds(m.odds_p1), oddsP2=fmtOdds(m.odds_p2);
    const p1cls=m.odds_p1<=1.01?'no-odds':'', p2cls=m.odds_p2<=1.01?'no-odds':'';
    const dur=m.duration_mins>0?`${m.duration_mins}m`:'';
    return `<div class="match-card">
      <div class="match-header">
        <div>
          <div class="players">${m.player1}<span class="vs">vs</span>${m.player2}</div>
          <div class="tournament"><span class="source-tag">${m.source.toUpperCase()}</span> &nbsp;
            <span class="${surf}">${m.surface.replace('_',' ')}</span> &middot; ${m.tournament} ${dur?'&middot; '+dur:''}
          </div>
        </div>
        <div class="score-box">
          <div class="score">Set ${m.current_set}${m.is_tiebreak?' TB':''} &nbsp;${m.games}</div>
          <div class="set-label">Sets ${m.score}</div>
        </div>
      </div>
      <div class="odds-row">
        <div class="odds-box"><div class="odds-label">${m.player1}</div><div class="odds-val ${p1cls}">${oddsP1}</div></div>
        <div class="odds-box"><div class="odds-label">${m.player2}</div><div class="odds-val ${p2cls}">${oddsP2}</div></div>
      </div>
    </div>`;
  }).join('');
}

const MARKET_LABEL={'match_winner':'Match Winner','next_game':'Next Game','next_set':'Next Set'};
const SURFACE_DOT={'clay':'🟤','grass':'🟢','hard':'🔵','indoor_hard':'🔵'};

function renderSignals(signals){
  const el=document.getElementById('signals');
  if(!signals.length){el.innerHTML='<div class="empty">No signals in the last 24 hours</div>';return;}
  el.innerHTML=signals.map(s=>{
    const emoji=SIG_EMOJI[s.signal_type]||'&#127934;';
    const label=s.signal_type.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
    const player=s.player_name||'—';
    const opp=s.opponent_name||'—';
    const tourn=s.tournament||'';
    const surf=SURFACE_DOT[s.surface]||'⚪';
    const mkt=MARKET_LABEL[s.market]||s.market;
    const stake=s.stake_pct>0?`<span class="sig-stake">Stake ${s.stake_pct}%</span>`:'';
    return `<div class="signal-row">
      <div class="sig-left">
        <span class="sig-type sig-${s.signal_type}">${emoji} ${label}</span>
        <div class="sig-time">${fmtTime(s.timestamp)}</div>
      </div>
      <div class="sig-body">
        <div class="sig-match">&#127934; <strong>${player}</strong> <span class="vs">vs</span> ${opp}</div>
        <div class="sig-tourn">${surf} ${tourn} &middot; ${mkt}</div>
        <div class="sig-desc">${s.trigger}</div>
      </div>
      <div class="sig-meta">
        <div class="sig-conf">${s.confidence}%</div>
        <div class="sig-edge">+${s.edge_pct}% edge</div>
        <div class="sig-odds-display">@ ${s.odds} &nbsp;fair&nbsp;${s.fair_odds}</div>
        ${stake}
      </div>
    </div>`;
  }).join('');
}

function renderStatus(st){
  const fs=st.flashscore, espn=st.espn, sc=st.sofascore, oa=st.odds_api;
  const sources=[
    {name:'Flashscore', ok:fs.http_ok&&fs.consecutive_zeros<10,
     detail: fs.http_ok?(fs.consecutive_zeros>0?`${fs.consecutive_zeros} zero polls`:'OK'):`${fs.consecutive_failures} HTTP failures`},
    {name:'ESPN', ok:true, detail:'Always available'},
    {name:'Sofascore', ok:!sc.blocked, detail:sc.blocked?'Blocked (cloud IP)':'Available'},
    {name:'Odds API', ok:oa.key_set, detail:oa.key_set?`Polling every ${oa.poll_interval_secs}s`:'No API key set'},
  ];
  document.getElementById('sources').innerHTML=sources.map(s=>`
    <div class="status-card">
      <div class="status-name"><span class="dot ${s.ok?'dot-green':'dot-red'}"></span>${s.name}</div>
      <div class="status-val">${s.detail}</div>
    </div>`).join('');

  document.getElementById('stat-odds').textContent=oa.key_set?'✓ Set':'✗ Missing';
  document.getElementById('stat-odds-sub').textContent=oa.key_set?`every ${oa.poll_interval_secs}s`:'Add ODDS_API_KEY';
}

async function refresh(){
  try{
    const [status,matches,signals]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/matches').then(r=>r.json()),
      fetch('/api/signals').then(r=>r.json()),
    ]);
    document.getElementById('stat-matches').textContent=matches.length;
    document.getElementById('stat-signals').textContent=signals.length;
    document.getElementById('stat-uptime').textContent=fmtUptime(status.uptime_seconds);
    document.getElementById('live-count').textContent=matches.length+' live';
    renderStatus(status);
    renderMatches(matches);
    renderSignals(signals);
    const now=new Date();
    document.getElementById('last-updated').textContent='Last updated: '+now.toLocaleTimeString();
    document.getElementById('refresh-label').textContent='Next refresh in 30s';
  }catch(e){
    document.getElementById('refresh-label').textContent='Error refreshing — retrying…';
  }
}

refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""


async def make_app(runner) -> web.Application:
    app = web.Application()

    # bind runner into each handler via closure
    app.router.add_get("/", lambda req: _dashboard(req))
    app.router.add_get("/health", lambda req: _health(runner, req))
    app.router.add_get("/api/status", lambda req: _api_status(runner, req))
    app.router.add_get("/api/matches", lambda req: _api_matches(runner, req))
    app.router.add_get("/api/signals", lambda req: _api_signals(runner, req))
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
