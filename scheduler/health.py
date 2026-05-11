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
  /* ── Match card — exchange style ── */
  .match-card{background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden;margin-bottom:14px}
  .mc-header{display:flex;align-items:center;gap:8px;padding:8px 14px;background:#162032;border-bottom:1px solid #1e3a5f;font-size:11px;color:#64748b}
  .source-tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#1e3a5f;color:#7dd3fc;font-weight:700;letter-spacing:.04em}
  .surface-clay{color:#f97316}.surface-grass{color:#22c55e}.surface-hard{color:#38bdf8}.surface-indoor_hard{color:#818cf8}
  .mc-body{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:0;padding:16px 14px}
  .mc-player{display:flex;flex-direction:column;gap:4px}
  .mc-player.right{align-items:flex-end;text-align:right}
  .mc-name{font-size:16px;font-weight:700;color:#f1f5f9}
  .mc-sets-won{font-size:28px;font-weight:900;color:#f1f5f9;line-height:1}
  .mc-center{display:flex;flex-direction:column;align-items:center;gap:6px;padding:0 20px}
  .mc-set-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#475569;font-weight:600}
  .mc-game-score{font-size:26px;font-weight:900;color:#f1f5f9;letter-spacing:2px}
  .mc-set-tag{font-size:11px;color:#94a3b8;background:#0f172a;padding:2px 8px;border-radius:4px}
  .mc-sets-row{display:grid;grid-template-columns:1fr repeat(var(--cols),36px) 1fr;gap:0;background:#162032;border-top:1px solid #1e3a5f}
  .mc-sets-row .sh{font-size:10px;color:#475569;text-align:center;padding:5px 0;font-weight:600;text-transform:uppercase}
  .mc-sets-row .sv{font-size:13px;font-weight:700;text-align:center;padding:5px 0}
  .mc-sets-row .sv.won{color:#38bdf8}.mc-sets-row .sv.cur{color:#f1f5f9}.mc-sets-row .sv.lost{color:#64748b}
  .mc-sets-row .pname{font-size:11px;color:#94a3b8;padding:5px 14px;font-weight:500}
  .mc-sets-row .pname.right{text-align:right}
  .mc-odds-row{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#0f172a;border-top:1px solid #334155}
  .mc-odds-box{padding:10px 14px;text-align:center;background:#1e293b}
  .mc-odds-box:hover{background:#243554;cursor:pointer}
  .mc-odds-label{font-size:10px;color:#64748b;margin-bottom:3px;font-weight:500}
  .mc-odds-val{font-size:26px;font-weight:900;color:#38bdf8;line-height:1}
  .mc-odds-val.fav{color:#34d399}
  .mc-odds-val.no-odds{color:#334155;font-size:18px}
  .mc-no-odds-hint{font-size:10px;color:#475569;margin-top:2px}
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
    const surfLabel=m.surface.replace('_',' ');
    const dur=m.duration_mins>0?` &middot; ${m.duration_mins}m`:'';
    const [setsP1,setsP2]=m.score.split('-').map(Number);
    const [gamesP1,gamesP2]=m.games.split('-').map(Number);
    const hasOdds=m.odds_p1>1.01&&m.odds_p2>1.01;

    // Rebuild per-set scores from game_log
    const sets=buildSets(m.game_log,setsP1,setsP2,gamesP1,gamesP2,m.current_set);
    const numCols=sets.length;

    // Sets breakdown table
    let setsHtml='';
    if(numCols>0){
      const p1last=m.player1.split(' ').pop();
      const p2last=m.player2.split(' ').pop();
      const setHeaderCols=sets.map((_,i)=>`<th>S${i+1}</th>`).join('');
      const p1Cells=sets.map((s,i)=>{
        const cur=i===numCols-1;
        const cls=cur?'cur':(s.p1>s.p2?'won':'lost');
        return `<td class="${cls}">${s.p1}</td>`;
      }).join('');
      const p2Cells=sets.map((s,i)=>{
        const cur=i===numCols-1;
        const cls=cur?'cur':(s.p2>s.p1?'won':'lost');
        return `<td class="${cls}">${s.p2}</td>`;
      }).join('');
      setsHtml=`<div class="mc-sets-wrap"><table class="mc-sets-table">
        <thead><tr><th class="pn"></th>${setHeaderCols}<th class="pn tot">Sets</th></tr></thead>
        <tbody>
          <tr><td class="pn">${p1last}</td>${p1Cells}<td class="pn tot won">${setsP1}</td></tr>
          <tr><td class="pn">${p2last}</td>${p2Cells}<td class="pn tot ${setsP2>setsP1?'won':''}">${setsP2}</td></tr>
        </tbody>
      </table></div>`;
    }

    // Odds
    const favP1=hasOdds&&m.odds_p1<m.odds_p2;
    const favP2=hasOdds&&m.odds_p2<m.odds_p1;
    const oddsHint=hasOdds?'':`<div class="mc-no-odds-hint">No live odds yet</div>`;
    const oddsRow=`<div class="mc-odds-row">
      <div class="mc-odds-box">
        <div class="mc-odds-label">Back ${m.player1.split(' ').pop()}</div>
        <div class="mc-odds-val ${hasOdds?(favP1?'fav':''):'no-odds'}">${hasOdds?m.odds_p1.toFixed(2):'—'}</div>
        ${!hasOdds?oddsHint:''}
      </div>
      <div class="mc-odds-box">
        <div class="mc-odds-label">Back ${m.player2.split(' ').pop()}</div>
        <div class="mc-odds-val ${hasOdds?(favP2?'fav':''):'no-odds'}">${hasOdds?m.odds_p2.toFixed(2):'—'}</div>
      </div>
    </div>`;

    const tbTag=m.is_tiebreak?' <span style="font-size:10px;background:#7c3aed;color:#ddd6fe;padding:1px 5px;border-radius:3px;vertical-align:middle">TB</span>':'';
    const hasScore=gamesP1>0||gamesP2>0||setsP1>0||setsP2>0;

    return `<div class="match-card">
      <div class="mc-header">
        <span class="source-tag">${m.source.toUpperCase()}</span>
        <span class="${surf}">${surfLabel}</span>
        <span>&middot; ${m.tournament}${dur}</span>
        ${m.is_tiebreak?'<span style="margin-left:auto;background:#7c3aed;color:#ddd6fe;font-size:10px;padding:1px 6px;border-radius:3px;font-weight:700">TIEBREAK</span>':''}
      </div>
      <div class="mc-body">
        <div class="mc-player">
          <div class="mc-name">${m.player1}</div>
          ${hasScore?`<div class="mc-sets-won">${setsP1}</div>`:''}
        </div>
        <div class="mc-center">
          <div class="mc-set-label">Set ${m.current_set}${tbTag}</div>
          <div class="mc-game-score">${hasScore?`${gamesP1} : ${gamesP2}`:'— : —'}</div>
          ${m.duration_mins>0?`<div class="mc-set-tag">${m.duration_mins} min</div>`:''}
        </div>
        <div class="mc-player right">
          <div class="mc-name">${m.player2}</div>
          ${hasScore?`<div class="mc-sets-won">${setsP2}</div>`:''}
        </div>
      </div>
      ${setsHtml}
      ${oddsRow}
    </div>`;
  }).join('');
}

function buildSets(gameLog,setsP1,setsP2,curGamesP1,curGamesP2,currentSet){
  // Reconstruct per-set game scores from game_log
  // Each entry is 1 (player1 won game) or 2 (player2 won game)
  const totalSets=setsP1+setsP2+1; // +1 for current set
  const sets=[];
  let log=[...(gameLog||[])];
  let p1=0,p2=0;
  for(let s=1;s<totalSets;s++){
    let sp1=0,sp2=0;
    // Play until someone wins the set (6+ with 2 gap, or 7 in tiebreak)
    while(log.length){
      const w=log.shift();
      if(w===1)sp1++;else sp2++;
      if((sp1>=6||sp2>=6)&&Math.abs(sp1-sp2)>=2){break;}
      if(sp1===7||sp2===7){break;}
    }
    sets.push({p1:sp1,p2:sp2});
  }
  // Current set
  sets.push({p1:curGamesP1,p2:curGamesP2});
  return sets;
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
