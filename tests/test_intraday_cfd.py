import asyncio
import math
from datetime import datetime, timezone

import pytest

from app import automation as auto_mod
from app.brokers import CfdPaperBroker, OrderRequest
from app.config import BotConfig
from app.intraday import Costs, STRATEGIES, features, resample, run_intraday
from app.main import create_app


def run(coro):
    return asyncio.run(coro)


def bars_from(prices, start=datetime(2025, 1, 6, tzinfo=timezone.utc), minutes=15, wick=0.5):
    t0 = int(start.timestamp())
    return [{"t": t0 + i * minutes * 60, "o": p, "h": p + wick, "l": p - wick, "c": p, "v": 1} for i, p in enumerate(prices)]


# --- engine mechanics ----------------------------------------------------------------

class _Always:
    """Test strategy: always signal one side with fixed stop/target distances."""

    def __init__(self, side, sd, td):
        self.sig = (side, sd, td)

    def __call__(self, bars, f, i, p):
        return self.sig


@pytest.fixture
def fake_strategy(monkeypatch):
    def install(side, sd, td, max_bars=0):
        monkeypatch.setitem(STRATEGIES, "_test", {"fn": _Always(side, sd, td), "label": "test", "params": {"max_bars": max_bars}})
    return install


def test_long_target_then_stop_with_costs(fake_strategy):
    fake_strategy("buy", 5.0, 5.0)
    # flat at 100 for warm-up, then a bar reaching +6 (target), later a drop
    prices = [100.0] * 300 + [100.0, 106.0, 100.0, 100.0, 94.0] + [100.0] * 5
    bars = bars_from(prices, wick=0.0)
    costs = Costs(spread=0.2, commission=0.0, leverage=100, min_qty=1, qty_step=1)
    r = run_intraday(bars, "_test", capital=10_000, risk_pct=1, costs=costs, flat_hour=None, start_index=299)
    first = r["trades"][0]
    assert first["entry"] == pytest.approx(100.2)  # bought at ask
    # 1% of 10,000 = 100 risk / (5 + 0.2) per unit -> 19 units
    assert first["qty"] == 19
    assert first["reason"] in ("target", "target (gap)")
    assert first["exit"] == pytest.approx(105.2) or first["exit"] == pytest.approx(106.0)


def test_stop_assumed_before_target_in_same_bar(fake_strategy):
    fake_strategy("buy", 2.0, 2.0)
    prices = [100.0] * 301
    bars = bars_from(prices, wick=0.0)
    bars.append({"t": bars[-1]["t"] + 900, "o": 100.0, "h": 103.0, "l": 97.0, "c": 100.0, "v": 1})  # wide bar touches both
    r = run_intraday(bars, "_test", capital=10_000, costs=Costs(spread=0.0, commission=0.0), flat_hour=None, start_index=299)
    hit = [t for t in r["trades"] if t["reason"].startswith(("stop", "target"))][0]
    assert hit["reason"] == "stop" and hit["pnl"] < 0


def test_short_uses_ask_for_stop(fake_strategy):
    fake_strategy("sell", 1.0, 10.0)
    prices = [100.0] * 302
    bars = bars_from(prices, wick=0.0)
    bars.append({"t": bars[-1]["t"] + 900, "o": 100.0, "h": 100.85, "l": 100.0, "c": 100.0, "v": 1})
    # bid high 100.85 + spread 0.3 = ask 101.15 >= stop 101.0 -> stopped
    r = run_intraday(bars, "_test", capital=10_000, costs=Costs(spread=0.3, commission=0.0), flat_hour=None, start_index=299)
    assert any(t["reason"] == "stop" and t["side"] == "sell" for t in r["trades"])


def test_min_size_rule_skips_when_too_risky(fake_strategy):
    fake_strategy("buy", 20.0, 20.0)
    bars = bars_from([2000.0] * 400, wick=0.0)
    # $100 account: 1 oz with a $20 stop risks ~20% > 5% cap -> every trade skipped
    r = run_intraday(bars, "_test", capital=100, risk_pct=1, max_risk_pct=5, costs=Costs(), flat_hour=None, start_index=299)
    assert r["metrics"]["trades"] == 0 and r["metrics"]["skippedMinSize"] > 0
    # allow up to 25% risk and it trades the minimum size
    r2 = run_intraday(bars, "_test", capital=100, risk_pct=1, max_risk_pct=25, costs=Costs(), flat_hour=None, start_index=299)
    assert r2["metrics"]["trades"] > 0 and r2["trades"][0]["qty"] == 1


def test_resample_and_features():
    m1 = bars_from([100 + i * 0.01 for i in range(120)], minutes=1, wick=0.0)
    m15 = resample(m1, 15)
    assert len(m15) == 8 and m15[0]["o"] == 100 and m15[0]["c"] == pytest.approx(100.14)
    f = features(bars_from([100.0] * 200, minutes=60))
    # first 7 hours of each day build the Asian range; known from 07:00 UTC on
    assert f.asia_hi[6] is None and f.asia_hi[7] is not None


@pytest.mark.parametrize("name", list(k for k in STRATEGIES if not k.startswith("_")))
def test_real_strategies_run_and_never_lookahead(name):
    prices = [2000 + 30 * math.sin(i / 40) + 8 * math.sin(i / 7) for i in range(3000)]
    bars = bars_from(prices, minutes=15, wick=2.0)
    r = run_intraday(bars, name, capital=1000, costs=Costs(), start_index=250)
    assert r["metrics"]["trades"] >= 0 and r["period"]["years"] > 0
    f_full = features(bars)
    spec = STRATEGIES[name]
    p = spec["params"]
    for k in (600, 1500, 2900):
        f_cut = features(bars[: k + 1])
        assert spec["fn"](bars[: k + 1], f_cut, k, p) == spec["fn"](bars, f_full, k, p)


# --- CFD broker --------------------------------------------------------------------------

class FixedMarket:
    mode = "demo"

    def __init__(self, price):
        self.price = price

    async def quote(self, symbol):
        return {"symbol": symbol, "price": self.price}


def test_cfd_long_short_brackets_and_margin(tmp_path):
    m = FixedMarket(2000.0)
    b = CfdPaperBroker(tmp_path / "c.json", m, starting_cash=100, leverage=100, spread=0.4, commission=0.0)
    # 1 oz needs 20 margin: fine; 6 oz needs 120: rejected
    assert run(b.place_order(OrderRequest(symbol="GC=F", side="buy", qty=6)))["status"] == "rejected"
    bad = run(b.place_order(OrderRequest(symbol="GC=F", side="buy", qty=1, stopLoss=2001, takeProfit=2010)))
    assert bad["status"] == "rejected" and "stop-loss must be below" in bad["message"]
    ok = run(b.place_order(OrderRequest(symbol="GC=F", side="buy", qty=1, stopLoss=1995, takeProfit=2005)))
    assert ok["status"] == "filled" and ok["avgFillPrice"] == pytest.approx(2000.2)
    m.price = 2006.0  # bid 2005.8 >= TP
    fills = run(b.reconcile())
    assert fills[0]["reason"] == "take-profit"
    assert run(b.account())["cash"] == pytest.approx(100 + 2005.8 - 2000.2)
    # short with stop-loss above
    run(b.place_order(OrderRequest(symbol="GC=F", side="sell", qty=1, stopLoss=2010, takeProfit=1990)))
    m.price = 2010.5
    assert run(b.reconcile())[0]["reason"] == "stop-loss"
    assert run(b.account())["positions"] == []


def test_cfd_stop_out(tmp_path):
    m = FixedMarket(2000.0)
    b = CfdPaperBroker(tmp_path / "c.json", m, starting_cash=30, leverage=100, spread=0.0, commission=0.0)
    run(b.place_order(OrderRequest(symbol="GC=F", side="buy", qty=1)))
    m.price = 1979.0  # equity 9 <= 50% of 19.79 margin -> stop-out
    assert run(b.reconcile())[-1]["reason"] == "stop-out"


def test_cash_paper_broker_rejects_brackets(tmp_path):
    app = create_app(data_dir=tmp_path, market_mode="demo")
    uta = app.state.utas.get("paper-main")
    r = run(uta.broker.place_order(OrderRequest(symbol="SPY", side="buy", qty=1, stopLoss=1)))
    assert r["status"] == "rejected" and "cfd" in r["message"].lower()


# --- intraday bot -----------------------------------------------------------------------

def test_intraday_bot_enters_with_sl_tp_and_exits(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path, market_mode="demo")
    trader = app.state.autotrader
    cfg = app.state.config
    auto = cfg.automation
    auto.bots = [BotConfig(id="gold-scalp", type="intraday", enabled=True, account="paper-gold", strategy="trend_pullback",
                           timeframe="15m", symbols=["GC=F"], riskPct=1, maxRiskPct=10, respectMarketHours=False)]
    cfg.save("automation.json", auto)
    market = app.state.utas.market
    now = datetime.now(timezone.utc)

    async def history(symbol, rng, interval):
        t_end = int(now.timestamp()) // 900 * 900
        return [{"t": t_end - (400 - i) * 900, "o": 2650.0, "h": 2651.0, "l": 2649.0, "c": 2650.0, "v": 1} for i in range(400)]

    monkeypatch.setattr(market, "history", history)
    monkeypatch.setattr(auto_mod.intraday, "latest_signal", lambda bars, s, p=None: ("buy", 5.0, 10.0))
    monkeypatch.setattr(auto_mod, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: now.replace(hour=10))}))
    out = run(trader.run_bot("gold-scalp"))
    act = out["actions"][0]
    assert act["action"] == "buy" and act["status"] == "pushed"
    uta = app.state.utas.get("paper-gold")
    pos = run(uta.broker.account())["positions"][0]
    assert pos["stopLoss"] < pos["avgPrice"] < pos["takeProfit"]
    # risk check: 1% of 1,000 = 10 / (5 + 0.3) -> 1 oz
    assert pos["qty"] == 1
    # same bar again: no duplicate entry
    assert run(trader.run_bot("gold-scalp"))["actions"][0]["action"] == "hold"


def test_intraday_bot_requires_cfd_account(tmp_path):
    from fastapi.testclient import TestClient
    with TestClient(create_app(data_dir=tmp_path, market_mode="demo")) as c:
        body = {"id": "g1", "type": "intraday", "account": "paper-main", "strategy": "trend_pullback", "symbols": ["GC=F"]}
        assert c.put("/api/automation/bots/g1", json=body).status_code == 400
        body["account"] = "paper-gold"
        assert c.put("/api/automation/bots/g1", json=body).status_code == 200


def test_asia_drift_signals_only_into_the_ny_break():
    from zoneinfo import ZoneInfo
    # hourly bars over two weeks spanning the US DST change (2025-11-02)
    start = datetime(2025, 10, 27, 0, tzinfo=timezone.utc)
    bars = bars_from([3000.0 + (i % 7) for i in range(24 * 14)], start=start, minutes=60, wick=1.0)
    f = features(bars)
    p = STRATEGIES["asia_session_drift"]["params"]
    hits = [i for i in range(250, len(bars)) if STRATEGIES["asia_session_drift"]["fn"](bars, f, i, p)]
    ny = ZoneInfo("America/New_York")
    for i in hits:
        close = datetime.fromtimestamp(bars[i]["t"] + 3600, ny)
        assert close.hour == 17 and close.weekday() <= 3
    # before DST ends the break closes at 21:00 UTC, after it at 22:00 UTC
    utc_hours = {datetime.fromtimestamp(bars[i]["t"] + 3600, timezone.utc).hour for i in hits}
    assert utc_hours <= {21, 22} and hits


def test_intraday_bot_waits_for_reopen(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = create_app(data_dir=tmp_path, market_mode="demo")
    trader, cfg = app.state.autotrader, app.state.config
    auto = cfg.automation
    auto.bots = [BotConfig(id="asia", type="intraday", enabled=True, account="paper-gold", strategy="asia_session_drift",
                           timeframe="60m", symbols=["GC=F"], riskPct=1, maxRiskPct=10, respectMarketHours=False)]
    cfg.save("automation.json", auto)
    ny = ZoneInfo("America/New_York")
    clock = {"now": datetime(2026, 10, 6, 17, 5, tzinfo=ny).astimezone(timezone.utc)}  # Tuesday, in the break
    bar_end = int(datetime(2026, 10, 6, 17, 0, tzinfo=ny).timestamp())

    async def history(symbol, rng, interval):
        return [{"t": bar_end - (400 - i) * 3600, "o": 2650.0, "h": 2651.0, "l": 2649.0, "c": 2650.0, "v": 1} for i in range(400)]

    monkeypatch.setattr(app.state.utas.market, "history", history)
    monkeypatch.setattr(auto_mod.intraday, "latest_signal", lambda bars, s, p=None: ("buy", 15.0, 100.0))
    monkeypatch.setattr(auto_mod, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: clock["now"])}))
    first = run(trader.run_bot("asia"))["actions"][0]
    assert first["action"] == "wait" and first["reason"] == "waiting for reopen"
    assert run(app.state.utas.get("paper-gold").broker.account())["positions"] == []
    clock["now"] = datetime(2026, 10, 6, 18, 2, tzinfo=ny).astimezone(timezone.utc)  # reopened, same last completed bar
    second = run(trader.run_bot("asia"))["actions"][0]
    assert second["action"] == "buy" and second["status"] == "pushed"


def test_mt5_adapter_refuses_without_demo_terminal(monkeypatch):
    """The MT5 adapter never trades unless a demo account is logged in."""
    import asyncio
    import sys
    import types

    from app.brokers import Mt5DemoBroker, OrderRequest

    fake = types.SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0, initialize=lambda *a, **k: True, last_error=lambda: (0, ""),
        account_info=lambda: types.SimpleNamespace(trade_mode=2, login=1, server="XMGlobal-Real"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    b = Mt5DemoBroker()
    b._terminal_running = lambda: True
    res = asyncio.run(b.place_order(OrderRequest(symbol="GC=F", side="buy", qty=1)))
    assert res["status"] == "rejected" and "REAL account" in res["message"]
    st = asyncio.run(b.status())
    assert st["connected"] is False
