import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import agent as agent_mod
from app.agent import RESEARCH_TOOLS, TRADING_TOOLS, AgentError
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path, market_mode="demo")
    with TestClient(app) as c:
        yield c


def test_status_and_market_endpoints(client):
    s = client.get("/api/status").json()
    assert s["marketData"] == "demo" and s["accounts"] == ["paper-main", "paper-gold", "paper-nas100", "paper-us30"]
    assert s["ai"]["configured"] is False
    assert client.get("/api/quote/AAPL").json()["symbol"] == "AAPL"
    assert len(client.get("/api/history/AAPL?range=6mo").json()["bars"]) > 100
    t = client.get("/api/technicals/AAPL").json()
    assert "rsi14" in t["summary"] and len(t["series"]["sma50"]) == len(t["series"]["t"])
    assert client.get("/api/search?q=apple").json()[0]["symbol"] == "AAPL"
    assert client.get("/api/news?symbol=AAPL").json()
    qs = client.get("/api/quotes?symbols=AAPL,BAD SYMBOL").json()
    assert qs[0]["symbol"] == "AAPL" and "error" in qs[1]
    assert client.get("/api/quote/..%2Fetc").status_code in (404, 502)


def test_trade_flow_over_http(client):
    r = client.post("/api/accounts/paper-main/stage", json={"symbol": "msft", "side": "buy", "qty": 2, "rationale": "test"})
    assert r.status_code == 200 and r.json()["order"]["symbol"] == "MSFT"
    c = client.post("/api/accounts/paper-main/commit", json={"message": "starter MSFT"}).json()

    # push needs explicit confirmation
    assert client.post("/api/accounts/paper-main/push", json={"hash": c["hash"]}).status_code == 400
    out = client.post("/api/accounts/paper-main/push", json={"hash": c["hash"], "confirm": True}).json()
    assert out["commits"][0]["status"] == "pushed"

    acct = client.get("/api/accounts/paper-main").json()
    assert acct["account"]["positions"][0]["symbol"] == "MSFT"
    assert client.get(f"/api/accounts/paper-main/commits/{c['hash']}").json()["message"] == "starter MSFT"
    assert client.get("/api/accounts/paper-main/risk").json()["positions"] == 1
    assert any(e["type"] == "trade.push" for e in client.get("/api/events").json())


def test_validation_errors(client):
    assert client.post("/api/accounts/paper-main/stage", json={"symbol": "AAPL", "side": "buy", "qty": -1}).status_code == 422
    assert client.post("/api/accounts/paper-main/stage", json={"symbol": "AAPL", "side": "buy", "qty": 1, "type": "limit"}).status_code == 422
    assert client.post("/api/accounts/nope/commit", json={"message": "x"}).status_code == 400
    assert client.post("/api/dcf", json={"freeCashFlow": 1, "growthRate": 0.1, "discountRate": 0.02, "terminalGrowth": 0.03, "sharesOutstanding": 1}).status_code == 400


def test_agent_endpoints_report_missing_credentials(client):
    r = client.post("/api/agent/chat", json={"message": "hi"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert client.post("/api/agent/panel", json={"symbol": "AAPL"}).status_code == 503


def test_watchlist_and_heartbeat_config(client):
    assert client.put("/api/watchlist", json={"symbols": ["aapl", "AAPL", "spy"]}).json()["symbols"] == ["AAPL", "SPY"]
    assert client.put("/api/heartbeat", json={"enabled": True, "intervalMinutes": 30}).json()["enabled"] is True
    assert client.put("/api/heartbeat", json={"enabled": True, "intervalMinutes": 1}).status_code == 422


def test_login_required_when_password_set(tmp_path):
    app = create_app(data_dir=tmp_path, market_mode="demo", auth=("admin", "s3cret"))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # open for load balancers
        r = c.get("/api/status")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.post("/api/accounts/paper-main/push", json={"confirm": True}).status_code == 401
        assert c.get("/api/status", auth=("admin", "s3cret")).status_code == 200


# --- agent internals without calling Claude -------------------------------------------

def test_agent_has_no_push_tool():
    names = {t["name"] for t in RESEARCH_TOOLS + TRADING_TOOLS}
    assert not any("push" in n for n in names)
    for t in RESEARCH_TOOLS + TRADING_TOOLS:
        assert t["input_schema"]["additionalProperties"] is False


def test_agent_tools_execute(client):
    agent = client.app.state.agent
    run = lambda coro: asyncio.run(coro)
    q = run(agent.run_tool("get_quote", {"symbols": ["AAPL", "???"]}, True))
    assert q[0]["symbol"] == "AAPL" and "error" in q[1]
    staged = run(agent.run_tool("stage_order", {"account_id": "paper-main", "symbol": "SPY", "side": "buy", "qty": 1, "order_type": "market", "limit_price": None, "rationale": "r"}, True))
    assert staged["staged"]["source"] == "agent"
    committed = run(agent.run_tool("commit_staged", {"account_id": "paper-main", "message": "agent idea"}, True))
    assert committed["status"] == "committed"
    with pytest.raises(AgentError):
        run(agent.run_tool("stage_order", {}, False))  # heartbeat context is read-only


class _FakeMessages:
    """Scripted stand-in for client.beta.messages: one tool call, then a reply."""

    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            block = SimpleNamespace(type="tool_use", id="tu_1", name="get_quote", input={"symbols": ["AAPL"]})
            return SimpleNamespace(content=[block], stop_reason="tool_use")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="AAPL looks fine.")], stop_reason="end_turn")


def test_chat_loop_with_fake_client(tmp_path, monkeypatch):
    fake = _FakeMessages()
    app = create_app(data_dir=tmp_path, market_mode="demo", client_factory=lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    monkeypatch.setattr(agent_mod, "ai_configured", lambda: True)
    with TestClient(app) as c:
        out = c.post("/api/agent/chat", json={"message": "How is AAPL?"}).json()
    assert out == {"reply": "AAPL looks fine.", "tools": ["get_quote"], "stop": "end_turn"}
    first = fake.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    # history: user, assistant(tool_use), user(tool_result), assistant(reply)
    history = fake.calls[1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    tool_result = history[2]["content"][0]
    assert tool_result["type"] == "tool_result" and '"AAPL"' in tool_result["content"]
