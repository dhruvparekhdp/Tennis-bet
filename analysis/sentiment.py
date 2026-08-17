from __future__ import annotations

import re

import structlog

from config.settings import settings

log = structlog.get_logger()

# Financial & Geopolitical high-impact keyword modifiers
_BULLISH_KEYWORDS = {
    "surge": 0.4, "rally": 0.4, "bullish": 0.5, "approved": 0.6, "approval": 0.6,
    "etf": 0.3, "inflow": 0.4, "partnership": 0.3, "adoption": 0.4, "breakout": 0.4,
    "all-time high": 0.5, "ath": 0.5, "upgrade": 0.3, "cut": 0.3, "easing": 0.3,
    "expansion": 0.3, "buy": 0.3, "staking": 0.2, "integration": 0.3,
}

_BEARISH_KEYWORDS = {
    "crash": -0.6, "dump": -0.5, "bearish": -0.5, "hack": -0.7, "exploit": -0.7,
    "lawsuit": -0.5, "sec": -0.3, "ban": -0.6, "liquidation": -0.5, "outflow": -0.4,
    "fraud": -0.7, "investigation": -0.4, "scam": -0.6, "insolvent": -0.8,
    "war": -0.5, "escalation": -0.4, "sanction": -0.4, "hike": -0.3, "inflation": -0.3,
}


class SentimentAnalyzer:
    """
    Analyzes news headlines and geopolitical events.
    Supports lightweight lexicon + VADER scoring by default (cloud-safe on 512MB RAM),
    or FinBERT transformer pipeline when USE_FINBERT=true.
    """

    def __init__(self) -> None:
        self._mode = "lexicon"
        self._pipe = None

        if settings.use_finbert:
            try:
                from transformers import pipeline
                log.info("loading_finbert_pipeline")
                self._pipe = pipeline("text-classification", model="ProsusAI/finbert")
                self._mode = "finbert"
                log.info("finbert_loaded_successfully")
            except Exception as exc:
                log.warning("finbert_load_failed_falling_back_to_lexicon", error=str(exc))
                self._mode = "lexicon"

        if self._mode == "lexicon":
            try:
                from nltk.sentiment.vader import SentimentIntensityAnalyzer
                self._vader = SentimentIntensityAnalyzer()
            except Exception:
                self._vader = None

    def score(self, headlines: list[str]) -> float:
        """
        Calculates aggregate sentiment from -1.0 (strongly bearish) to +1.0 (strongly bullish).
        """
        if not headlines:
            return 0.0

        if self._mode == "finbert" and self._pipe:
            try:
                results = self._pipe(headlines, truncation=True, max_length=512)
                scores: list[float] = []
                for r in results:
                    lbl = r.get("label", "").lower()
                    conf = float(r.get("score", 0.0))
                    if lbl == "positive":
                        scores.append(conf)
                    elif lbl == "negative":
                        scores.append(-conf)
                    else:
                        scores.append(0.0)
                return round(sum(scores) / len(scores), 3)
            except Exception as exc:
                log.warning("finbert_inference_failed", error=str(exc))

        # Lexicon + VADER fallback
        scores = []
        for text in headlines:
            s = self._score_single_text(text)
            scores.append(s)

        return round(sum(scores) / len(scores), 3) if scores else 0.0

    def _score_single_text(self, text: str) -> float:
        text_lower = text.lower()
        base_score = 0.0

        if self._vader is not None:
            try:
                base_score = self._vader.polarity_scores(text)["compound"]
            except Exception:
                base_score = 0.0

        # Apply crypto domain keyword modifiers
        keyword_delta = 0.0
        for kw, delta in _BULLISH_KEYWORDS.items():
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                keyword_delta += delta

        for kw, delta in _BEARISH_KEYWORDS.items():
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                keyword_delta += delta

        combined = base_score * 0.6 + keyword_delta * 0.4
        return max(-1.0, min(1.0, combined))
