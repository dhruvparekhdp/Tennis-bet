"""
Level policy for short-hold leveraged futures — the "scalping" frame.

Why this module exists
----------------------
The analyzers used to set levels as `price +/- atr * k`. On the live dashboard
that produced XRP signals with a target 0.050% away, against a round-trip cost
of 0.118%: trades that lose money *when they win*. Two faults compounded.

1. ATR collapse. The REST poller writes candles with open==high==low==close,
   so the true range is zero and ATR decays toward zero with it. Any multiple
   of a near-zero number is still near zero.
2. Fixed 4-decimal rounding. At XRP's ~$1.00 that is a 0.01% tick, so a
   0.05% target is five ticks wide and rounding error is a fifth of the edge.
   At BTC's ~$62,000 the same rule is absurdly fine. One hardcoded precision
   cannot serve both.

The deeper fault is that neither analyzer ever asked the only question that
matters for a short hold: *is this move big enough to be worth paying for?*
A scalp's edge is the move minus the cost, and the cost is fixed. So cost is
the floor that every level must clear, and volatility decides whether there is
a trade at all — not the other way round.

Everything here is a pure function of price, volatility and cost. No I/O, no
state, so the live path and the backtest can share it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum

from analysis.instruments import spec_for, tick_for


class NoTrade(StrEnum):
    """Why a setup was refused. Shown to the user instead of a silent drop."""

    TOO_QUIET = "too_quiet"            # ATR below the cost floor — nothing to win
    TARGET_TOO_SMALL = "target_small"  # move cannot cover the round trip
    TICK_TOO_COARSE = "tick_coarse"    # rounding error is a large share of the edge
    POOR_REWARD = "poor_reward"        # reward-to-risk below the floor
    WALL_IN_THE_WAY = "wall"           # resting size sits between entry and target
    STOP_INSIDE_NOISE = "stop_noise"   # stop sits inside one bar's ordinary range
    FUNDING_WINDOW = "funding_window"  # settlement too close to open a short hold


REASON_TEXT = {
    NoTrade.TOO_QUIET: "market too quiet — the swing is smaller than the cost of trading it",
    NoTrade.TARGET_TOO_SMALL: "target does not clear the round-trip cost",
    NoTrade.TICK_TOO_COARSE: "price steps are too coarse for a move this small",
    NoTrade.POOR_REWARD: "risking more than the trade can win",
    NoTrade.WALL_IN_THE_WAY: "a large resting order sits between entry and target",
    NoTrade.STOP_INSIDE_NOISE: "stop sits inside one bar's normal range — noise would take it out",
    NoTrade.FUNDING_WINDOW: "funding settles too soon for a short hold",
}


def tick_for_price(price: float, symbol: str = "") -> float:
    """
    Smallest price step for an instrument trading near `price`.

    Prefers the measured tick from the instrument table and falls back to a
    magnitude rule. The fallback alone was wrong by 10x for both ETH and XAU,
    which quote to 0.01 where the rule says 0.1 — coarse enough to refuse
    setups that are perfectly tradeable.
    """
    return tick_for(symbol, price)


# Float noise, in units of ticks. 62000 * (1 - 0.0056) evaluates to
# 61652.799999999996, which is 616527.9999999999 ticks — a bare floor() then
# moves the level a WHOLE tick, widening the stop and pushing reward-to-risk
# under 1.0. That silently refused signals until it was traced.
_TICK_EPSILON = 1e-6


def round_to_tick(price: float, tick: float, *, up: bool) -> float:
    """
    Snap to a tick, always away from entry so a level never lands inside it.

    Values already on the grid stay put: a level that is within float noise of
    an exact tick is treated as being on it, rather than shunted a full tick in
    whichever direction the error happened to point.
    """
    if tick <= 0:
        return price
    steps = price / tick
    nearest = round(steps)
    if abs(steps - nearest) < _TICK_EPSILON:
        return nearest * tick
    return (math.ceil(steps) if up else math.floor(steps)) * tick


@dataclass(frozen=True)
class ScalpConfig:
    """
    The cost frame a short hold has to beat.

    Defaults are CoinDCX INR futures as measured from a real ledger, not the
    published rate card. Every figure is a fraction of notional.
    """

    round_trip_fee_pct: float = 0.00118   # 0.05% x 1.18 GST, both sides
    spread_pct: float = 0.00020           # crossed on entry and on exit
    slippage_buffer_pct: float = 0.00030  # what fills actually cost beyond the spread

    # A target merely equal to cost is a coin flip you pay to enter. This is
    # how much of the move must survive as profit, and it follows from
    # kept = 1 - 1/x: at 2.0 a trade keeps half its gross, at 3.0 two thirds,
    # at 5.0 four fifths.
    #
    # Set to 3.0 on the ledger's evidence. A real 0.177% ETH move — exactly
    # 1.5x the round trip — grossed Rs57.53, paid Rs38.24 in fees and kept
    # Rs19.29. Keeping a third of what a trade earns is not a trade worth
    # taking. The paper engine reads this same value, so there is one floor
    # rather than a band where a signal is published and then refused.
    min_edge_multiple: float = 3.0

    # Volatility gate. If the recent swing cannot cover the cost floor, there
    # is no trade here at any confidence — this is the check that would have
    # refused every signal in the XRP screenshot.
    atr_floor_multiple: float = 1.0

    # Rounding must be small relative to the edge, or the tick eats the profit.
    max_tick_share_of_target: float = 0.10

    # What the setup AIMS for, as a multiple of the initial stop distance.
    #
    # Every signal this system has ever fired used 1.00 — target and stop the
    # same distance from entry. That makes the outcome a coin flip that pays a
    # toll: break-even needs p = 0.5 + cost/(2*move), which on a 0.83% ETH move
    # is 60.1%. Measured over 170 resolved signals the hit rate was 50.6%, so
    # the design was losing by construction, not by bad luck.
    #
    # Raising this does NOT create edge. What it does is lower how much edge is
    # needed, because the round trip is paid once whatever the target:
    #
    #     R      break-even p   random-walk p   real edge required
    #     1.00       60.1%          50.0%          +10.1 pts
    #     1.50       48.1%          40.0%           +8.1
    #     2.00       40.1%          33.3%           +6.7
    #     3.00       30.0%          25.0%           +5.0
    #
    # The cost of 2.00 is a stop half as far away, which ordinary noise reaches
    # more often — min_stop_atr_multiple below is what stops that becoming
    # absurd.
    target_reward_risk: float = 2.0

    # A stop must sit outside one bar's ordinary range. Deriving the stop as
    # target/R means raising R tightens the stop, and a stop inside the noise
    # is not a risk control — it is a way of paying the round trip to be shaken
    # out of trades that were never wrong.
    min_stop_atr_multiple: float = 1.0

    min_reward_risk: float = 1.0
    max_hold_minutes: int = 30
    funding_blackout_minutes: int = 15
    max_signal_age_seconds: int = 90

    @classmethod
    def high_conviction(cls) -> ScalpConfig:
        """
        Fewer trades, each with room to actually pay.

        Calibrated on the closed trades from the account. What separates a good
        one from a poor one is not the win — all of them won — but how much of
        the gross survived the round trip, and that is pure arithmetic:

            kept = 1 - 1 / (move / round_trip_cost)

        Observed, and matching the formula to the percentage point:

            34.0x cost -> kept 97%     0.803% gold move
            15.9x      -> kept 94%     1.878% ETH move
             5.4x      -> kept 82%     0.637% ETH move
             1.5x      -> kept 34%     0.177% ETH move, Rs38 of fees on Rs58

        The default 2.0 multiple only asks a trade to keep half its gross. At
        5.0 it keeps 80%, which is where the account's good trades sat and
        below which the fee starts owning the outcome.
        """
        return cls(min_edge_multiple=5.0, min_reward_risk=1.0)

    def with_measured_execution(self, execution_pct: float) -> ScalpConfig:
        """
        Replace the assumed spread and slippage with a reading from the book.

        Two thirds of the cost floor were guesses: a flat 0.020% spread and a
        flat 0.030% slippage, the same for every market at every size. Those
        two numbers decide whether a setup is taken, so guessing them is
        guessing the answer.

        The measurement is folded into `spread_pct` and the buffer zeroed,
        because walking both sides of the book already includes the spread —
        keeping the old buffer on top would double-count it.
        """
        return replace(self, spread_pct=max(0.0, execution_pct),
                       slippage_buffer_pct=0.0)

    def for_symbol(self, symbol: str) -> ScalpConfig:
        """
        Re-cost this frame for one market.

        Gold's brokerage is a fifth of ether's, which moves the minimum viable
        target from 0.336% to 0.071%. Holding every market to the crypto floor
        would refuse gold scalps that are comfortably profitable.
        """
        return replace(self, round_trip_fee_pct=spec_for(symbol).round_trip_pct)

    @property
    def cost_floor_pct(self) -> float:
        """Everything a round trip costs before the market moves at all."""
        return self.round_trip_fee_pct + self.spread_pct + self.slippage_buffer_pct

    @property
    def min_target_pct(self) -> float:
        """The smallest target worth executing."""
        return self.cost_floor_pct * self.min_edge_multiple

    def reachable_move_pct(self, atr_pct: float, horizon_minutes: float,
                           bar_minutes: float = 1.0) -> float:
        """
        How far this instrument is expected to travel over `horizon_minutes`.

        The gate used to hold a ONE-MINUTE ATR against a per-trade cost, which
        are different units, and the mismatch quietly reduced the watchlist to
        a single coin. Measured on the live board: BCH at +21% a day has a
        1-minute ATR near 0.19% and cleared the floor, while BTC at +7.6% sits
        near 0.07% and ETH near 0.035%. Neither can move 0.5% in a minute —
        but nobody was asking them to, since positions are held for hours.

        Volatility grows with the square root of time under a random walk, so
        a window of N bars expects sqrt(N) times the per-bar range. Real series
        trend slightly, so the realised move over a window tends to be larger
        than this, never smaller — the estimate errs toward refusing trades.
        """
        if atr_pct <= 0 or horizon_minutes <= 0 or bar_minutes <= 0:
            return 0.0
        return atr_pct * math.sqrt(max(1.0, horizon_minutes / bar_minutes))

    def roe_target_is_viable(self, roe_target: float, leverage: float) -> bool:
        """Does a fixed ROE target still ask for a big enough price move?"""
        return roe_to_price_move(roe_target, leverage) >= self.min_target_pct

    def max_leverage_for(self, roe_target: float) -> float:
        """The leverage ceiling at which this ROE target stays worth taking."""
        return max_leverage_for_roe_target(roe_target, self.min_target_pct)


def roe_to_price_move(roe_target: float, leverage: float) -> float:
    """
    What a fixed return-on-margin target actually asks of price.

    move = roe / leverage. This is the trap in setting every trade to the same
    percentage: the target is denominated in MARGIN, but the cost of trading
    is denominated in PRICE. Raise leverage and the same 20% asks for a
    smaller and smaller price move, until it asks for less than the round trip
    costs — at which point the trade loses money at the moment it succeeds.

        20% ROE at  10x -> 2.000% price move
        20% ROE at  25x -> 0.800%
        20% ROE at  50x -> 0.400%
        20% ROE at 100x -> 0.200%   below ether's 0.336% floor

    Observed: three ETH trades captured 1.878%, 0.637% and 0.177%. The last
    one paid Rs38.24 in fees to keep Rs19.29 — a 20% target that had shrunk
    beneath its own costs.
    """
    if leverage <= 0:
        return 0.0
    return roe_target / leverage


def max_leverage_for_roe_target(roe_target: float, min_move: float) -> float:
    """
    Highest leverage at which a fixed ROE target still clears the cost floor.

    Above this the target is unreachable in the only sense that matters: it
    can be hit and still lose money.
    """
    if min_move <= 0:
        return float("inf")
    return roe_target / min_move


@dataclass(frozen=True)
class TrailPlan:
    """
    What to do with the stop once the trade is working, in prices.

    A reward:risk above 1 and a trailing stop are one decision, not two. On
    their own, a 2R target caps the winner that pays for the losers; a trail
    with a 1R target never arms, because the position closes at the target
    first. Together the stop cuts at 1R, the trail takes over at 0.75R, and
    nothing caps the upside.

    Expressed as prices rather than percentages because the venue's TP/SL box
    takes prices, and this has to be usable by hand.
    """

    risk: float               # 1R, the initial stop distance in price
    arm_price: float          # move the stop the first time price reaches here
    breakeven_stop: float     # where it goes then: entry plus the round trip
    trail_distance: float     # afterwards, ride this far behind the best price
    step: float               # ignore moves smaller than this

    def stop_at(self, best_price: float, is_long: bool) -> float:
        """Where the stop sits once the trail has armed and price reached `best`."""
        sign = 1.0 if is_long else -1.0
        candidate = best_price - sign * self.trail_distance
        return max(candidate, self.breakeven_stop) if is_long \
            else min(candidate, self.breakeven_stop)


def trailing_plan(entry: float, stop: float, cfg: ScalpConfig,
                  activate_at_r: float = 0.75, trail_r: float = 1.0,
                  step_r: float = 0.10) -> TrailPlan | None:
    """
    Derive the trail from the levels the signal already carries.

    Distances are multiples of the initial stop, never percentages of margin.
    A margin-denominated trail is a function of the leverage dial rather than
    of the setup: at 10x a 20%-of-margin trail is 2.0% of price, which against
    an ATR-derived stop of 0.43% sits nearly five times further out than the
    original stop and can never ratchet anything.
    """
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0:
        return None
    sign = 1.0 if stop < entry else -1.0
    return TrailPlan(
        risk=risk,
        arm_price=entry + sign * activate_at_r * risk,
        # The full round trip, not brokerage alone. A "breakeven" stop that
        # only covers the fee still books a loss once the spread and the fill
        # are paid, which is the quietest way to lose money on a trade that
        # was supposed to be a scratch.
        breakeven_stop=entry * (1 + sign * cfg.cost_floor_pct),
        trail_distance=trail_r * risk,
        step=step_r * risk,
    )


# Horizons offered to a setup, shortest first. A move reachable in the short
# window is a stronger setup than one needing the long one, so the label
# records which window the signal qualified under and both can be compared
# live.
#
# The short window was ten minutes and is now fifteen, because ten produced
# a lot of wrong calls. The fault was not the setups but the deadline. Nothing
# about the entry changes between the two: the target is set by whichever is
# larger, the projected move or the cost floor, and for every symbol except
# the most volatile the cost floor binds — so at ten minutes and at fifteen
# the SAME target was being asked for, with half again less time to reach it.
# A signal that was right about direction still resolved as a loss, because
# the stop or the clock arrived before the move did.
#
# Where the projection does bind, fifteen bars also asks for more: sqrt(15) is
# 3.87 against sqrt(10)'s 3.16, so BCH's target grows from 0.605% to 0.738%
# and its edge over the round trip from 3.6x to 4.4x.
#
# Note the direction of the volatility gate, which is easy to get backwards:
# a longer window projects further, so it admits QUIETER markets, not fewer.
# Ten minutes needed a 0.053% one-minute ATR to clear the floor, fifteen needs
# 0.043%, thirty needs 0.031%. Fifteen is not a stricter filter — it is the
# same filter with a realistic deadline attached.
HORIZONS_MINUTES: tuple[int, ...] = (15, 30)


@dataclass(frozen=True)
class ScalpLevels:
    entry: float
    target: float
    stop: float
    tick: float
    target_pct: float
    stop_pct: float
    cost_pct: float
    horizon_minutes: float = 30.0

    @property
    def reward_risk(self) -> float:
        return self.target_pct / self.stop_pct if self.stop_pct else 0.0

    @property
    def edge_after_costs_pct(self) -> float:
        """What is actually left if the target is hit. The only number that pays."""
        return self.target_pct - self.cost_pct


def scalp_levels(
    entry: float,
    is_long: bool,
    atr_pct: float,
    cfg: ScalpConfig,
    reward_risk: float | None = None,
    atr_target_multiple: float = 1.0,
    minutes_to_funding: float | None = None,
    symbol: str = "",
    horizon_minutes: float = 30.0,
    bar_minutes: float = 1.0,
) -> ScalpLevels | NoTrade:
    """
    Build target and stop for a short hold, or explain why there is no trade.

    `atr_pct` is ATR as a fraction of price. The target is the larger of what
    volatility offers and what cost demands, so a quiet market cannot produce a
    tiny target — it produces no trade. That inversion is the whole point: the
    old code let volatility set the target and never consulted cost.
    """
    if entry <= 0 or atr_pct <= 0:
        return NoTrade.TOO_QUIET
    if minutes_to_funding is not None and minutes_to_funding < cfg.funding_blackout_minutes:
        return NoTrade.FUNDING_WINDOW
    # Like for like: what the instrument can travel in the time we hold it,
    # against what a round trip costs.
    reachable = cfg.reachable_move_pct(atr_pct, horizon_minutes, bar_minutes)
    if reachable < cfg.cost_floor_pct * cfg.atr_floor_multiple:
        return NoTrade.TOO_QUIET

    # None means "whatever this cost frame aims for", so the ratio lives in one
    # place instead of being repeated as a literal in five analyzers.
    rr = cfg.target_reward_risk if reward_risk is None else reward_risk

    target_pct = max(reachable * atr_target_multiple, cfg.min_target_pct)
    stop_pct = target_pct / rr if rr > 0 else target_pct

    # The stop is derived from the target, so a higher R pulls it closer to
    # entry. Past a point it lands inside the range a single bar covers, where
    # it is taken out by noise rather than by being wrong.
    if stop_pct < atr_pct * cfg.min_stop_atr_multiple:
        return NoTrade.STOP_INSIDE_NOISE

    tick = tick_for_price(entry, symbol)
    if tick > 0 and tick / entry > target_pct * cfg.max_tick_share_of_target:
        return NoTrade.TICK_TOO_COARSE

    sign = 1.0 if is_long else -1.0
    # Round each level away from entry, so snapping can only ever make the
    # target harder and the stop wider — never flatter the setup.
    target = round_to_tick(entry * (1 + sign * target_pct), tick, up=is_long)
    stop = round_to_tick(entry * (1 - sign * stop_pct), tick, up=not is_long)

    actual_target_pct = abs(target - entry) / entry
    actual_stop_pct = abs(stop - entry) / entry
    if actual_target_pct < cfg.min_target_pct:
        return NoTrade.TARGET_TOO_SMALL
    if actual_stop_pct <= 0:
        return NoTrade.TICK_TOO_COARSE

    # Rounding both levels away from entry can widen the stop by more than it
    # widens the target, leaving reward-to-risk a fraction under the floor —
    # a shortfall smaller than one tick, which no price can express. Rather
    # than refuse a setup for being un-representable, extend the target by
    # whole ticks until it clears. Capped, so a genuinely poor setup is still
    # refused rather than stretched into looking acceptable.
    for _ in range(3):
        if actual_stop_pct <= 0:
            break
        if actual_target_pct / actual_stop_pct >= cfg.min_reward_risk - 1e-9:
            break
        target = target + (tick if is_long else -tick)
        actual_target_pct = abs(target - entry) / entry
    if actual_target_pct / actual_stop_pct < cfg.min_reward_risk - 1e-9:
        return NoTrade.POOR_REWARD

    return ScalpLevels(
        entry=entry, target=target, stop=stop, tick=tick,
        target_pct=actual_target_pct, stop_pct=actual_stop_pct,
        cost_pct=cfg.cost_floor_pct, horizon_minutes=horizon_minutes,
    )
