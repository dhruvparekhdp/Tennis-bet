"""
The live paper-trading cycle.

This is the piece that turns the engine into something that runs. It holds one
cycle's wallet, opens positions from gated signals, and resolves them against
live prices on every tick — using the same functions the backtest uses, so a
backtest result and a live result mean the same thing.

Two deliberate choices:

* State lives in the database, not in memory. A redeploy mid-cycle would
  otherwise abandon open positions silently, and on a free host redeploys are
  frequent. Every tick reads the open positions back and writes them again.

* Resolution uses the tick price as both high and low. A live poller sees
  prices, not bars, so there is no intrabar range to reason about. That makes
  the live path slightly *less* likely to trigger a stop than the backtest,
  which resolves against a real bar's extremes — worth knowing when comparing
  the two, and the honest direction for the difference to run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from analysis.crypto_signal import CryptoSignal
from analysis.instruments import spec_for
from analysis.paper_trading import (
    ClosedTrade,
    CycleConfig,
    ExitReason,
    FeeModel,
    Position,
    Side,
    close_position,
    liquidation_price,
    open_position,
    resolve_candle,
)

log = structlog.get_logger()


@dataclass
class CycleState:
    """Everything a tick needs to know, loaded from the database."""

    cycle_id: int
    wallet: float
    peak_wallet: float
    positions: list[Position]
    position_ids: dict[int, int]      # index in `positions` -> DB row id


def config_for_cycle(row) -> CycleConfig:
    """Rebuild the exact configuration a cycle was started under."""
    from analysis.paper_trading import SizingConfig, TrailingStop

    return CycleConfig(
        starting_wallet=row.starting_wallet,
        target_wallet=row.target_wallet,
        leverage=row.leverage,
        stop_pct_of_margin=row.stop_pct_of_margin,
        reward_risk=row.reward_risk,
        min_confidence=row.min_confidence,
        sizing=SizingConfig() if row.scaled_sizing else None,
        trailing=TrailingStop(enabled=row.trailing_enabled, activate_at_r=0.75),
    )


def fees_for(symbol: str) -> FeeModel:
    """Per-market costs — gold is a fifth of ether, so this cannot be global."""
    spec = spec_for(symbol)
    return FeeModel(
        taker_pct=spec.taker_pct,
        maintenance_margin_pct=spec.maintenance_margin_pct,
        funding_rate_per_8h=spec.funding_rate_per_8h,
    )


def committed_margin(positions: list[Position]) -> float:
    return sum(p.margin for p in positions)


def should_open(
    signal: CryptoSignal,
    cfg: CycleConfig,
    state: CycleState,
    now: datetime,
) -> tuple[bool, str]:
    """
    Decide whether this signal becomes a position. Returns (ok, reason).

    The reason is returned even on success so the caller can log why a tick did
    nothing — a cycle that quietly takes no trades for hours is otherwise
    indistinguishable from one that is broken.
    """
    if signal.confidence < cfg.min_confidence:
        return False, "confidence_below_floor"
    if len(state.positions) >= cfg.max_concurrent:
        return False, "max_concurrent"
    if any(p.symbol == signal.symbol for p in state.positions):
        return False, "already_open_in_symbol"

    margin = cfg.margin_for_signal(state.wallet, signal.confidence,
                                   committed_margin(state.positions))
    if margin <= 0:
        return False, "no_free_margin"
    return True, "ok"


def open_from_signal(
    signal: CryptoSignal,
    cfg: CycleConfig,
    state: CycleState,
    now: datetime,
    usdt_inr: float,
) -> Position | None:
    """Build a position from a signal, or None if it fails the viability gate."""
    margin = cfg.margin_for_signal(state.wallet, signal.confidence,
                                   committed_margin(state.positions))
    if margin <= 0:
        return None

    spec = spec_for(signal.symbol)
    pos = open_position(
        symbol=signal.symbol,
        side=Side.LONG if signal.direction == "long" else Side.SHORT,
        entry_price=signal.current_price,
        margin=margin,
        leverage=cfg.leverage,
        fees=fees_for(signal.symbol),
        stop_pct_of_margin=cfg.stop_pct_of_margin,
        reward_risk=cfg.reward_risk,
        opened_at=now,
        signal_type=signal.signal_type,
        timeframe=signal.timeframe,
        confidence=signal.confidence,
        expires_at=now + timedelta(minutes=cfg.max_hold_minutes),
        usdt_inr=usdt_inr,
        lot_step=spec.lot_step,
        slippage=cfg.slippage,
    )
    if pos.coin_qty <= 0:
        log.info("paper.rejected", symbol=signal.symbol, reason="below_one_lot")
        return None
    if not cfg.is_target_viable(pos.entry_price, pos.target_price):
        log.info("paper.rejected", symbol=signal.symbol, reason="target_not_viable")
        return None
    return pos


def resolve_at_price(
    pos: Position,
    price: float,
    now: datetime,
    cfg: CycleConfig,
    wallet: float,
) -> ClosedTrade | None:
    """
    Close the position if this tick price hits a level. None means it survives.

    High and low are both the tick price: a poller sees prices, not bars.
    """
    hit = resolve_candle(pos, high=price, low=price, close=price, ts=now,
                         slippage=cfg.slippage)
    if hit is None:
        pos.update_trail(price, price, cfg.trailing, fees_for(pos.symbol))
        return None
    reason, fill = hit
    return close_position(pos, fill, reason, now, fees_for(pos.symbol), wallet)


def cycle_outcome(wallet: float, cfg: CycleConfig, open_count: int) -> str | None:
    """
    Has the cycle finished? Returns a status, or None to keep running.

    Busting is checked against free margin rather than the wallet reaching
    exactly zero: once there is not enough left to open the smallest allowed
    position, the cycle cannot recover and should stop rather than idle.
    """
    if wallet >= cfg.target_wallet:
        return "hit_target"
    if open_count == 0 and wallet < cfg.min_margin:
        return "busted"
    return None


def recompute_liquidation(pos: Position) -> float:
    """Re-derive liquidation after a scale-in changed the average entry."""
    return liquidation_price(pos.entry_price, pos.side, pos.effective_leverage,
                             fees_for(pos.symbol).maintenance_margin_pct)


def summarise(trades: list, wallet: float, cfg: CycleConfig) -> dict:
    """Scorecard for one cycle. Costs stay broken out, never netted away."""
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.trading_fees for t in trades)
    funding = sum(t.funding_paid for t in trades)
    net = sum(t.net_pnl for t in trades)

    avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0

    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t.net_pnl <= 0 else 0
        worst = max(worst, streak)

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    return {
        "trades": n,
        "wins": len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "wallet": round(wallet, 2),
        "gross_pnl": round(gross, 2),
        "trading_fees": round(fees, 2),
        "funding_paid": round(funding, 2),
        "net_pnl": round(net, 2),
        # The number that says whether costs are the problem.
        "costs_as_pct_of_gross": round((fees + funding) / gross * 100, 1) if gross > 0 else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "realised_reward_risk": round(abs(avg_win / avg_loss), 2) if avg_loss else None,
        "expectancy_per_trade": round(net / n, 2) if n else 0.0,
        "longest_losing_streak": worst,
        "exits_by_reason": by_reason,
        "break_even_move_pct": round(cfg.break_even_move_pct(), 4),
    }


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CycleState",
    "ExitReason",
    "config_for_cycle",
    "committed_margin",
    "cycle_outcome",
    "fees_for",
    "now_utc",
    "open_from_signal",
    "recompute_liquidation",
    "resolve_at_price",
    "should_open",
    "summarise",
]
