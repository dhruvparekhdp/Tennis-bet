from __future__ import annotations

from analysis.crypto_signal import CryptoSignal
from config.settings import settings


def format_crypto_signal(sig: CryptoSignal) -> str:
    """Format a CryptoSignal dataclass into a Telegram message."""
    dir_emoji = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
    symbol_display = sig.symbol.upper()

    conf_pct = int(sig.confidence * 100)
    filled_blocks = int(sig.confidence * 10)
    empty_blocks = 10 - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks

    target_str = f"${sig.target_price:,.4f}" if sig.target_price else "n/a"
    stop_str = f"${sig.stop_loss:,.4f}" if sig.stop_loss else "n/a"

    stake_amt = round(sig.stake_pct * settings.bank_size, 2)
    stake_pct_display = round(sig.stake_pct * 100, 2)

    sent_emoji = "⚪ Neutral"
    if sig.sentiment_score >= 0.2:
        sent_emoji = f"🟢 Bullish ({sig.sentiment_score:+.2f})"
    elif sig.sentiment_score <= -0.2:
        sent_emoji = f"🔴 Bearish ({sig.sentiment_score:+.2f})"

    return (
        f"📊 <b>CRYPTO TRADE SIGNAL</b>\n\n"
        f"🪙 <b>{symbol_display}</b> — {dir_emoji} [{sig.timeframe.upper()}]\n"
        f"   Entry: <b>${sig.current_price:,.4f}</b>\n"
        f"   Target: <b>{target_str}</b>\n"
        f"   Stop Loss: <b>{stop_str}</b>\n"
        f"   Edge: <b>{sig.edge_pct:+.2f}%</b>\n\n"
        f"⚡ <b>Analysis:</b>\n"
        f"   {sig.trigger_description}\n"
        f"   Indicators: {sig.indicators_summary}\n"
        f"   News Sentiment: {sent_emoji}\n\n"
        f"📊 <b>Confidence:</b> {conf_pct}% {bar}\n\n"
        f"💵 <b>Stake Suggestion:</b>\n"
        f"   {stake_pct_display}% of bank ≈ ₹{stake_amt:,.0f} / ${stake_amt:,.0f}\n"
        f"   (Quarter-Kelly risk management)"
    )
