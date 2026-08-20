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

import statistics
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
from analysis.crypto_state_store import append_candle, recalculate_indicators
from analysis.paper_trading import (
    ClosedTrade,
    CycleConfig,
    ExitReason,
    Position,
    Side,
    adverse_fraction_of_stop,
    close_position,
    is_market_shock,
    open_position,
    resolve_candle,
)
from collectors.historical_klines import Candle

log = structlog.get_logger()


def _drift_pct(candles: list, i: int, lookback: int) -> float:
    """
    Average per-bar price change over the `lookback` bars ending before `i`.

    Bar `i` is excluded deliberately. Its own move is the thing we are trying
    to trade; letting it set our fill price would be look-ahead of the most
    flattering kind.
    """
    if lookback <= 0 or i <= 0:
        return 0.0
    start = max(0, i - lookback)
    first, last = candles[start].close, candles[i - 1].close
    bars = i - start
    if first <= 0 or bars <= 0:
        return 0.0
    return ((last - first) / first) / bars


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
    early_exits: int = 0
    signals_rejected_unviable: int = 0
    trail_moves: int = 0
    contradictions_seen: int = 0
    shocks_detected: int = 0
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
        return statistics.median(self.target_distances_pct)

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
                "rejected_target_too_small": self.signals_rejected_unviable,
                "trail_moves": self.trail_moves,
                "contradictory_readings": self.contradictions_seen,
                "early_exits": self.early_exits,
                "market_shocks": self.shocks_detected,
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
        self._last_review: dict[int, object] = {}
        self._fast_mode_until = None

    def _review_position(self, pos: Position, state: CryptoState, candle,
                         shock: bool, cfg: CycleConfig) -> ExitReason | None:
        """
        Decide whether an open position should be closed early.

        Returns the exit reason, or None to keep holding. Three ways out:

        1. A shock bar that is already driving price most of the way to the
           stop — take the exit now rather than at a worse fill.
        2. The analyzers now point the other way.
        3. Conviction has decayed below the floor.

        Reviews are rate-limited, and run far more often once the market is
        judged to be moving. Every early exit costs a round trip, so the bar
        for acting is deliberately high.
        """
        rc = cfg.review
        held_min = (candle.ts - pos.opened_at).total_seconds() / 60.0
        if held_min < rc.min_hold_minutes_before_review:
            return None

        # A violent bar moving hard against us — act immediately, no rate limit.
        if shock:
            if adverse_fraction_of_stop(pos, candle.close) >= rc.shock_adverse_stop_fraction:
                return ExitReason.MARKET_SHOCK

        # Rate-limit the ordinary re-check; tighten the cadence in fast mode.
        in_fast = self._fast_mode_until is not None and candle.ts <= self._fast_mode_until
        interval = rc.fast_interval_minutes if in_fast else rc.normal_interval_minutes
        last = self._last_review.get(id(pos))
        if last is not None and (candle.ts - last).total_seconds() / 60.0 < interval:
            return None
        self._last_review[id(pos)] = candle.ts

        # Re-run the analyzers and see what they say now.
        best = None
        for az in self.analyzers:
            try:
                sig = az.analyze(state)
            except Exception:
                continue
            if sig is not None and (best is None or sig.confidence > best.confidence):
                best = sig
        if best is None:
            return None   # no fresh read is not evidence against the position

        now_side = Side.LONG if best.direction == "long" else Side.SHORT
        if rc.exit_on_direction_flip and now_side is not pos.side:
            return ExitReason.SIGNAL_FLIP
        if (rc.exit_confidence_floor is not None
                and now_side is pos.side
                and best.confidence < rc.exit_confidence_floor):
            return ExitReason.CONVICTION_LOST
        return None

    def run(self, symbol: str, candles: list[Candle]) -> BacktestResult:
        cfg = self.config
        res = BacktestResult(symbol=symbol, config=cfg,
                             starting_wallet=cfg.starting_wallet,
                             final_wallet=cfg.starting_wallet)
        if not candles:
            res.ended_reason = "no_data"
            return res

        self._last_review = {}
        self._fast_mode_until = None
        res.first_ts = candles[0].ts.isoformat()
        res.last_ts = candles[-1].ts.isoformat()

        base = symbol.lower().replace("usdt", "")
        state = CryptoState(symbol=symbol.lower(), base_asset=base.upper())
        wallet = cfg.starting_wallet
        open_positions: list[Position] = []

        for i, c in enumerate(candles):
            res.candles_processed += 1

            # Recent per-bar drift, from CLOSED bars only. This drives the
            # trend term in the fill price, so it must never peek at the bar
            # we are about to trade on.
            drift = _drift_pct(candles, i, cfg.drift_lookback)

            # 1. Resolve existing positions against THIS candle before anything new
            #    is opened, so a position can never be opened and closed on the
            #    same bar using the same information.
            still_open: list[Position] = []
            for pos in open_positions:
                hit = resolve_candle(pos, c.high, c.low, c.close, c.ts,
                                     slippage=cfg.slippage, drift_pct=drift)
                if hit is None:
                    # Only now, once this bar could not close the position at
                    # the stop it actually had. Trailing first would let this
                    # bar's high pull the stop above this bar's low.
                    fav = c.high if pos.side is Side.LONG else c.low
                    if pos.apply_ladder(fav, cfg.ladder, cfg.fees):
                        res.trail_moves += 1
                    if pos.update_trail(c.high, c.low, cfg.trailing, cfg.fees):
                        res.trail_moves += 1
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
            append_candle(state, OHLCVCandle(
                open=c.open, high=c.high, low=c.low, close=c.close,
                volume=c.volume, timestamp=c.ts, is_closed=True))
            if len(state.candles_1m) < 30:
                continue
            recalculate_indicators(state)

            # rolling 24h volume baseline for the volume analyzer
            recent = state.candles_1m[-60:]
            state.volume_24h = sum(x.volume for x in recent)
            state.price_24h_ago = state.candles_1m[0].close

            # 2b. Re-check open positions against the *current* signal state.
            #     A stop answers "did price move against me"; this answers
            #     "is the reason I opened this still true".
            if open_positions and cfg.review.enabled:
                avg_vol = state.volume_24h / len(recent) if recent else 0.0
                shock = is_market_shock(c.high - c.low, state.atr_14,
                                        c.volume, avg_vol, cfg.review)
                if shock:
                    res.shocks_detected += 1
                    self._fast_mode_until = c.ts + timedelta(
                        minutes=cfg.review.fast_mode_duration_minutes)

                survivors: list[Position] = []
                for pos in open_positions:
                    verdict = self._review_position(pos, state, c, shock, cfg)
                    if verdict is None:
                        survivors.append(pos)
                        continue
                    # A review exit is a market order like a stop, not a limit
                    # order like a target, so it pays the same fill penalty.
                    fill = cfg.slippage.exit_fill(c.close, pos.side, verdict, drift)
                    trade = close_position(pos, fill, verdict, c.ts, cfg.fees, wallet)
                    wallet = trade.wallet_after
                    res.trades.append(trade)
                    res.early_exits += 1
                open_positions = survivors

            # 3. Open new positions — at most one per symbol, capped overall
            if len(open_positions) >= cfg.max_concurrent:
                continue
            if any(p.symbol == symbol for p in open_positions):
                continue

            # Collect every analyzer's view first, then act on the single most
            # confident one. Taking them individually is how you end up holding
            # a long and a short in the same coin at the same time — perfectly
            # hedged, zero exposure, paying fees and funding on both.
            candidates = []
            for az in self.analyzers:
                try:
                    sig = az.analyze(state)
                except Exception:
                    continue
                if sig is None:
                    continue
                res.signals_generated += 1
                candidates.append(sig)

            if not candidates:
                continue

            contradictory = len({s.direction for s in candidates}) > 1
            if contradictory:
                res.contradictions_seen += 1

            sig = max(candidates, key=lambda s: s.confidence)

            if sig.confidence < cfg.min_confidence:
                res.signals_rejected_confidence += 1
                continue

            # Size by conviction, respecting what is already at risk.
            committed = sum(p.margin for p in open_positions)
            margin = cfg.margin_for_signal(wallet, sig.confidence, committed)
            if margin <= 0 or margin > wallet:
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
                usdt_inr=cfg.usdt_inr, lot_step=cfg.lot_step,
                slippage=cfg.slippage, drift_pct=drift,
            )

            # Lot rounding can refuse a size outright on a small wallet.
            if pos.coin_qty <= 0:
                res.signals_rejected_unviable += 1
                continue

            # Refuse targets that cannot pay for the round trip. This is the
            # filter that would have rejected every signal in the live dashboard.
            if not cfg.is_target_viable(pos.entry_price, pos.target_price):
                res.signals_rejected_unviable += 1
                continue

            wallet -= margin              # margin is locked while the position lives
            open_positions.append(pos)
            res.signals_taken += 1
            res.target_distances_pct.append(
                abs(pos.target_price - pos.entry_price) / pos.entry_price * 100.0)

        # 4. Close anything still open at the final price
        if open_positions:
            last = candles[res.candles_processed - 1]
            for pos in open_positions:
                fill = cfg.slippage.exit_fill(last.close, pos.side,
                                              ExitReason.CYCLE_END, 0.0)
                trade = close_position(pos, fill, ExitReason.CYCLE_END,
                                       last.ts, cfg.fees, wallet)
                wallet = trade.wallet_after
                res.trades.append(trade)

        res.final_wallet = wallet
        return res
