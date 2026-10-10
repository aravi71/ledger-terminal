"""Alpaca adapter, deliberately limited to the PAPER trading endpoint.

Keys come from environment variables named in the account's brokerConfig
(never from the JSON file). The base URL is a constant, not configuration,
so this adapter cannot be pointed at a live-money account by editing config.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .base import BrokerError, OrderRequest

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class AlpacaPaperBroker:
    kind = "alpaca-paper"

    def __init__(self, key_id_env: str = "ALPACA_API_KEY_ID", secret_env: str = "ALPACA_API_SECRET_KEY"):
        key_id, secret = os.getenv(key_id_env), os.getenv(secret_env)
        if not key_id or not secret:
            raise BrokerError(f"Set {key_id_env} and {secret_env} to use the Alpaca paper account")
        self._client = httpx.AsyncClient(
            base_url=PAPER_BASE_URL,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=15,
        )

    async def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise BrokerError(f"Alpaca network error: {e}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise BrokerError(f"Alpaca {r.status_code}: {msg}")
        return r.json() if r.content else {}

    async def account(self) -> dict[str, Any]:
        acct = await self._req("GET", "/v2/account")
        positions = await self._req("GET", "/v2/positions")
        orders = await self._req("GET", "/v2/orders", params={"status": "open"})
        return {
            "broker": self.kind,
            "cash": float(acct["cash"]),
            "equity": float(acct["equity"]),
            "buyingPower": float(acct["buying_power"]),
            "totalPnl": float(acct["equity"]) - float(acct.get("last_equity", acct["equity"])),
            "positions": [
                {
                    "symbol": p["symbol"],
                    "qty": float(p["qty"]),
                    "avgPrice": float(p["avg_entry_price"]),
                    "marketPrice": float(p["current_price"]),
                    "marketValue": float(p["market_value"]),
                    "unrealizedPnl": float(p["unrealized_pl"]),
                    "unrealizedPnlPct": float(p["unrealized_plpc"]) * 100,
                }
                for p in positions
            ],
            "openOrders": [
                {"id": o["id"], "symbol": o["symbol"], "side": o["side"], "qty": float(o["qty"]), "type": o["type"], "limitPrice": float(o["limit_price"]) if o.get("limit_price") else None}
                for o in orders
            ],
        }

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        if order.stopLoss is not None or order.takeProfit is not None:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": None,
                    "message": "Stop-loss/take-profit brackets are not implemented for the Alpaca adapter yet"}
        body: dict[str, Any] = {"symbol": order.symbol, "qty": str(order.qty), "side": order.side, "type": order.type, "time_in_force": "day"}
        if order.type == "limit":
            body["limit_price"] = str(order.limitPrice)
        try:
            o = await self._req("POST", "/v2/orders", json=body)
        except BrokerError as e:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": None, "message": str(e)}
        filled = float(o.get("filled_qty") or 0)
        return {
            "status": "filled" if o.get("status") == "filled" else "accepted",
            "filledQty": filled,
            "avgFillPrice": float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
            "brokerOrderId": o.get("id"),
            "message": f"alpaca status: {o.get('status')}",
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        await self._req("DELETE", f"/v2/orders/{order_id}")
        return {"cancelled": True}

    async def reconcile(self) -> list[dict[str, Any]]:
        return []  # Alpaca fills resting orders server-side
