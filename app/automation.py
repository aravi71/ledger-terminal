"""Automated trading bots (paper accounts only).

Every automated order goes through the same pipeline as a manual one:
stage -> commit -> push, with the account's guards enforced at push. What
automation adds is that the push is done by the bot (``approver=auto:<id>``)
instead of a person, plus these extra controls:

- global kill switch (``automation.json`` -> ``killSwitch``)
- per-bot daily loss limit: if account equity falls more than
  ``maxDailyLossPct`` below the day's starting equity, the bot halts and
  stays halted until a person resumes it
- market-hours check (US equities 09:30-16:00 ET and Indian NSE/BSE
  09:15-15:30 IST on weekdays; FX and futures on weekdays; crypto 24/7).
  No exchange-holiday calendar.
- strategy bots act on the last *completed* daily bar, exactly like the
  backtester (signal at close, fill at next open)
- each bot only sells what it bought (it tracks its own holdings from its
  commit history), so it never touches positions you opened by hand
- AI bots may only trade their listed symbols; anything else is reverted
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .agent import AgentError, TradingAgent
from .brokers import OrderRequest
from .config import BotConfig, Config
from .events import EventLog
from .strategies import get_signals
from .uta import TradingError, UTAManager

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
PAPER_ACCOUNT_TYPES = {"paper", "alpaca"}  # the Alpaca adapter is hard-wired to its paper endpoint


def is_crypto(symbol: str) -> bool:
    return "-" in symbol and symbol.split("-")[-1] in {"USD", "USDT", "USDC", "EUR", "BTC", "ETH"}


def is_india(symbol: str) -> bool:
    return symbol.endswith((".NS", ".BO")) or symbol in {"^NSEI", "^NSEBANK", "^BSESN"}


def market_open(symbol: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if is_crypto(symbol):
        return True
    if is_india(symbol):  # NSE/BSE: 09:15-15:30 IST, weekdays
        ist = now.astimezone(IST)
        return ist.weekday() < 5 and dtime(9, 15) <= ist.time() < dtime(15, 30)
    et = now.astimezone(NY)
    if et.weekday() >= 5:
        return False
    if symbol.endswith("=X") or symbol.endswith("=F"):
        return True
    return dtime(9, 30) <= et.time() < dtime(16, 0)


def completed_bars(symbol: str, bars: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Drop today's still-forming daily bar so live decisions match the backtest."""
    now = now or datetime.now(timezone.utc)
    tz = timezone.utc if is_crypto(symbol) else IST if is_india(symbol) else NY
    today = now.astimezone(tz).date()
    return [b for b in bars if datetime.fromtimestamp(b["t"], tz).date() < today]


def bot_ledger(uta, bot_id: str) -> dict[str, Any]:
    """Holdings and realized P&L for one bot, rebuilt from its pushed commits."""
    author = f"bot:{bot_id}"
    holdings: dict[str, dict[str, float]] = {}
    realized, fills = 0.0, 0
    for c in uta.state["commits"]:
        if c.get("author") != author or c["status"] not in ("pushed", "partial"):
            continue
        staged_px = {s["order"]["symbol"]: s.get("priceAtStage") for s in c["orders"]}
        for r in c.get("results", []):
            if r["status"] not in ("filled", "accepted"):
                continue
            qty = float(r.get("filledQty") or r["qty"])
            px = float(r.get("avgFillPrice") or staged_px.get(r["symbol"]) or 0)
            h = holdings.setdefault(r["symbol"], {"qty": 0.0, "avgCost": 0.0})
            if r["side"] == "buy":
                total = h["qty"] + qty
                h["avgCost"] = (h["qty"] * h["avgCost"] + qty * px) / total if total else 0.0
                h["qty"] = total
            else:
                sold = min(qty, h["qty"])
                realized += (px - h["avgCost"]) * sold
                h["qty"] -= sold
            fills += 1
    return {"holdings": {s: h for s, h in holdings.items() if h["qty"] > 1e-9}, "realizedPnl": realized, "fills": fills}


class AutoTrader:
    def __init__(self, config: Config, market, utas: UTAManager, agent: TradingAgent, events: EventLog):
        self.config = config
        self.market = market
        self.utas = utas
        self.agent = agent
        self.events = events
        self.path: Path = config.data_dir / "automation_state.json"
        self.state: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"bots": {}}
        self.running: set[str] = set()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def bot_state(self, bot_id: str) -> dict[str, Any]:
        return self.state["bots"].setdefault(bot_id, {"lastRun": None, "lastAction": None, "lastError": None, "halted": False, "haltReason": None, "dayStart": None})

    def _record(self, bot: BotConfig, action: str, event: str = "bot.run", **extra) -> None:
        st = self.bot_state(bot.id)
        st["lastAction"] = action
        st["lastRun"] = time.time()
        if event == "bot.error":
            st["lastError"] = action
        self._save()
        self.events.emit(event, {"bot": bot.id, "action": action, **extra})

    # --- public controls ---------------------------------------------------------
    def resume(self, bot_id: str) -> None:
        st = self.bot_state(bot_id)
        st.update(halted=False, haltReason=None, dayStart=None)
        self._save()
        self.events.emit("bot.resumed", {"bot": bot_id})

    async def status(self) -> dict[str, Any]:
        auto = self.config.automation
        bots = []
        for bot in auto.bots:
            st = self.bot_state(bot.id)
            perf: dict[str, Any] = {}
            if bot.account in self.utas.utas:
                ledger = bot_ledger(self.utas.get(bot.account), bot.id)
                unrealized = 0.0
                for sym, h in ledger["holdings"].items():
                    try:
                        unrealized += (float((await self.market.quote(sym))["price"]) - h["avgCost"]) * h["qty"]
                    except Exception:
                        pass
                perf = {**ledger, "unrealizedPnl": unrealized, "totalPnl": ledger["realizedPnl"] + unrealized}
            bots.append({**bot.model_dump(), "state": st, "performance": perf, "running": bot.id in self.running})
        return {"killSwitch": auto.killSwitch, "bots": bots}

    # --- scheduling --------------------------------------------------------------------
    async def tick(self) -> None:
        auto = self.config.automation
        if auto.killSwitch:
            return
        now = time.time()
        for bot in auto.bots:
            st = self.bot_state(bot.id)
            due = st["lastRun"] is None or now - st["lastRun"] >= bot.intervalMinutes * 60
            if bot.enabled and due and not st["halted"] and bot.id not in self.running:
                await self.run_bot(bot.id)

    async def run_bot(self, bot_id: str) -> dict[str, Any]:
        auto = self.config.automation
        bot = next((b for b in auto.bots if b.id == bot_id), None)
        if bot is None:
            raise TradingError(f"No bot {bot_id!r}")
        if auto.killSwitch:
            raise TradingError("Kill switch is ON; automation is stopped")
        st = self.bot_state(bot.id)
        if st["halted"]:
            raise TradingError(f"Bot {bot.id} is halted: {st['haltReason']}. Resume it first.")
        uta = self.utas.get(bot.account)
        if uta.cfg.type not in PAPER_ACCOUNT_TYPES:
            raise TradingError(f"Automation is only allowed on paper accounts ({uta.id} is {uta.cfg.type})")
        if bot.id in self.running:
            raise TradingError(f"Bot {bot.id} is already running")
        self.running.add(bot.id)
        try:
            if await self._loss_limit_hit(bot, uta):
                return {"bot": bot.id, "halted": True, "reason": st["haltReason"]}
            if bot.type == "ai":
                return await self._run_ai(bot, uta)
            return await self._run_strategy(bot, uta)
        except (TradingError, AgentError, ValueError) as e:
            self._record(bot, f"error: {e}", "bot.error")
            return {"bot": bot.id, "error": str(e)}
        finally:
            self.running.discard(bot.id)

    async def _loss_limit_hit(self, bot: BotConfig, uta) -> bool:
        st = self.bot_state(bot.id)
        equity = float((await uta.broker.account())["equity"])
        today = datetime.now(timezone.utc).date().isoformat()
        if not st["dayStart"] or st["dayStart"]["date"] != today:
            st["dayStart"] = {"date": today, "equity": equity}
            self._save()
        start = st["dayStart"]["equity"]
        drop = (start - equity) / start * 100 if start else 0.0
        if drop > bot.maxDailyLossPct:
            st["halted"] = True
            st["haltReason"] = f"daily loss {drop:.2f}% exceeded limit {bot.maxDailyLossPct}% (equity {equity:,.2f} vs {start:,.2f} at start of day)"
            self._record(bot, "halted", "bot.halted", reason=st["haltReason"])
            return True
        return False

    async def _push_bot_commit(self, bot: BotConfig, uta, staged_ids: list[str], message: str) -> dict[str, Any]:
        commit = uta.commit(f"[bot:{bot.id}] {message}", author=f"bot:{bot.id}", staged_ids=staged_ids)
        result = (await uta.push(commit["hash"], approver=f"auto:{bot.id}"))[0]
        return {"hash": result["hash"], "status": result["status"]}

    async def _run_strategy(self, bot: BotConfig, uta) -> dict[str, Any]:
        holdings = bot_ledger(uta, bot.id)["holdings"]
        actions = []
        for symbol in bot.symbols:
            if bot.respectMarketHours and not market_open(symbol):
                actions.append({"symbol": symbol, "action": "skip", "reason": "market closed"})
                continue
            bars = completed_bars(symbol, await self.market.history(symbol, "2y", "1d"))
            if len(bars) < 30:
                actions.append({"symbol": symbol, "action": "skip", "reason": "not enough history"})
                continue
            signal = get_signals(bot.strategy, bars, bot.params)[-1]
            held = holdings.get(symbol, {}).get("qty", 0.0)
            if signal == 1 and held <= 0:
                price = float((await self.market.quote(symbol))["price"])
                qty = round(bot.allocationPerSymbol / price, 6) if is_crypto(symbol) else math.floor(bot.allocationPerSymbol / price)
                if qty <= 0:
                    actions.append({"symbol": symbol, "action": "skip", "reason": f"allocation {bot.allocationPerSymbol} < price {price:.2f}"})
                    continue
                order = OrderRequest(symbol=symbol, side="buy", qty=qty)
                rationale = f"{bot.strategy} {bot.params or ''} signal turned LONG on last completed bar; allocation ${bot.allocationPerSymbol:,.0f}"
            elif signal == 0 and held > 0:
                order = OrderRequest(symbol=symbol, side="sell", qty=held)
                rationale = f"{bot.strategy} signal turned FLAT; closing the bot's {held:g} {symbol}"
            else:
                actions.append({"symbol": symbol, "action": "hold", "signal": signal, "botQty": held})
                continue
            staged = await uta.stage(order, rationale, source="bot")
            res = await self._push_bot_commit(bot, uta, [staged["id"]], f"{order.side} {order.qty:g} {symbol} ({bot.strategy})")
            actions.append({"symbol": symbol, "action": order.side, "qty": order.qty, **res})
            if res["status"] in ("pushed", "partial"):
                self.events.emit("bot.trade", {"bot": bot.id, "symbol": symbol, "side": order.side, "qty": order.qty, "hash": res["hash"]})
        summary = "; ".join(f"{a['symbol']}: {a['action']}" + (f" ({a.get('reason') or a.get('status')})" if a.get("reason") or a.get("status") else "") for a in actions) or "no symbols"
        self._record(bot, summary, actions=actions)
        return {"bot": bot.id, "actions": actions}

    async def _run_ai(self, bot: BotConfig, uta) -> dict[str, Any]:
        if not self.agent.configured:
            raise AgentError("Claude is not configured; the AI autopilot needs ANTHROPIC_API_KEY")
        open_syms = [s for s in bot.symbols if not bot.respectMarketHours or market_open(s)]
        if not open_syms:
            self._record(bot, "skip: market closed for all symbols")
            return {"bot": bot.id, "actions": [], "reason": "market closed"}
        prompt = (
            self.config.prompt("autopilot")
            + f"\n\nAccount: {uta.id}\nSymbols you may trade: {', '.join(open_syms)}\n"
            + f"Max order value per symbol: ${bot.allocationPerSymbol:,.0f}"
        )
        author = f"bot:{bot.id}"
        before = {c["hash"] for c in uta.state["commits"]}
        out = await self.agent.autopilot(bot.id, prompt, author=author)
        actions = []
        for c in [c for c in uta.state["commits"] if c["hash"] not in before and c["status"] == "committed" and c["author"] == author]:
            bad = [s["order"]["symbol"] for s in c["orders"] if s["order"]["symbol"] not in open_syms]
            if bad:
                uta.revert(c["hash"])
                actions.append({"hash": c["hash"], "action": "reverted", "reason": f"symbols not allowed: {bad}"})
                continue
            res = (await uta.push(c["hash"], approver=f"auto:{bot.id}"))[0]
            actions.append({"hash": c["hash"], "action": "push", "status": res["status"]})
        self._record(bot, (f"{len(actions)} commit(s) pushed" if actions else "no trade") + f" · {out['reply'][:200]}", actions=actions)
        return {"bot": bot.id, "actions": actions, "reply": out["reply"]}
