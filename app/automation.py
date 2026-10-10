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

from . import intraday
from .agent import AgentError, TradingAgent
from .brokers import OrderRequest
from .config import BotConfig, Config
from .events import EventLog
from .strategies import get_signals
from .uta import TradingError, UTAManager

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
PAPER_ACCOUNT_TYPES = {"paper", "alpaca", "cfd"}  # Alpaca is hard-wired to its paper endpoint; cfd is a local simulator


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
            if bot.type == "intraday" and bot.account in self.utas.utas:
                acct = await self.utas.get(bot.account).broker.account()
                perf = {"holdings": {p["symbol"]: {"qty": p["qty"], "avgCost": p["avgPrice"]} for p in acct["positions"]},
                        "realizedPnl": acct["cash"] - acct.get("startingCash", acct["cash"]),
                        "unrealizedPnl": acct["equity"] - acct["cash"], "totalPnl": acct["totalPnl"], "equity": acct["equity"]}
            elif bot.account in self.utas.utas:
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
            if bot.type == "intraday":
                return await self._run_intraday(bot, uta)
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

    async def _run_intraday(self, bot: BotConfig, uta) -> dict[str, Any]:
        """Scalp one symbol on a CFD account with stop-loss / take-profit.

        Exits (stop, target, time limit, end of day) never go through guards;
        entries are staged, committed and pushed like any other order.
        """
        if uta.cfg.type != "cfd":
            raise TradingError("Intraday bots need a CFD account (type 'cfd'): leverage, shorts and stop-loss/take-profit")
        if bot.strategy not in intraday.STRATEGIES:
            raise TradingError(f"Unknown intraday strategy {bot.strategy!r}. Options: {list(intraday.STRATEGIES)}")
        if len(bot.symbols) != 1:
            raise TradingError("An intraday bot trades exactly one symbol")
        symbol, broker, st = bot.symbols[0], uta.broker, self.bot_state(bot.id)
        minutes = int(bot.timeframe.rstrip("m"))
        params = {**intraday.STRATEGIES[bot.strategy]["params"], **bot.params}
        for fill in await broker.reconcile():
            self.events.emit("bot.exit", {"bot": bot.id, **fill})
        now = datetime.now(timezone.utc)
        rng = {5: "5d", 15: "1mo", 30: "1mo", 60: "3mo"}[minutes]
        bars = [b for b in await self.market.history(symbol, rng, bot.timeframe) if b["t"] + minutes * 60 <= now.timestamp()]
        if len(bars) < 250:
            self._record(bot, f"skip: only {len(bars)} completed {bot.timeframe} bars")
            return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "skip", "reason": "not enough bars"}]}
        acct = await broker.account()
        pos = next((p for p in acct["positions"] if p["symbol"] == symbol), None)
        flat = params.get("flat_hour", 21)
        flat = None if flat is None or flat < 0 else int(flat)
        if pos:
            held = (bars[-1]["t"] - (st.get("openedBarT") or bars[-1]["t"])) // (minutes * 60)  # completed bars after the entry bar
            max_bars = int(params.get("max_bars") or 0)
            end_of_day = flat is not None and now.hour >= flat
            if (max_bars and held >= max_bars) or end_of_day:
                fill = await broker.close_position(symbol, "end of day" if end_of_day else "time exit")
                self.events.emit("bot.exit", {"bot": bot.id, **fill})
                self._record(bot, f"{symbol}: closed ({fill['reason']}), realized {fill['realized']:+.2f}")
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "close", "reason": fill["reason"]}]}
            self._record(bot, f"{symbol}: holding {pos['qty']:+g} @ {pos['avgPrice']:.2f}, SL {pos['stopLoss']}, TP {pos['takeProfit']}, open P&L {pos['unrealizedPnl']:+.2f}")
            return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "hold"}]}
        if bot.respectMarketHours and not market_open(symbol, now):
            self._record(bot, f"{symbol}: skip (market closed)")
            return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "skip", "reason": "market closed"}]}
        if flat is not None and now.hour >= flat - 1:
            self._record(bot, f"{symbol}: no new entries after {flat - 1:02d}:00 UTC")
            return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "skip", "reason": "after entry window"}]}
        # Gold and CME futures pause 17:00-18:00 New York time; a signal on the bar that closes
        # into the break is held until the reopen, matching the backtest's next-bar-open entry.
        in_break = not is_crypto(symbol) and now.astimezone(NY).hour == 17
        pending = st.get("pending")
        if pending and pending["t"] == bars[-1]["t"]:
            if in_break:
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "wait", "reason": "waiting for reopen"}]}
            sig = (pending["side"], pending["sd"], pending["td"])
            st["pending"] = None
        else:
            if st.get("lastSignalBar") == bars[-1]["t"]:
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "wait", "reason": "no new bar"}]}
            st["lastSignalBar"] = bars[-1]["t"]
            st["pending"] = None
            self._save()
            sig = intraday.latest_signal(bars, bot.strategy, bot.params)
            if not sig:
                self._record(bot, f"{symbol}: no signal on the {bot.timeframe} bar")
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "wait", "reason": "no signal"}]}
            if in_break:
                st["pending"] = {"side": sig[0], "sd": sig[1], "td": sig[2], "t": bars[-1]["t"]}
                self._record(bot, f"{symbol}: {sig[0]} signal, waiting for the 18:00 New York reopen")
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "wait", "reason": "waiting for reopen"}]}
        side, stop_dist, target_dist = sig
        price = float((await self.market.quote(symbol))["price"])
        entry = price + broker.spread / 2 if side == "buy" else price - broker.spread / 2
        risk_unit = stop_dist + broker.spread
        equity = acct["equity"]
        qty = math.floor(equity * bot.riskPct / 100 / risk_unit / broker.min_qty) * broker.min_qty
        if qty < broker.min_qty:
            if broker.min_qty * risk_unit <= equity * bot.maxRiskPct / 100:
                qty = broker.min_qty
            else:
                msg = f"{symbol}: {side} signal skipped, minimum size risks {broker.min_qty * risk_unit / equity:.1%} of equity (cap {bot.maxRiskPct}%)"
                self._record(bot, msg)
                return {"bot": bot.id, "actions": [{"symbol": symbol, "action": "skip", "reason": "minimum size too risky"}]}
        sl = round(entry - stop_dist if side == "buy" else entry + stop_dist, 2)
        tp = round(entry + target_dist if side == "buy" else entry - target_dist, 2)
        order = OrderRequest(symbol=symbol, side=side, qty=qty, stopLoss=sl, takeProfit=tp)
        rationale = (f"{bot.strategy} on {bot.timeframe}: {side} {qty:g} @ ~{entry:.2f}, SL {sl} ({stop_dist:.2f}), TP {tp} ({target_dist:.2f}), "
                     f"risking {qty * risk_unit:.2f} = {qty * risk_unit / equity:.1%} of equity")
        staged = await uta.stage(order, rationale, source="bot")
        res = await self._push_bot_commit(bot, uta, [staged["id"]], f"{side} {qty:g} {symbol} SL {sl} TP {tp} ({bot.strategy})")
        if res["status"] == "pushed":
            # the entry happens inside the bar now forming; time exits count completed bars from it
            st["openedBarT"] = int(now.timestamp()) // (minutes * 60) * (minutes * 60)
            self.events.emit("bot.trade", {"bot": bot.id, "symbol": symbol, "side": side, "qty": qty, "hash": res["hash"], "sl": sl, "tp": tp})
        self._record(bot, rationale + f" -> {res['status']}")
        return {"bot": bot.id, "actions": [{"symbol": symbol, "action": side, "qty": qty, "sl": sl, "tp": tp, **res}]}

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
