"""
Formats Signal objects into human-readable Telegram messages.
Uses MarkdownV2 format (Telegram's default rich formatting).
"""
from analysis.signal import Signal
from config.settings import settings

_SIGNAL_EMOJI = {
    "momentum":         "⚡",
    "odds_value":       "📉",
    "serve_degradation":"🎯",
    "set_pattern":      "📊",
    "fatigue":          "😤",
    "ml_value":         "🤖",
    "endgame":          "⏱",
    "break_momentum":   "💥",
    "second_set_fade":  "🔄",
}

_SIGNAL_NAME = {
    "momentum":         "Momentum Surge",
    "odds_value":       "Odds Value",
    "serve_degradation":"Serve Degradation",
    "set_pattern":      "Set Pattern",
    "fatigue":          "Fatigue Signal",
    "ml_value":         "ML Value",
    "endgame":          "Endgame Scalp",
    "break_momentum":   "Break Momentum",
    "second_set_fade":  "Second Set Fade",
}

_MARKET_LABEL = {
    "match_winner":     "Match Winner",
    "next_game":        "Next Game Winner",
    "next_set":         "Next Set Winner",
    "set_winner_set2":  "Set 2 Winner",
}

_SURFACE_EMOJI = {
    "clay":         "🟤",
    "grass":        "🟢",
    "hard":         "🔵",
    "indoor_hard":  "🔵",
}


def _confidence_bar(confidence: float) -> str:
    filled = round(confidence * 10)
    return "█" * filled + "░" * (10 - filled)


def _prob_bar(prob: float, width: int = 12) -> str:
    filled = round(prob * width)
    return "▓" * filled + "░" * (width - filled)


def _escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def _e(value) -> str:
    return _escape_md(str(value))


def format_signal(sig: Signal) -> str:
    emoji = _SIGNAL_EMOJI.get(sig.signal_type, "🎾")
    surface_emoji = _SURFACE_EMOJI.get(sig.surface, "⚪")
    signal_name = _SIGNAL_NAME.get(sig.signal_type, sig.signal_type.replace("_", " ").title())
    market_label = _MARKET_LABEL.get(sig.recommended_market, sig.recommended_market)
    confidence_pct = round(sig.confidence * 100)
    bar = _confidence_bar(sig.confidence)
    stake_amount = round(settings.bank_size * sig.stake_pct)
    stake_pct_display = round(sig.stake_pct * 100, 1)

    # Win probability from market odds
    market_prob = round(100.0 / sig.current_odds, 1) if sig.current_odds > 1 else 0
    fair_prob = round(100.0 / sig.fair_odds, 1) if sig.fair_odds > 1 else 0
    edge_display = _e(round(sig.edge_pct * 100, 1))

    # Win probability bars
    model_bar = _prob_bar(fair_prob / 100)
    market_bar_str = _prob_bar(market_prob / 100)

    # Clean up trigger description — remove internal model jargon
    trigger = sig.trigger_description
    # Truncate very long descriptions
    if len(trigger) > 200:
        trigger = trigger[:197] + "..."

    lines = [
        f"{emoji} *{_e(signal_name)} Detected*",
        "",
        # ── WHO TO BET ON — crystal clear ──────────────────────
        f"🎯 *BET ON: {_escape_md(sig.player_name.upper())}*",
        f"   _{_escape_md(market_label)}_",
        f"   vs {_escape_md(sig.opponent_name)}",
        "",
        # ── Match context ───────────────────────────────────────
        f"📍 {surface_emoji} *{_escape_md(sig.tournament)}*",
        f"   Score: {_escape_md(sig.score_summary)}",
        "",
        # ── Why this bet ────────────────────────────────────────
        "💡 *Why:*",
        f"   {_escape_md(trigger)}",
        "",
        # ── Probability comparison ──────────────────────────────
        "📈 *Win Probability*",
        f"   Model:  {model_bar} {_e(fair_prob)}%",
        f"   Market: {market_bar_str} {_e(market_prob)}%",
        f"   Edge:   \\+{edge_display}% value",
        "",
        # ── Bet details ─────────────────────────────────────────
        f"✅ *Back {_escape_md(sig.player_name)} @ {_e(sig.current_odds)}*",
        f"   Fair value: \\~{_e(sig.fair_odds)}",
        "",
        # ── Confidence ─────────────────────────────────────────
        f"📊 Confidence: *{_e(confidence_pct)}%*  {bar}",
        "",
        # ── Stake ──────────────────────────────────────────────
        "💵 *Suggested Stake*",
        f"   {_e(stake_pct_display)}% of bank ≈ ₹{_e(f'{stake_amount:,}')}",
        f"   \\(max cap: {_e(round(settings.max_stake_pct*100))}%\\)",
    ]
    return "\n".join(lines)
