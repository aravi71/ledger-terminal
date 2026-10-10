"""Intraday / scalping backtester with stop-loss and take-profit (CFD-style).

Execution model
- Bars are BID prices. Buys fill at ask (bid + spread); sells at bid. A long's
  stop/target trigger on the bid; a short's on the ask (bid high/low + spread).
- A signal is evaluated on the close of bar i and entered at bar i+1's open.
- Stop-loss and take-profit are checked against each bar's high/low. If both
  could have been hit inside one bar, the STOP is assumed to fill first
  (pessimistic). A gap through a level fills at the bar's open.
- One position at a time. Optional time exit (``max_bars``) and a daily
  flat time (``flat_hour`` UTC).
- Size: ``risk_pct`` of current equity at risk between entry and stop,
  rounded down to ``qty_step``. If that is below ``min_qty`` the minimum is
  used only while its risk stays under ``max_risk_pct``; otherwise the trade
  is skipped (and counted). Margin = notional / leverage must fit in equity.
- Commission is charged per unit per side. The account stops trading if
  equity falls below the margin needed for the minimum size.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from zoneinfo import ZoneInfo

from . import indicators

NY = ZoneInfo("America/New_York")

Bars = list[dict[str, float]]


# ----------------------------------------------------------------------------- data
def load_history(data_dir: Path, symbol: str, minutes: int) -> Bars:
    """Load cached Dukascopy candles and resample to ``minutes`` (5, 15, 30, 60, 240)."""
    base = data_dir / "history" / symbol.upper()
    src = base / ("m1" if minutes < 60 or not (base / "h1").exists() else "h1")
    if minutes < 60 and not (base / "m1").exists():
        raise FileNotFoundError(f"No minute history for {symbol}. Run scripts/fetch_dukascopy.py --tf minute first.")
    if not src.exists():
        raise FileNotFoundError(f"No history for {symbol} in {base}. Run scripts/fetch_dukascopy.py first.")
    raw: Bars = []
    files = sorted(src.glob("*.csv"))
    if src.name == "m1":  # use only the most recent unbroken run of days (downloads fill newest first)
        files = _contiguous_tail(files)
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                raw.append({"t": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"])})
    raw.sort(key=lambda b: b["t"])
    return resample(raw, minutes)


def _contiguous_tail(files: list[Path]) -> list[Path]:
    from datetime import date, timedelta
    days = [date.fromisoformat(f.stem) for f in files]
    start = 0
    for i in range(len(days) - 1, 0, -1):
        if days[i] - days[i - 1] > timedelta(days=3):  # a weekend is at most a 2-day gap
            start = i
            break
    return files[start:]


def resample(bars: Bars, minutes: int) -> Bars:
    step = minutes * 60
    out: Bars = []
    cur = None
    for b in bars:
        key = b["t"] - b["t"] % step
        if cur is None or cur["t"] != key:
            if cur:
                out.append(cur)
            cur = {"t": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] += b["v"]
    if cur:
        out.append(cur)
    return out


# ----------------------------------------------------------------------------- features
@dataclass
class Features:
    hour: list[int]
    day: list[int]
    weekday: list[int]
    ema20: list[float | None]
    ema50: list[float | None]
    ema200: list[float | None]
    rsi2: list[float | None]
    rsi14: list[float | None]
    atr: list[float | None]
    asia_hi: list[float | None]   # today's 00:00-07:00 UTC range, known from 07:00
    asia_lo: list[float | None]
    ny_close_hour: list[int]      # New York hour at which the bar closes (DST-aware)
    ny_weekday: list[int]
    ny_min: list[int] = field(default_factory=list)    # New York minutes since midnight at the bar's START
    ny_date: list[int] = field(default_factory=list)   # New York calendar date (ordinal) of the bar's start
    step_min: int = 60
    cache: dict = field(default_factory=dict)          # per-run memo for strategy-specific series
    rollover: list[bool] = field(default_factory=list)  # bar closes at/after a 17:00 New York rollover


def features(bars: Bars) -> Features:
    closes = [b["c"] for b in bars]
    hour, day, wd = [], [], []
    for b in bars:
        d = datetime.fromtimestamp(b["t"], timezone.utc)
        hour.append(d.hour)
        day.append(b["t"] // 86400)
        wd.append(d.weekday())
    diffs = [bars[k + 1]["t"] - bars[k]["t"] for k in range(min(len(bars) - 1, 200))]
    step = min((d for d in diffs if d > 0), default=3600)
    ny = [datetime.fromtimestamp(b["t"] + step, NY) for b in bars]
    ny_start = [datetime.fromtimestamp(b["t"], NY) for b in bars]
    asia_hi: list[float | None] = [None] * len(bars)
    asia_lo: list[float | None] = [None] * len(bars)
    cur_day, hi, lo, done = None, -math.inf, math.inf, False
    for i, b in enumerate(bars):
        if day[i] != cur_day:
            cur_day, hi, lo, done = day[i], -math.inf, math.inf, False
        if hour[i] < 7:
            hi, lo = max(hi, b["h"]), min(lo, b["l"])
        else:
            done = hi > -math.inf
        if done:
            asia_hi[i], asia_lo[i] = hi, lo
    return Features(
        hour, day, wd,
        indicators.ema(closes, 20), indicators.ema(closes, 50), indicators.ema(closes, 200),
        indicators.rsi(closes, 2), indicators.rsi(closes, 14), indicators.atr(bars, 14),
        asia_hi, asia_lo,
        [d.hour for d in ny], [d.weekday() for d in ny],
        [d.hour * 60 + d.minute for d in ny_start], [d.toordinal() for d in ny_start], step // 60,
        {}, _rollovers(ny),
    )


def _rollovers(ny_end: list[datetime]) -> list[bool]:
    """True for the bar whose close is the first at or after 17:00 New York (the daily rollover)."""
    keys = [(d - timedelta(hours=17)).date() for d in ny_end]  # a bar ending exactly 17:00 holds through it
    return [False] + [keys[k] != keys[k - 1] for k in range(1, len(keys))]


# ----------------------------------------------------------------------------- strategies
# Each returns None or (side, stop_distance, target_distance) decided on bar i's close.
Signal = tuple[str, float, float] | None


def trend_pullback(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """With the 50/200 EMA trend, buy a dip that tags the 20 EMA and closes back above it (mirror for shorts)."""
    a, e20, e50, e200, r = f.atr[i], f.ema20[i], f.ema50[i], f.ema200[i], f.rsi14[i]
    if None in (a, e20, e50, e200, r) or not (p["session_start"] <= f.hour[i] < p["session_end"]):
        return None
    b = bars[i]
    if e50 > e200 and b["l"] <= e20 < b["c"] and r < p["rsi_max_long"]:
        return ("buy", p["sl_atr"] * a, p["tp_atr"] * a)
    if e50 < e200 and b["h"] >= e20 > b["c"] and r > 100 - p["rsi_max_long"]:
        return ("sell", p["sl_atr"] * a, p["tp_atr"] * a)
    return None


def asian_breakout(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """London open: first close beyond the Asian (00-07 UTC) range plus a buffer; stop at the other side."""
    hi, lo, a = f.asia_hi[i], f.asia_lo[i], f.atr[i]
    if None in (hi, lo, a) or not (7 <= f.hour[i] < p["entry_until"]):
        return None
    rng = hi - lo
    if rng <= 0 or rng > p["max_range_atr"] * a:
        return None
    c, buf = bars[i]["c"], p["buffer_atr"] * a
    stop = min(rng + buf, p["sl_cap_atr"] * a)
    if c > hi + buf and bars[i - 1]["c"] <= hi + buf:
        return ("buy", stop, p["rr"] * stop)
    if c < lo - buf and bars[i - 1]["c"] >= lo - buf:
        return ("sell", stop, p["rr"] * stop)
    return None


def asian_sweep_fade(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Fade a failed London break of the Asian range: price pokes beyond it, then closes back inside."""
    hi, lo, a = f.asia_hi[i], f.asia_lo[i], f.atr[i]
    if None in (hi, lo, a) or not (7 <= f.hour[i] < p["entry_until"]):
        return None
    b, mid, depth = bars[i], (hi + lo) / 2, p["sweep_atr"] * a
    if b["h"] > hi + depth and b["c"] < hi:
        stop = b["h"] - b["c"] + p["sl_pad_atr"] * a
        return ("sell", stop, max(b["c"] - mid, 0.2 * a))
    if b["l"] < lo - depth and b["c"] > lo:
        stop = b["c"] - b["l"] + p["sl_pad_atr"] * a
        return ("buy", stop, max(mid - b["c"], 0.2 * a))
    return None


def rsi2_highwin(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """The classic 'high win rate' setup: buy a sharp RSI(2) dip in an uptrend (mirror for shorts),
    small take-profit, wide stop. Expect a high win rate and occasional large losses."""
    a, r, e200 = f.atr[i], f.rsi2[i], f.ema200[i]
    if None in (a, r, e200) or not (p["session_start"] <= f.hour[i] < p["session_end"]):
        return None
    c = bars[i]["c"]
    if c > e200 and r < p["rsi_entry"]:
        return ("buy", p["sl_atr"] * a, p["tp_atr"] * a)
    if c < e200 and r > 100 - p["rsi_entry"]:
        return ("sell", p["sl_atr"] * a, p["tp_atr"] * a)
    return None


def asia_session_drift(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Hold gold through Asian hours: on the close of the bar before ``entry_hour`` UTC, buy;
    the position is closed by the time limit (``max_bars``) or its protective stop.
    Based on gold's intraday seasonality (it has tended to rise in Asian hours)."""
    a = f.atr[i]
    # Signal on the bar that closes at 17:00 New York time (the daily break), Monday to Thursday;
    # the entry is the next bar, the first one after the reopen.
    if a is None or f.ny_close_hour[i] != int(p["ny_signal_hour"]) or f.ny_weekday[i] > 3:
        return None
    if p.get("trend_filter") and (f.ema200[i] is None or bars[i]["c"] < f.ema200[i]):
        return None
    return ("buy", p["sl_atr"] * a, p["tp_atr"] * a)


def _opening_range(bars: Bars, f: Features, start: int, minutes: int):
    """Per bar: the day's New York opening range [start, start+minutes) once it is complete,
    and the index of the first bar that closed outside it (None until it happens)."""
    key = ("orb", start, minutes)
    if key in f.cache:
        return f.cache[key]
    n = len(bars)
    hi: list[float | None] = [None] * n
    lo: list[float | None] = [None] * n
    first_break: list[int | None] = [None] * n
    day, rh, rl, done, broke = None, -math.inf, math.inf, False, None
    for i, b in enumerate(bars):
        if f.ny_date[i] != day:
            day, rh, rl, done, broke = f.ny_date[i], -math.inf, math.inf, False, None
        m = f.ny_min[i]
        if start <= m < start + minutes:
            rh, rl = max(rh, b["h"]), min(rl, b["l"])
        elif m >= start + minutes and rh > -math.inf:
            done = True
        if done:
            hi[i], lo[i] = rh, rl
            if broke is None and (b["c"] > rh or b["c"] < rl):
                broke = i
            first_break[i] = broke
    f.cache[key] = (hi, lo, first_break)
    return f.cache[key]


def ny_orb(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """New York opening-range breakout: the range of the first ``or_minutes`` after ``or_start``
    (minutes after midnight New York, 570 = 09:30); trade the FIRST close outside it before
    ``entry_until``. Stop ``sl_frac`` of the range back from the breakout edge (1 = other side,
    0.5 = middle); target ``rr`` times the stop. One trade a day."""
    hi, lo, first = _opening_range(bars, f, int(p["or_start"]), int(p["or_minutes"]))
    h, l = hi[i], lo[i]
    if h is None or first[i] != i or f.ny_min[i] >= p["entry_until"] or f.ny_weekday[i] > 4:
        return None
    rng = h - l
    a = f.atr[i]
    if rng <= 0 or (a and p.get("max_range_atr") and rng > p["max_range_atr"] * a):
        return None
    c = bars[i]["c"]
    if c > h:
        sd = (c - h) + p["sl_frac"] * rng
        return ("buy", sd, p["rr"] * sd)
    sd = (l - c) + p["sl_frac"] * rng
    return ("sell", sd, p["rr"] * sd)


def ema_rsi_scalp(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Trend scalp: with price on the trend side of the 200 EMA, enter when RSI(14) turns back
    up through ``rsi_low`` (down through 100 - rsi_low for shorts). Stop ``sl_atr`` x ATR,
    target ``rr`` x stop. Only between ``sess_start`` and ``sess_end`` New York time."""
    a, e200, r, r0 = f.atr[i], f.ema200[i], f.rsi14[i], f.rsi14[i - 1] if i else None
    if None in (a, e200, r, r0) or not (p["sess_start"] <= f.ny_min[i] < p["sess_end"]):
        return None
    c = bars[i]["c"]
    sd = p["sl_atr"] * a
    if c > e200 and r0 < p["rsi_low"] <= r:
        return ("buy", sd, p["rr"] * sd)
    if c < e200 and r0 > 100 - p["rsi_low"] >= r:
        return ("sell", sd, p["rr"] * sd)
    return None


def silver_bullet(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """ICT 'Silver Bullet' (simplified, mechanical): inside the ``win_start``-``win_end`` New York
    window (600-660 = 10:00-11:00), take the FIRST fair-value gap of the day there, formed by a
    displacement candle: bullish if low[i] > high[i-2] and bar i closes up (bearish mirrored),
    gap at least ``min_gap_atr`` x ATR. Stop beyond candle i-2 plus ``sl_pad_atr`` x ATR;
    target ``rr`` x stop. Entry at the next bar's open (market, no limit-order fill assumed)."""
    a = f.atr[i]
    if a is None or i < 2 or not (p["win_start"] <= f.ny_min[i] < p["win_end"]) or f.ny_weekday[i] > 4:
        return None

    def gap(k: int) -> str | None:
        b, b2 = bars[k], bars[k - 2]
        if b["l"] > b2["h"] and b["c"] > b["o"] and b["l"] - b2["h"] >= p["min_gap_atr"] * a:
            return "buy"
        if b["h"] < b2["l"] and b["c"] < b["o"] and b2["l"] - b["h"] >= p["min_gap_atr"] * a:
            return "sell"
        return None

    side = gap(i)
    if not side:
        return None
    k = i - 1  # only the first gap of today's window counts
    while k >= 2 and f.ny_date[k] == f.ny_date[i] and f.ny_min[k] >= p["win_start"]:
        if gap(k):
            return None
        k -= 1
    c = bars[i]["c"]
    pad = p["sl_pad_atr"] * a
    sd = (c - bars[i - 2]["l"] + pad) if side == "buy" else (bars[i - 2]["h"] - c + pad)
    return (side, sd, p["rr"] * sd) if sd > 0 else None


def orb_fvg(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Opening range + fair-value-gap scalp (as taught in popular YouTube videos), for 1-minute bars:
    1) range = high/low of the first ``or_minutes`` after 09:30 New York;
    2) bias = direction of the first 5-minute close outside the range;
    3) entry = the first 1-minute fair-value gap in that direction (low[i] > high[i-2] for longs),
       stop beyond the middle candle (i-1), target ``rr`` x stop; before ``entry_until``. One trade a day."""
    hi, lo, _ = _opening_range(bars, f, 570, int(p["or_minutes"]))
    h, l = hi[i], lo[i]
    if h is None or i < 3 or f.ny_min[i] >= p["entry_until"] or f.ny_weekday[i] > 4:
        return None
    key = ("orb_fvg_bias", int(p["or_minutes"]))
    if key not in f.cache:  # per bar: the day's bias once a 5-minute close has left the range
        bias: list[str | None] = [None] * len(bars)
        day, cur = None, None
        for k, b in enumerate(bars):
            if f.ny_date[k] != day:
                day, cur = f.ny_date[k], None
            if cur is None and hi[k] is not None and (f.ny_min[k] + f.step_min) % 5 == 0:
                cur = "buy" if b["c"] > hi[k] else "sell" if b["c"] < lo[k] else None
            bias[k] = cur
        f.cache[key] = bias
    side = f.cache[key][i - 1]  # bias known before this bar
    if side is None:
        return None

    def fvg(k: int) -> bool:
        return bars[k]["l"] > bars[k - 2]["h"] if side == "buy" else bars[k]["h"] < bars[k - 2]["l"]

    if not fvg(i):
        return None
    k = i - 1  # only the first qualifying gap after the bias was set
    while k >= 2 and f.ny_date[k] == f.ny_date[i] and f.cache[key][k - 1] == side:
        if fvg(k):
            return None
        k -= 1
    c = bars[i]["c"]
    sd = c - bars[i - 1]["l"] if side == "buy" else bars[i - 1]["h"] - c
    return (side, sd, p["rr"] * sd) if sd > 0 else None


def supply_demand(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Supply/demand zone retest (mechanical version of the popular US30 video): a small 'base'
    candle followed by an impulsive candle (body >= ``impulse_atr`` x ATR) marks a zone at the base
    candle's range. The first time price returns into a fresh zone within ``lookback`` bars, trade in
    the impulse direction; stop beyond the zone (+ ``sl_pad_atr`` x ATR), target ``rr`` x stop."""
    a = f.atr[i]
    if a is None or not (p["sess_start"] <= f.ny_min[i] < p["sess_end"]):
        return None
    trend = 0
    if p.get("trend_filter"):
        if f.ema50[i] is None or f.ema200[i] is None:
            return None
        trend = 1 if f.ema50[i] > f.ema200[i] else -1
    b = bars[i]
    for k in range(i - 2, max(2, i - int(p["lookback"])), -1):
        base, imp = bars[k - 1], bars[k]
        ak = f.atr[k]
        if ak is None or abs(base["c"] - base["o"]) > p["base_atr"] * ak or abs(imp["c"] - imp["o"]) < p["impulse_atr"] * ak:
            continue
        up = imp["c"] > imp["o"]
        if trend and (1 if up else -1) != trend:
            continue
        top, bot = base["h"], base["l"]
        between = bars[k + 1:i]
        if up:
            if any(x["l"] <= top for x in between) or not (b["l"] <= top and b["c"] > bot):
                continue
            sd = b["c"] - bot + p["sl_pad_atr"] * a
            return ("buy", sd, p["rr"] * sd) if sd > 0 else None
        if any(x["h"] >= bot for x in between) or not (b["h"] >= bot and b["c"] < top):
            continue
        sd = top - b["c"] + p["sl_pad_atr"] * a
        return ("sell", sd, p["rr"] * sd) if sd > 0 else None
    return None



# ----------------------------------------------------------------------------- strategies from popular videos
def _smma(values: Sequence[float], period: int) -> list[float | None]:
    """Smoothed (Wilder / RMA) moving average, as used in TradingView's 'SMMA'."""
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for k in range(period, len(values)):
        prev = (prev * (period - 1) + values[k]) / period
        out[k] = prev
    return out


def _cached(f: Features, key, build):
    if key not in f.cache:
        f.cache[key] = build()
    return f.cache[key]


def three_line_strike(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Trend scalp from a 1.6M-view gold video: SMMA 21 > 50 > 200 fanned out (longs; reversed for
    shorts) and RSI(14) on the trend side of 50; three candles against the trend, then an engulfing
    candle back with the trend covering >= ``engulf_frac`` of those three. Stop beyond the pattern's
    extreme (+ pad); target ``rr`` x stop; out after ``max_bars`` bars."""
    if i < 4 or f.atr[i] is None or f.rsi14[i] is None:
        return None
    closes = None
    s21 = _cached(f, ("smma", 21), lambda: _smma([b["c"] for b in bars], 21))
    s50 = _cached(f, ("smma", 50), lambda: _smma([b["c"] for b in bars], 50))
    s200 = _cached(f, ("smma", 200), lambda: _smma([b["c"] for b in bars], 200))
    a, b, c, d = s21[i], s50[i], s200[i], f.rsi14[i]
    if None in (a, b, c):
        return None
    three, e = bars[i - 3:i], bars[i]
    move_hi, move_lo = max(x["h"] for x in three), min(x["l"] for x in three)
    pad = p["sl_pad_atr"] * f.atr[i]
    if a > b > c and e["c"] > c and d > 50 and all(x["c"] < x["o"] for x in three):
        if e["c"] > e["o"] and e["c"] >= three[-1]["c"] + p["engulf_frac"] * (three[0]["o"] - three[-1]["c"]):
            sd = e["c"] - min(move_lo, e["l"]) + pad
            return ("buy", sd, p["rr"] * sd)
    if a < b < c and e["c"] < c and d < 50 and all(x["c"] > x["o"] for x in three):
        if e["c"] < e["o"] and e["c"] <= three[-1]["c"] - p["engulf_frac"] * (three[-1]["c"] - three[0]["o"]):
            sd = max(move_hi, e["h"]) - e["c"] + pad
            return ("sell", sd, p["rr"] * sd)
    return None


def range4h_fade(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """False breakout of the day's first 4-hour candle (00:00-04:00 New York), from a 1.9M-view video:
    after a candle CLOSES outside the range, trade back the other way on the first close back inside.
    Stop at the breakout move's extreme; target ``rr`` x stop; same day, before ``entry_until``."""
    hi, lo, _ = _opening_range(bars, f, 0, 240)
    h, l = hi[i], lo[i]
    if h is None or f.ny_min[i] >= p["entry_until"] or f.ny_weekday[i] > 4:
        return None
    key = ("r4h_state",)

    def build():
        state = [None] * len(bars)  # (direction of the outside close, extreme so far) or "done"
        day, st = None, None
        for k, b in enumerate(bars):
            if f.ny_date[k] != day:
                day, st = f.ny_date[k], None
            if hi[k] is not None and st != "done":
                if st is None:
                    if b["c"] > hi[k]:
                        st = ("up", b["h"])
                    elif b["c"] < lo[k]:
                        st = ("down", b["l"])
                else:
                    st = (st[0], max(st[1], b["h"]) if st[0] == "up" else min(st[1], b["l"]))
            state[k] = st
        return state

    state = _cached(f, key, build)
    prev = state[i - 1] if i else None
    if not prev or prev == "done":
        return None
    c = bars[i]["c"]
    if prev[0] == "up" and c < h:
        sd = max(prev[1], bars[i]["h"]) - c
        if sd > 0:
            _mark_done(state, f, i)
            return ("sell", sd, p["rr"] * sd)
    if prev[0] == "down" and c > l:
        sd = c - min(prev[1], bars[i]["l"])
        if sd > 0:
            _mark_done(state, f, i)
            return ("buy", sd, p["rr"] * sd)
    return None


def _mark_done(state: list, f: Features, i: int) -> None:
    k = i
    while k < len(state) and f.ny_date[k] == f.ny_date[i]:
        state[k] = "done"
        k += 1


def orb_retest(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """'9:30 candle' scalp (437k-view video): range of the first ``or_minutes`` from 09:30 New York;
    after a close beyond it, enter on the first retest — a bar that tags the broken level and closes
    back beyond it. Stop beyond the retest bar (+ pad); target ``rr`` x stop; before ``entry_until``."""
    hi, lo, first = _opening_range(bars, f, 570, int(p["or_minutes"]))
    h, l, fb = hi[i], lo[i], first[i]
    if h is None or fb is None or fb >= i or f.ny_min[i] >= p["entry_until"] or f.ny_weekday[i] > 4:
        return None
    side = "buy" if bars[fb]["c"] > h else "sell"
    b = bars[i]
    pad = p["sl_pad_atr"] * (f.atr[i] or 0)
    retest = (b["l"] <= h < b["c"]) if side == "buy" else (b["h"] >= l > b["c"])
    if not retest:
        return None
    for k in range(fb + 1, i):  # only the first retest
        x = bars[k]
        if (x["l"] <= h < x["c"]) if side == "buy" else (x["h"] >= l > x["c"]):
            return None
    sd = (b["c"] - b["l"] + pad) if side == "buy" else (b["h"] - b["c"] + pad)
    return (side, sd, p["rr"] * sd) if sd > 0 else None


def fib_pullback(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Fibonacci 'golden pocket' pullback (385k-view video): in a trend (EMA 50 vs 200), take the swing
    of the last ``lookback`` bars (must span >= ``min_swing_atr`` x ATR); when price pulls back into
    the 0.5-0.618 zone and closes back out of it toward the trend, enter. Stop beyond the swing's
    origin; target the swing's end (about 1:1.6 from 0.618)."""
    a, e50, e200 = f.atr[i], f.ema50[i], f.ema200[i]
    n = int(p["lookback"])
    if None in (a, e50, e200) or i < n + 1 or not (p["sess_start"] <= f.ny_min[i] < p["sess_end"]):
        return None
    win = bars[i - n:i]
    hi_k = max(range(len(win)), key=lambda k: win[k]["h"])
    lo_k = min(range(len(win)), key=lambda k: win[k]["l"])
    H, L = win[hi_k]["h"], win[lo_k]["l"]
    if H - L < p["min_swing_atr"] * a:
        return None
    b = bars[i]
    if e50 > e200 and lo_k < hi_k:  # up-swing, pulling back
        z618, z50 = H - 0.618 * (H - L), H - 0.5 * (H - L)
        if b["l"] <= z50 and b["l"] >= L and b["c"] > z618 and b["c"] < H:
            return ("buy", b["c"] - L + p["sl_pad_atr"] * a, H - b["c"])
    if e50 < e200 and hi_k < lo_k:  # down-swing, pulling back up
        z618, z50 = L + 0.618 * (H - L), L + 0.5 * (H - L)
        if b["h"] >= z50 and b["h"] <= H and b["c"] < z618 and b["c"] > L:
            return ("sell", H - b["c"] + p["sl_pad_atr"] * a, b["c"] - L)
    return None


def bias_orb(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """'3 step' scalp (500-trade video): bias from the 07:00 and 08:00 New York hourly candles (the 8am
    candle's body engulfing the 7am body sets the direction); then the 09:30-09:45 range; the first
    close beyond it IN the bias direction is the entry; stop at the other side (``sl_frac`` of the
    range); target ``rr`` x stop; before ``entry_until`` (11:00)."""
    hi, lo, first = _opening_range(bars, f, 570, int(p["or_minutes"]))
    h, l, fb = hi[i], lo[i], first[i]
    if h is None or fb != i or f.ny_min[i] >= p["entry_until"] or f.ny_weekday[i] > 4:
        return None

    def build():
        out = [None] * len(bars)
        day, c7, c8, cur = None, None, None, None
        for k, b in enumerate(bars):
            if f.ny_date[k] != day:
                day, c7, c8, cur = f.ny_date[k], None, None, None
            m = f.ny_min[k]
            for which, start in (("c7", 420), ("c8", 480)):
                if start <= m < start + 60:
                    cand = c7 if which == "c7" else c8
                    cand = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} if cand is None else {**cand, "h": max(cand["h"], b["h"]), "l": min(cand["l"], b["l"]), "c": b["c"]}
                    if which == "c7":
                        c7 = cand
                    else:
                        c8 = cand
            if cur is None and m >= 540 and c7 and c8:
                up = c8["c"] > c8["o"] and c8["c"] >= max(c7["o"], c7["c"]) and c8["o"] <= min(c7["o"], c7["c"])
                dn = c8["c"] < c8["o"] and c8["c"] <= min(c7["o"], c7["c"]) and c8["o"] >= max(c7["o"], c7["c"])
                cur = "buy" if up else "sell" if dn else "none"
            out[k] = cur
        return out

    bias = _cached(f, ("bias78",), build)[i]
    c = bars[i]["c"]
    side = "buy" if c > h else "sell"
    if bias != side:
        return None
    rng = h - l
    sd = (c - h + p["sl_frac"] * rng) if side == "buy" else (l - c + p["sl_frac"] * rng)
    return (side, sd, p["rr"] * sd) if sd > 0 else None


def _heikin(bars: Bars) -> list[dict[str, float]]:
    out = []
    for k, b in enumerate(bars):
        hc = (b["o"] + b["h"] + b["l"] + b["c"]) / 4
        ho = (b["o"] + b["c"]) / 2 if k == 0 else (out[-1]["o"] + out[-1]["c"]) / 2
        out.append({"o": ho, "c": hc, "h": max(b["h"], ho, hc), "l": min(b["l"], ho, hc)})
    return out


def heikin_ashi_trend(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """Heikin-Ashi trend ride (591k-view gold video): trend from a long EMA (``trend_ema``, a daily-ish
    filter on hourly bars); enter on the first HA candle of a colour change in the trend direction
    that has no opposite wick; protective stop ``sl_atr`` x ATR; exit when HA colour flips."""
    if i < 2 or f.atr[i] is None:
        return None
    ha = _cached(f, ("ha",), lambda: _heikin(bars))
    ema = _cached(f, ("ema", int(p["trend_ema"])), lambda: indicators.ema([b["c"] for b in bars], int(p["trend_ema"])))
    if ema[i] is None:
        return None
    cur, prev = ha[i], ha[i - 1]
    green, was_green = cur["c"] > cur["o"], prev["c"] > prev["o"]
    tol = 1e-9 * max(1.0, abs(cur["o"]))
    sd = p["sl_atr"] * f.atr[i]
    if green and not was_green and abs(cur["l"] - min(cur["o"], cur["c"])) <= tol and bars[i]["c"] > ema[i]:
        return ("buy", sd, p["tp_atr"] * f.atr[i])
    if not green and was_green and abs(cur["h"] - max(cur["o"], cur["c"])) <= tol and bars[i]["c"] < ema[i]:
        return ("sell", sd, p["tp_atr"] * f.atr[i])
    return None


def heikin_ashi_exit(bars: Bars, f: Features, i: int, p: dict, side: str) -> bool:
    ha = _cached(f, ("ha",), lambda: _heikin(bars))
    green = ha[i]["c"] > ha[i]["o"]
    return (side == "buy" and not green) or (side == "sell" and green)


def _ut_trail(bars: Bars, atr_vals: list, key: float) -> list[float | None]:
    """UT Bot trailing stop (TradingView 'UT Bot Alerts'): an ATR trailing stop that ratchets with
    price and flips side when price closes through it."""
    out: list[float | None] = [None] * len(bars)
    prev = None
    for k, b in enumerate(bars):
        a = atr_vals[k]
        if a is None:
            continue
        loss, c = key * a, b["c"]
        pc = bars[k - 1]["c"] if k else c
        if prev is None:
            t = c - loss
        elif c > prev and pc > prev:
            t = max(prev, c - loss)
        elif c < prev and pc < prev:
            t = min(prev, c + loss)
        elif c > prev:
            t = c - loss
        else:
            t = c + loss
        out[k] = prev = t
    return out


def ut_bot(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """UT Bot Alerts (popular on reels): buy when the close crosses above the ATR trailing stop,
    sell when it crosses below. Stop at the trailing stop; target ``rr`` x stop; exit on the
    opposite cross."""
    trail = _cached(f, ("ut", p["key"], int(p["atr_len"])),
                    lambda: _ut_trail(bars, indicators.atr(bars, int(p["atr_len"])), p["key"]))
    if i < 2 or trail[i] is None or trail[i - 1] is None:
        return None
    if not (p["sess_start"] <= f.ny_min[i] < p["sess_end"]):
        return None
    c, pc = bars[i]["c"], bars[i - 1]["c"]
    if pc <= trail[i - 1] and c > trail[i]:
        sd = c - trail[i]
        return ("buy", sd, p["rr"] * sd) if sd > 0 else None
    if pc >= trail[i - 1] and c < trail[i]:
        sd = trail[i] - c
        return ("sell", sd, p["rr"] * sd) if sd > 0 else None
    return None


def ut_bot_exit(bars: Bars, f: Features, i: int, p: dict, side: str) -> bool:
    trail = f.cache.get(("ut", p["key"], int(p["atr_len"])))
    if not trail or trail[i] is None:
        return False
    c = bars[i]["c"]
    return (side == "buy" and c < trail[i]) or (side == "sell" and c > trail[i])


def pdhl_rejection(bars: Bars, f: Features, i: int, p: dict) -> Signal:
    """'V-pattern' reel (NAS100): mark the previous New York day's high and low. During the session,
    when a candle pokes through one of them and closes back inside (a rejection candle), trade back
    toward the other side. Only the second touch counts if ``retest_only``. Stop beyond the wick
    (+ pad); target ``rr`` x stop (or the opposite level, whichever is nearer)."""
    a = f.atr[i]
    if a is None or not (p["sess_start"] <= f.ny_min[i] < p["sess_end"]) or f.ny_weekday[i] > 4:
        return None

    def build():
        prev_hl = [None] * len(bars)
        day, hi, lo, last = None, None, None, None
        for k, b in enumerate(bars):
            if f.ny_date[k] != day:
                if day is not None and hi is not None:
                    last = (hi, lo)
                day, hi, lo = f.ny_date[k], b["h"], b["l"]
            else:
                hi, lo = max(hi, b["h"]), min(lo, b["l"])
            prev_hl[k] = last
        return prev_hl

    prev = _cached(f, ("pdhl",), build)[i]
    if not prev:
        return None
    H, L = prev
    b = bars[i]
    pad = p["sl_pad_atr"] * a

    def touches(k, level, up):
        x = bars[k]
        return x["h"] >= level if up else x["l"] <= level

    for up, level, other in ((True, H, L), (False, L, H)):
        rejected = (b["h"] > level and b["c"] < level) if up else (b["l"] < level and b["c"] > level)
        if not rejected:
            continue
        if p.get("retest_only"):
            k, seen = i - 1, False
            while k >= 0 and f.ny_date[k] == f.ny_date[i]:
                if touches(k, level, up):
                    seen = True
                    break
                k -= 1
            if not seen:
                continue
        if up:
            sd = b["h"] - b["c"] + pad
            td = min(p["rr"] * sd, max(b["c"] - other, 0.5 * sd))
            return ("sell", sd, td)
        sd = b["c"] - b["l"] + pad
        td = min(p["rr"] * sd, max(other - b["c"], 0.5 * sd))
        return ("buy", sd, td)
    return None


STRATEGIES: dict[str, dict[str, Any]] = {
    "trend_pullback": {
        "fn": trend_pullback, "label": "Trend pullback (EMA 20/50/200)",
        "params": {"sl_atr": 1.5, "tp_atr": 2.0, "rsi_max_long": 50, "session_start": 7, "session_end": 20, "max_bars": 48},
    },
    "asian_breakout": {
        "fn": asian_breakout, "label": "Asian-range breakout at London open",
        "params": {"buffer_atr": 0.1, "max_range_atr": 6.0, "sl_cap_atr": 3.0, "rr": 1.5, "entry_until": 12, "max_bars": 0},
    },
    "asian_sweep_fade": {
        "fn": asian_sweep_fade, "label": "Fade failed Asian-range break",
        "params": {"sweep_atr": 0.3, "sl_pad_atr": 0.3, "entry_until": 12, "max_bars": 32},
    },
    "asia_session_drift": {
        "fn": asia_session_drift, "label": "Asian-hours drift (long from the 18:00 New York reopen, ~9 hours)",
        "params": {"ny_signal_hour": 17, "max_bars": 8, "sl_atr": 3.0, "tp_atr": 20.0, "trend_filter": 0, "flat_hour": -1},
    },
    "ny_orb": {
        "fn": ny_orb, "label": "New York opening-range breakout (09:30)",
        "params": {"or_start": 570, "or_minutes": 15, "sl_frac": 1.0, "rr": 1.0, "entry_until": 690,
                   "max_range_atr": 0, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "orb_fvg": {
        "fn": orb_fvg, "label": "Opening range + fair-value gap (YouTube 1-minute scalp)",
        "params": {"or_minutes": 15, "rr": 2.0, "entry_until": 690, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "supply_demand": {
        "fn": supply_demand, "label": "Supply/demand zone retest (YouTube US30 strategy)",
        "params": {"base_atr": 0.5, "impulse_atr": 1.5, "lookback": 48, "sl_pad_atr": 0.1, "rr": 2.0, "trend_filter": 0,
                   "sess_start": 570, "sess_end": 900, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "three_line_strike": {
        "fn": three_line_strike, "label": "Three-line strike trend scalp (SMMA 21/50/200 + RSI)",
        "params": {"engulf_frac": 0.5, "sl_pad_atr": 0.1, "rr": 1.5, "max_bars": 6},
    },
    "range4h_fade": {
        "fn": range4h_fade, "label": "4-hour range false breakout (fade)",
        "params": {"rr": 2.0, "entry_until": 900, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "orb_retest": {
        "fn": orb_retest, "label": "9:30 candle break and retest",
        "params": {"or_minutes": 5, "rr": 2.0, "sl_pad_atr": 0.05, "entry_until": 660, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "fib_pullback": {
        "fn": fib_pullback, "label": "Fibonacci 0.5-0.618 pullback in trend",
        "params": {"lookback": 30, "min_swing_atr": 3.0, "sl_pad_atr": 0.1, "sess_start": 180, "sess_end": 690, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "bias_orb": {
        "fn": bias_orb, "label": "7/8 AM engulfing bias + 9:30 opening range",
        "params": {"or_minutes": 15, "sl_frac": 1.0, "rr": 2.0, "entry_until": 660, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "heikin_ashi_trend": {
        "fn": heikin_ashi_trend, "exit": heikin_ashi_exit, "label": "Heikin-Ashi trend ride",
        "params": {"trend_ema": 200, "sl_atr": 3.0, "tp_atr": 50.0, "max_bars": 0, "flat_hour": -1},
    },
    "ut_bot": {
        "fn": ut_bot, "exit": ut_bot_exit, "label": "UT Bot Alerts (ATR trailing stop flip)",
        "params": {"key": 2.0, "atr_len": 10, "rr": 3.0, "sess_start": 180, "sess_end": 900, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "pdhl_rejection": {
        "fn": pdhl_rejection, "label": "Previous-day high/low rejection ('V-pattern' reel)",
        "params": {"retest_only": 1, "sl_pad_atr": 0.1, "rr": 2.0, "sess_start": 570, "sess_end": 840, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "silver_bullet": {
        "fn": silver_bullet, "label": "ICT Silver Bullet (FVG, 10:00-11:00 New York)",
        "params": {"win_start": 600, "win_end": 660, "min_gap_atr": 0.1, "sl_pad_atr": 0.1, "rr": 2.0, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "ema_rsi_scalp": {
        "fn": ema_rsi_scalp, "label": "EMA-200 trend + RSI pullback scalp (1:2)",
        "params": {"rsi_low": 30, "sl_atr": 1.0, "rr": 2.0, "sess_start": 180, "sess_end": 720, "max_bars": 0, "flat_ny": 945, "flat_hour": -1},
    },
    "rsi2_highwin": {
        "fn": rsi2_highwin, "label": "RSI(2) dip, small target / wide stop (high win rate)",
        "params": {"rsi_entry": 10, "tp_atr": 0.5, "sl_atr": 3.0, "session_start": 7, "session_end": 20, "max_bars": 24},
    },
}


@dataclass
class Costs:
    spread: float = 0.30        # price units (USD/oz for gold), paid on every round trip
    commission: float = 0.035   # per unit per side (≈ $7 per 100 oz lot round trip)
    leverage: float = 100.0
    min_qty: float = 1.0        # 0.01 lot of gold = 1 oz
    qty_step: float = 1.0
    swap_long: float = 0.0      # financing cost per unit per night held through 17:00 New York
    swap_short: float = 0.0


def run_intraday(
    bars: Bars,
    strategy: str,
    params: dict[str, Any] | None = None,
    capital: float = 1000.0,
    risk_pct: float = 1.0,
    max_risk_pct: float = 5.0,
    costs: Costs | None = None,
    flat_hour: int | None = 21,
    start_index: int = 0,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}")
    if len(bars) < 300:
        raise ValueError("Need at least 300 bars")
    costs = costs or Costs()
    spec = STRATEGIES[strategy]
    p = {**spec["params"], **(params or {})}
    fn: Callable = spec["fn"]
    f = features(bars)
    max_bars = int(p.get("max_bars") or 0)
    if "flat_hour" in p:  # a strategy can set its own daily flat time; negative = hold overnight
        flat_hour = None if p["flat_hour"] is None or p["flat_hour"] < 0 else int(p["flat_hour"])
    flat_ny = p.get("flat_ny")  # minutes after midnight New York; close at the bar that ends there
    flat_ny = None if flat_ny is None or flat_ny < 0 else int(flat_ny)

    equity = cash = float(capital)
    pos: dict[str, Any] | None = None
    pending: tuple | None = None
    trades: list[dict[str, Any]] = []
    curve: list[tuple[int, float]] = []
    skipped = 0
    blown = False
    spread = costs.spread

    def close(i: int, price_bid: float, reason: str) -> None:
        nonlocal cash, pos
        exit_px = price_bid if pos["side"] == "buy" else price_bid + spread
        pnl = (exit_px - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "buy" else -1) - costs.commission * pos["qty"]
        cash += pnl
        pnl -= pos.get("swap", 0.0)  # already taken from cash night by night
        trades.append({
            "entryTime": bars[pos["i"]]["t"], "exitTime": bars[i]["t"], "side": pos["side"], "qty": pos["qty"],
            "entry": round(pos["entry"], 3), "exit": round(exit_px, 3), "sl": round(pos["sl"], 3), "tp": round(pos["tp"], 3),
            "pnl": round(pnl, 2), "r": round(pnl / pos["risk"], 3) if pos["risk"] else 0.0, "reason": reason, "bars": i - pos["i"],
            "u": round(pos["risk"] / pos["qty"], 4) if pos["qty"] else 0.0,
            "swap": round(pos.get("swap", 0.0), 2),
        })
        pos = None

    for i in range(max(start_index, 1), len(bars)):
        b = bars[i]
        ask_o, ask_h, ask_l = b["o"] + spread, b["h"] + spread, b["l"] + spread
        # 1) enter a pending signal at this bar's open
        if pending and pos is None and not blown:
            side, sd, td = pending
            entry = ask_o if side == "buy" else b["o"]
            risk_unit = sd + spread  # stop distance plus the cost to get out
            qty = math.floor((cash * risk_pct / 100) / risk_unit / costs.qty_step) * costs.qty_step
            if qty < costs.min_qty:
                qty = costs.min_qty if costs.min_qty * risk_unit <= cash * max_risk_pct / 100 else 0
            max_by_margin = math.floor(cash * costs.leverage / entry / costs.qty_step) * costs.qty_step
            qty = min(qty, max_by_margin)
            if qty >= costs.min_qty:
                sl = entry - sd if side == "buy" else entry + sd
                tp = entry + td if side == "buy" else entry - td
                cash -= costs.commission * qty
                pos = {"side": side, "qty": qty, "entry": entry, "sl": sl, "tp": tp, "i": i, "risk": qty * risk_unit}
            else:
                skipped += 1
                if costs.min_qty * entry / costs.leverage > cash:
                    blown = True
        pending = None
        # 2) manage the open position inside this bar (stop first if both touched)
        if pos:
            if pos["side"] == "buy":
                if b["o"] <= pos["sl"]:
                    close(i, b["o"], "stop (gap)")
                elif b["l"] <= pos["sl"]:
                    close(i, pos["sl"], "stop")
                elif b["o"] >= pos["tp"]:
                    close(i, b["o"], "target (gap)")
                elif b["h"] >= pos["tp"]:
                    close(i, pos["tp"], "target")
            else:
                if ask_o >= pos["sl"]:
                    close(i, b["o"], "stop (gap)")
                elif ask_h >= pos["sl"]:
                    close(i, pos["sl"] - spread, "stop")
                elif ask_o <= pos["tp"]:
                    close(i, b["o"], "target (gap)")
                elif ask_l <= pos["tp"]:
                    close(i, pos["tp"] - spread, "target")
        if pos and f.rollover[i] and (costs.swap_long or costs.swap_short):
            fee = (costs.swap_long if pos["side"] == "buy" else costs.swap_short) * pos["qty"]
            cash -= fee
            pos["swap"] = pos.get("swap", 0.0) + fee
        exit_fn = spec.get("exit")
        if pos and exit_fn and exit_fn(bars, f, i, p, pos["side"]):
            close(i, b["c"], "exit signal")
        if pos and max_bars and i - pos["i"] >= max_bars:
            close(i, b["c"], "time")
        if pos and flat_hour is not None and f.hour[i] >= flat_hour:
            close(i, b["c"], "end of day")
        if pos and flat_ny is not None and f.ny_min[i] + f.step_min >= flat_ny:
            close(i, b["c"], "end of session")
        # 3) mark to market
        unreal = 0.0
        if pos:
            px = b["c"] if pos["side"] == "buy" else b["c"] + spread
            unreal = (px - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "buy" else -1)
        equity = cash + unreal
        if i % 4 == 0 or i == len(bars) - 1:
            curve.append((b["t"], round(equity, 2)))
        if equity <= 0:
            blown = True
        # 4) new signal on this bar's close
        late = flat_ny is not None and f.ny_min[i] + 2 * f.step_min > flat_ny
        if pos is None and not blown and not late and (flat_hour is None or f.hour[i] < flat_hour - 1):
            sig = fn(bars, f, i, p)
            if sig and sig[1] > 0 and sig[2] > 0:
                pending = sig

    if pos:
        close(len(bars) - 1, bars[-1]["c"], "end of test")
        equity = cash
    return summarize(trades, curve, capital, cash, bars, start_index, skipped, blown, p, strategy, costs, risk_pct)


def summarize(trades, curve, capital, final, bars, start_index, skipped, blown, params, strategy, costs, risk_pct) -> dict[str, Any]:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    peak, mdd = capital, 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0)
    t0, t1 = bars[max(start_index, 1)]["t"], bars[-1]["t"]
    years = max((t1 - t0) / (365.25 * 86400), 1e-9)
    streak = worst_streak = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)
    yearly: dict[int, dict[str, float]] = {}
    for t in trades:
        y = datetime.fromtimestamp(t["exitTime"], timezone.utc).year
        row = yearly.setdefault(y, {"year": y, "trades": 0, "wins": 0, "pnl": 0.0})
        row["trades"] += 1
        row["wins"] += t["pnl"] > 0
        row["pnl"] = round(row["pnl"] + t["pnl"], 2)
    months: list[dict[str, Any]] = []
    prev_eq, cur_key, last_eq = capital, None, capital
    for t, e in curve:
        key = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")
        if cur_key and key != cur_key:
            months.append({"month": cur_key, "return": last_eq / prev_eq - 1 if prev_eq > 0 else 0.0})
            prev_eq = last_eq
        cur_key, last_eq = key, e
    if cur_key:
        months.append({"month": cur_key, "return": final / prev_eq - 1 if prev_eq > 0 else 0.0})
    mrets = sorted(m["return"] for m in months)
    step = max(1, len(curve) // 800)
    return {
        "strategy": strategy, "label": STRATEGIES[strategy]["label"], "params": params,
        "period": {"start": datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d"), "end": datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%d"), "years": round(years, 2)},
        "assumptions": {"capital": capital, "riskPct": risk_pct, "spread": costs.spread, "commissionPerUnitSide": costs.commission, "leverage": costs.leverage, "minQty": costs.min_qty},
        "metrics": {
            "trades": len(trades), "winRate": len(wins) / len(trades) if trades else None,
            "profitFactor": gross_win / gross_loss if gross_loss else None,
            "netProfit": round(final - capital, 2), "finalEquity": round(final, 2),
            "totalReturn": final / capital - 1,
            "cagr": (final / capital) ** (1 / years) - 1 if final > 0 and years > 0.25 else None,
            "maxDrawdown": mdd, "avgWin": gross_win / len(wins) if wins else None, "avgLoss": -gross_loss / len(losses) if losses else None,
            "expectancy": (final - capital) / len(trades) if trades else None,
            "avgR": sum(t["r"] for t in trades) / len(trades) if trades else None,
            "tradesPerWeek": len(trades) / (years * 52), "worstLosingStreak": worst_streak,
            "skippedMinSize": skipped, "blown": blown,
            "medianMonth": mrets[len(mrets) // 2] if mrets else None,
            "avgMonth": sum(mrets) / len(mrets) if mrets else None,
            "positiveMonths": sum(1 for r in mrets if r > 0) / len(mrets) if mrets else None,
            "bestMonth": mrets[-1] if mrets else None, "worstMonth": mrets[0] if mrets else None,
            "monthsAbove30pct": sum(1 for r in mrets if r >= 0.30),
        },
        "monthly": months,
        "yearly": [dict(r, winRate=r["wins"] / r["trades"]) for r in sorted(yearly.values(), key=lambda r: r["year"])],
        "curve": curve[::step] + ([curve[-1]] if curve and curve[-1] not in curve[::step] else []),
        "rs": [t["r"] for t in trades],
        "compact": [[t["exitTime"], t["r"], t["u"], 1 if t["side"] == "buy" else -1, t["entry"]] for t in trades],
        "trades": trades[-200:],
    }


def latest_signal(bars: Bars, strategy: str, params: dict[str, Any] | None = None) -> Signal:
    """Signal on the last bar in ``bars`` (callers pass completed bars only)."""
    spec = STRATEGIES[strategy]
    p = {**spec["params"], **(params or {})}
    f = features(bars)
    return spec["fn"](bars, f, len(bars) - 1, p)


# ----------------------------------------------------------------------------- instruments
INSTRUMENTS: dict[str, dict[str, Any]] = {
    # data = Dukascopy symbol for history; live = Yahoo symbol the paper bot trades
    # Swap figures are rough 2025-26 retail estimates per unit per night; check your broker's.
    "XAUUSD": {"data": "XAUUSD", "live": "GC=F", "unit": "oz",
               "costs": Costs(spread=0.30, commission=0.035, leverage=100, min_qty=1.0, qty_step=1.0, swap_long=0.60, swap_short=0.20)},
    "US30": {"data": "USA30IDXUSD", "live": "YM=F", "unit": "$1/point",
             "costs": Costs(spread=2.0, commission=0.0, leverage=100, min_qty=0.1, qty_step=0.1, swap_long=6.0, swap_short=2.0)},
    "NAS100": {"data": "USATECHIDXUSD", "live": "NQ=F", "unit": "$1/point",
               "costs": Costs(spread=1.0, commission=0.0, leverage=100, min_qty=0.1, qty_step=0.1, swap_long=3.0, swap_short=1.0)},
}


MICRO_MIN = {"XAUUSD": 0.01, "US30": 0.01, "NAS100": 0.01}


def instrument_costs(symbol: str, account_type: str = "standard", spread: float | None = None, leverage: float | None = None) -> Costs:
    """Cost preset for a market; ``micro`` accounts trade 100x smaller minimum sizes (cent/micro lots)."""
    base = INSTRUMENTS[symbol]["costs"]
    c = Costs(**vars(base))
    if account_type == "micro":
        c.min_qty = c.qty_step = MICRO_MIN[symbol]
    if spread is not None:
        c.spread = spread
    if leverage is not None:
        c.leverage = leverage
    return c


def goal_plan(result: dict, target_monthly: float, capital: float, costs: Costs,
              risk_levels=(0.25, 0.5, 1, 1.5, 2, 3, 4, 5, 7.5, 10, 15, 20, 25, 30)) -> dict[str, Any]:
    """What it would take to reach ``target_monthly`` (0.30 = 30%) with this strategy's own trades."""
    rs = result.get("rs") or []
    m = result["metrics"]
    tpm = m["trades"] / max(1e-9, result["period"]["years"] * 12)
    sims = risk_simulation(rs, tpm, risk_levels) if rs else []
    needed = next((x for x in sims if x["medianMonthly"] >= target_monthly), None)
    best = max(sims, key=lambda x: x["medianMonthly"], default=None)
    stops = sorted(abs(t["entry"] - t["sl"]) + costs.spread for t in result.get("trades", []) if t.get("sl"))
    typical_stop = stops[len(stops) // 2] if stops else None
    min_risk_pct = costs.min_qty * typical_stop / capital * 100 if typical_stop else None
    if not rs:
        verdict = "This strategy made no trades on this data."
    elif (m.get("avgR") or 0) <= 0:
        verdict = "This strategy lost money after costs, so no amount of risk reaches the target; more risk only loses faster."
    elif needed is None:
        verdict = (f"Not reachable: the best typical month was {best['medianMonthly']:.1%} at {best['riskPct']}% risk per trade, "
                   f"where the account halves in {best['probHalved']:.0%} of simulated years.")
    elif needed["probHalved"] >= 0.25 or needed["probRuin"] >= 0.02:
        verdict = (f"Possible only at {needed['riskPct']}% risk per trade: the account halves in {needed['probHalved']:.0%} "
                   f"and is wiped out in {needed['probRuin']:.0%} of simulated years. Not a sound plan.")
    else:
        verdict = f"Reachable at about {needed['riskPct']}% risk per trade with a {needed['probHalved']:.0%} chance of halving in a year."
    if min_risk_pct and min_risk_pct > 5:
        verdict += f" With {capital:,.0f} on this account type the smallest position already risks about {min_risk_pct:.0f}% per trade."
    return {
        "targetMonthly": target_monthly, "targetYearly": (1 + target_monthly) ** 12 - 1,
        "tradesPerMonth": tpm, "avgR": m.get("avgR"), "needed": needed, "best": best, "table": sims,
        "typicalStop": typical_stop, "minRiskPctAtCapital": min_risk_pct, "verdict": verdict,
    }


def risk_simulation(r_multiples: Sequence[float], trades_per_month: float, risk_levels=(0.5, 1, 2, 3, 5, 10, 20),
                    months: int = 12, sims: int = 2000, seed: int = 7) -> list[dict[str, Any]]:
    """Bootstrap the strategy's own trade results (in R, costs included) at different risk per
    trade. Returns, per risk level, the median monthly return and how often the account halved
    or was wiped out (below 10% of the start) within ``months``. Ignores minimum-size limits."""
    import random
    rng = random.Random(seed)
    rs = list(r_multiples)
    n_trades = max(1, round(trades_per_month * months))
    out = []
    for risk in risk_levels:
        f = risk / 100
        monthly, halved, ruined = [], 0, 0
        for _ in range(sims):
            eq, peak, hit_half, dead = 1.0, 1.0, False, False
            for _k in range(n_trades):
                eq *= max(0.0, 1 + f * rng.choice(rs))
                peak = max(peak, eq)
                if eq <= 0.5 * peak:
                    hit_half = True
                if eq < 0.1:
                    dead = True
                    break
            halved += hit_half
            ruined += dead
            monthly.append((max(eq, 1e-9)) ** (1 / months) - 1 if not dead else -1.0)
        monthly.sort()
        out.append({"riskPct": risk, "medianMonthly": monthly[len(monthly) // 2], "p10Monthly": monthly[len(monthly) // 10],
                    "probHalved": halved / sims, "probRuin": ruined / sims})
    return out
