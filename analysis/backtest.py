"""
Event-driven backtest engine.

Walks historical candles forward one at a time, feeding each into the same
CryptoState the live collectors populate, running the same analyzers, and
executing fills through the same paper_trading maths. Nothing here is a
parallel implementation — a backtest result and a live paper cycle differ only
in where the candles came from.

Look-ahead is prevented structurally: a signal generated on candle N can only
be filled at candle N's close, and is only ever resolved against candles N+1
onward. The engine never sees a bar before it would have existed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import structlog

from analysis.crypto_signals import (
    BollingerSqueezeAnalyzer,
    RSIDivergenceAnalyzer,
    SentimentShiftAnalyzer,
    VolumeSpikeAnalyzer,
)
from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.crypto_state_store import (
    _compute_atr,
    _compute_bollinger,
    _compute_ema,
    _compute_rsi,
)
from analysis.paper_trading import (
    ClosedTrade,
    CycleConfig,
    ExitReason,
    Position,
    Side,
    close_position,
    open_position,
    resolve_candle,
)
from collectors.historical_klines import Candle

log = structlog.get_logger()

CANDLE_WINDOW = 120   # matches the live store's rolling window


@dataclass
class BacktestResult:
    symbol: str
    config: CycleConfig
    trades: list[ClosedTrade] = field(default_factory=list)
    starting_wallet: float = 0.0
    final_wallet: float = 0.0
    candles_processed: int = 0
    signals_generated: int = 0
    signals_taken: int = 0
    signals_rejected_confidence: int = 0
    signals_rejected_capital: int = 0
    ended_reason: str = "data_exhausted"
    first_ts: str | None = None
    last_ts: str | None = None
    target_distances_pct: list[float] = field(default_factory=list)

    # ── derived stats ────────────────────────────────────────────────────

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def losses(self) -> int:
        return len(self.trades) - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def net_pnl(self) -> float:
        return self.final_wallet - self.starting_wallet

    @property
    def total_fees(self) -> float:
        return sum(t.fees_paid for t in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def liquidations(self) -> int:
        return sum(1 for t in self.trades if t.reason is ExitReason.LIQUIDATION)

    @property
    def avg_win(self) -> float:
        w = [t.net_pnl for t in self.trades if t.won]
        return sum(w) / len(w) if w else 0.0

    @property
    def avg_loss(self) -> float:
        losing = [t.net_pnl for t in self.trades if not t.won]
        return sum(losing) / len(losing) if losing else 0.0

    @property
    def realised_rr(self) -> float:
        """Actual reward-to-risk achieved, as opposed to the configured target."""
        return abs(self.avg_win / self.avg_loss) if self.avg_loss else 0.0

    @property
    def expectancy(self) -> float:
        """Average net rupees per trade — the number that decides the cycle."""
        return self.net_pnl / len(self.trades) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = self.starting_wallet
        worst = 0.0
        w = self.starting_wallet
        for t in self.trades:
            w = t.wallet_after
            peak = max(peak, w)
            if peak > 0:
                worst = max(worst, (peak - w) / peak)
        return worst * 100.0

    @property
    def longest_losing_streak(self) -> int:
        best = cur = 0
        for t in self.trades:
            cur = 0 if t.won else cur + 1
            best = max(best, cur)
        return best

    @property
    def median_target_distance_pct(self) -> float:
        if not self.target_distances_pct:
            return 0.0
        s = sorted(self.target_distances_pct)
        return s[len(s) // 2]

    def by_signal_type(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for t in self.trades:
            k = t.position.signal_type or "unknown"
            d = out.setdefault(k, {"trades": 0, "wins": 0, "net": 0.0, "fees": 0.0})
            d["trades"] += 1
            d["wins"] += 1 if t.won else 0
            d["net"] += t.net_pnl
            d["fees"] += t.fees_paid
        for d in out.values():
            d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0.0
            d["net"] = round(d["net"], 2)
            d["fees"] = round(d["fees"], 2)
        return out

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "period": {"from": self.first_ts, "to": self.last_ts,
                       "candles": self.candles_processed},
            "wallet": {
                "start": round(self.starting_wallet, 2),
                "final": round(self.final_wallet, 2),
                "net_pnl": round(self.net_pnl, 2),
                "return_pct": round(self.net_pnl / self.starting_wallet * 100, 2)
                if self.starting_wallet else 0.0,
                "ended_reason": self.ended_reason,
            },
            "trades": {
                "total": len(self.trades),
                "wins": self.wins,
                "losses": self.losses,
                "win_rate_pct": round(self.win_rate * 100, 1),
                "liquidations": self.liquidations,
                "expectancy_per_trade": round(self.expectancy, 2),
                "avg_win": round(self.avg_win, 2),
                "avg_loss": round(self.avg_loss, 2),
                "realised_reward_risk": round(self.realised_rr, 2),
                "longest_losing_streak": self.longest_losing_streak,
                "max_drawdown_pct": round(self.max_drawdown_pct, 1),
            },
            "costs": {
                "gross_pnl": round(self.gross_pnl, 2),
                "total_fees": round(self.total_fees, 2),
                "fees_as_pct_of_gross": round(
                    abs(self.total_fees / self.gross_pnl) * 100, 1) if self.gross_pnl else None,
            },
            "signals": {
                "generated": self.signals_generated,
                "taken": self.signals_taken,
                "rejected_low_confidence": self.signals_rejected_confidence,
                "rejected_no_capital": self.signals_rejected_capital,
                "median_target_distance_pct": round(self.median_target_distance_pct, 4),
                "break_even_move_pct": round(self.config.break_even_move_pct(), 4),
            },
            "by_signal_type": self.by_signal_type(),
        }


class BacktestEngine:
    """Replays candles through the live analyzers and the live fill maths."""

    def __init__(self, config: CycleConfig, use_sentiment: bool = False) -> None:
        self.config = config
        self.analyzers = [
            RSIDivergenceAnalyzer(),
            VolumeSpikeAnalyzer(),
            BollingerSqueezeAnalyzer(),
        ]
        # Sentiment needs a live news feed, so it is off for historical replay
        # unless a sentiment series is supplied alongside the candles.
        if use_sentiment:
            self.analyzers.append(SentimentShiftAnalyzer())

    def _recompute(self, state: CryptoState) -> None:
        """Same indicator maths as the live store, over real OHLC this time."""
        closes = [c.close for c in state.candles_1m]
        if len(closes) < 14:
            return
        state.rsi_14_prev = state.rsi_14
        state.rsi_14 = _compute_rsi(closes, 14)
        state.macd_line = _compute_ema(closes, 12) - _compute_ema(closes, 26)
        state.ema_9 = _compute_ema(closes, 9)
        state.ema_20 = _compute_ema(closes, 20)
        state.ema_50 = _compute_ema(closes, 50)
        state.ema_200 = _compute_ema(closes, 200)
        up, mid, low, bw = _compute_bollinger(closes, 20, 2.0)
        state.bollinger_upper, state.bollinger_mid = up, mid
        state.bollinger_lower, state.bollinger_bandwidth = low, bw
        state.atr_14 = _compute_atr(state.candles_1m, 14)

    def run(self, symbol: str, candles: list[Candle]) -> BacktestResult:
        cfg = self.config
        res = BacktestResult(symbol=symbol, config=cfg,
                             starting_wallet=cfg.starting_wallet,
                             final_wallet=cfg.starting_wallet)
        if not candles:
            res.ended_reason = "no_data"
            return res

        res.first_ts = candles[0].ts.isoformat()
        res.last_ts = candles[-1].ts.isoformat()

        base = symbol.lower().replace("usdt", "")
        state = CryptoState(symbol=symbol.lower(), base_asset=base.upper())
        wallet = cfg.starting_wallet
        open_positions: list[Position] = []

        for i, c in enumerate(candles):
            res.candles_processed += 1

            # 1. Resolve existing positions against THIS candle before anything new
            #    is opened, so a position can never be opened and closed on the
            #    same bar using the same information.
            still_open: list[Position] = []
            for pos in open_positions:
                hit = resolve_candle(pos, c.high, c.low, c.close, c.ts)
                if hit is None:
                    still_open.append(pos)
                    continue
                reason, price = hit
                trade = close_position(pos, price, reason, c.ts, cfg.fees, wallet)
                wallet = trade.wallet_after
                res.trades.append(trade)
            open_positions = still_open

            if wallet >= cfg.target_wallet:
                res.ended_reason = "hit_target"
                break
            if wallet <= 0 or wallet < cfg.min_margin:
                res.ended_reason = "busted"
                break

            # 2. Feed the candle into state and refresh indicators
            state.current_price = c.close
            state.timestamp = c.ts
            state.high_24h = max(state.high_24h, c.high)
            state.low_24h = min(state.low_24h, c.low) if state.low_24h else c.low
            state.candles_1m.append(OHLCVCandle(
                open=c.open, high=c.high, low=c.low, close=c.close,
                volume=c.volume, timestamp=c.ts, is_closed=True))
            if len(state.candles_1m) > CANDLE_WINDOW:
                state.candles_1m = state.candles_1m[-CANDLE_WINDOW:]
            if len(state.candles_1m) < 30:
                continue
            self._recompute(state)

            # rolling 24h volume baseline for the volume analyzer
            recent = state.candles_1m[-60:]
            state.volume_24h = sum(x.volume for x in recent)
            state.price_24h_ago = state.candles_1m[0].close

            # 3. Generate signals — one position per symbol at a time
            if open_positions or len(open_positions) >= cfg.max_concurrent:
                continue

            for az in self.analyzers:
                try:
                    sig = az.analyze(state)
                except Exception:
                    continue
                if sig is None:
                    continue
                res.signals_generated += 1

                if sig.confidence < cfg.min_confidence:
                    res.signals_rejected_confidence += 1
                    continue

                margin = cfg.margin_for(wallet)
                if margin > wallet or margin < cfg.min_margin:
                    res.signals_rejected_capital += 1
                    continue

                side = Side.LONG if sig.direction == "long" else Side.SHORT
                pos = open_position(
                    symbol=symbol, side=side, entry_price=c.close,
                    margin=margin, leverage=cfg.leverage, fees=cfg.fees,
                    stop_pct_of_margin=cfg.stop_pct_of_margin,
                    reward_risk=cfg.reward_risk, opened_at=c.ts,
                    signal_type=sig.signal_type, timeframe=sig.timeframe,
                    confidence=sig.confidence,
                    expires_at=c.ts + timedelta(minutes=cfg.max_hold_minutes),
                )
                wallet -= margin              # margin is locked while the position lives
                open_positions.append(pos)
                res.signals_taken += 1
                res.target_distances_pct.append(
                    abs(pos.target_price - pos.entry_price) / pos.entry_price * 100.0)
                break                          # at most one new position per candle

        # 4. Close anything still open at the final price
        if open_positions:
            last = candles[min(res.candles_processed, len(candles)) - 1]
            for pos in open_positions:
                trade = close_position(pos, last.close, ExitReason.CYCLE_END,
                                       last.ts, cfg.fees, wallet)
                wallet = trade.wallet_after
                res.trades.append(trade)

        res.final_wallet = wallet
        return res
