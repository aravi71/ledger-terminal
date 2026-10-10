"""Technical indicators computed locally from OHLCV bars."""

from __future__ import annotations

from typing import Any, Sequence


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period

    def _val(g: float, l: float) -> float:
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

    out[period] = _val(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = _val(avg_g, avg_l)
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float | None]]:
    ef, es = ema(values, fast), ema(values, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_part = ema([v for v in line[start:] if v is not None], signal)
    sig: list[float | None] = [None] * start + sig_part
    hist = [l - s if l is not None and s is not None else None for l, s in zip(line, sig)]
    return {"macd": line, "signal": sig, "hist": hist}


def bollinger(values: Sequence[float], period: int = 20, k: float = 2.0) -> dict[str, list[float | None]]:
    mid = sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in window) / period) ** 0.5  # type: ignore[operator]
        upper[i], lower[i] = m + k * sd, m - k * sd  # type: ignore[operator]
    return {"mid": mid, "upper": upper, "lower": lower}


def atr(bars: Sequence[dict[str, float]], period: int = 14) -> list[float | None]:
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    out: list[float | None] = [None] * len(trs)
    if len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _last(series: list[float | None]) -> float | None:
    return next((v for v in reversed(series) if v is not None), None)


def summary(bars: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Latest indicator values plus plain-language signals."""
    closes = [b["c"] for b in bars]
    if len(closes) < 2:
        return {"error": "not enough history"}
    price = closes[-1]
    s20, s50, s200 = _last(sma(closes, 20)), _last(sma(closes, 50)), _last(sma(closes, 200))
    r = _last(rsi(closes))
    m = macd(closes)
    bb = bollinger(closes)
    a = _last(atr(bars))
    signals = []
    if s50 is not None:
        signals.append(f"price {'above' if price > s50 else 'below'} 50-day SMA")
    if s200 is not None:
        signals.append(f"price {'above' if price > s200 else 'below'} 200-day SMA")
    if s50 is not None and s200 is not None:
        signals.append("50/200 trend: " + ("bullish (golden-cross regime)" if s50 > s200 else "bearish (death-cross regime)"))
    if r is not None:
        signals.append("RSI overbought (>70)" if r > 70 else "RSI oversold (<30)" if r < 30 else "RSI neutral")
    ml, ms = _last(m["macd"]), _last(m["signal"])
    if ml is not None and ms is not None:
        signals.append("MACD above signal" if ml > ms else "MACD below signal")
    window = closes[-252:]
    return {
        "price": price,
        "sma20": s20,
        "sma50": s50,
        "sma200": s200,
        "rsi14": r,
        "macd": ml,
        "macdSignal": ms,
        "macdHist": _last(m["hist"]),
        "bbUpper": _last(bb["upper"]),
        "bbLower": _last(bb["lower"]),
        "atr14": a,
        "return1m": (price / closes[-22] - 1) * 100 if len(closes) > 22 else None,
        "return3m": (price / closes[-64] - 1) * 100 if len(closes) > 64 else None,
        "returnPeriod": (price / window[0] - 1) * 100,
        "periodHigh": max(window),
        "periodLow": min(window),
        "signals": signals,
    }
