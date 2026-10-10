import asyncio
import time

import pytest

from app.brokers import OrderRequest, PaperBroker
from app.config import Config, GuardConfig
from app.events import EventLog
from app.guards import GuardContext, check_order
from app.market import DemoMarketData
from app.uta import TradingError, UTAManager


def run(coro):
    return asyncio.run(coro)


# --- guards -------------------------------------------------------------------

def test_guards_pass_and_fail():
    g = GuardConfig(maxOrderValue=1000, maxPositionValue=1500, cooldownSeconds=60, maxOrdersPerDay=2, symbolWhitelist=["aapl"])
    ok = OrderRequest(symbol="AAPL", side="buy", qty=5)
    assert check_order(ok, 100, g, GuardContext(now=1000)) == []

    big = OrderRequest(symbol="AAPL", side="buy", qty=20)
    assert any(v.startswith("maxOrderValue") for v in check_order(big, 100, g, GuardContext(now=1000)))

    other = OrderRequest(symbol="TSLA", side="buy", qty=1)
    assert any(v.startswith("symbolWhitelist") for v in check_order(other, 100, g, GuardContext(now=1000)))

    pos = check_order(ok, 100, g, GuardContext(position_qty=12, now=1000))
    assert any(v.startswith("maxPositionValue") for v in pos)

    cool = check_order(ok, 100, g, GuardContext(last_trade_ts={"AAPL": 990}, now=1000))
    assert any(v.startswith("cooldownSeconds") for v in cool)

    daily = check_order(ok, 100, g, GuardContext(orders_today=2, now=1000))
    assert any(v.startswith("maxOrdersPerDay") for v in daily)

    short = OrderRequest(symbol="AAPL", side="sell", qty=3)
    assert any(v.startswith("allowShort") for v in check_order(short, 100, g, GuardContext(position_qty=1, now=1000)))


def test_reducing_an_oversized_position_is_allowed():
    g = GuardConfig(maxPositionValue=1000, maxOrderValue=None)
    sell = OrderRequest(symbol="X", side="sell", qty=5)
    assert check_order(sell, 100, g, GuardContext(position_qty=20, now=0)) == []


def test_limit_order_requires_price():
    with pytest.raises(ValueError):
        OrderRequest(symbol="AAPL", side="buy", qty=1, type="limit")


# --- paper broker ----------------------------------------------------------------

def test_paper_broker_fills_and_rejects(tmp_path):
    b = PaperBroker(tmp_path / "b.json", DemoMarketData(), starting_cash=10_000, slippage_bps=0)
    r = run(b.place_order(OrderRequest(symbol="AAPL", side="buy", qty=10)))
    assert r["status"] == "filled"
    acct = run(b.account())
    assert acct["positions"][0]["qty"] == 10
    assert acct["cash"] == pytest.approx(10_000 - 10 * r["avgFillPrice"])

    too_many = run(b.place_order(OrderRequest(symbol="AAPL", side="sell", qty=11)))
    assert too_many["status"] == "rejected" and "short selling disabled" in too_many["message"]

    broke = run(b.place_order(OrderRequest(symbol="BTC-USD", side="buy", qty=5)))
    assert broke["status"] == "rejected" and "Insufficient cash" in broke["message"]

    # state persists across instances
    b2 = PaperBroker(tmp_path / "b.json", DemoMarketData())
    assert run(b2.account())["positions"][0]["symbol"] == "AAPL"


def test_paper_limit_order_rests_then_cancels(tmp_path):
    m = DemoMarketData()
    b = PaperBroker(tmp_path / "b.json", m, starting_cash=10_000)
    px = run(m.quote("MSFT"))["price"]
    r = run(b.place_order(OrderRequest(symbol="MSFT", side="buy", qty=1, type="limit", limitPrice=round(px * 0.5, 2))))
    assert r["status"] == "accepted"
    assert len(run(b.account())["openOrders"]) == 1
    assert run(b.reconcile()) == []
    assert run(b.cancel_order(r["brokerOrderId"]))["cancelled"] is True


# --- trading-as-git -------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path):
    cfg = Config(tmp_path)
    events = EventLog(tmp_path / "events.jsonl")
    return UTAManager(cfg, DemoMarketData(), events), events


def test_stage_commit_push_flow(manager):
    mgr, events = manager
    uta = mgr.get("paper-main")
    staged = run(uta.stage(OrderRequest(symbol="AAPL", side="buy", qty=3), "test", source="agent"))
    assert staged["warnings"] == []

    c = uta.commit("buy 3 AAPL starter", author="agent")
    assert len(c["hash"]) == 8 and c["status"] == "committed" and c["parent"] is None
    assert uta.state["staged"] == []

    pushed = run(uta.push(approver="user"))[0]
    assert pushed["status"] == "pushed"
    assert pushed["results"][0]["status"] == "filled"
    assert pushed["snapshot"]["positions"]["AAPL"] == 3
    assert uta.show(c["hash"])["approver"] == "user"
    types = [e["type"] for e in events.recent()]
    assert types[-3:] == ["trade.stage", "trade.commit", "trade.push"]

    # second commit chains to the first
    run(uta.stage(OrderRequest(symbol="MSFT", side="buy", qty=1)))
    c2 = uta.commit("add MSFT")
    assert c2["parent"] == c["hash"]
    assert [l["hash"] for l in uta.log()] == [c2["hash"], c["hash"]]


def test_push_rejects_whole_commit_on_guard_violation(manager):
    mgr, events = manager
    uta = mgr.get("paper-main")
    run(uta.stage(OrderRequest(symbol="AAPL", side="buy", qty=1)))
    big = run(uta.stage(OrderRequest(symbol="NVDA", side="buy", qty=10_000)))
    assert any("maxOrderValue" in w for w in big["warnings"])
    c = uta.commit("one good, one too big")
    out = run(uta.push())[0]
    assert out["status"] == "rejected"
    assert "results" not in out  # nothing reached the broker
    assert run(uta.broker.account())["positions"] == []
    assert events.recent()[-1]["type"] == "trade.rejected"


def test_cooldown_blocks_rapid_repeat(manager):
    mgr, _ = manager
    uta = mgr.get("paper-main")  # default cooldown 60s
    run(uta.stage(OrderRequest(symbol="AAPL", side="buy", qty=1)))
    uta.commit("first")
    assert run(uta.push())[0]["status"] == "pushed"
    run(uta.stage(OrderRequest(symbol="AAPL", side="buy", qty=1)))
    uta.commit("again")
    out = run(uta.push())[0]
    assert out["status"] == "rejected"
    assert any("cooldownSeconds" in v for ch in out["guardResults"] for v in ch["violations"])


def test_revert_and_errors(manager):
    mgr, _ = manager
    uta = mgr.get("paper-main")
    with pytest.raises(TradingError):
        uta.commit("nothing staged")
    run(uta.stage(OrderRequest(symbol="SPY", side="buy", qty=1)))
    with pytest.raises(TradingError):
        uta.commit("   ")
    c = uta.commit("spy")
    assert uta.revert(c["hash"])["status"] == "reverted"
    with pytest.raises(TradingError):
        run(uta.push())
    with pytest.raises(TradingError):
        uta.revert(c["hash"])
    with pytest.raises(TradingError):
        mgr.get("alpaca-paper")  # disabled by default


def test_unstage(manager):
    mgr, _ = manager
    uta = mgr.get("paper-main")
    s = run(uta.stage(OrderRequest(symbol="SPY", side="buy", qty=1)))
    uta.unstage(s["id"])
    assert uta.state["staged"] == []
    with pytest.raises(TradingError):
        uta.unstage("nope")
