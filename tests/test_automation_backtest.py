import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import automation as auto_mod
from app.automation import AutoTrader, bot_ledger, completed_bars, market_open
from app.backtest import run_backtest
from app.brokers import OrderRequest
from app.config import BotConfig
from app.main import create_app
from app.strategies import get_signals


def run(coro):
    return asyncio.run(coro)


def make_bars(closes, start=1_600_000_000):
    return [{"t": start + i * 86400, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1} for i, c in enumerate(closes)]


# --- strategies -------------------------------------------------------------------

@pytest.mark.parametrize("name,params", [("sma_cross", {"fast": 5, "slow": 20}), ("rsi_reversion", {}), ("breakout", {})])
def test_strategies_have_no_lookahead(name, params):
    import math
    bars = make_bars([100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(300)])
    full = get_signals(name, bars, params)
    for k in (50, 120, 299):
        assert get_signals(name, bars[:k], params) == full[:k]


def test_strategy_param_validation():
    bars = make_bars([100] * 50)
    with pytest.raises(ValueError):
        get_signals("sma_cross", bars, {"fast": 50, "slow": 20})
    with pytest.raises(ValueError):
        get_signals("sma_cross", bars, {"fast": 1})
    with pytest.raises(ValueError):
        get_signals("nope", bars)


# --- backtester ----------------------------------------------------------------------

def test_backtest_buy_and_hold_matches_benchmark_and_fills_next_open():
    bars = make_bars([100 + i for i in range(60)])
    r = run_backtest(bars, "buy_and_hold", initial_cash=10_000, allocation=5_000, slippage_bps=0)
    # signal on bar 0 -> buy at bar 1 open (101): floor(5000/101) = 49 shares
    assert r["trades"][0]["entryPrice"] == 101 and r["trades"][0]["qty"] == 49
    assert r["metrics"]["finalEquity"] == pytest.approx(10_000 - 49 * 101 + 49 * 159)
    assert r["metrics"]["totalReturn"] == pytest.approx(r["benchmark"]["totalReturn"])
    assert r["metrics"]["openTrade"] is True and r["metrics"]["trades"] == 0


def test_yearly_breakdown_chains_to_total():
    from datetime import datetime as dt
    start = int(dt(2023, 12, 1, tzinfo=timezone.utc).timestamp())
    bars = make_bars([100 + i * 0.5 for i in range(500)], start=start)
    r = run_backtest(bars, "buy_and_hold", initial_cash=10_000, allocation=10_000, slippage_bps=0)
    years = [y["year"] for y in r["yearly"]]
    assert years == sorted(years) and years[0] == 2023 and r["yearly"][0]["partial"] and not r["yearly"][1]["partial"]
    growth = 1.0
    for y in r["yearly"]:
        growth *= 1 + y["strategy"]
    assert growth - 1 == pytest.approx(r["metrics"]["totalReturn"])
    assert sum(y["strategyPnl"] for y in r["yearly"]) == pytest.approx(r["metrics"]["finalEquity"] - 10_000)


def test_backtest_costs_reduce_returns():
    bars = make_bars([100 + (i % 10) for i in range(200)])
    cheap = run_backtest(bars, "rsi_reversion", {"period": 3, "entry": 30, "exit": 60}, slippage_bps=0)
    costly = run_backtest(bars, "rsi_reversion", {"period": 3, "entry": 30, "exit": 60}, slippage_bps=50, commission=5)
    assert cheap["metrics"]["trades"] > 3
    assert costly["metrics"]["finalEquity"] < cheap["metrics"]["finalEquity"]


def test_backtest_sma_round_trip():
    closes = [100] * 30 + [100 + i for i in range(40)] + [140 - i * 2 for i in range(40)]
    r = run_backtest(make_bars(closes), "sma_cross", {"fast": 5, "slow": 20}, slippage_bps=0)
    closed = [t for t in r["trades"] if not t["open"]]
    assert len(closed) == 1 and closed[0]["exitDate"] > closed[0]["entryDate"]
    assert r["metrics"]["winRate"] in (0.0, 1.0)


# --- market hours / bars ---------------------------------------------------------------

def test_market_hours():
    sat = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    mon_open = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)   # 11:00 ET
    mon_late = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)   # 17:00 ET
    assert market_open("BTC-USD", sat) is True
    assert market_open("SPY", sat) is False
    assert market_open("SPY", mon_open) is True
    assert market_open("SPY", mon_late) is False
    assert market_open("EURUSD=X", mon_late) is True
    assert market_open("^NSEI", datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)) is True    # 10:30 IST
    assert market_open("RELIANCE.NS", mon_open) is False                                       # 20:30 IST


def test_completed_bars_drops_today():
    now = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
    today = int(datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc).timestamp())
    bars = [{"t": today - 86400, "c": 1}, {"t": today, "c": 2}]
    assert completed_bars("SPY", bars, now) == bars[:1]


# --- bots -------------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path, market_mode="demo")
    trader: AutoTrader = app.state.autotrader
    cfg = app.state.config
    auto = cfg.automation
    auto.bots = [BotConfig(id="t1", enabled=True, strategy="sma_cross", params={"fast": 5, "slow": 20}, symbols=["BTC-USD"], allocationPerSymbol=3000, maxDailyLossPct=5)]
    cfg.save("automation.json", auto)
    signal = {"v": 1}
    monkeypatch.setattr(auto_mod, "get_signals", lambda name, bars, params=None: [signal["v"]] * len(bars))
    uta = app.state.utas.get("paper-main")
    uta.cfg.guards.cooldownSeconds = 0
    return app, trader, uta, signal


def test_strategy_bot_buys_holds_and_sells_only_its_own(env):
    app, trader, uta, signal = env
    # a position the user opened by hand must survive the bot's exit
    run(uta.stage(OrderRequest(symbol="BTC-USD", side="buy", qty=0.01)))
    uta.commit("manual btc")
    run(uta.push())
    manual_staged = run(uta.stage(OrderRequest(symbol="SPY", side="buy", qty=1)))

    out = run(trader.run_bot("t1"))
    assert out["actions"][0]["action"] == "buy" and out["actions"][0]["status"] == "pushed"
    c = uta.state["commits"][-1]
    assert c["author"] == "bot:t1" and c["approver"] == "auto:t1"
    assert [s["id"] for s in uta.state["staged"]] == [manual_staged["id"]]  # not swept into the bot's commit
    bot_qty = bot_ledger(uta, "t1")["holdings"]["BTC-USD"]["qty"]
    assert bot_qty == pytest.approx(3000 / run(app.state.utas.market.quote("BTC-USD"))["price"], rel=1e-3)

    assert run(trader.run_bot("t1"))["actions"][0]["action"] == "hold"

    signal["v"] = 0
    out = run(trader.run_bot("t1"))
    assert out["actions"][0]["action"] == "sell" and out["actions"][0]["qty"] == pytest.approx(bot_qty)
    pos = {p["symbol"]: p["qty"] for p in run(uta.broker.account())["positions"]}
    assert pos["BTC-USD"] == pytest.approx(0.01)  # the manual position is untouched
    assert bot_ledger(uta, "t1")["holdings"] == {}


def test_kill_switch_and_scheduler(env):
    app, trader, uta, _ = env
    cfg = app.state.config
    auto = cfg.automation
    auto.killSwitch = True
    cfg.save("automation.json", auto)
    run(trader.tick())
    assert not [c for c in uta.state["commits"] if c["author"] == "bot:t1"]
    with pytest.raises(Exception):
        run(trader.run_bot("t1"))
    auto.killSwitch = False
    cfg.save("automation.json", auto)
    run(trader.tick())
    assert [c for c in uta.state["commits"] if c["author"] == "bot:t1"]


def test_daily_loss_limit_halts_until_resumed(env, monkeypatch):
    app, trader, uta, _ = env
    run(trader.run_bot("t1"))  # records start-of-day equity
    real = uta.broker.account

    async def crashed():
        acct = await real()
        return {**acct, "equity": acct["equity"] * 0.9}

    monkeypatch.setattr(uta.broker, "account", crashed)
    out = run(trader.run_bot("t1"))
    assert out["halted"] is True
    assert trader.bot_state("t1")["halted"] is True
    with pytest.raises(Exception):
        run(trader.run_bot("t1"))
    monkeypatch.setattr(uta.broker, "account", real)
    trader.resume("t1")
    assert "actions" in run(trader.run_bot("t1"))


def test_equity_bot_skips_when_market_closed(env, monkeypatch):
    app, trader, uta, _ = env
    monkeypatch.setattr(auto_mod, "market_open", lambda s, now=None: False)
    cfg = app.state.config
    auto = cfg.automation
    auto.bots[0].symbols = ["SPY"]
    cfg.save("automation.json", auto)
    out = run(trader.run_bot("t1"))
    assert out["actions"] == [{"symbol": "SPY", "action": "skip", "reason": "market closed"}]


def test_ai_autopilot_pushes_allowed_and_reverts_others(env, monkeypatch):
    app, trader, uta, _ = env
    cfg = app.state.config
    auto = cfg.automation
    auto.bots = [BotConfig(id="ai1", type="ai", enabled=True, symbols=["BTC-USD"], allocationPerSymbol=2000)]
    cfg.save("automation.json", auto)
    agent = app.state.agent
    monkeypatch.setattr(auto_mod.TradingAgent, "configured", property(lambda self: True))

    async def fake_autopilot(bot_id, prompt, author):
        ctx = {"author": author, "staged": []}
        base = {"account_id": "paper-main", "side": "buy", "order_type": "market", "limit_price": None, "rationale": "r"}
        await agent.run_tool("stage_order", {**base, "symbol": "BTC-USD", "qty": 0.01}, True, ctx)
        await agent.run_tool("commit_staged", {"account_id": "paper-main", "message": "ok"}, True, ctx)
        ctx["staged"] = []
        await agent.run_tool("stage_order", {**base, "symbol": "TSLA", "qty": 1}, True, ctx)
        await agent.run_tool("commit_staged", {"account_id": "paper-main", "message": "not allowed"}, True, ctx)
        return {"reply": "done", "tools": [], "staged": []}

    monkeypatch.setattr(agent, "autopilot", fake_autopilot)
    out = run(trader.run_bot("ai1"))
    statuses = sorted(a.get("status") or a["action"] for a in out["actions"])
    assert statuses == ["pushed", "reverted"]
    assert {c["status"] for c in uta.state["commits"] if c["author"] == "bot:ai1"} == {"pushed", "reverted"}


# --- HTTP ----------------------------------------------------------------------------------

def test_backtest_and_automation_endpoints(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, market_mode="demo")) as c:
        assert {s["id"] for s in c.get("/api/strategies").json()} >= {"sma_cross", "rsi_reversion", "breakout", "buy_and_hold"}
        r = c.post("/api/backtest", json={"symbol": "SPY", "strategy": "sma_cross", "params": {"fast": 50, "slow": 200}, "range": "5y"})
        assert r.status_code == 200
        body = r.json()
        assert body["period"]["bars"] > 1000 and "sharpe" in body["metrics"] and body["curve"]
        assert c.post("/api/backtest", json={"symbol": "SPY", "strategy": "sma_cross", "params": {"fast": 300, "slow": 200}}).status_code == 400

        status = c.get("/api/automation").json()
        assert status["killSwitch"] is False and {b["id"] for b in status["bots"]} == {"spy-trend", "btc-breakout", "ai-autopilot"}
        bad = {"id": "x1", "strategy": "sma_cross", "symbols": [], "account": "paper-main"}
        assert c.put("/api/automation/bots/x1", json=bad).status_code == 400
        good = {**bad, "symbols": ["btc-usd"], "strategy": "buy_and_hold"}
        assert c.put("/api/automation/bots/x1", json=good).json()["symbols"] == ["BTC-USD"]
        out = c.post("/api/automation/bots/x1/run").json()
        assert out["actions"][0]["action"] == "buy"
        assert c.post("/api/automation/kill", json={"on": True}).json()["killSwitch"] is True
        assert c.post("/api/automation/bots/x1/run").status_code == 400
        assert c.delete("/api/automation/bots/x1").json()["ok"] is True
