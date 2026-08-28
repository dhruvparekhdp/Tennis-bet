"""
Post-mortem on fired signals, and a catalogue of the code that fired them.

Why this module exists
----------------------
The dashboard shows what the system *thinks* right now. Nothing showed what
it thought last week and whether that turned out to be right, broken down by
the thing you suspect. "Only BCH fires" and "fees are bigger than the profits"
were both true and both invisible, because the only way to see either was to
read the signal log by hand.

Two halves, deliberately in one place:

* `audit()` turns resolved signal rows into a verdict table plus slices — by
  symbol, setup, horizon, direction, confidence band and edge band — so a
  failure can be attributed rather than guessed at.
* `method_catalogue()` lists every function on the path from candle to signal,
  read out of the modules themselves. Introspected, not transcribed, so it
  cannot drift from the code it documents. A renamed function fails the
  catalogue test instead of quietly becoming a lie on a page.

The point of pairing them is that a slice on the left has code on the right.
If the short horizon underperforms, the horizon policy is one scroll away.

Pure functions over plain rows. No I/O, no ORM import — the web layer passes
whatever it read, and the tests pass fakes.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analysis.scalp_levels import ScalpConfig

# Outcomes the resolver can write. "pending" is not a verdict — it is the
# absence of one, and counting it as a loss is how a young dataset gets
# reported as a broken strategy.
DECIDED = ("won", "lost")
TERMINAL = ("won", "lost", "expired")


# ── Verdict rows ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """One fired signal with the arithmetic that decides whether it paid."""

    id: int
    symbol: str
    signal_type: str
    direction: str
    horizon: str
    confidence: float          # 0..1
    entry: float
    target: float
    stop: float
    outcome: str
    pnl_pct: float             # signed price move, gross of costs
    cost_pct: float            # round trip for THIS market, in percent
    timestamp: datetime | None

    @property
    def move_pct(self) -> float:
        """How far the target sat from entry, as a percentage of price."""
        if not self.entry or not self.target:
            return 0.0
        return abs(self.target - self.entry) / self.entry * 100.0

    @property
    def risk_pct(self) -> float:
        if not self.entry or not self.stop:
            return 0.0
        return abs(self.entry - self.stop) / self.entry * 100.0

    @property
    def reward_risk(self) -> float:
        return self.move_pct / self.risk_pct if self.risk_pct else 0.0

    @property
    def x_cost(self) -> float:
        """The target expressed in round trips. Below 1.0 it loses when it wins."""
        return self.move_pct / self.cost_pct if self.cost_pct else 0.0

    @property
    def net_pnl_pct(self) -> float:
        """
        What the trade actually kept.

        A win of 0.18% against a 0.168% round trip is a win on the chart and
        roughly nothing in the wallet. Every aggregate below nets the cost off,
        because the gross column is the one that flattered this system for
        weeks.
        """
        return self.pnl_pct - self.cost_pct if self.outcome in TERMINAL else 0.0

    @property
    def decided(self) -> bool:
        return self.outcome in DECIDED

    @property
    def won(self) -> bool:
        return self.outcome == "won"

    # Gross and net can disagree, and that disagreement is the finding.
    @property
    def net_won(self) -> bool:
        return self.outcome in TERMINAL and self.net_pnl_pct > 0


def to_verdict(row: Any, cfg: ScalpConfig | None = None) -> Verdict:
    """
    Normalise one `CryptoSignalLog` row (or any object with those fields).

    The cost is re-derived per market rather than taken from the caller's
    config: spread and slippage are assumptions and follow `cfg`, but the
    brokerage is a fact about the instrument, so `for_symbol` reimposes it.
    An audit that let a caller zero the fee could show anything as profitable.
    """
    base = cfg or ScalpConfig()
    symbol = (getattr(row, "symbol", "") or "").lower()
    return Verdict(
        id=int(getattr(row, "id", 0) or 0),
        symbol=symbol,
        signal_type=getattr(row, "signal_type", "") or "",
        direction=getattr(row, "direction", "") or "",
        horizon=getattr(row, "timeframe", "") or "",
        confidence=float(getattr(row, "confidence", 0.0) or 0.0),
        entry=float(getattr(row, "current_price", 0.0) or 0.0),
        target=float(getattr(row, "target_price", 0.0) or 0.0),
        stop=float(getattr(row, "stop_loss", 0.0) or 0.0),
        outcome=getattr(row, "outcome", "pending") or "pending",
        pnl_pct=float(getattr(row, "pnl_pct", 0.0) or 0.0),
        cost_pct=base.for_symbol(symbol).cost_floor_pct * 100.0,
        timestamp=getattr(row, "timestamp", None),
    )


# ── Aggregation ───────────────────────────────────────────────────────────


@dataclass
class Bucket:
    """Scoreboard for one slice. Gross and net are both kept, never merged."""

    key: str
    label: str = ""
    n: int = 0
    won: int = 0
    lost: int = 0
    expired: int = 0
    pending: int = 0
    gross_sum: float = 0.0
    net_sum: float = 0.0
    net_wins: int = 0
    move_sum: float = 0.0
    x_cost_sum: float = 0.0
    conf_sum: float = 0.0

    @property
    def decided(self) -> int:
        return self.won + self.lost

    @property
    def hit_rate(self) -> float | None:
        """Null, not zero, when nothing has resolved. A blank slice has no rate."""
        return round(self.won / self.decided * 100, 1) if self.decided else None

    @property
    def net_hit_rate(self) -> float | None:
        """Share that finished ahead *after* costs — the honest version."""
        settled = self.won + self.lost + self.expired
        return round(self.net_wins / settled * 100, 1) if settled else None

    @property
    def avg_gross_pct(self) -> float | None:
        settled = self.won + self.lost + self.expired
        return round(self.gross_sum / settled, 4) if settled else None

    @property
    def expectancy_pct(self) -> float | None:
        """
        Average net price move per signal taken.

        This is the number that decides whether the slice is worth firing at
        all: positive means the setup pays for its own costs on average,
        negative means every additional signal is a slow leak no win rate can
        rescue.
        """
        settled = self.won + self.lost + self.expired
        return round(self.net_sum / settled, 4) if settled else None

    def add(self, v: Verdict) -> None:
        self.n += 1
        if v.outcome == "won":
            self.won += 1
        elif v.outcome == "lost":
            self.lost += 1
        elif v.outcome == "expired":
            self.expired += 1
        else:
            self.pending += 1
        if v.outcome in TERMINAL:
            self.gross_sum += v.pnl_pct
            self.net_sum += v.net_pnl_pct
            if v.net_won:
                self.net_wins += 1
        self.move_sum += v.move_pct
        self.x_cost_sum += v.x_cost
        self.conf_sum += v.confidence

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label or self.key,
            "n": self.n,
            "won": self.won,
            "lost": self.lost,
            "expired": self.expired,
            "pending": self.pending,
            "decided": self.decided,
            "hit_rate_pct": self.hit_rate,
            "net_hit_rate_pct": self.net_hit_rate,
            "avg_gross_pct": self.avg_gross_pct,
            "expectancy_pct": self.expectancy_pct,
            "avg_move_pct": round(self.move_sum / self.n, 4) if self.n else None,
            "avg_x_cost": round(self.x_cost_sum / self.n, 2) if self.n else None,
            "avg_confidence_pct": round(self.conf_sum / self.n * 100, 1) if self.n else None,
        }


def _group(rows: Iterable[Verdict], key, label=None) -> list[dict]:
    buckets: dict[str, Bucket] = {}
    for v in rows:
        k = key(v)
        if k in (None, ""):
            continue
        k = str(k)
        b = buckets.get(k)
        if b is None:
            b = buckets[k] = Bucket(key=k, label=label(v) if label else k)
        b.add(v)
    # Most-fired first: the slice with the most evidence is the one to read.
    return [b.as_dict() for b in sorted(buckets.values(),
                                        key=lambda b: (-b.n, b.key))]


def _confidence_band(v: Verdict) -> str:
    pct = v.confidence * 100
    lo = int(pct // 5 * 5)
    return f"{lo}-{lo + 5}%"


def _x_cost_band(v: Verdict) -> str:
    """
    Bands chosen from `kept = 1 - 1/x`: at 2x a trade keeps half its gross,
    at 3x two thirds, at 5x four fifths. The boundaries are where the fee
    stops and starts owning the outcome.
    """
    x = v.x_cost
    if x < 1:
        return "<1x (loses when it wins)"
    if x < 2:
        return "1-2x (keeps <half)"
    if x < 3:
        return "2-3x (keeps ~half)"
    if x < 5:
        return "3-5x (keeps ~2/3)"
    return "5x+ (keeps 4/5+)"


def is_valid_verdict(v: Verdict) -> bool:
    """Filter out corrupt or distorted legacy signal records (e.g. entry=0.0001, target=-1799.9)."""
    if v.entry <= 0.001 or v.target <= 0 or v.stop <= 0:
        return False
    if v.move_pct > 100.0 or v.risk_pct > 100.0:
        return False
    if abs(v.pnl_pct) > 200.0:
        return False
    return True


def audit(rows: Iterable[Any], cfg: ScalpConfig | None = None) -> dict:
    """
    Turn stored signal rows into a table plus every slice worth suspecting.

    The slices are fixed rather than user-defined on purpose: these six are
    the ones that have actually explained a problem here — a symbol that never
    fires, a setup that only wins on paper, a horizon that is too short, a
    direction bias, a confidence number that means nothing, and a target that
    never cleared its own costs.
    """
    verdicts = [to_verdict(r, cfg) for r in rows]
    verdicts = [v for v in verdicts if is_valid_verdict(v)]
    total = Bucket(key="all", label="All signals")
    for v in verdicts:
        total.add(v)

    return {
        "totals": total.as_dict(),
        "records": [
            {
                "id": v.id,
                "symbol": v.symbol.upper(),
                "signal_type": v.signal_type,
                "direction": v.direction,
                "horizon": v.horizon,
                "confidence_pct": round(v.confidence * 100),
                "entry": v.entry,
                "target": v.target,
                "stop": v.stop,
                "move_pct": round(v.move_pct, 4),
                "risk_pct": round(v.risk_pct, 4),
                "reward_risk": round(v.reward_risk, 2),
                "x_cost": round(v.x_cost, 2),
                "cost_pct": round(v.cost_pct, 4),
                "outcome": v.outcome,
                "pnl_pct": round(v.pnl_pct, 4),
                "net_pnl_pct": round(v.net_pnl_pct, 4),
                "net_won": v.net_won,
                "timestamp": v.timestamp.isoformat() if v.timestamp else None,
            }
            for v in verdicts
        ],
        "by_symbol": _group(verdicts, lambda v: v.symbol.upper()),
        "by_setup": _group(verdicts, lambda v: v.signal_type),
        "by_horizon": _group(verdicts, lambda v: v.horizon),
        "by_direction": _group(verdicts, lambda v: v.direction),
        "by_confidence": _group(verdicts, _confidence_band),
        "by_edge": _group(verdicts, _x_cost_band),
    }


# ── Method catalogue ──────────────────────────────────────────────────────
#
# The path a candle takes to become a signal, in the order it is walked. Each
# entry names a real attribute of a real module; `method_catalogue()` reads the
# signature, the docstring and the source line out of the object itself, so
# this list is the only thing that has to be maintained and a stale name is a
# test failure rather than a wrong page.

STAGES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "1. Ingest",
        "Raw ticks become 1-minute candles and rolling indicator state.",
        (
            ("analysis.crypto_state_store", "append_candle"),
            ("analysis.crypto_state_store", "recalculate_indicators"),
            ("analysis.crypto_state_store", "CryptoStateStore"),
        ),
    ),
    (
        "2. Indicators",
        "Everything derived from price, volume and range. Pure maths, no opinion.",
        (
            ("analysis.indicators", "sma"),
            ("analysis.indicators", "ema"),
            ("analysis.indicators", "wilder"),
            ("analysis.indicators", "rsi"),
            ("analysis.indicators", "rsi_series"),
            ("analysis.indicators", "stochastic"),
            ("analysis.indicators", "macd"),
            ("analysis.indicators", "roc"),
            ("analysis.indicators", "true_ranges"),
            ("analysis.indicators", "atr"),
            ("analysis.indicators", "atr_pct"),
            ("analysis.indicators", "bollinger"),
            ("analysis.indicators", "keltner"),
            ("analysis.indicators", "squeeze_on"),
            ("analysis.indicators", "volatility_percentile"),
            ("analysis.indicators", "adx"),
            ("analysis.indicators", "supertrend"),
            ("analysis.indicators", "obv"),
            ("analysis.indicators", "mfi"),
            ("analysis.indicators", "vwap"),
            ("analysis.indicators", "has_usable_volume"),
            ("analysis.indicators", "relative_volume"),
            ("analysis.indicators", "volume_trend"),
            ("analysis.indicators", "swing_pivots"),
            ("analysis.indicators", "divergence"),
            ("analysis.indicators", "trend_structure"),
            ("analysis.indicators", "support_resistance"),
        ),
    ),
    (
        "3. Patterns",
        "Candle shapes. Each returns a direction and a strength, or nothing.",
        (
            ("analysis.patterns", "engulfing"),
            ("analysis.patterns", "pin_bar"),
            ("analysis.patterns", "inside_bar"),
            ("analysis.patterns", "three_bar_reversal"),
            ("analysis.patterns", "range_breakout"),
            ("analysis.patterns", "double_top_bottom"),
            ("analysis.patterns", "detect_all"),
        ),
    ),
    (
        "4. Confluence vote",
        "Five independent families, one vote each, so a single idea counted "
        "five ways cannot masquerade as agreement.",
        (
            ("analysis.confluence", "trend_vote"),
            ("analysis.confluence", "momentum_vote"),
            ("analysis.confluence", "volume_vote"),
            ("analysis.confluence", "structure_vote"),
            ("analysis.confluence", "pattern_vote"),
            ("analysis.confluence", "volatility_veto"),
            ("analysis.confluence", "thin_volume_veto"),
            ("analysis.confluence", "evaluate"),
            ("analysis.confluence", "ConvictionGate"),
        ),
    ),
    (
        "5. Level policy",
        "Cost sets the floor and volatility decides whether there is a trade "
        "at all — not the other way round.",
        (
            ("analysis.scalp_levels", "ScalpConfig"),
            ("analysis.scalp_levels", "scalp_levels"),
            ("analysis.scalp_levels", "trailing_plan"),
            ("analysis.scalp_levels", "tick_for_price"),
            ("analysis.scalp_levels", "round_to_tick"),
            ("analysis.scalp_levels", "roe_to_price_move"),
            ("analysis.scalp_levels", "max_leverage_for_roe_target"),
        ),
    ),
    (
        "6. Analyzers",
        "What fires. Each says whether and which way; none of them says how far.",
        (
            ("analysis.crypto_signals", "RSIDivergenceAnalyzer"),
            ("analysis.crypto_signals", "VolumeSpikeAnalyzer"),
            ("analysis.crypto_signals", "BollingerSqueezeAnalyzer"),
            ("analysis.crypto_signals", "SentimentShiftAnalyzer"),
            ("analysis.crypto_signals", "ConfluenceAnalyzer"),
        ),
    ),
    (
        "7. Execution model",
        "What a signal would cost and be worth if it were actually traded.",
        (
            ("analysis.paper_trading", "FeeModel"),
            ("analysis.paper_trading", "SlippageModel"),
            ("analysis.paper_trading", "TrailingStop"),
            ("analysis.paper_trading", "Position"),
            ("analysis.paper_trading", "ProfitLadder"),
            ("analysis.paper_trading", "liquidation_price"),
            ("analysis.paper_trading", "resolve_candle"),
        ),
    ),
)


@dataclass(frozen=True)
class Method:
    stage: str
    module: str
    name: str
    kind: str            # "function" | "class"
    signature: str
    summary: str
    doc: str
    source: str          # "analysis/indicators.py:73"
    members: list[str] = field(default_factory=list)


def _first_paragraph(doc: str) -> str:
    """The one-line version. Full text is kept alongside for the drawer."""
    if not doc:
        return ""
    para = doc.strip().split("\n\n", 1)[0]
    return " ".join(para.split())


def _describe(stage: str, module_name: str, attr: str) -> Method:
    import importlib

    mod = importlib.import_module(module_name)
    obj = getattr(mod, attr)
    doc = inspect.getdoc(obj) or ""
    try:
        sig = f"{attr}{inspect.signature(obj)}"
    except (TypeError, ValueError):
        sig = attr
    try:
        line = inspect.getsourcelines(obj)[1]
    except (OSError, TypeError):
        line = 0
    members = []
    if inspect.isclass(obj):
        members = [n for n, m in vars(obj).items()
                   if not n.startswith("_") and (inspect.isfunction(m)
                                                 or isinstance(m, (property, classmethod)))]
    return Method(
        stage=stage,
        module=module_name,
        name=attr,
        kind="class" if inspect.isclass(obj) else "function",
        signature=sig,
        summary=_first_paragraph(doc),
        doc=doc,
        source=f"{module_name.replace('.', '/')}.py:{line}",
        members=members,
    )


def method_catalogue() -> list[dict]:
    """
    Every method on the path from candle to signal, read from the code.

    Returned grouped by stage and in pipeline order, because the useful
    question on the test page is never "what functions exist" but "what ran,
    in what order, before this signal appeared".
    """
    out = []
    for stage, blurb, entries in STAGES:
        methods = [_describe(stage, mod, attr) for mod, attr in entries]
        out.append({
            "stage": stage,
            "description": blurb,
            "count": len(methods),
            "methods": [
                {
                    "module": m.module, "name": m.name, "kind": m.kind,
                    "signature": m.signature, "summary": m.summary,
                    "doc": m.doc, "source": m.source, "members": m.members,
                }
                for m in methods
            ],
        })
    return out
