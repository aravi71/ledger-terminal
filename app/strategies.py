"""Rule-based strategies shared by the backtester and the live trading bots.

A strategy turns daily bars into a target exposure per bar: 1 = hold a long
position, 0 = flat. ``signals[i]`` may only use bars ``0..i`` (no look-ahead).
The backtester acts on ``signals[i]`` at bar ``i+1``'s open; a live bot acts on
the signal from the last *completed* bar, so both see the same information.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from . import indicators

Bars = Sequence[dict[str, Any]]


def sma_cross(bars: Bars, fast: int = 50, slow: int = 200) -> list[int]:
    if fast >= slow:
        raise ValueError("fast must be shorter than slow")
    closes = [b["c"] for b in bars]
    f, s = indicators.sma(closes, int(fast)), indicators.sma(closes, int(slow))
    return [1 if a is not None and b is not None and a > b else 0 for a, b in zip(f, s)]


def rsi_reversion(bars: Bars, period: int = 14, entry: float = 30, exit: float = 55) -> list[int]:
    if entry >= exit:
        raise ValueError("entry must be below exit")
    r = indicators.rsi([b["c"] for b in bars], int(period))
    out, pos = [], 0
    for v in r:
        if v is not None:
            if pos == 0 and v < entry:
                pos = 1
            elif pos == 1 and v > exit:
                pos = 0
        out.append(pos)
    return out


def breakout(bars: Bars, entry_lookback: int = 20, exit_lookback: int = 10) -> list[int]:
    """Donchian channel: buy a close above the prior N-day high, exit below the prior M-day low."""
    n, m = int(entry_lookback), int(exit_lookback)
    out, pos = [], 0
    for i, b in enumerate(bars):
        if i >= n and pos == 0 and b["c"] > max(x["h"] for x in bars[i - n : i]):
            pos = 1
        elif i >= m and pos == 1 and b["c"] < min(x["l"] for x in bars[i - m : i]):
            pos = 0
        out.append(pos)
    return out


def buy_and_hold(bars: Bars) -> list[int]:
    return [1] * len(bars)


STRATEGIES: dict[str, dict[str, Any]] = {
    "sma_cross": {
        "label": "SMA crossover (trend)",
        "fn": sma_cross,
        "params": {"fast": {"default": 50, "min": 2, "max": 200}, "slow": {"default": 200, "min": 5, "max": 400}},
        "description": "Long while the fast moving average is above the slow one; flat otherwise.",
    },
    "rsi_reversion": {
        "label": "RSI mean reversion",
        "fn": rsi_reversion,
        "params": {"period": {"default": 14, "min": 2, "max": 50}, "entry": {"default": 30, "min": 5, "max": 50}, "exit": {"default": 55, "min": 30, "max": 95}},
        "description": "Buy when RSI drops below the entry level; sell when it recovers above the exit level.",
    },
    "breakout": {
        "label": "Donchian breakout",
        "fn": breakout,
        "params": {"entry_lookback": {"default": 20, "min": 5, "max": 120}, "exit_lookback": {"default": 10, "min": 3, "max": 120}},
        "description": "Buy a close above the prior N-day high; exit on a close below the prior M-day low.",
    },
    "buy_and_hold": {
        "label": "Buy and hold (baseline)",
        "fn": buy_and_hold,
        "params": {},
        "description": "Always long. Use it as the benchmark every other strategy must beat.",
    },
}


def get_signals(name: str, bars: Bars, params: dict[str, Any] | None = None) -> list[int]:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy {name!r}. Options: {list(STRATEGIES)}")
    spec = STRATEGIES[name]
    clean: dict[str, Any] = {}
    for key, rule in spec["params"].items():
        value = (params or {}).get(key, rule["default"])
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if not rule["min"] <= value <= rule["max"]:
            raise ValueError(f"{key} must be between {rule['min']} and {rule['max']}")
        clean[key] = value
    fn: Callable[..., list[int]] = spec["fn"]
    return fn(bars, **clean)


def catalog() -> list[dict[str, Any]]:
    return [{"id": k, "label": v["label"], "description": v["description"], "params": v["params"]} for k, v in STRATEGIES.items()]
