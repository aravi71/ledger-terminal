"""HTTP API + static UI for Ledger Terminal."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, indicators, intraday, strategies
from .agent import AgentError, TradingAgent
from .automation import AutoTrader, is_crypto
from .backtest import run_backtest
from .brokers import OrderRequest
from .config import ROOT, AutomationConfig, BotConfig, Config, HeartbeatConfig, WatchlistConfig, market_data_mode
from .events import EventLog
from .market import MarketDataError, create_market_data, normalize_symbol
from .uta import TradingError, UTAManager

STATIC = ROOT / "static"


class StageBody(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0)
    type: Literal["market", "limit"] = "market"
    limitPrice: float | None = None
    stopLoss: float | None = None
    takeProfit: float | None = None
    rationale: str = ""


class CommitBody(BaseModel):
    message: str


class PushBody(BaseModel):
    hash: str | None = None
    confirm: bool = False


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    channel: str = "web"


class PanelBody(BaseModel):
    symbol: str
    lenses: list[str] | None = None


class BacktestBody(BaseModel):
    symbol: str
    strategy: str
    params: dict[str, float] = Field(default_factory=dict)
    range: Literal["1y", "2y", "5y", "10y", "max"] = "5y"
    initialCash: float = Field(default=100_000, gt=0)
    allocation: float | None = Field(default=10_000, gt=0)
    slippageBps: float = Field(default=2, ge=0, le=500)
    commission: float = Field(default=0, ge=0)


class IntradayBody(BaseModel):
    strategy: str
    timeframe: Literal["15m", "60m"] = "60m"
    years: float = Field(default=10, gt=0, le=20)
    capital: float = Field(default=1000, gt=0)
    riskPct: float = Field(default=1, gt=0, le=10)
    maxRiskPct: float = Field(default=5, gt=0, le=50)
    spread: float = Field(default=0.30, ge=0, le=10)
    leverage: float = Field(default=100, ge=1, le=1000)
    params: dict[str, float] = Field(default_factory=dict)


class KillBody(BaseModel):
    on: bool


class DCFBody(BaseModel):
    freeCashFlow: float
    growthRate: float
    years: int = 5
    discountRate: float
    terminalGrowth: float
    netDebt: float = 0
    sharesOutstanding: float = Field(gt=0)


def _basic_auth_ok(header: str | None, username: str, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user, username) & secrets.compare_digest(pw, password)


def create_app(
    data_dir: str | Path | None = None,
    market_mode: str | None = None,
    client_factory=None,
    auth: tuple[str, str] | None = None,
) -> FastAPI:
    load_dotenv(ROOT / ".env")
    if auth is None and os.getenv("LEDGER_PASSWORD"):
        auth = (os.getenv("LEDGER_USERNAME", "admin"), os.environ["LEDGER_PASSWORD"])
    config = Config(data_dir)
    events = EventLog(config.data_dir / "events.jsonl")
    market = create_market_data(market_mode or market_data_mode())
    utas = UTAManager(config, market, events)
    agent = TradingAgent(config, market, utas, events, client_factory=client_factory)
    autotrader = AutoTrader(config, market, utas, agent, events)
    state: dict[str, Any] = {"lastHeartbeat": None, "heartbeatRunning": False}

    async def run_heartbeat() -> dict[str, Any]:
        if state["heartbeatRunning"]:
            raise AgentError("A heartbeat is already running")
        state["heartbeatRunning"] = True
        try:
            return await agent.heartbeat()
        finally:
            state["heartbeatRunning"] = False
            state["lastHeartbeat"] = time.time()

    async def scheduler() -> None:
        """Reconcile resting paper orders every minute; fire the heartbeat on schedule."""
        while True:
            await asyncio.sleep(60)
            for u in list(utas.utas.values()):
                try:
                    fills = await u.broker.reconcile()
                    if fills:
                        events.emit("trade.fill", {"account": u.id, "fills": fills})
                except Exception as e:  # keep the scheduler alive
                    events.emit("system.error", {"where": "reconcile", "account": u.id, "error": str(e)})
            hb = config.heartbeat
            due = state["lastHeartbeat"] is None or time.time() - state["lastHeartbeat"] >= hb.intervalMinutes * 60
            if hb.enabled and agent.configured and due and not state["heartbeatRunning"]:
                try:
                    await run_heartbeat()
                except Exception as e:
                    events.emit("system.error", {"where": "heartbeat", "error": str(e)})
            try:
                await autotrader.tick()
            except Exception as e:
                events.emit("system.error", {"where": "automation", "error": str(e)})

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        events.emit("system.start", {"marketData": market.mode, "accounts": list(utas.utas), "ai": agent.configured})
        task = asyncio.create_task(scheduler())
        try:
            yield
        finally:
            task.cancel()
            await market.aclose()

    app = FastAPI(title="Ledger Terminal", lifespan=lifespan)
    app.state.config, app.state.utas, app.state.agent, app.state.events = config, utas, agent, events
    app.state.autotrader = autotrader

    if auth:
        # HTTP Basic auth on every route (UI, API and the SSE stream). Browsers
        # remember the credentials, so fetch() and EventSource keep working.
        @app.middleware("http")
        async def require_login(request: Request, call_next):
            if request.url.path == "/healthz" or _basic_auth_ok(request.headers.get("authorization"), *auth):
                return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Ledger Terminal"'})

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # Make browsers re-check the UI files so an upgrade is picked up immediately.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.exception_handler(MarketDataError)
    async def _market_err(_r: Request, e: MarketDataError):
        return JSONResponse(status_code=502, content={"detail": str(e)})

    @app.exception_handler(TradingError)
    async def _trading_err(_r: Request, e: TradingError):
        return JSONResponse(status_code=400, content={"detail": str(e)})

    @app.exception_handler(AgentError)
    async def _agent_err(_r: Request, e: AgentError):
        return JSONResponse(status_code=503, content={"detail": str(e)})

    # --- status / config -----------------------------------------------------
    @app.get("/api/status")
    async def status():
        hb = config.heartbeat
        return {
            "marketData": market.mode,
            "ai": {"configured": agent.configured, "model": config.ai.model, "effort": config.ai.effort},
            "accounts": list(utas.utas),
            "accountErrors": utas.errors,
            "heartbeat": {**hb.model_dump(), "last": state["lastHeartbeat"], "running": state["heartbeatRunning"]},
            "watchlist": config.watchlist,
            "automation": {"killSwitch": config.automation.killSwitch, "enabledBots": [b.id for b in config.automation.bots if b.enabled]},
            "serverTime": time.time(),
        }

    @app.get("/api/watchlist")
    async def get_watchlist():
        return {"symbols": config.watchlist}

    @app.put("/api/watchlist")
    async def put_watchlist(body: WatchlistConfig):
        symbols = list(dict.fromkeys(normalize_symbol(s) for s in body.symbols))[:40]
        config.save("watchlist.json", WatchlistConfig(symbols=symbols))
        return {"symbols": symbols}

    @app.put("/api/heartbeat")
    async def put_heartbeat(body: HeartbeatConfig):
        config.save("heartbeat.json", body)
        events.emit("heartbeat.config", body.model_dump())
        return body

    @app.post("/api/heartbeat/run")
    async def heartbeat_now():
        return await run_heartbeat()

    # --- market data -----------------------------------------------------------
    @app.get("/api/quote/{symbol}")
    async def quote(symbol: str):
        return await market.quote(symbol)

    @app.get("/api/quotes")
    async def quotes(symbols: str = Query(..., max_length=500)):
        async def safe(s: str):
            try:
                return await market.quote(s)
            except MarketDataError as e:
                return {"symbol": s.upper(), "error": str(e)}

        return await asyncio.gather(*(safe(s) for s in symbols.split(",")[:40] if s.strip()))

    @app.get("/api/history/{symbol}")
    async def history(symbol: str, range: str = "1y", interval: str = "1d"):
        return {"symbol": normalize_symbol(symbol), "bars": await market.history(symbol, range, interval)}

    @app.get("/api/technicals/{symbol}")
    async def technicals(symbol: str, range: str = "2y"):
        bars = await market.history(symbol, range, "1d")
        closes = [b["c"] for b in bars]
        return {
            "symbol": normalize_symbol(symbol),
            "summary": indicators.summary(bars),
            "series": {
                "t": [b["t"] for b in bars],
                "sma20": indicators.sma(closes, 20),
                "sma50": indicators.sma(closes, 50),
                "sma200": indicators.sma(closes, 200),
                "rsi14": indicators.rsi(closes),
            },
        }

    @app.get("/api/search")
    async def search(q: str = Query(..., min_length=1, max_length=60)):
        return await market.search(q)

    @app.get("/api/news")
    async def news(symbol: str | None = None):
        return await market.news(symbol or None)

    @app.post("/api/dcf")
    async def dcf(body: DCFBody):
        try:
            return analytics.dcf(body.freeCashFlow, body.growthRate, body.years, body.discountRate, body.terminalGrowth, body.netDebt, body.sharesOutstanding)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # --- accounts / trading-as-git ---------------------------------------------
    @app.get("/api/accounts")
    async def accounts():
        return {"accounts": [await u.summary() for u in utas.utas.values()], "errors": utas.errors}

    @app.get("/api/accounts/{account_id}")
    async def account(account_id: str):
        return await utas.get(account_id).summary()

    @app.post("/api/accounts/{account_id}/stage")
    async def stage(account_id: str, body: StageBody):
        try:
            order = OrderRequest(symbol=normalize_symbol(body.symbol), side=body.side, qty=body.qty, type=body.type,
                                 limitPrice=body.limitPrice, stopLoss=body.stopLoss, takeProfit=body.takeProfit)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return await utas.get(account_id).stage(order, body.rationale, source="user")

    @app.delete("/api/accounts/{account_id}/stage/{staged_id}")
    async def unstage(account_id: str, staged_id: str):
        utas.get(account_id).unstage(staged_id)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/commit")
    async def commit(account_id: str, body: CommitBody):
        return utas.get(account_id).commit(body.message, author="user")

    @app.post("/api/accounts/{account_id}/push")
    async def push(account_id: str, body: PushBody):
        if not body.confirm:
            raise HTTPException(400, "Push executes orders. Resend with confirm=true after reviewing the commit.")
        return {"commits": await utas.get(account_id).push(body.hash, approver="user")}

    @app.post("/api/accounts/{account_id}/revert/{hash_}")
    async def revert(account_id: str, hash_: str):
        return utas.get(account_id).revert(hash_)

    @app.get("/api/accounts/{account_id}/log")
    async def log(account_id: str, limit: int = 50):
        return utas.get(account_id).log(limit)

    @app.get("/api/accounts/{account_id}/commits/{hash_}")
    async def show(account_id: str, hash_: str):
        return utas.get(account_id).show(hash_)

    @app.post("/api/accounts/{account_id}/orders/{order_id}/cancel")
    async def cancel(account_id: str, order_id: str):
        res = await utas.get(account_id).broker.cancel_order(order_id)
        events.emit("trade.cancel", {"account": account_id, "orderId": order_id, **res})
        return res

    @app.get("/api/accounts/{account_id}/risk")
    async def risk(account_id: str):
        acct = (await utas.get(account_id).summary())["account"] or {"positions": []}
        histories = {}
        for p in acct["positions"]:
            try:
                histories[p["symbol"]] = await market.history(p["symbol"], "1y", "1d")
            except MarketDataError:
                continue
        return analytics.portfolio_risk(acct["positions"], histories)

    # --- strategies / backtest ---------------------------------------------------
    @app.get("/api/strategies")
    async def strategy_catalog():
        return strategies.catalog()

    @app.post("/api/backtest")
    async def backtest(body: BacktestBody):
        symbol = normalize_symbol(body.symbol)
        bars = await market.history(symbol, body.range, "1d")
        try:
            result = run_backtest(
                bars, body.strategy, body.params, body.initialCash, body.allocation,
                slippage_bps=body.slippageBps, commission=body.commission,
                # Indices aren't bought unit by unit; treat them like an index fund.
                fractional=is_crypto(symbol) or symbol.startswith("^"),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        events.emit("backtest.run", {"symbol": symbol, "strategy": body.strategy, "params": body.params, "totalReturn": result["metrics"]["totalReturn"], "benchmark": result["benchmark"]["totalReturn"]})
        return {"symbol": symbol, "dataSource": market.mode, **result}

    # --- intraday (gold) backtests on local history ---------------------------------
    history_cache: dict[int, tuple[int, list]] = {}

    def gold_history(minutes: int) -> list:
        sub = config.data_dir / "history" / "XAUUSD" / ("h1" if minutes >= 60 else "m1")
        count = len(list(sub.glob("*.csv"))) if sub.exists() else 0
        hit = history_cache.get(minutes)
        if hit and hit[0] == count:
            return hit[1]
        bars = intraday.load_history(config.data_dir, "XAUUSD", minutes)
        history_cache[minutes] = (count, bars)
        return bars

    @app.get("/api/intraday/strategies")
    async def intraday_strategies():
        avail = {}
        for tf, sub in (("60m", "h1"), ("15m", "m1")):
            files = sorted((config.data_dir / "history" / "XAUUSD" / sub).glob("*.csv")) if (config.data_dir / "history" / "XAUUSD" / sub).exists() else []
            avail[tf] = {"files": len(files), "from": files[0].stem if files else None, "to": files[-1].stem if files else None}
        return {"strategies": [{"id": k, "label": v["label"], "params": v["params"]} for k, v in intraday.STRATEGIES.items() if not k.startswith("_")],
                "history": avail, "symbol": "XAUUSD"}

    @app.post("/api/intraday/backtest")
    async def intraday_backtest(body: IntradayBody):
        if body.strategy not in intraday.STRATEGIES:
            raise HTTPException(400, f"Unknown strategy. Options: {list(intraday.STRATEGIES)}")
        minutes = 60 if body.timeframe == "60m" else 15
        try:
            bars = await asyncio.to_thread(gold_history, minutes)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))
        cutoff = bars[-1]["t"] - int(body.years * 365.25 * 86400) if bars else 0
        bars = [b for b in bars if b["t"] >= cutoff]
        costs = intraday.Costs(spread=body.spread, leverage=body.leverage)
        try:
            result = await asyncio.to_thread(
                intraday.run_intraday, bars, body.strategy, body.params, body.capital, body.riskPct, body.maxRiskPct, costs, 21, 250)
        except ValueError as e:
            raise HTTPException(400, str(e))
        m = result["metrics"]
        events.emit("backtest.run", {"symbol": "XAUUSD", "strategy": body.strategy, "params": body.params,
                                     "totalReturn": m["totalReturn"], "benchmark": 0.0, "winRate": m["winRate"], "timeframe": body.timeframe})
        return {"symbol": "XAUUSD", "timeframe": body.timeframe, **result}

    # --- automation ------------------------------------------------------------------
    @app.get("/api/automation")
    async def automation_status():
        return await autotrader.status()

    @app.post("/api/automation/kill")
    async def kill_switch(body: KillBody):
        auto = config.automation
        auto.killSwitch = body.on
        config.save("automation.json", auto)
        events.emit("automation.killswitch", {"on": body.on})
        return {"killSwitch": body.on}

    @app.put("/api/automation/bots/{bot_id}")
    async def upsert_bot(bot_id: str, body: BotConfig):
        if body.id != bot_id:
            raise HTTPException(400, "bot id in path and body differ")
        if body.account not in utas.utas:
            raise HTTPException(400, f"Unknown or disabled account {body.account!r}")
        if body.type == "intraday":
            if len(body.symbols) != 1:
                raise HTTPException(400, "An intraday bot trades exactly one symbol")
            if body.strategy not in intraday.STRATEGIES:
                raise HTTPException(400, f"Unknown intraday strategy. Options: {list(intraday.STRATEGIES)}")
            if utas.get(body.account).cfg.type != "cfd":
                raise HTTPException(400, "Intraday bots need a CFD account (type 'cfd')")
        if body.type == "strategy":
            if not body.symbols:
                raise HTTPException(400, "A strategy bot needs at least one symbol")
            try:
                strategies.get_signals(body.strategy, [{"t": i, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0} for i in range(40)], body.params)
            except ValueError as e:
                raise HTTPException(400, str(e))
        body.symbols = [normalize_symbol(s) for s in body.symbols]
        auto = config.automation
        auto.bots = [b for b in auto.bots if b.id != bot_id] + [body]
        config.save("automation.json", auto)
        events.emit("bot.config", {"bot": bot_id, "enabled": body.enabled, "type": body.type, "strategy": body.strategy, "symbols": body.symbols})
        return body

    @app.delete("/api/automation/bots/{bot_id}")
    async def delete_bot(bot_id: str):
        auto = config.automation
        auto.bots = [b for b in auto.bots if b.id != bot_id]
        config.save("automation.json", auto)
        events.emit("bot.deleted", {"bot": bot_id})
        return {"ok": True}

    @app.post("/api/automation/bots/{bot_id}/run")
    async def run_bot_now(bot_id: str):
        return await autotrader.run_bot(bot_id)

    @app.post("/api/automation/bots/{bot_id}/resume")
    async def resume_bot(bot_id: str):
        autotrader.resume(bot_id)
        return {"ok": True}

    # --- agent -------------------------------------------------------------------
    @app.post("/api/agent/chat")
    async def chat(body: ChatBody):
        return await agent.chat(body.channel, body.message)

    @app.post("/api/agent/reset")
    async def reset(channel: str = "web"):
        agent.reset(channel)
        return {"ok": True}

    @app.post("/api/agent/panel")
    async def panel(body: PanelBody):
        return await agent.investor_panel(normalize_symbol(body.symbol), body.lenses)

    # --- events --------------------------------------------------------------------
    @app.get("/api/events")
    async def recent_events(limit: int = 100, prefix: str | None = None):
        return events.recent(min(limit, 500), prefix)

    @app.get("/api/events/stream")
    async def stream(request: Request):
        queue = events.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                events.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # --- UI ------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app
