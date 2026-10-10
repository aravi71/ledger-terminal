"""Local simulated broker.

Fills market orders at the current quote (plus configurable slippage), rests
limit orders until they become marketable, and never allows margin or (unless
explicitly configured) short selling. State persists to a JSON file.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import BrokerError, OrderRequest


class PaperBroker:
    kind = "paper"

    def __init__(self, state_path: Path, market, starting_cash: float = 100_000, slippage_bps: float = 2, commission: float = 0, allow_short: bool = False):
        self.path = state_path
        self.market = market
        self.slippage = slippage_bps / 10_000
        self.commission = commission
        self.allow_short = allow_short
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"cash": float(starting_cash), "startingCash": float(starting_cash), "positions": {}, "openOrders": [], "fills": []}
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def _price(self, symbol: str) -> float:
        try:
            return float((await self.market.quote(symbol))["price"])
        except Exception as e:  # market errors surface as broker rejections
            raise BrokerError(f"No price for {symbol}: {e}") from e

    async def account(self) -> dict[str, Any]:
        positions = []
        equity = self.state["cash"]
        for sym, p in sorted(self.state["positions"].items()):
            try:
                px = await self._price(sym)
            except BrokerError:
                px = p["avgPrice"]
            mv = p["qty"] * px
            equity += mv
            positions.append({
                "symbol": sym,
                "qty": p["qty"],
                "avgPrice": p["avgPrice"],
                "marketPrice": px,
                "marketValue": mv,
                "unrealizedPnl": (px - p["avgPrice"]) * p["qty"],
                "unrealizedPnlPct": (px / p["avgPrice"] - 1) * 100 if p["avgPrice"] else 0,
            })
        return {
            "broker": self.kind,
            "cash": self.state["cash"],
            "equity": equity,
            "buyingPower": self.state["cash"],
            "startingCash": self.state.get("startingCash"),
            "totalPnl": equity - self.state.get("startingCash", equity),
            "positions": positions,
            "openOrders": list(self.state["openOrders"]),
        }

    def _apply_fill(self, order: dict[str, Any], price: float) -> dict[str, Any]:
        sym, qty, side = order["symbol"], float(order["qty"]), order["side"]
        pos = self.state["positions"].get(sym, {"qty": 0.0, "avgPrice": 0.0})
        if side == "buy":
            cost = qty * price + self.commission
            if cost > self.state["cash"] + 1e-9:
                raise BrokerError(f"Insufficient cash: need {cost:,.2f}, have {self.state['cash']:,.2f}")
            new_qty = pos["qty"] + qty
            pos["avgPrice"] = (pos["qty"] * pos["avgPrice"] + qty * price) / new_qty if new_qty else 0.0
            pos["qty"] = new_qty
            self.state["cash"] -= cost
        else:
            if not self.allow_short and qty > pos["qty"] + 1e-9:
                raise BrokerError(f"Cannot sell {qty:g} {sym}: only {pos['qty']:g} held (short selling disabled)")
            pos["qty"] -= qty
            self.state["cash"] += qty * price - self.commission
        if abs(pos["qty"]) < 1e-9:
            self.state["positions"].pop(sym, None)
        else:
            self.state["positions"][sym] = pos
        fill = {"orderId": order["id"], "symbol": sym, "side": side, "qty": qty, "price": price, "ts": time.time()}
        self.state["fills"].append(fill)
        return fill

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        oid = "pp-" + uuid.uuid4().hex[:10]
        record = {"id": oid, **order.model_dump(), "createdAt": time.time()}
        if order.stopLoss is not None or order.takeProfit is not None:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": oid,
                    "message": "This cash paper account has no stop-loss/take-profit orders; use a CFD account (type 'cfd')"}
        try:
            px = await self._price(order.symbol)
            if order.type == "market":
                fill_px = px * (1 + self.slippage if order.side == "buy" else 1 - self.slippage)
                self._apply_fill(record, fill_px)
                self._save()
                return {"status": "filled", "filledQty": order.qty, "avgFillPrice": fill_px, "brokerOrderId": oid, "message": "filled at market"}
            marketable = (order.side == "buy" and px <= order.limitPrice) or (order.side == "sell" and px >= order.limitPrice)
            if marketable:
                self._apply_fill(record, px)
                self._save()
                return {"status": "filled", "filledQty": order.qty, "avgFillPrice": px, "brokerOrderId": oid, "message": "limit marketable, filled"}
            self.state["openOrders"].append(record)
            self._save()
            return {"status": "accepted", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": oid, "message": f"resting limit @ {order.limitPrice}"}
        except BrokerError as e:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": oid, "message": str(e)}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        before = len(self.state["openOrders"])
        self.state["openOrders"] = [o for o in self.state["openOrders"] if o["id"] != order_id]
        self._save()
        return {"cancelled": before != len(self.state["openOrders"])}

    async def reconcile(self) -> list[dict[str, Any]]:
        fills, still_open = [], []
        for o in self.state["openOrders"]:
            try:
                px = await self._price(o["symbol"])
            except BrokerError:
                still_open.append(o)
                continue
            if (o["side"] == "buy" and px <= o["limitPrice"]) or (o["side"] == "sell" and px >= o["limitPrice"]):
                try:
                    fills.append(self._apply_fill(o, px))
                except BrokerError as e:
                    fills.append({"orderId": o["id"], "symbol": o["symbol"], "rejected": str(e)})
            else:
                still_open.append(o)
        if fills:
            self.state["openOrders"] = still_open
            self._save()
        return fills
