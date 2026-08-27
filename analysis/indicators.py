"""
Technical indicator library — pure functions over OHLCV series.

Everything here takes plain lists and returns plain numbers, so the live path,
the backtest and the tests all call the identical code. No state, no I/O.

Conventions
-----------
* Series are oldest-first. The last element is the most recent bar.
* Every function returns None when there is not enough history, rather than a
  neutral-looking default. A 50.0 RSI from four bars of data is indistinguishable
  from a real 50.0, and that ambiguity is how a warm-up period turns into a
  signal.
* Smoothing follows Wilder where Wilder defined the indicator (RSI, ATR, ADX),
  because that is what charting packages draw and what the published thresholds
  refer to.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ── basics ────────────────────────────────────────────────────────────────

def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    """Seeded with an SMA, then smoothed — the standard construction."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = (v - out) * k + out
    return out


def ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA history — needed by indicators that smooth a smoothed series."""
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def wilder(values: list[float], period: int) -> list[float]:
    """Wilder's smoothing: an EMA with k = 1/period, seeded on the mean."""
    if period <= 0 or len(values) < period:
        return []
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(out[-1] + (v - out[-1]) / period)
    return out


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


# ── momentum ──────────────────────────────────────────────────────────────

def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI. Needs period+1 closes to produce its first value."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = wilder(gains, period), wilder(losses, period)
    if not ag or not al:
        return None
    if al[-1] == 0:
        return 100.0 if ag[-1] > 0 else 50.0
    rs = ag[-1] / al[-1]
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """RSI at every bar — required for divergence, which compares two points."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = wilder(gains, period), wilder(losses, period)
    out = []
    for g, loss in zip(ag, al):
        if loss == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + g / loss))
    return out


def stochastic(highs: list[float], lows: list[float], closes: list[float],
               period: int = 14, smooth: int = 3) -> tuple[float, float] | None:
    """Returns (%K, %D). Where RSI measures speed, this measures position in range."""
    if len(closes) < period + smooth:
        return None
    ks = []
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        ks.append(50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0)
    if len(ks) < smooth:
        return None
    return ks[-1], sum(ks[-smooth:]) / smooth


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[float, float, float] | None:
    """
    Returns (line, signal, histogram).

    The histogram is the part worth acting on: it turns before the lines cross,
    and a cross that has already happened is old news on a short hold.
    """
    if len(closes) < slow + signal:
        return None
    fast_s, slow_s = ema_series(closes, fast), ema_series(closes, slow)
    # Align: the slow series starts later, so trim the fast one to match.
    offset = len(fast_s) - len(slow_s)
    line_series = [f - s for f, s in zip(fast_s[offset:], slow_s)]
    sig_series = ema_series(line_series, signal)
    if not sig_series:
        return None
    line, sig = line_series[-1], sig_series[-1]
    return line, sig, line - sig


def roc(closes: list[float], period: int = 10) -> float | None:
    """Rate of change, as a fraction. Raw speed, no smoothing."""
    if len(closes) < period + 1 or closes[-period - 1] == 0:
        return None
    return (closes[-1] - closes[-period - 1]) / closes[-period - 1]


# ── volatility ────────────────────────────────────────────────────────────

def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    out = []
    for i in range(1, len(closes)):
        out.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> float | None:
    tr = true_ranges(highs, lows, closes)
    sm = wilder(tr, period)
    return sm[-1] if sm else None


def atr_pct(highs: list[float], lows: list[float], closes: list[float],
            period: int = 14) -> float | None:
    """ATR as a fraction of price — the form the cost floor compares against."""
    a = atr(highs, lows, closes, period)
    if a is None or not closes or closes[-1] <= 0:
        return None
    return a / closes[-1]


def bollinger(closes: list[float], period: int = 20,
              mult: float = 2.0) -> tuple[float, float, float, float] | None:
    """Returns (upper, mid, lower, bandwidth-as-fraction-of-mid)."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    sd = stdev(window)
    upper, lower = mid + sd * mult, mid - sd * mult
    return upper, mid, lower, (upper - lower) / mid if mid else 0.0


def keltner(highs: list[float], lows: list[float], closes: list[float],
            period: int = 20, mult: float = 1.5) -> tuple[float, float, float] | None:
    """EMA channel widened by ATR. Paired with Bollinger to detect a squeeze."""
    mid = ema(closes, period)
    a = atr(highs, lows, closes, period)
    if mid is None or a is None:
        return None
    return mid + a * mult, mid, mid - a * mult


def squeeze_on(highs: list[float], lows: list[float], closes: list[float],
               period: int = 20) -> bool | None:
    """
    True when Bollinger bands sit inside the Keltner channel.

    This is the real squeeze definition, and it is a far better one than "band
    width below an arbitrary constant": it is scale-free, so it means the same
    thing on a $1 coin and on BTC.
    """
    bb = bollinger(closes, period)
    kc = keltner(highs, lows, closes, period)
    if bb is None or kc is None:
        return None
    return bb[0] < kc[0] and bb[2] > kc[2]


def volatility_percentile(highs: list[float], lows: list[float], closes: list[float],
                          period: int = 14, lookback: int = 100) -> float | None:
    """
    Where current ATR sits within its own recent history, 0 to 1.

    Absolute volatility says nothing on its own; a coin that always moves 3% is
    not volatile today because it moved 3%. This makes the comparison relative.
    """
    if len(closes) < period + lookback:
        return None
    values = []
    for end in range(len(closes) - lookback, len(closes) + 1):
        a = atr(highs[:end], lows[:end], closes[:end], period)
        if a is not None and closes[end - 1] > 0:
            values.append(a / closes[end - 1])
    if len(values) < 2:
        return None
    current = values[-1]
    below = sum(1 for v in values[:-1] if v < current)
    return below / (len(values) - 1)


# ── trend ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ADX:
    adx: float
    plus_di: float
    minus_di: float

    @property
    def is_trending(self) -> bool:
        """The conventional threshold. Below it, trend-following is guesswork."""
        return self.adx >= 25.0

    @property
    def direction(self) -> int:
        return 1 if self.plus_di > self.minus_di else -1


def adx(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> ADX | None:
    """Wilder's ADX with directional indicators. Measures trend STRENGTH, not direction."""
    if len(closes) < period * 2 + 1:
        return None
    plus_dm, minus_dm = [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    tr = true_ranges(highs, lows, closes)
    tr_s, p_s, m_s = wilder(tr, period), wilder(plus_dm, period), wilder(minus_dm, period)
    if not tr_s or not p_s or not m_s:
        return None

    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        if t == 0:
            continue
        pdi, mdi = p / t * 100.0, m / t * 100.0
        denom = pdi + mdi
        dx.append(abs(pdi - mdi) / denom * 100.0 if denom else 0.0)
    smoothed = wilder(dx, period)
    if not smoothed or tr_s[-1] == 0:
        return None
    return ADX(adx=smoothed[-1],
               plus_di=p_s[-1] / tr_s[-1] * 100.0,
               minus_di=m_s[-1] / tr_s[-1] * 100.0)


def supertrend(highs: list[float], lows: list[float], closes: list[float],
               period: int = 10, mult: float = 3.0) -> tuple[float, int] | None:
    """
    Returns (level, direction). Direction is +1 for up, -1 for down.

    An ATR-banded trend follower that flips only when price closes through the
    opposite band, so it does not whip on a single spike the way a moving
    average cross does.
    """
    if len(closes) < period + 2:
        return None
    tr = true_ranges(highs, lows, closes)
    atr_s = wilder(tr, period)
    if not atr_s:
        return None

    offset = len(closes) - len(atr_s)
    direction, level = 1, closes[offset - 1] if offset else closes[0]
    for i, a in enumerate(atr_s):
        idx = offset + i
        mid = (highs[idx] + lows[idx]) / 2.0
        upper, lower = mid + mult * a, mid - mult * a
        if direction > 0:
            level = max(lower, level)
            if closes[idx] < level:
                direction, level = -1, upper
        else:
            level = min(upper, level)
            if closes[idx] > level:
                direction, level = 1, lower
    return level, direction


# ── volume ────────────────────────────────────────────────────────────────

def median_bar_minutes(timestamps: list, default: float = 1.0) -> float:
    """
    How far apart these bars actually are, in minutes.

    Every projection here scales volatility by sqrt(horizon / bar), and that
    ratio was hardcoded to treat one bar as one minute. Two things break it.
    A venue that ignores the requested interval and returns five-minute
    candles inflates the ATR by sqrt(5) and, with the bar length still assumed
    to be one, every target and every "within 15m" label silently means
    something else. And on a free instance that sleeps, the poll-aggregated
    history has gaps, so consecutive bars can be an hour apart.

    The median is used rather than the mean because one gap must not restate
    the whole series. Falls back to `default` when there is not enough history
    to measure, which is the same assumption as before — but now it is an
    assumption made once, in the open, rather than everywhere implicitly.
    """
    if len(timestamps) < 3:
        return default
    gaps = []
    for a, b in zip(timestamps, timestamps[1:]):
        try:
            delta = (b - a).total_seconds() / 60.0
        except (AttributeError, TypeError):
            return default
        if delta > 0:
            gaps.append(delta)
    if not gaps:
        return default
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    return median if median > 0 else default


def has_usable_volume(volumes: list[float], min_bars: int = 20) -> bool:
    """
    Is this a real per-bar volume series, or a placeholder?

    Two feeds reach this code. The WebSocket path carries genuine per-minute
    volume; the REST poller does not know it and writes zero. A third case is
    worse than either: a feed that stamps the same rolling 24-hour total onto
    every bar, which looks like data and is not — relative volume comes out at
    1.0 forever, VWAP collapses to an unweighted average, and money flow ends
    up driven purely by price with a constant weight attached.

    So both are rejected: nothing traded, and nothing *changing*. A market
    genuinely printing an identical volume every minute for twenty minutes
    does not exist, and refusing to read that series costs one abstention
    while accepting it costs a fabricated vote.
    """
    if len(volumes) < min_bars:
        return False
    # Negatives are checked across the whole series, not just the window: a
    # negative volume anywhere means the feed is broken, and the next call
    # with a different window would otherwise disagree with this one.
    if any(v < 0 for v in volumes):
        return False
    window = volumes[-min_bars:]
    total = sum(window)
    if total <= 0:
        return False
    mean = total / len(window)
    spread = max(window) - min(window)
    return spread / mean > 0.01


def relative_volume(volumes: list[float], period: int = 20) -> float | None:
    """
    The latest bar's volume against the average of the bars before it.

    1.0 is an ordinary minute. Above ~1.5 the move has participation behind
    it; below ~0.6 the tape has dried up and the price is drifting on very
    few trades, which is where a short-horizon target is least trustworthy
    and a modelled fill is least likely to be the fill you get.

    Deliberately excludes the current bar from its own baseline — including
    it damps exactly the spike the measure exists to detect.
    """
    if not has_usable_volume(volumes, min_bars=period + 1):
        return None
    prior = volumes[-period - 1:-1]
    base = sum(prior) / len(prior)
    if base <= 0:
        return None
    return volumes[-1] / base


def volume_trend(closes: list[float], volumes: list[float],
                 period: int = 20) -> float | None:
    """
    Does volume arrive on the up bars or the down bars?

    Returns roughly -1..+1: the share of volume traded on rising bars minus
    the share on falling ones. Positive means buyers are the ones showing up.

    This is the question OBV answers as a running total, restated as a bounded
    number over a fixed window so it can be compared across symbols — an OBV
    of 4.2 million means nothing without knowing the coin.
    """
    if len(closes) != len(volumes):
        return None
    if not has_usable_volume(volumes, min_bars=period + 1):
        return None
    up = down = 0.0
    for i in range(len(closes) - period, len(closes)):
        if closes[i] > closes[i - 1]:
            up += volumes[i]
        elif closes[i] < closes[i - 1]:
            down += volumes[i]
    total = up + down
    if total <= 0:
        return None
    return (up - down) / total


def obv(closes: list[float], volumes: list[float]) -> float | None:
    """On-balance volume: does volume confirm the direction, or contradict it."""
    if len(closes) < 2 or len(volumes) != len(closes):
        return None
    total = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            total += volumes[i]
        elif closes[i] < closes[i - 1]:
            total -= volumes[i]
    return total


def mfi(highs: list[float], lows: list[float], closes: list[float],
        volumes: list[float], period: int = 14) -> float | None:
    """Money Flow Index — RSI weighted by volume, so conviction counts."""
    n = len(closes)
    if n < period + 1 or len(volumes) != n:
        return None
    pos = neg = 0.0
    for i in range(n - period, n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        prev = (highs[i - 1] + lows[i - 1] + closes[i - 1]) / 3.0
        flow = tp * volumes[i]
        if tp > prev:
            pos += flow
        elif tp < prev:
            neg += flow
    if neg == 0:
        return 100.0 if pos > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + pos / neg)


def vwap(highs: list[float], lows: list[float], closes: list[float],
         volumes: list[float]) -> float | None:
    """Volume-weighted average price — where the money actually traded."""
    if not closes or len(volumes) != len(closes):
        return None
    total_v = sum(volumes)
    if total_v <= 0:
        return None
    return sum((h + low + c) / 3.0 * v
               for h, low, c, v in zip(highs, lows, closes, volumes)) / total_v


# ── market structure ──────────────────────────────────────────────────────

def swing_pivots(values: list[float], left: int = 2, right: int = 2,
                 find_highs: bool = True) -> list[int]:
    """
    Indices of confirmed swing points — a bar higher (or lower) than `left`
    bars before and `right` bars after.

    This is what "a new low" should have meant. Comparing the minimum of one
    half-window to the other, as the old divergence check did, fires on about
    half of all bars of pure noise: it detects wiggle, not structure.
    """
    out = []
    for i in range(left, len(values) - right):
        window = values[i - left:i + right + 1]
        if find_highs and values[i] == max(window) and values[i] > values[i - 1]:
            out.append(i)
        elif not find_highs and values[i] == min(window) and values[i] < values[i - 1]:
            out.append(i)
    return out


def divergence(prices: list[float], oscillator: list[float], left: int = 2,
               right: int = 2, bullish: bool = True) -> bool:
    """
    Real divergence: compare the oscillator AT two confirmed swing points.

    Bullish — price makes a lower low while the oscillator makes a higher low.
    Bearish — price makes a higher high while the oscillator makes a lower high.

    The oscillator must be read at the same bars as the price pivots. Reading
    "is RSI ticking up right now" instead, as the old code did, tests something
    entirely different and much more common.
    """
    if len(prices) != len(oscillator) or len(prices) < left + right + 4:
        return False
    pivots = swing_pivots(prices, left, right, find_highs=not bullish)
    if len(pivots) < 2:
        return False
    a, b = pivots[-2], pivots[-1]
    if bullish:
        return prices[b] < prices[a] and oscillator[b] > oscillator[a]
    return prices[b] > prices[a] and oscillator[b] < oscillator[a]


def trend_structure(highs: list[float], lows: list[float],
                    left: int = 2, right: int = 2) -> int:
    """
    +1 for higher highs and higher lows, -1 for lower lows and lower highs, 0 otherwise.

    This is price structure rather than an average of price, so it says what the
    market is doing rather than what it did on average recently.
    """
    hp = swing_pivots(highs, left, right, find_highs=True)
    lp = swing_pivots(lows, left, right, find_highs=False)
    if len(hp) < 2 or len(lp) < 2:
        return 0
    hh = highs[hp[-1]] > highs[hp[-2]]
    hl = lows[lp[-1]] > lows[lp[-2]]
    lh = highs[hp[-1]] < highs[hp[-2]]
    ll = lows[lp[-1]] < lows[lp[-2]]
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


def support_resistance(highs: list[float], lows: list[float],
                       left: int = 2, right: int = 2) -> tuple[float | None, float | None]:
    """Nearest confirmed swing low below and swing high above — real levels, not round numbers."""
    hp = swing_pivots(highs, left, right, find_highs=True)
    lp = swing_pivots(lows, left, right, find_highs=False)
    resistance = highs[hp[-1]] if hp else None
    support = lows[lp[-1]] if lp else None
    return support, resistance
