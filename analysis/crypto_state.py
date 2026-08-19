from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class OHLCVCandle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime
    is_closed: bool = True


@dataclass
class CryptoState:
    symbol: str                                                    # e.g., "btcusdt"
    base_asset: str                                                # e.g., "BTC"
    quote_asset: str = "USDT"                                      # e.g., "USDT"
    current_price: float = 0.0
    price_24h_ago: float = 0.0
    volume_24h: float = 0.0
    volume_24h_avg: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0

    # Candle histories for multiple timeframes
    candles_1m: list[OHLCVCandle] = field(default_factory=list)    # last 120 candles (2h)
    candles_5m: list[OHLCVCandle] = field(default_factory=list)    # last 72 candles (6h)
    candles_15m: list[OHLCVCandle] = field(default_factory=list)   # last 96 candles (24h)
    candles_1h: list[OHLCVCandle] = field(default_factory=list)    # last 168 candles (7d)
    candles_4h: list[OHLCVCandle] = field(default_factory=list)    # last 60 candles (10d)
    candles_1d: list[OHLCVCandle] = field(default_factory=list)    # last 30 candles (30d)

    # Key Technical Indicators (Computed periodically on candle updates)
    rsi_14: float = 50.0
    rsi_14_prev: float = 50.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_mid: float = 0.0
    bollinger_lower: float = 0.0
    bollinger_bandwidth: float = 0.0                               # (upper - lower) / mid
    ema_9: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    atr_14: float = 0.0                                            # Average True Range (volatility)

    # Sentiment (FinBERT / VADER / CryptoPanic)
    sentiment_score: float = 0.0                                   # -1.0 (bearish) to +1.0 (bullish)
    sentiment_news_count: int = 0
    last_sentiment_update: datetime | None = None

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def price_change_24h_pct(self) -> float:
        if self.price_24h_ago <= 0:
            return 0.0
        return ((self.current_price - self.price_24h_ago) / self.price_24h_ago) * 100.0

    @property
    def volume_ratio(self) -> float:
        """Ratio of recent volume to average volume (values > 2.0 indicate volume surges)."""
        if self.volume_24h_avg <= 0:
            return 1.0
        return self.volume_24h / self.volume_24h_avg

    def rsi_divergence(self, lookback: int = 40) -> str | None:
        """
        Regular RSI divergence, measured at confirmed swing pivots.

        Bullish — price makes a lower low while RSI makes a higher low.
        Bearish — price makes a higher high while RSI makes a lower high.

        The previous version compared the minimum of one half-window against
        the other and then asked whether RSI happened to be ticking upward. It
        never read RSI at the two lows, which is the entire definition. Measured
        on pure noise that condition fired on 51% of bars — a coin flip, and the
        reason nearly every signal on the dashboard was a "momentum reversal".
        """
        from analysis.indicators import divergence, rsi_series

        candles = self.candles_1m[-lookback:] if len(self.candles_1m) >= lookback \
            else self.candles_1m
        if len(candles) < 20:
            return None

        closes = [c.close for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        rs = rsi_series(closes)
        if len(rs) < 8:
            return None

        # RSI starts later than price, so both series are trimmed to the same
        # bars before any comparison — otherwise the pivots do not line up.
        offset = len(closes) - len(rs)
        lows_a, highs_a = lows[offset:], highs[offset:]

        if divergence(lows_a, rs, bullish=True):
            return "bullish"
        if divergence(highs_a, rs, bullish=False):
            return "bearish"
        return None


@dataclass
class CommodityState:
    symbol: str                                                    # e.g., "XAU/USD", "WTI/USD"
    name: str                                                      # "Gold", "Silver", "Crude Oil"
    current_price: float = 0.0
    price_1h_ago: float = 0.0
    price_24h_ago: float = 0.0
    price_history: list[tuple[float, datetime]] = field(default_factory=list)  # [(price, ts)]
    atr_14: float = 0.0
    rsi_14: float = 50.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def price_change_24h_pct(self) -> float:
        if self.price_24h_ago <= 0:
            return 0.0
        return ((self.current_price - self.price_24h_ago) / self.price_24h_ago) * 100.0


@dataclass
class NewsItem:
    title: str
    source: str
    currencies: list[str]
    native_sentiment: str                                          # "bullish", "bearish", "neutral"
    published_at: datetime
    url: str

    @classmethod
    def from_api(cls, post: dict) -> NewsItem:
        currencies = [c.get("code", "") for c in post.get("currencies", []) if c.get("code")]
        votes = post.get("votes", {})
        bullish_votes = votes.get("bullish", 0)
        bearish_votes = votes.get("bearish", 0)

        sentiment = "neutral"
        if bullish_votes > bearish_votes:
            sentiment = "bullish"
        elif bearish_votes > bullish_votes:
            sentiment = "bearish"

        created_str = post.get("created_at")
        try:
            pub_date = datetime.fromisoformat(created_str.replace("Z", "+00:00")) if created_str else datetime.now(UTC)
        except Exception:
            pub_date = datetime.now(UTC)

        return cls(
            title=post.get("title", ""),
            source=post.get("source", {}).get("title", ""),
            currencies=currencies,
            native_sentiment=sentiment,
            published_at=pub_date,
            url=post.get("url", ""),
        )
