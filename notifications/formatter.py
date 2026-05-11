"""
Formats Signal objects into human-readable Telegram messages.
Uses MarkdownV2 format (Telegram's default rich formatting).
"""
from analysis.signal import Signal
from config.settings import settings

_SIGNAL_EMOJI = {
    "momentum": "⚡",
    "odds_value": "📉",
    "serve_degradation": "🎯",
    "set_pattern": "📊",
    "fatigue": "😤",
}

_MARKET_LABEL = {
    "match_winner": "Match Winner",
    "next_game": "Next Game Winner",
    "next_set": "Next Set Winner",
}

_SURFACE_EMOJI = {
    "clay": "🟤",
    "grass": "🟢",
    "hard": "🔵",
    "indoor_hard": "🔵",
}


def _confidence_bar(confidence: float) -> str:
    filled = round(confidence * 10)
    return "█" * filled + "░" * (10 - filled)


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    special = r"\_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def format_signal(sig: Signal) -> str:
    emoji = _SIGNAL_EMOJI.get(sig.signal_type, "🎾")
    surface_emoji = _SURFACE_EMOJI.get(sig.surface, "⚪")
    market_label = _MARKET_LABEL.get(sig.recommended_market, sig.recommended_market)
    confidence_pct = round(sig.confidence * 100)
    bar = _confidence_bar(sig.confidence)
    stake_amount = round(settings.bank_size * sig.stake_pct)
    stake_pct_display = round(sig.stake_pct * 100, 1)

    signal_type_label = sig.signal_type.replace("_", " ").title()

    lines = [
        "🎾 *TENNIS TRADE SIGNAL*",
        "",
        f"📍 *{_escape_md(sig.player_name)} vs {_escape_md(sig.opponent_name)}*",
        f"   {surface_emoji} {_escape_md(sig.tournament)} \\| {sig.surface.replace('_', ' ').title()}",
        f"   Score: {_escape_md(sig.score_summary)}",
        f"   Match time: {sig.match_duration_mins} mins",
        "",
        f"{emoji} *{signal_type_label} Detected*",
        f"   {_escape_md(sig.trigger_description)}",
        "",
        f"💰 *TRADE SUGGESTION*",
        f"   Back: *{_escape_md(sig.player_name)}*",
        f"   Market: {_escape_md(market_label)}",
        f"   Odds: {sig.current_odds} \\(fair: \\~{sig.fair_odds}\\)",
        f"   Edge: \\+{round(sig.edge_pct * 100, 1)}%",
        "",
        f"📊 Confidence: *{confidence_pct}%*  {bar}",
        "",
        f"💵 *Stake Suggestion*",
        f"   {stake_pct_display}% of bank ≈ ₹{stake_amount:,}",
        f"   \\(Quarter\\-Kelly, capped at {round(settings.max_stake_pct*100)}%\\)",
    ]
    return "\n".join(lines)
