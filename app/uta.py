"""Unified Trading Accounts with a git-style order workflow.

Each UTA wraps one broker connection with its own guards and history.
Orders move through three steps:

    stage  -> an order sits in the staging area (anyone, including the agent)
    commit -> staged orders are bundled under a message and an 8-char hash
    push   -> guards run, orders go to the broker, the account is snapshotted

Push is the approval gate and is only exposed to the human (HTTP + UI);
the AI agent has no push tool. A commit is atomic at the guard stage: if any
order violates a guard, nothing in that commit is sent.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .brokers import AlpacaPaperBroker, BrokerError, CfdPaperBroker, Mt5DemoBroker, OrderRequest, PaperBroker
from .config import AccountConfig, Config
from .events import EventLog
from .guards import GuardContext, check_order


class TradingError(Exception):
    pass


class UTA:
    def __init__(self, cfg: AccountConfig, broker, market, events: EventLog, store: Path):
        self.cfg = cfg
        self.broker = broker
        self.market = market
        self.events = events
        self.store = store
        if store.exists():
            self.state = json.loads(store.read_text(encoding="utf-8"))
        else:
            self.state = {"staged": [], "commits": []}
            self._save()

    @property
    def id(self) -> str:
        return self.cfg.id

    def _save(self) -> None:
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.store)

    # --- helpers -------------------------------------------------------------
    def _commit(self, hash_: str) -> dict[str, Any]:
        for c in self.state["commits"]:
            if c["hash"] == hash_:
                return c
        raise TradingError(f"No commit {hash_} in {self.id}")

    def _activity(self) -> tuple[dict[str, float], int]:
        last: dict[str, float] = {}
        today = datetime.now(timezone.utc).date()
        count = 0
        for c in self.state["commits"]:
            if c["status"] not in ("pushed", "partial"):
                continue
            for r in c.get("results", []):
                if r["status"] in ("filled", "accepted"):
                    last[r["symbol"]] = max(last.get(r["symbol"], 0), c["pushedAt"])
                    if datetime.fromtimestamp(c["pushedAt"], timezone.utc).date() == today:
                        count += 1
        return last, count

    async def _preview(self, orders: list[OrderRequest]) -> list[dict[str, Any]]:
        """Run guards over a list of orders as if pushed in sequence."""
        acct = await self.broker.account()
        held = {p["symbol"]: p["qty"] for p in acct["positions"]}
        last, count = self._activity()
        now = time.time()
        out = []
        for o in orders:
            try:
                price = o.limitPrice if o.type == "limit" else float((await self.market.quote(o.symbol))["price"])
                ctx = GuardContext(position_qty=held.get(o.symbol, 0.0), last_trade_ts=last, orders_today=count, now=now)
                violations = check_order(o, price, self.cfg.guards, ctx)
            except Exception as e:  # unpriceable order is itself a violation
                price, violations = None, [f"price: {e}"]
            out.append({"order": o.model_dump(), "price": price, "notional": o.qty * price if price else None, "violations": violations})
            held[o.symbol] = held.get(o.symbol, 0.0) + (o.qty if o.side == "buy" else -o.qty)
            count += 1
        return out

    # --- git verbs ------------------------------------------------------------
    async def stage(self, order: OrderRequest, rationale: str = "", source: str = "user") -> dict[str, Any]:
        preview = (await self._preview([order]))[0]
        item = {
            "id": uuid.uuid4().hex[:8],
            "order": order.model_dump(),
            "rationale": rationale.strip()[:2000],
            "source": source,
            "stagedAt": time.time(),
            "priceAtStage": preview["price"],
            "warnings": preview["violations"],
        }
        self.state["staged"].append(item)
        self._save()
        self.events.emit("trade.stage", {"account": self.id, **item})
        return item

    def unstage(self, staged_id: str) -> None:
        before = len(self.state["staged"])
        self.state["staged"] = [s for s in self.state["staged"] if s["id"] != staged_id]
        if len(self.state["staged"]) == before:
            raise TradingError(f"No staged order {staged_id}")
        self._save()
        self.events.emit("trade.unstage", {"account": self.id, "id": staged_id})

    def commit(self, message: str, author: str = "user", staged_ids: list[str] | None = None) -> dict[str, Any]:
        """Commit all staged orders, or only ``staged_ids`` (bots use this so
        they never sweep up orders a person staged by hand)."""
        message = (message or "").strip()
        if not message:
            raise TradingError("Commit message is required")
        chosen = [s for s in self.state["staged"] if staged_ids is None or s["id"] in staged_ids]
        if not chosen:
            raise TradingError("Nothing staged to commit")
        parent = self.state["commits"][-1]["hash"] if self.state["commits"] else None
        created = time.time()
        body = json.dumps({"parent": parent, "message": message, "orders": chosen, "ts": created}, sort_keys=True)
        commit = {
            "hash": hashlib.sha256(body.encode()).hexdigest()[:8],
            "parent": parent,
            "message": message[:1000],
            "author": author,
            "createdAt": created,
            "orders": chosen,
            "status": "committed",
        }
        self.state["commits"].append(commit)
        picked = {s["id"] for s in chosen}
        self.state["staged"] = [s for s in self.state["staged"] if s["id"] not in picked]
        self._save()
        self.events.emit("trade.commit", {"account": self.id, "hash": commit["hash"], "message": commit["message"], "author": author, "orders": len(commit["orders"])})
        return commit

    def revert(self, hash_: str) -> dict[str, Any]:
        c = self._commit(hash_)
        if c["status"] != "committed":
            raise TradingError(f"Only unpushed commits can be reverted ({hash_} is {c['status']})")
        c["status"] = "reverted"
        c["revertedAt"] = time.time()
        self._save()
        self.events.emit("trade.revert", {"account": self.id, "hash": hash_})
        return c

    async def push(self, hash_: str | None = None, approver: str = "user") -> list[dict[str, Any]]:
        """Execute pending commits (all, or one by hash) in order."""
        pending = [c for c in self.state["commits"] if c["status"] == "committed" and (hash_ is None or c["hash"] == hash_)]
        if not pending:
            raise TradingError("No committed, unpushed commits" + (f" matching {hash_}" if hash_ else ""))
        outcomes = []
        for c in pending:
            orders = [OrderRequest.model_validate(s["order"]) for s in c["orders"]]
            checks = await self._preview(orders)
            c["guardResults"] = checks
            if any(ch["violations"] for ch in checks):
                c["status"] = "rejected"
                c["pushedAt"] = time.time()
                c["approver"] = approver
                self._save()
                self.events.emit("trade.rejected", {"account": self.id, "hash": c["hash"], "violations": [v for ch in checks for v in ch["violations"]]})
                outcomes.append(c)
                continue
            results = []
            for o in orders:
                r = await self.broker.place_order(o)
                results.append({"symbol": o.symbol, "side": o.side, "qty": o.qty, **r})
            c["results"] = results
            ok = [r for r in results if r["status"] != "rejected"]
            c["status"] = "pushed" if len(ok) == len(results) else "partial" if ok else "failed"
            c["pushedAt"] = time.time()
            c["approver"] = approver
            snap = await self.broker.account()
            c["snapshot"] = {"cash": snap["cash"], "equity": snap["equity"], "positions": {p["symbol"]: p["qty"] for p in snap["positions"]}}
            self._save()
            self.events.emit("trade.push", {"account": self.id, "hash": c["hash"], "status": c["status"], "results": results, "equity": snap["equity"]})
            outcomes.append(c)
        return outcomes

    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {k: c.get(k) for k in ("hash", "parent", "message", "author", "createdAt", "status", "pushedAt", "approver")} | {"orders": len(c["orders"])}
            for c in reversed(self.state["commits"][-limit:])
        ]

    def show(self, hash_: str) -> dict[str, Any]:
        return self._commit(hash_)

    async def summary(self) -> dict[str, Any]:
        try:
            acct = await self.broker.account()
            error = None
        except BrokerError as e:
            acct, error = None, str(e)
        return {
            "id": self.id,
            "label": self.cfg.label or self.id,
            "type": self.cfg.type,
            "guards": self.cfg.guards.model_dump(),
            "account": acct,
            "error": error,
            "staged": self.state["staged"],
            "pending": [c for c in self.state["commits"] if c["status"] == "committed"],
            "log": self.log(25),
        }


class UTAManager:
    def __init__(self, config: Config, market, events: EventLog):
        self.config = config
        self.market = market
        self.events = events
        self.utas: dict[str, UTA] = {}
        self.errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.utas.clear()
        self.errors.clear()
        for acc in self.config.accounts:
            if not acc.enabled:
                continue
            try:
                broker = self._broker(acc)
            except BrokerError as e:
                self.errors[acc.id] = str(e)
                continue
            self.utas[acc.id] = UTA(acc, broker, self.market, self.events, self.config.data_dir / "utas" / f"{acc.id}.json")

    def _broker(self, acc: AccountConfig):
        bc = acc.brokerConfig
        if acc.type == "paper":
            return PaperBroker(
                self.config.data_dir / "brokers" / f"{acc.id}.json",
                self.market,
                starting_cash=float(bc.get("startingCash", 100_000)),
                slippage_bps=float(bc.get("slippageBps", 2)),
                commission=float(bc.get("commissionPerOrder", 0)),
                allow_short=acc.guards.allowShort,
            )
        if acc.type == "cfd":
            return CfdPaperBroker(
                self.config.data_dir / "brokers" / f"{acc.id}.json",
                self.market,
                starting_cash=float(bc.get("startingCash", 1000)),
                leverage=float(bc.get("leverage", 100)),
                spread=float(bc.get("spread", 0.30)),
                commission=float(bc.get("commissionPerUnitSide", 0.035)),
                min_qty=float(bc.get("minQty", 1)),
            )
        if acc.type == "alpaca":
            return AlpacaPaperBroker(bc.get("keyIdEnv", "ALPACA_API_KEY_ID"), bc.get("secretKeyEnv", "ALPACA_API_SECRET_KEY"))
        if acc.type == "mt5":
            return Mt5DemoBroker(bc.get("symbolMap"), bc.get("terminalPath"))
        raise BrokerError(f"Unknown account type {acc.type}")

    def get(self, account_id: str) -> UTA:
        if account_id not in self.utas:
            raise TradingError(f"Unknown or disabled account {account_id!r}. Available: {list(self.utas)}")
        return self.utas[account_id]
