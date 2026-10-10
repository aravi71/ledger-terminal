"""Paper CFD broker: leverage, long and short, stop-loss / take-profit brackets.

Models a typical gold CFD account. One net position per symbol. Equity is
balance + unrealized P&L; margin = |qty| * price / leverage. Orders fill at
the current quote plus half the configured spread (buys) or minus it (sells).
Brackets are checked on every ``reconcile`` call (the server runs it every
minute) against the latest quote, so a quick spike between checks can be
missed: the live check is coarser than the backtest's bar high/low test.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import BrokerError, OrderRequest


class CfdPaperBroker:
    kind = "cfd-paper"

    def __init__(self, state_path: Path, market, starting_cash: float = 1000, leverage: float = 100,
                 spread: float = 0.30, commission: float = 0.035, min_qty: float = 1.0):
        self.path, self.market = state_path, market
        self.leverage, self.spread, self.commission, self.min_qty = leverage, spread, commission, min_qty
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"balance": float(starting_cash), "startingCash": float(starting_cash), "positions": {}, "openOrders": [], "fills": []}
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def _price(self, symbol: str) -> float:
        try:
            return float((await self.market.quote(symbol))["price"])
        except Exception as e:
            raise BrokerError(f"No price for {symbol}: {e}") from e

    async def account(self) -> dict[str, Any]:
        positions, unreal, margin = [], 0.0, 0.0
        for sym, p in sorted(self.state["positions"].items()):
            try:
                px = await self._price(sym)
            except BrokerError:
                px = p["avgPrice"]
            exit_px = px - self.spread / 2 if p["qty"] > 0 else px + self.spread / 2
            u = (exit_px - p["avgPrice"]) * p["qty"]
            unreal += u
            margin += abs(p["qty"]) * px / self.leverage
            positions.append({
                "symbol": sym, "qty": p["qty"], "avgPrice": p["avgPrice"], "marketPrice": px,
                "marketValue": p["qty"] * px, "unrealizedPnl": u,
                "unrealizedPnlPct": u / (abs(p["qty"]) * p["avgPrice"]) * 100 if p["avgPrice"] else 0,
                "stopLoss": p.get("sl"), "takeProfit": p.get("tp"),
            })
        equity = self.state["balance"] + unreal
        return {
            "broker": self.kind, "cash": self.state["balance"], "equity": equity, "buyingPower": max(0.0, (equity - margin) * self.leverage),
            "margin": margin, "leverage": self.leverage, "startingCash": self.state["startingCash"],
            "totalPnl": equity - self.state["startingCash"], "positions": positions, "openOrders": [],
        }

    def _fill(self, sym: str, side: str, qty: float, px: float, sl: float | None, tp: float | None, reason: str) -> dict[str, Any]:
        signed = qty if side == "buy" else -qty
        pos = self.state["positions"].get(sym)
        realized = -self.commission * qty
        if pos and (pos["qty"] > 0) != (signed > 0):  # reducing or flipping
            closing = min(abs(signed), abs(pos["qty"]))
            realized += (px - pos["avgPrice"]) * closing * (1 if pos["qty"] > 0 else -1)
            remaining = pos["qty"] + signed
            if abs(remaining) < 1e-9:
                self.state["positions"].pop(sym)
            elif (remaining > 0) == (pos["qty"] > 0):
                pos["qty"] = remaining
            else:
                self.state["positions"][sym] = {"qty": remaining, "avgPrice": px, "sl": sl, "tp": tp}
        elif pos:
            total = pos["qty"] + signed
            pos["avgPrice"] = (pos["qty"] * pos["avgPrice"] + signed * px) / total
            pos["qty"] = total
            pos["sl"], pos["tp"] = sl if sl is not None else pos.get("sl"), tp if tp is not None else pos.get("tp")
        else:
            self.state["positions"][sym] = {"qty": signed, "avgPrice": px, "sl": sl, "tp": tp}
        self.state["balance"] += realized
        fill = {"symbol": sym, "side": side, "qty": qty, "price": px, "realized": round(realized, 2), "reason": reason, "ts": time.time()}
        self.state["fills"].append(fill)
        return fill

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        oid = "cfd-" + uuid.uuid4().hex[:10]
        try:
            if order.type != "market":
                raise BrokerError("The CFD paper broker accepts market orders only (use stop-loss / take-profit for exits)")
            if order.qty < self.min_qty:
                raise BrokerError(f"Minimum size is {self.min_qty}")
            mid = await self._price(order.symbol)
            px = mid + self.spread / 2 if order.side == "buy" else mid - self.spread / 2
            sl, tp = order.stopLoss, order.takeProfit
            if order.side == "buy" and ((sl is not None and sl >= px) or (tp is not None and tp <= px)):
                raise BrokerError(f"For a buy, stop-loss must be below and take-profit above {px:.2f}")
            if order.side == "sell" and ((sl is not None and sl <= px) or (tp is not None and tp >= px)):
                raise BrokerError(f"For a sell, stop-loss must be above and take-profit below {px:.2f}")
            acct = await self.account()
            pos = self.state["positions"].get(order.symbol)
            reduces = pos and (pos["qty"] > 0) != (order.side == "buy") and order.qty <= abs(pos["qty"])
            if not reduces and acct["margin"] + order.qty * mid / self.leverage > acct["equity"]:
                raise BrokerError(f"Insufficient margin: need {order.qty * mid / self.leverage:,.2f} more, free {acct['equity'] - acct['margin']:,.2f}")
            self._fill(order.symbol, order.side, order.qty, px, sl, tp, "order")
            self._save()
            return {"status": "filled", "filledQty": order.qty, "avgFillPrice": px, "brokerOrderId": oid, "message": "filled at market"}
        except BrokerError as e:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": oid, "message": str(e)}

    async def close_position(self, symbol: str, reason: str = "close") -> dict[str, Any] | None:
        pos = self.state["positions"].get(symbol)
        if not pos:
            return None
        mid = await self._price(symbol)
        side = "sell" if pos["qty"] > 0 else "buy"
        px = mid - self.spread / 2 if side == "sell" else mid + self.spread / 2
        fill = self._fill(symbol, side, abs(pos["qty"]), px, None, None, reason)
        self._save()
        return fill

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"cancelled": False}

    async def reconcile(self) -> list[dict[str, Any]]:
        """Close positions whose stop-loss or take-profit has been reached; stop out at zero equity."""
        fills = []
        for sym, pos in list(self.state["positions"].items()):
            try:
                mid = await self._price(sym)
            except BrokerError:
                continue
            exit_px = mid - self.spread / 2 if pos["qty"] > 0 else mid + self.spread / 2
            long = pos["qty"] > 0
            sl, tp = pos.get("sl"), pos.get("tp")
            hit = None
            if sl is not None and (exit_px <= sl if long else exit_px >= sl):
                hit = "stop-loss"
            elif tp is not None and (exit_px >= tp if long else exit_px <= tp):
                hit = "take-profit"
            if hit:
                fills.append(self._fill(sym, "sell" if long else "buy", abs(pos["qty"]), exit_px, None, None, hit))
        acct = await self.account()
        if acct["positions"] and acct["equity"] <= acct["margin"] * 0.5:  # broker-style stop-out at 50% margin level
            for p in acct["positions"]:
                fills.append(await self.close_position(p["symbol"], "stop-out"))
        if fills:
            self._save()
        return fills
