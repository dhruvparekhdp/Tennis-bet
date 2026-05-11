# Tennis Bet — Live Match Signal Monitor

24/7 tennis match analyser that sends trade signals to Telegram.
Monitors live matches via Sofascore, runs 5 prediction algorithms,
and alerts you when there's a value betting opportunity.

**Cost: ₹0/month** — Sofascore (free), Telegram Bot API (free), Koyeb hosting (free).

---

## How it works

```
Sofascore live API (every 30s)
        ↓
  MatchStateStore
        ↓
  5 Analyzers
  ┣ Momentum Shift        — back player on 3+ game winning streak
  ┣ Odds Overreaction     — fade large odds moves with no score change
  ┣ Serve Degradation     — back returner when server collapses
  ┣ Set Pattern           — back historical slow-starters after losing set 1
  ┗ Fatigue               — back fresher player in 2.5hr+ matches
        ↓
  Telegram Alert (with stake sizing)
        ↓
  You place bet manually on 1xBet / Parimatch / Stake.com
```

---

## Setup

### Step 1 — Create your Telegram bot

1. Open Telegram → message `@BotFather`
2. Send `/newbot` and follow prompts → copy the **bot token**
3. Start your bot (send any message to it)
4. Get your **chat ID**: open this URL in a browser (replace `<TOKEN>` with your token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":XXXXXXXXX}` in the response — that number is your chat ID.

### Step 2 — Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
BANK_SIZE=10000        # your bank in rupees (or any currency)
MIN_CONFIDENCE=0.65    # signals below this are ignored (0.0–1.0)
```

### Step 3 — Run locally (test first)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Check Telegram — you should receive a startup message.
Check health: `curl http://localhost:8080/health`

---

## Deploy to Koyeb (free, always-on)

Koyeb gives 512MB RAM, 0.1 vCPU, always-on — free forever.

### 1. Push this repo to GitHub (if not already)

### 2. Sign up at koyeb.com (free, no credit card needed)

### 3. Create a new service

- **Source**: GitHub → select this repo → branch `main`
- **Build**: Docker (Koyeb auto-detects the `Dockerfile`)
- **Instance type**: Free
- **Port**: 8080
- **Environment variables** — add these in the Koyeb dashboard:
  ```
  TELEGRAM_BOT_TOKEN = your_token
  TELEGRAM_CHAT_ID   = your_chat_id
  BANK_SIZE          = 10000
  MIN_CONFIDENCE     = 0.65
  ```

### 4. Deploy

Koyeb builds and runs your Docker container. Once green:
- You'll get a Telegram message: "Tennis-bet monitor started."
- Health URL: `https://your-app.koyeb.app/health`

---

## Signal format (Telegram)

```
🎾 TENNIS TRADE SIGNAL

📍 Djokovic vs Alcaraz
   🟤 Madrid Open | Clay
   Score: 3-6, 2-2 in set 2
   Match time: 83 mins

⚡ Momentum Shift Detected
   Alcaraz won last 4 consecutive games.
   Odds moved only 6% — market lagging momentum.

💰 TRADE SUGGESTION
   Back: ALCARAZ
   Market: Next Game Winner
   Odds: 1.72 (fair: ~1.55)
   Edge: +10.9%

📊 Confidence: 75%  ███████░░░

💵 Stake Suggestion
   1.2% of bank ≈ ₹120
   (Quarter-Kelly, capped at 3%)
```

---

## Project structure

```
tennis-bet/
├── main.py                   # entry point
├── config/settings.py        # all config via .env
├── collectors/sofascore.py   # live match data (Sofascore API)
├── analysis/
│   ├── match_state.py        # canonical match data model
│   ├── momentum.py           # Signal 1: consecutive game streaks
│   ├── odds_value.py         # Signal 2: odds overreaction
│   ├── server_performance.py # Signal 3: serve degradation
│   ├── set_patterns.py       # Signal 4: slow starter patterns
│   ├── fatigue.py            # Signal 5: physical fatigue
│   └── engine.py             # orchestrates all analyzers
├── notifications/
│   ├── formatter.py          # formats signals to Telegram messages
│   └── telegram_notifier.py  # sends via Bot API
├── storage/                  # SQLite via SQLAlchemy
└── scheduler/runner.py       # APScheduler 24/7 loop
```

---

## Tuning signals

Edit `.env` to adjust sensitivity:

| Setting | Default | Effect |
|---------|---------|--------|
| `MIN_CONFIDENCE` | 0.65 | Lower = more alerts; raise to 0.75 for fewer, higher-quality signals |
| `SIGNAL_COOLDOWN_MINUTES` | 10 | Minimum gap between same signal type per match |
| `MAX_STAKE_PCT` | 0.03 | Hard cap on stake (3% of bank per bet) |
| `BANK_SIZE` | 10000 | Your bank — only affects stake size suggestion in alerts |

---

## Future: ML enhancement (after 2+ weeks of data)

Once signal history accumulates in `tennis_bet.db`, you can export and train:

```bash
sqlite3 tennis_bet.db "SELECT * FROM signal_log" > signals.csv
```

Train a RandomForest on historical signal outcomes vs match results to improve
confidence calibration per surface, tournament level, and player type.
