from __future__ import annotations

from analysis.football_signals import FootballSignal

_SIGNAL_NAMES = {
    "late_lead": "Late Lead — Sure Winner",
    "heavy_fav_dominating": "Favourite Dominating",
    "late_draw_fade": "Draw Fade",
    "red_card_advantage": "Red Card Edge",
    "clean_sheet_likely": "Clean Sheet Likely",
}

_SIGNAL_EMOJIS = {
    "late_lead": "⏱️",
    "heavy_fav_dominating": "💪",
    "late_draw_fade": "🔄",
    "red_card_advantage": "🟥",
    "clean_sheet_likely": "🧤",
}

_MARKET_NAMES = {
    "match_winner": "Match Winner",
    "draw_no_bet": "Draw No Bet (DNB)",
    "asian_handicap_0": "Asian Handicap 0",
}


def format_football_signal(sig: FootballSignal) -> str:
    signal_name = _SIGNAL_NAMES.get(sig.signal_type, sig.signal_type.replace("_", " ").title())
    emoji = _SIGNAL_EMOJIS.get(sig.signal_type, "⚽")
    market_name = _MARKET_NAMES.get(sig.market, sig.market)
    side = "HOME 🏠" if sig.is_home else "AWAY ✈️"

    conf_pct = round(sig.confidence * 100)
    bar = "█" * round(conf_pct / 10) + "░" * (10 - round(conf_pct / 10))

    model_pct = round((1.0 / sig.fair_odds) * 100) if sig.fair_odds > 1.0 else 0
    market_pct = round((1.0 / sig.current_odds) * 100) if sig.current_odds > 1.0 else 0
    model_bar = "▓" * round(model_pct / 5) + "░" * (20 - round(model_pct / 5))
    market_bar = "▓" * round(market_pct / 5) + "░" * (20 - round(market_pct / 5))

    lines = [
        f"⚽ FOOTBALL — {emoji} {signal_name}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🎯 BET ON: {sig.team_to_back.upper()}",
        f"   {side} | {market_name}",
        f"   Odds: {sig.current_odds:.2f}  |  Fair Value: {sig.fair_odds:.2f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 {sig.tournament}",
        f"   {sig.team_to_back} vs {sig.opponent}",
        f"   Score: {sig.score_summary}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "📊 WHY:",
        f"   {sig.trigger_description}",
        "",
        f"📈 Model:  {model_bar} {model_pct}%",
        f"📉 Market: {market_bar} {market_pct}%",
        f"   Edge: +{sig.edge_pct * 100:.1f}%",
        "",
        f"Confidence: {conf_pct}%  {bar}",
        f"Stake: {sig.stake_pct * 100:.1f}% of bank",
        "",
        "⚠️  Bet responsibly. Signal is informational only.",
    ]
    return "\n".join(lines)
