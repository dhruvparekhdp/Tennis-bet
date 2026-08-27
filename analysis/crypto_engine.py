from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_signals import (
    BollingerSqueezeAnalyzer,
    ConfluenceAnalyzer,
    RSIDivergenceAnalyzer,
    SentimentShiftAnalyzer,
    VolumeSpikeAnalyzer,
)
from analysis.crypto_state import CryptoState
from config.settings import settings

log = structlog.get_logger()


class CryptoEngine:
    """Orchestrates all crypto signal detectors with cooldown deduplication and thresholding."""

    def __init__(self) -> None:
        # Listed first because it is the one that requires agreement; the
        # others each fire on a single observation.
        self.confluence = ConfluenceAnalyzer()
        self.rsi_divergence = RSIDivergenceAnalyzer()
        self.volume_spike = VolumeSpikeAnalyzer()
        self.bollinger_squeeze = BollingerSqueezeAnalyzer()
        self.sentiment_shift = SentimentShiftAnalyzer()

        # Cooldown map: (symbol, signal_type) -> last_fired_utc
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._recent_signals: list[CryptoSignal] = []
        # Last signal per (symbol, direction) that has neither hit its target
        # nor its stop. A second signal while the first is still live is the
        # same trade at a slightly later price, not a new idea.
        self._live: dict[tuple[str, str], CryptoSignal] = {}

    def process(self, state: CryptoState) -> list[CryptoSignal]:
        """Evaluate all signal strategies against current crypto market state."""
        candidates: list[CryptoSignal | None] = [
            self.confluence.analyze(state),
            self.rsi_divergence.analyze(state),
            self.volume_spike.analyze(state),
            self.bollinger_squeeze.analyze(state),
            self.sentiment_shift.analyze(state),
        ]

        fired: list[CryptoSignal] = []
        now = datetime.now(UTC)

        for sig in candidates:
            if sig is None:
                continue

            if sig.confidence < settings.crypto_min_confidence:
                log.debug("crypto_signal_below_threshold", symbol=sig.symbol, confidence=sig.confidence)
                continue

            if self._is_on_cooldown(sig.symbol, sig.signal_type):
                log.debug("crypto_signal_on_cooldown", symbol=sig.symbol, type=sig.signal_type)
                continue

            if self._still_live(sig, state.current_price):
                log.debug("crypto_signal_duplicate_of_live", symbol=sig.symbol,
                          direction=sig.direction)
                continue

            self._set_cooldown(sig.symbol, sig.signal_type, now)
            self._live[(sig.symbol, sig.direction)] = sig
            fired.append(sig)
            self._recent_signals.append(sig)
            if len(self._recent_signals) > 100:
                self._recent_signals = self._recent_signals[-100:]

        return fired

    def _get_signal_ttl(self, sig: CryptoSignal) -> timedelta:
        """Derive time-to-live from signal timeframe so live tracking does not deadlock."""
        tf = (sig.timeframe or "").strip().lower()
        if tf.endswith("m"):
            try:
                mins = int(tf[:-1])
                return timedelta(minutes=max(30, int(mins * 1.5)))
            except ValueError:
                pass
        elif tf.endswith("h"):
            try:
                hrs = int(tf[:-1])
                return timedelta(hours=max(1, hrs))
            except ValueError:
                pass
        return timedelta(minutes=60)

    def _still_live(self, sig: CryptoSignal, price: float, now: datetime | None = None) -> bool:
        """
        Is a previous signal for this symbol and direction still running?

        A timer alone cannot tell a fresh setup from the same move re-detected:
        one strongly trending coin produced four "new" longs inside an hour,
        every one of them the same continuous move at a later price.

        However, holding indefinitely without TTL caused complete watchlist
        starvation after a few hours when price hovered between target and stop.
        We check price resolution first, then age out old signals past their TTL.
        """
        prev = self._live.get((sig.symbol, sig.direction))
        if prev is None or price <= 0:
            return False

        cur_now = now or datetime.now(UTC)
        ttl = self._get_signal_ttl(prev)
        if (cur_now - prev.timestamp) >= ttl:
            self._live.pop((sig.symbol, sig.direction), None)
            return False

        long_ = sig.direction == "long"
        resolved = (price >= prev.target_price or price <= prev.stop_loss) if long_ \
            else (price <= prev.target_price or price >= prev.stop_loss)
        if resolved:
            self._live.pop((sig.symbol, sig.direction), None)
            return False
        return True

    def _is_on_cooldown(self, symbol: str, signal_type: str) -> bool:
        key = (symbol.lower(), signal_type)
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = timedelta(minutes=settings.crypto_signal_cooldown_minutes)
        return (datetime.now(UTC) - last) < cooldown

    def _set_cooldown(self, symbol: str, signal_type: str, timestamp: datetime) -> None:
        self._cooldowns[(symbol.lower(), signal_type)] = timestamp

    def get_recent_signals(self, hours: int = 24) -> list[CryptoSignal]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return [s for s in self._recent_signals if s.timestamp >= cutoff]
