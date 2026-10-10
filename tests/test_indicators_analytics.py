import math

import pytest

from app import analytics, indicators


def test_sma_and_ema():
    vals = [1, 2, 3, 4, 5, 6]
    assert indicators.sma(vals, 3) == [None, None, 2, 3, 4, 5]
    e = indicators.ema(vals, 3)
    assert e[2] == 2  # seeded with the SMA
    assert e[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5


def test_rsi_extremes():
    up = list(range(1, 40))
    assert indicators.rsi(up)[-1] == 100.0
    down = list(range(40, 1, -1))
    assert indicators.rsi(down)[-1] == pytest.approx(0.0)


def test_macd_and_summary_shape():
    closes = [100 + math.sin(i / 5) * 5 + i * 0.1 for i in range(300)]
    m = indicators.macd(closes)
    assert len(m["macd"]) == len(m["signal"]) == len(closes)
    bars = [{"t": i, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1} for i, c in enumerate(closes)]
    s = indicators.summary(bars)
    for key in ("sma50", "sma200", "rsi14", "macd", "atr14", "signals"):
        assert s[key] is not None


def test_dcf_matches_hand_calculation():
    r = analytics.dcf(100, 0.10, 2, 0.10, 0.02, net_debt=50, shares_outstanding=10)
    # FCF 110, 121; PVs 100, 100; TV = 121*1.02/0.08 = 1542.75; PV TV = 1542.75/1.21
    assert r["pvOfCashFlows"] == pytest.approx(200)
    assert r["pvOfTerminalValue"] == pytest.approx(1542.75 / 1.21)
    assert r["valuePerShare"] == pytest.approx((200 + 1542.75 / 1.21 - 50) / 10)


def test_dcf_rejects_bad_rates():
    with pytest.raises(ValueError):
        analytics.dcf(100, 0.05, 5, 0.02, 0.03)


def test_risk_metrics():
    assert analytics.max_drawdown([100, 120, 60, 90]) == pytest.approx(0.5)
    rets = [0.01, -0.02] * 30
    assert analytics.historical_var(rets) == pytest.approx(0.02)
    assert analytics.annualized_volatility(rets) > 0


def test_portfolio_risk_combines_positions():
    hist = {
        "A": [{"t": d * 86400, "c": 100 + d} for d in range(60)],
        "B": [{"t": d * 86400, "c": 50 - d * 0.1} for d in range(60)],
    }
    out = analytics.portfolio_risk([{"symbol": "A", "marketValue": 750}, {"symbol": "B", "marketValue": 250}], hist)
    assert out["positions"] == 2
    assert out["weights"]["A"] == pytest.approx(0.75)
    assert out["observations"] == 59
