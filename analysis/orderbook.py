"""
What the resting orders say, as distinct from what the price did.

Why this module exists
----------------------
Every other input in this system is a transformation of past prices: RSI, ATR,
pivots, patterns, even volume. They describe what already happened. The order
book is the one source here that describes what other people have committed to
do NEXT, and it answers two questions nothing else can.

*What does this trade actually cost?* The cost floor is 0.168% and two thirds
of it were guesses — a flat 0.020% spread and a flat 0.030% slippage buffer,
identical for every market at every size. Those two numbers decide whether a
setup is taken, so guessing them is guessing the answer. Walking the book gives
the real figure for the real size.

*Will the target be reached?* A target 0.85% away with 400 ETH resting 0.3%
above is not a 0.85% target. Price stops at the wall, the horizon expires, and
the trade books an expiry that looks like bad luck and was arithmetic.

Deliberately NOT a sixth voting family. The confluence gate asks for three of
five families to agree; adding a sixth would make three-of-six a materially
easier bar and quietly increase the signal count, which is the opposite of what
this system needs. Depth enters as a measurement of cost and as a veto — it can
refuse a setup and it can reprice one, but it never elects a direction.

Pure functions over plain (price, size) pairs. No I/O, so the live path, the
backtest and the tests all see the same arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass

# A level is (price, size). Bids descend from best, asks ascend from best —
# the order every venue returns them in, and the order the walk depends on.
Level = tuple[float, float]


@dataclass(frozen=True)
class OrderBook:
    """One snapshot of resting size on both sides."""

    symbol: str
    bids: list[Level]
    asks: list[Level]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return self.best_bid or self.best_ask
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def is_usable(self) -> bool:
        """
        Enough of a book to measure. A crossed or empty book is a bad snapshot,
        not a tight market, and treating it as one would report a negative
        spread and a free trade.
        """
        return bool(self.bids and self.asks and self.best_ask > self.best_bid)

    @property
    def spread_pct(self) -> float:
        """The real half-spread cost of crossing, as a fraction of mid."""
        if not self.is_usable or self.mid <= 0:
            return 0.0
        return (self.best_ask - self.best_bid) / self.mid


def walk_book(levels: list[Level], quantity: float) -> tuple[float, float]:
    """
    Average fill price for `quantity`, and how much of it the book can fill.

    Returns (average_price, filled_quantity). A partial fill is reported rather
    than extrapolated: pretending the book continues past its last level is how
    a size that cannot be traded comes back with a comfortable cost estimate.
    """
    if quantity <= 0 or not levels:
        return 0.0, 0.0
    spent = filled = 0.0
    for price, size in levels:
        if price <= 0 or size <= 0:
            continue
        take = min(size, quantity - filled)
        spent += take * price
        filled += take
        if filled >= quantity:
            break
    return (spent / filled if filled > 0 else 0.0), filled


def slippage_pct(book: OrderBook, is_buy: bool, quantity: float) -> float | None:
    """
    How far the average fill lands from mid, as a fraction. Always signed
    against the trader.

    None when the book cannot fill the size — a distinct answer from "it costs
    nothing", and the one that should stop a trade rather than price it.
    """
    if not book.is_usable or quantity <= 0:
        return None
    mid = book.mid
    if mid <= 0:
        return None
    levels = book.asks if is_buy else book.bids
    avg, filled = walk_book(levels, quantity)
    if filled < quantity or avg <= 0:
        return None
    return (avg - mid) / mid if is_buy else (mid - avg) / mid


def round_trip_execution_pct(book: OrderBook, quantity: float) -> float | None:
    """
    Measured spread-and-slippage for opening AND closing this size.

    This is the pair of guesses in ScalpConfig — spread_pct plus
    slippage_buffer_pct — replaced by a reading. Both sides are walked because
    a book that is deep on the bid and thin on the ask costs differently to
    enter than to leave, and a scalp pays both.
    """
    buy = slippage_pct(book, True, quantity)
    sell = slippage_pct(book, False, quantity)
    if buy is None or sell is None:
        return None
    return buy + sell


def depth_within(book: OrderBook, pct: float, is_bid: bool) -> float:
    """Total resting size within `pct` of mid on one side."""
    if not book.is_usable or pct <= 0:
        return 0.0
    mid = book.mid
    levels = book.bids if is_bid else book.asks
    limit = mid * (1 - pct) if is_bid else mid * (1 + pct)
    return sum(size for price, size in levels
               if (price >= limit if is_bid else price <= limit))


def imbalance(book: OrderBook, pct: float = 0.002) -> float | None:
    """
    Resting bid size against ask size near the touch, as −1..+1.

    Positive means more size is waiting to buy than to sell. Reported for the
    record and for confidence, never as a vote: depth is visible and therefore
    gameable, and a book can be stacked precisely because people read it.
    """
    if not book.is_usable:
        return None
    bid = depth_within(book, pct, True)
    ask = depth_within(book, pct, False)
    total = bid + ask
    if total <= 0:
        return None
    return (bid - ask) / total


def wall_before(book: OrderBook, entry: float, target: float,
                min_multiple: float = 3.0, quantity: float = 0.0) -> float | None:
    """
    The price of a resting block big enough to stop the move, or None.

    "Big enough" is measured against the BOOK, not against the trade. Sizing
    the threshold off the order was the first attempt and it is wrong in the
    direction that matters: at 0.074 ETH every resting level is more than three
    times the trade, so the best ask came back as a wall and every setup would
    have been vetoed. What stops a move is a level that is large compared with
    the levels around it — several times the median.

    The trade size still enters, as a floor: a level smaller than the order
    cannot stop it, it just gets consumed on the way past.

    Only levels strictly between entry and target count. A wall AT the target
    is why the target does not fill; a wall beyond it is irrelevant.
    """
    if not book.is_usable or entry <= 0 or target <= 0 or entry == target:
        return None
    is_long = target > entry
    levels = book.asks if is_long else book.bids
    inside = [(p, s) for p, s in levels
              if (entry < p < target if is_long else target < p < entry)]
    if not inside:
        return None

    sizes = sorted(s for _, s in levels if s > 0)
    if not sizes:
        return None
    threshold = sizes[len(sizes) // 2] * min_multiple

    big = [p for p, s in inside if s >= threshold and s >= quantity]
    if not big:
        return None
    # The nearest one is the one price meets first.
    return min(big) if is_long else max(big)
