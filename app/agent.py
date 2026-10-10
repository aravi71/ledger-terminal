"""Claude-powered research and trading agent.

Three entry points:
- ``chat``: a tool-using conversation per channel. The agent can research
  and stage/commit orders, but has no push tool: execution stays human.
- ``investor_panel``: several independent analysis lenses run in parallel on
  one symbol, each returning structured JSON.
- ``heartbeat``: a periodic read-only review that only notifies when
  something matters.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from . import indicators
from .brokers import OrderRequest
from .config import Config
from .events import EventLog
from .market import MarketDataError
from .uta import TradingError, UTAManager

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(Exception):
    pass


def ai_configured() -> bool:
    """True when the SDK has a credential source to resolve."""
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


RESEARCH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_symbols",
        "description": "Find ticker symbols by company/asset name. Covers stocks, ETFs, crypto (e.g. BTC-USD), futures (GC=F) and FX (EURUSD=X).",
        "input_schema": _obj({"query": {"type": "string"}}),
    },
    {
        "name": "get_quote",
        "description": "Latest (delayed) quote for one or more symbols: price, change, day range, 52-week range.",
        "input_schema": _obj({"symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 15}}),
    },
    {
        "name": "get_technicals",
        "description": "Technical snapshot from daily bars: SMA20/50/200, RSI14, MACD, Bollinger, ATR, 1m/3m returns and plain-language signals.",
        "input_schema": _obj({"symbol": {"type": "string"}, "range": {"type": "string", "enum": ["3mo", "6mo", "1y", "2y"]}}),
    },
    {
        "name": "get_price_history",
        "description": "Compact daily closing-price history (date, close) for a symbol, at most 130 points.",
        "input_schema": _obj({"symbol": {"type": "string"}, "range": {"type": "string", "enum": ["1mo", "3mo", "6mo", "1y"]}}),
    },
    {
        "name": "get_news",
        "description": "Recent headlines for a symbol (or the broad market when symbol is empty).",
        "input_schema": _obj({"symbol": {"type": "string"}}),
    },
    {
        "name": "get_accounts",
        "description": "All trading accounts: cash, equity, positions, guard limits, staged orders and unpushed commits.",
        "input_schema": _obj({}),
    },
    {
        "name": "get_trade_log",
        "description": "Commit history (git-log style) for one account, newest first.",
        "input_schema": _obj({"account_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
    },
]

TRADING_TOOLS: list[dict[str, Any]] = [
    {
        "name": "stage_order",
        "description": (
            "Stage an order in an account's staging area. Staging does NOT execute anything. "
            "Guards are previewed and any warnings are returned. Use only when the user asked for a trade or a trade proposal."
        ),
        "input_schema": _obj(
            {
                "account_id": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "qty": {"type": "number", "exclusiveMinimum": 0},
                "order_type": {"type": "string", "enum": ["market", "limit"]},
                "limit_price": {"type": ["number", "null"]},
                "rationale": {"type": "string"},
            }
        ),
    },
    {
        "name": "commit_staged",
        "description": "Bundle all staged orders in an account into a commit with a message. The commit then waits for a human to review and push it.",
        "input_schema": _obj({"account_id": {"type": "string"}, "message": {"type": "string"}}),
    },
]

PANEL_LENSES = {
    "value": "a disciplined value investor: margin of safety, durability of the business, price relative to long-run averages, avoiding permanent capital loss",
    "growth": "a growth investor: trajectory, momentum of the narrative, size of the opportunity, willingness to pay up for quality",
    "technical": "a technical/momentum trader: trend, moving averages, RSI/MACD, support and resistance, volatility-adjusted position sizing",
    "macro": "a macro strategist: rates, dollar, sector rotation, risk-on/risk-off regime and how this asset fits in it",
    "risk": "a risk manager: what could go wrong, drawdown history, volatility, concentration, and what would invalidate a long thesis",
}

PANEL_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "conviction": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "thesis": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "what_would_change_my_mind": {"type": "string"},
    },
    "required": ["stance", "conviction", "thesis", "key_points", "risks", "what_would_change_my_mind"],
    "additionalProperties": False,
}

HEARTBEAT_SCHEMA = {
    "type": "object",
    "properties": {
        "notify": {"type": "boolean"},
        "headline": {"type": "string"},
        "details": {"type": "string"},
    },
    "required": ["notify", "headline", "details"],
    "additionalProperties": False,
}


def _fmt(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _fmt(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_fmt(v) for v in x]
    return x


class TradingAgent:
    def __init__(self, config: Config, market, utas: UTAManager, events: EventLog, client_factory: Callable[[], Any] | None = None):
        self.config = config
        self.market = market
        self.utas = utas
        self.events = events
        self._client_factory = client_factory or (lambda: anthropic.AsyncAnthropic())
        self._client = None
        self.conversations: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return ai_configured()

    def _get_client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # --- tools ----------------------------------------------------------------
    async def run_tool(self, name: str, args: dict[str, Any], allow_trading: bool, ctx: dict[str, Any] | None = None) -> Any:
        """``ctx`` (autopilot runs) sets the commit author and restricts commits
        to orders staged in the same run."""
        ctx = ctx or {}
        if name in ("stage_order", "commit_staged") and not allow_trading:
            raise AgentError(f"{name} is not available in this context")
        if name == "search_symbols":
            return await self.market.search(args["query"])
        if name == "get_quote":
            out = []
            for s in args["symbols"][:15]:
                try:
                    out.append(await self.market.quote(s))
                except MarketDataError as e:
                    out.append({"symbol": s, "error": str(e)})
            return out
        if name == "get_technicals":
            bars = await self.market.history(args["symbol"], args.get("range") or "1y", "1d")
            return {"symbol": args["symbol"].upper(), **indicators.summary(bars)}
        if name == "get_price_history":
            bars = await self.market.history(args["symbol"], args.get("range") or "6mo", "1d")
            step = max(1, len(bars) // 130)
            return [{"date": datetime.fromtimestamp(b["t"], timezone.utc).strftime("%Y-%m-%d"), "close": b["c"]} for b in bars[::step]]
        if name == "get_news":
            items = await self.market.news(args.get("symbol") or None, limit=10)
            return [{"title": n["title"], "published": n["published"], "summary": n["summary"][:240]} for n in items]
        if name == "get_accounts":
            out = []
            for u in self.utas.utas.values():
                s = await u.summary()
                out.append({k: s[k] for k in ("id", "label", "type", "guards", "account", "error", "staged", "pending")})
            return {"accounts": out, "disabledOrErrored": self.utas.errors}
        if name == "get_trade_log":
            return self.utas.get(args["account_id"]).log(int(args.get("limit") or 20))
        if name == "stage_order":
            order = OrderRequest(
                symbol=args["symbol"],
                side=args["side"],
                qty=args["qty"],
                type=args.get("order_type") or "market",
                limitPrice=args.get("limit_price"),
            )
            item = await self.utas.get(args["account_id"]).stage(order, rationale=args.get("rationale", ""), source="agent")
            if ctx.get("staged") is not None:
                ctx["staged"].append(item["id"])
            return {"staged": item, "note": "Staged only. Nothing executes until the user pushes a commit."}
        if name == "commit_staged":
            c = self.utas.get(args["account_id"]).commit(args["message"], author=ctx.get("author", "agent"), staged_ids=ctx.get("staged"))
            return {"hash": c["hash"], "orders": len(c["orders"]), "status": c["status"], "note": "Awaiting human review and push."}
        raise AgentError(f"Unknown tool {name}")

    async def _dispatch(self, block, allow_trading: bool, channel: str, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        args = block.input if isinstance(block.input, dict) else {}
        self.events.emit("agent.tool", {"channel": channel, "tool": block.name, "input": args})
        try:
            result = await self.run_tool(block.name, args, allow_trading, ctx)
            content, is_error = json.dumps(_fmt(result), default=str)[:30_000], False
        except (MarketDataError, TradingError, AgentError, ValueError, KeyError) as e:
            content, is_error = f"Error: {e}", True
        return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}

    # --- core loop ------------------------------------------------------------
    def _system(self, extra: str = "") -> list[dict[str, Any]]:
        accounts = ", ".join(f"{u.id} ({u.cfg.type})" for u in self.utas.utas.values()) or "none"
        text = (
            self.config.prompt("persona")
            + f"\n\nAvailable trading accounts: {accounts}.\nMarket data source: {self.market.mode}."
            + (f"\n\n{extra}" if extra else "")
        )
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    async def _create(self, **kw):
        ai = self.config.ai
        try:
            return await self._get_client().beta.messages.create(
                model=ai.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **kw,
            )
        except anthropic.AuthenticationError as e:
            raise AgentError("Claude rejected the credentials. Set ANTHROPIC_API_KEY in .env or run `ant auth login`.") from e
        except anthropic.RateLimitError as e:
            raise AgentError("Rate limited by the Claude API. Try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise AgentError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AgentError(f"Could not reach the Claude API: {e}") from e
        except TypeError as e:  # no credential could be resolved
            raise AgentError(f"Claude is not configured: {e}") from e

    async def _loop(self, messages: list, tools: list, allow_trading: bool, channel: str, effort: str, fmt: dict | None = None, ctx: dict[str, Any] | None = None) -> tuple[str, list[str], str]:
        tool_names: list[str] = []
        output_config: dict[str, Any] = {"effort": effort}
        if fmt:
            output_config["format"] = {"type": "json_schema", "schema": fmt}
        for _ in range(self.config.ai.maxAgentSteps):
            resp = await self._create(system=self._system(), tools=tools, messages=messages, output_config=output_config)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "The model declined this request.", tool_names, "refusal"
            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and uses:
                # Run tool calls in order: a commit must see the stages before it.
                results = [await self._dispatch(b, allow_trading, channel, ctx) for b in uses]
                tool_names += [b.name for b in uses]
                messages.append({"role": "user", "content": list(results)})
                continue
            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            return text, tool_names, resp.stop_reason or "end_turn"
        return "Stopped after reaching the step limit; ask me to continue.", tool_names, "step_limit"

    # --- public API -------------------------------------------------------------
    async def chat(self, channel: str, text: str) -> dict[str, Any]:
        if not self.configured:
            raise AgentError("Claude is not configured. Add ANTHROPIC_API_KEY to .env (or run `ant auth login`) and restart.")
        lock = self._locks.setdefault(channel, asyncio.Lock())
        async with lock:
            history = self.conversations.setdefault(channel, [])
            checkpoint = len(history)
            history.append({"role": "user", "content": text})
            self.events.emit("agent.user", {"channel": channel, "text": text})
            try:
                reply, tools, stop = await self._loop(history, RESEARCH_TOOLS + TRADING_TOOLS, True, channel, self.config.ai.effort)
            except AgentError:
                del history[checkpoint:]  # keep the transcript valid for the next turn
                raise
            self.events.emit("agent.reply", {"channel": channel, "text": reply, "tools": tools, "stop": stop})
            return {"reply": reply, "tools": tools, "stop": stop}

    async def autopilot(self, bot_id: str, prompt: str, author: str) -> dict[str, Any]:
        """One unattended run: fresh context, commits attributed to the bot."""
        if not self.configured:
            raise AgentError("Claude is not configured.")
        channel = f"autopilot:{bot_id}"
        ctx: dict[str, Any] = {"author": author, "staged": []}
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        self.events.emit("agent.user", {"channel": channel, "text": prompt[:500]})
        reply, tools, stop = await self._loop(messages, RESEARCH_TOOLS + TRADING_TOOLS, True, channel, self.config.ai.effort, ctx=ctx)
        self.events.emit("agent.reply", {"channel": channel, "text": reply, "tools": tools, "stop": stop})
        return {"reply": reply, "tools": tools, "staged": ctx["staged"]}

    def reset(self, channel: str) -> None:
        self.conversations.pop(channel, None)

    async def investor_panel(self, symbol: str, lenses: list[str] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise AgentError("Claude is not configured. Add ANTHROPIC_API_KEY to .env (or run `ant auth login`) and restart.")
        lenses = [l for l in (lenses or list(PANEL_LENSES)) if l in PANEL_LENSES]
        quote = await self.market.quote(symbol)
        bars = await self.market.history(symbol, "2y", "1d")
        tech = indicators.summary(bars)
        news = await self.market.news(symbol, limit=8)
        dossier = json.dumps(_fmt({"quote": quote, "technicals": tech, "headlines": [n["title"] for n in news]}), default=str)

        async def one(lens: str) -> dict[str, Any]:
            prompt = (
                f"Analyse {quote['symbol']} ({quote['name']}) as {PANEL_LENSES[lens]}.\n"
                f"Use only this data snapshot; say where data is missing rather than inventing it:\n{dossier}"
            )
            try:
                resp = await self._create(
                    system=self._system("You are writing one lens of a multi-perspective research panel. Be specific and concise."),
                    messages=[{"role": "user", "content": prompt}],
                    output_config={"effort": self.config.ai.effort, "format": {"type": "json_schema", "schema": PANEL_SCHEMA}},
                )
                if resp.stop_reason == "refusal":
                    return {"lens": lens, "error": "declined"}
                text = next(b.text for b in resp.content if b.type == "text")
                return {"lens": lens, **json.loads(text)}
            except (AgentError, StopIteration, json.JSONDecodeError) as e:
                return {"lens": lens, "error": str(e) or "no output"}

        results = await asyncio.gather(*(one(l) for l in lenses))
        self.events.emit("agent.panel", {"symbol": quote["symbol"], "stances": {r["lens"]: r.get("stance") for r in results}})
        return {"symbol": quote["symbol"], "quote": quote, "technicals": tech, "lenses": results}

    async def heartbeat(self) -> dict[str, Any]:
        if not self.configured:
            raise AgentError("Claude is not configured.")
        prompt = self.config.prompt("heartbeat") + f"\n\nWatchlist: {', '.join(self.config.watchlist)}"
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        text, tools, _stop = await self._loop(messages, RESEARCH_TOOLS, False, "heartbeat", self.config.ai.heartbeatEffort, fmt=HEARTBEAT_SCHEMA)
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = {"notify": True, "headline": "Heartbeat returned unstructured output", "details": text[:2000]}
        result["tools"] = tools
        self.events.emit("heartbeat.alert" if result.get("notify") else "heartbeat.quiet", result)
        return result
