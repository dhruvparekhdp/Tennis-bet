"""
Leveraged futures position maths for the paper-trading simulator.

Pure functions and dataclasses only — no I/O, no global state — so the exact
same code runs in a historical backtest and in a live paper-trading cycle.
That equivalence is the point: a backtest result you cannot reproduce live is
worthless.

Conventions
-----------
* Fees are charged on NOTIONAL (margin x leverage), not on margin. This is the
  detail that makes leverage dangerous: at 10x, a 0.075% taker fee costs 0.75%
  of your margin per side, 1.5% for the round trip.
* Liquidation is modelled exactly rather than with the usual "1/leverage"
  approximation, because notional shrinks as a long moves against you.
* Where a single candle contains both the stop and the target we assume the
  STOP filled first. We cannot know the path within a candle, and a simulator
  that resolves ambiguity in its own favour is worse than no simulator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    LIQUIDATION = "liquidation"
    EXPIRY = "expiry"
    CYCLE_END = "cycle_end"


@dataclass(frozen=True)
class FeeModel:
    """
    CoinDCX INR futures costs, calibrated against real account transactions
    rather than published rate cards.

    Reconciled 18 Aug 2026 against a live ETH/USDT position:
      * open fee Rs6.31 on a Rs10,681.44 position  -> 0.0591% effective
        which is 0.05% base x 1.18 GST. The 18% GST is charged on the
        brokerage and is NOT optional, so the effective rate is what matters.
      * liquidation at 1820.97 from entry 1906.50 at 20x (-4.49%) implies a
        maintenance margin near 0.53%, not the 1.5% quoted for larger tiers.
      * funding was Rs0.23 / Rs0.50 / Rs0.70 across three 8-hourly windows,
        roughly 0.0066% of notional per window.
    """

    taker_pct: float = 0.0005         # 0.05% base brokerage
    maker_pct: float = 0.0005         # INR futures charges the same both ways
    gst_pct: float = 0.18             # 18% GST on the brokerage, unavoidable
    maintenance_margin_pct: float = 0.0053   # measured, not the quoted 1.5%

    # Perpetual futures pay/charge funding every 8 hours while a position is
    # open. Ignoring it understates the cost of anything held for hours, which
    # is most of what this system does. Positive = longs pay shorts.
    funding_rate_per_8h: float = 0.0000655

    # Strictly, liquidating at the liquidation price leaves the maintenance margin
    # behind. In practice CoinDCX charges a liquidation clearance fee, and a fast
    # market fills you worse than the trigger price, so the residual usually
    # disappears. Default to the conservative assumption that a liquidation costs
    # the whole margin; set False to keep the exact residual.
    liquidation_consumes_margin: bool = True

    @property
    def effective_taker_pct(self) -> float:
        """What actually leaves the wallet, GST included."""
        return self.taker_pct * (1 + self.gst_pct)

    def entry_fee(self, notional: float) -> float:
        return notional * self.effective_taker_pct

    def exit_fee(self, notional: float) -> float:
        return notional * self.effective_taker_pct

    def funding_cost(self, notional: float, hours_held: float) -> float:
        """
        Funding paid over the life of a position, charged on notional.

        Modelled as a cost in both directions: the rate flips sign with market
        positioning, so assuming you always receive it would flatter results.
        """
        periods = max(0.0, hours_held) / 8.0
        return notional * self.funding_rate_per_8h * periods

    def round_trip_pct(self) -> float:
        """Price move required just to break even, as a fraction (leverage-independent)."""
        return 2 * self.effective_taker_pct


def liquidation_price(entry: float, side: Side, leverage: float, mm_pct: float) -> float:
    """
    Exact liquidation price — where equity falls to the maintenance requirement.

    LONG:  entry * (1 - 1/L) / (1 - mm)
    SHORT: entry * (1 + 1/L) / (1 + mm)

    Derived from  margin + (P - entry)*qty == P*qty*mm  (and its short mirror),
    so it accounts for the position's own notional changing with price. The
    common `entry * (1 - 1/L)` shortcut is slightly pessimistic for longs.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    inv = 1.0 / leverage
    if side is Side.LONG:
        return entry * (1.0 - inv) / (1.0 - mm_pct)
    return entry * (1.0 + inv) / (1.0 + mm_pct)


def stop_and_target(
    entry: float,
    side: Side,
    leverage: float,
    stop_pct_of_margin: float,
    reward_risk: float,
) -> tuple[float, float]:
    """
    Convert a risk budget expressed in margin terms into actual prices.

    stop_pct_of_margin=0.20 at 10x means "risk 20% of margin", which is a
    20%/10 = 2.0% adverse price move. The target is placed reward_risk times
    that distance away, which is the ratio that decides whether the strategy
    can survive fees at all.
    """
    stop_move = stop_pct_of_margin / leverage
    target_move = stop_move * reward_risk
    if side is Side.LONG:
        return entry * (1.0 - stop_move), entry * (1.0 + target_move)
    return entry * (1.0 + stop_move), entry * (1.0 - target_move)


@dataclass
class Position:
    symbol: str
    side: Side
    entry_price: float
    margin: float
    leverage: float
    stop_price: float
    target_price: float
    liq_price: float
    opened_at: datetime
    entry_fee: float
    signal_type: str = ""
    timeframe: str = ""
    confidence: float = 0.0
    expires_at: datetime | None = None

    @property
    def notional(self) -> float:
        return self.margin * self.leverage

    @property
    def quantity(self) -> float:
        return self.notional / self.entry_price

    def gross_pnl(self, exit_price: float) -> float:
        if self.side is Side.LONG:
            return (exit_price - self.entry_price) * self.quantity
        return (self.entry_price - exit_price) * self.quantity

    def unrealised(self, mark: float, fees: FeeModel) -> float:
        """Net P&L if closed right now, including the exit fee not yet paid."""
        return self.gross_pnl(mark) - fees.exit_fee(mark * self.quantity)


@dataclass
class ClosedTrade:
    position: Position
    exit_price: float
    closed_at: datetime
    reason: ExitReason
    gross_pnl: float
    fees_paid: float          # trading fees + funding, all-in
    net_pnl: float
    wallet_after: float
    funding_paid: float = 0.0
    hours_held: float = 0.0

    @property
    def return_on_margin(self) -> float:
        return self.net_pnl / self.position.margin if self.position.margin else 0.0

    @property
    def won(self) -> bool:
        return self.net_pnl > 0


def open_position(
    symbol: str,
    side: Side,
    entry_price: float,
    margin: float,
    leverage: float,
    fees: FeeModel,
    stop_pct_of_margin: float,
    reward_risk: float,
    opened_at: datetime,
    signal_type: str = "",
    timeframe: str = "",
    confidence: float = 0.0,
    expires_at: datetime | None = None,
) -> Position:
    stop, target = stop_and_target(entry_price, side, leverage, stop_pct_of_margin, reward_risk)
    notional = margin * leverage
    return Position(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        margin=margin,
        leverage=leverage,
        stop_price=stop,
        target_price=target,
        liq_price=liquidation_price(entry_price, side, leverage, fees.maintenance_margin_pct),
        opened_at=opened_at,
        entry_fee=fees.entry_fee(notional),
        signal_type=signal_type,
        timeframe=timeframe,
        confidence=confidence,
        expires_at=expires_at,
    )


def resolve_candle(
    pos: Position,
    high: float,
    low: float,
    close: float,
    ts: datetime,
) -> tuple[ExitReason, float] | None:
    """
    Decide whether a candle closes this position, and at what price.

    Priority is deliberate and pessimistic:
      1. Liquidation — a hard exchange action that overrides any of our orders.
      2. Stop — if both stop and target are inside the candle's range we cannot
         know which was touched first, so we book the loss.
      3. Target.
      4. Time expiry, filled at the close.

    Returns None if the position survives the candle.
    """
    if pos.side is Side.LONG:
        if low <= pos.liq_price:
            return ExitReason.LIQUIDATION, pos.liq_price
        if low <= pos.stop_price:
            return ExitReason.STOP, pos.stop_price
        if high >= pos.target_price:
            return ExitReason.TARGET, pos.target_price
    else:
        if high >= pos.liq_price:
            return ExitReason.LIQUIDATION, pos.liq_price
        if high >= pos.stop_price:
            return ExitReason.STOP, pos.stop_price
        if low <= pos.target_price:
            return ExitReason.TARGET, pos.target_price

    if pos.expires_at is not None and ts >= pos.expires_at:
        return ExitReason.EXPIRY, close
    return None


def close_position(
    pos: Position,
    exit_price: float,
    reason: ExitReason,
    closed_at: datetime,
    fees: FeeModel,
    wallet_before: float,
) -> ClosedTrade:
    """
    Settle a position and return the wallet impact.

    The margin was already deducted from the wallet when the position opened,
    so settlement returns margin + net P&L. A liquidation is floored at losing
    the entire margin — you cannot lose more than you posted.
    """
    gross = pos.gross_pnl(exit_price)
    exit_fee = fees.exit_fee(exit_price * pos.quantity)
    hours_held = max(0.0, (closed_at - pos.opened_at).total_seconds() / 3600.0)
    funding = fees.funding_cost(pos.notional, hours_held)
    total_fees = pos.entry_fee + exit_fee + funding
    net = gross - total_fees

    if reason is ExitReason.LIQUIDATION and fees.liquidation_consumes_margin:
        # Clearance fee + slippage past the trigger price: assume nothing comes back.
        net = -pos.margin

    if net < -pos.margin:            # you can never lose more than you posted
        net = -pos.margin

    return ClosedTrade(
        position=pos,
        exit_price=exit_price,
        closed_at=closed_at,
        reason=reason,
        gross_pnl=gross,
        fees_paid=total_fees,
        funding_paid=funding,
        hours_held=hours_held,
        net_pnl=net,
        wallet_after=wallet_before + pos.margin + net,
    )


@dataclass
class CycleConfig:
    """One run of the simulator, start to finish."""

    starting_wallet: float = 1000.0
    target_wallet: float = 20000.0
    leverage: float = 10.0
    margin_per_trade_pct: float = 0.20      # of current wallet
    min_margin: float = 50.0
    stop_pct_of_margin: float = 0.20        # risk 20% of margin per trade
    reward_risk: float = 2.0                # target sits 2x the stop distance away
    min_confidence: float = 0.70
    max_concurrent: int = 3
    max_hold_minutes: int = 240
    fees: FeeModel = field(default_factory=FeeModel)

    def margin_for(self, wallet: float) -> float:
        return max(self.min_margin, wallet * self.margin_per_trade_pct)

    def break_even_move_pct(self) -> float:
        return self.fees.round_trip_pct() * 100.0

    def stop_move_pct(self) -> float:
        return self.stop_pct_of_margin / self.leverage * 100.0

    def target_move_pct(self) -> float:
        return self.stop_move_pct() * self.reward_risk

    def liquidation_move_pct(self) -> float:
        """Approximate adverse move that triggers liquidation, in percent."""
        return (1.0 / self.leverage - self.fees.maintenance_margin_pct) * 100.0

    def sanity_report(self) -> dict:
        """Is this configuration even capable of making money? Checked before a run."""
        be = self.break_even_move_pct()
        tgt = self.target_move_pct()
        return {
            "break_even_move_pct": round(be, 4),
            "target_move_pct": round(tgt, 4),
            "stop_move_pct": round(self.stop_move_pct(), 4),
            "liquidation_move_pct": round(self.liquidation_move_pct(), 4),
            "target_clears_fees": tgt > be,
            "target_to_fee_ratio": round(tgt / be, 2) if be else None,
            "stop_inside_liquidation": self.stop_move_pct() < self.liquidation_move_pct(),
        }
