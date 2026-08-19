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

            self._set_cooldown(sig.symbol, sig.signal_type, now)
            fired.append(sig)
            self._recent_signals.append(sig)
            if len(self._recent_signals) > 100:
                self._recent_signals = self._recent_signals[-100:]

        return fired

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
