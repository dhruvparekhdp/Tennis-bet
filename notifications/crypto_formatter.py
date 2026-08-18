from __future__ import annotations

from analysis.crypto_signal import CryptoSignal
from config.settings import settings

# Plain-English headline for each signal_type — the underlying jargon
# (RSI divergence, Bollinger squeeze, etc.) is still explained in the
# glossary on the dashboard's Crypto tab, not repeated in every alert.
SIGNAL_NAME = {
    "rsi_divergence": "Momentum Reversal",
    "volume_spike": "Volume Surge",
    "bollinger_squeeze": "Breakout Setup",
    "sentiment_shift": "News Catalyst",
}


def format_crypto_signal(sig: CryptoSignal) -> str:
    """Format a CryptoSignal dataclass into a short, plain-English Telegram message."""
    dir_emoji = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
    symbol_display = sig.symbol.upper()
    name = SIGNAL_NAME.get(sig.signal_type, sig.signal_type.replace("_", " ").title())

    conf_pct = int(sig.confidence * 100)
    target_str = f"${sig.target_price:,.4f}" if sig.target_price else "n/a"
    stop_str = f"${sig.stop_loss:,.4f}" if sig.stop_loss else "n/a"
    stake_amt = round(sig.stake_pct * settings.bank_size, 2)
    stake_pct_display = round(sig.stake_pct * 100, 2)

    return (
        f"🪙 <b>{symbol_display}</b> · {dir_emoji}\n"
        f"<b>{name}</b> ({sig.timeframe})\n\n"
        f"{sig.trigger_description}\n\n"
        f"Entry <b>${sig.current_price:,.4f}</b> → "
        f"Target <b>{target_str}</b> → Stop <b>{stop_str}</b>\n"
        f"Confidence <b>{conf_pct}%</b> · Suggested stake <b>₹{stake_amt:,.0f}</b> "
        f"({stake_pct_display}% of bank)"
    )
