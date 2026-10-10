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
from dataclasses import dataclass
from datetime import datetime, timezone
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


def features(bars: Bars) -> Features:
    closes = [b["c"] for b in bars]
    hour, day, wd = [], [], []
    for b in bars:
        d = datetime.fromtimestamp(b["t"], timezone.utc)
        hour.append(d.hour)
        day.append(b["t"] // 86400)
        wd.append(d.weekday())
    step = bars[1]["t"] - bars[0]["t"] if len(bars) > 1 else 3600
    ny = [datetime.fromtimestamp(b["t"] + step, NY) for b in bars]
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
    )


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
        trades.append({
            "entryTime": bars[pos["i"]]["t"], "exitTime": bars[i]["t"], "side": pos["side"], "qty": pos["qty"],
            "entry": round(pos["entry"], 3), "exit": round(exit_px, 3), "sl": round(pos["sl"], 3), "tp": round(pos["tp"], 3),
            "pnl": round(pnl, 2), "r": round(pnl / pos["risk"], 3) if pos["risk"] else 0.0, "reason": reason, "bars": i - pos["i"],
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
        if pos and max_bars and i - pos["i"] >= max_bars:
            close(i, b["c"], "time")
        if pos and flat_hour is not None and f.hour[i] >= flat_hour:
            close(i, b["c"], "end of day")
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
        if pos is None and not blown and (flat_hour is None or f.hour[i] < flat_hour - 1):
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
        },
        "yearly": [dict(r, winRate=r["wins"] / r["trades"]) for r in sorted(yearly.values(), key=lambda r: r["year"])],
        "curve": curve[::step] + ([curve[-1]] if curve and curve[-1] not in curve[::step] else []),
        "trades": trades[-200:],
    }


def latest_signal(bars: Bars, strategy: str, params: dict[str, Any] | None = None) -> Signal:
    """Signal on the last bar in ``bars`` (callers pass completed bars only)."""
    spec = STRATEGIES[strategy]
    p = {**spec["params"], **(params or {})}
    f = features(bars)
    return spec["fn"](bars, f, len(bars) - 1, p)
