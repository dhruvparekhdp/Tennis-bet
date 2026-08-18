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

    def rsi_divergence(self, lookback: int = 14) -> str | None:
        """
        Detect Regular Bullish or Bearish RSI Divergence:
        - Bullish: Price made lower low, but RSI made higher low.
        - Bearish: Price made higher high, but RSI made lower high.
        """
        candles = self.candles_1m[-lookback:] if len(self.candles_1m) >= lookback else self.candles_1m
        if len(candles) < 8:
            return None

        closes = [c.close for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]

        # Recent vs older half
        half = len(candles) // 2
        older_low, recent_low = min(lows[:half]), min(lows[half:])
        older_high, recent_high = max(highs[:half]), max(highs[half:])

        # Bullish: price lower low while RSI is in oversold/recovering state
        if recent_low < older_low and self.rsi_14 > self.rsi_14_prev and self.rsi_14 < 45:
            return "bullish"

        # Bearish: price higher high while RSI is in overbought/cooling state
        if recent_high > older_high and self.rsi_14 < self.rsi_14_prev and self.rsi_14 > 55:
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
