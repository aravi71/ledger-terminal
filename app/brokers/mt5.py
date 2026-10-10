"""MetaTrader 5 adapter (for an XM demo account), restricted to DEMO accounts.

It attaches to a MetaTrader 5 terminal already running on this Windows machine
and logged in by the user. No login, password or server is ever stored or sent
by this app. Every call checks the terminal's account trade mode and refuses to
trade unless it is a demo account, so a live login cannot be traded by mistake.

Quantities in the app are units (ounces of gold, index points per $1); they are
converted to lots with the symbol's contract size and rounded to its volume step.
App symbols are mapped to the broker's names with brokerConfig.symbolMap.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import time
from typing import Any

from .base import BrokerError, OrderRequest

DEFAULT_SYMBOL_MAP = {"GC=F": "GOLD", "XAUUSD": "GOLD", "YM=F": "US30Cash", "US30": "US30Cash", "NQ=F": "US100Cash", "NAS100": "US100Cash"}
MAGIC = 771_771  # tags orders placed by this app


def _mt5():
    try:
        import MetaTrader5 as mt5  # Windows only
    except ImportError as e:
        raise BrokerError("Install the MetaTrader5 package (pip install MetaTrader5) on Windows") from e
    return mt5


class Mt5DemoBroker:
    kind = "mt5-demo"

    def __init__(self, symbol_map: dict[str, str] | None = None, terminal_path: str | None = None, deviation: int = 30):
        self.symbol_map = {**DEFAULT_SYMBOL_MAP, **(symbol_map or {})}
        self.terminal_path = terminal_path
        self.deviation = deviation
        self._lock = asyncio.Lock()
        self._seen_deals: set[int] = set()
        self._since = time.time()
        self._proc_checked = (0.0, False)

    # ---------------------------------------------------------------- plumbing
    def _terminal_running(self) -> bool:
        """Is a MetaTrader 5 terminal process open? initialize() would otherwise try to launch
        one and block the whole server (the library holds the interpreter lock while it waits)."""
        if sys.platform != "win32":
            return True
        now = time.time()
        if now - self._proc_checked[0] < 5:
            return self._proc_checked[1]
        exe = os.path.basename(self.terminal_path) if self.terminal_path else "terminal64.exe"
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"], capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            running = exe.lower() in out.lower()
        except (OSError, subprocess.SubprocessError):
            running = False
        self._proc_checked = (now, running)
        return running

    def _connect(self):
        mt5 = _mt5()
        if not self._terminal_running():
            raise BrokerError("MetaTrader 5 is not open. Open it and log in to your XM demo account.")
        ok = mt5.initialize(self.terminal_path, timeout=8000) if self.terminal_path else mt5.initialize(timeout=8000)
        if not ok:
            raise BrokerError(f"Cannot reach the MetaTrader 5 terminal ({mt5.last_error()}). Open MetaTrader 5 and log in to your XM demo account.")
        info = mt5.account_info()
        if info is None:
            raise BrokerError("MetaTrader 5 is open but not logged in. Log in to your XM demo account (File → Login to Trade Account).")
        if info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise BrokerError("The MetaTrader 5 terminal is logged in to a REAL account. This app only trades demo accounts: switch to your XM demo login.")
        return mt5, info

    def _sym(self, mt5, symbol: str) -> tuple[str, Any]:
        name = self.symbol_map.get(symbol.upper(), symbol.upper())
        si = mt5.symbol_info(name)
        if si is None:
            raise BrokerError(f"{name} not found at this broker. Set brokerConfig.symbolMap for {symbol} (e.g. GOLD, US30Cash, US100Cash).")
        if not si.visible:
            mt5.symbol_select(name, True)
        return name, si

    def _app_symbol(self, broker_name: str) -> str:
        for app, b in self.symbol_map.items():
            if b == broker_name and "=" in app:
                return app
        return broker_name

    async def _run(self, fn, *a):
        try:
            async with asyncio.timeout(20):
                async with self._lock:
                    return await asyncio.to_thread(fn, *a)
        except TimeoutError as e:
            raise BrokerError("MetaTrader 5 did not respond in 20 s. Is the terminal open and logged in?") from e

    # ---------------------------------------------------------------- broker protocol
    def _account(self) -> dict[str, Any]:
        mt5, info = self._connect()
        positions = []
        for p in mt5.positions_get() or []:
            si = mt5.symbol_info(p.symbol)
            cs = si.trade_contract_size if si else 1
            sign = 1 if p.type == mt5.POSITION_TYPE_BUY else -1
            qty = sign * p.volume * cs
            positions.append({
                "symbol": self._app_symbol(p.symbol), "brokerSymbol": p.symbol, "ticket": p.ticket, "qty": qty, "lots": p.volume,
                "avgPrice": p.price_open, "marketPrice": p.price_current, "marketValue": qty * p.price_current,
                "unrealizedPnl": p.profit + p.swap, "unrealizedPnlPct": (p.profit / (abs(qty) * p.price_open) * 100) if p.price_open else 0,
                "stopLoss": p.sl or None, "takeProfit": p.tp or None,
            })
        return {
            "broker": self.kind, "login": info.login, "server": info.server, "currency": info.currency,
            "cash": info.balance, "equity": info.equity, "margin": info.margin, "buyingPower": info.margin_free * (info.leverage or 1),
            "leverage": info.leverage, "startingCash": info.balance, "totalPnl": info.profit, "positions": positions, "openOrders": [],
        }

    async def account(self) -> dict[str, Any]:
        return await self._run(self._account)

    def _quote(self, symbol: str) -> dict[str, float]:
        mt5, _ = self._connect()
        name, _si = self._sym(mt5, symbol)
        t = mt5.symbol_info_tick(name)
        if t is None or not t.bid:
            raise BrokerError(f"No price for {name} (market closed?)")
        return {"bid": t.bid, "ask": t.ask, "price": (t.bid + t.ask) / 2, "spread": t.ask - t.bid, "time": t.time}

    async def quote(self, symbol: str) -> dict[str, float]:
        return await self._run(self._quote, symbol)

    def _spec(self, symbol: str) -> dict[str, float]:
        mt5, _ = self._connect()
        _name, si = self._sym(mt5, symbol)
        return {"contractSize": si.trade_contract_size, "minLots": si.volume_min, "lotStep": si.volume_step,
                "minQty": si.volume_min * si.trade_contract_size, "qtyStep": si.volume_step * si.trade_contract_size, "digits": si.digits}

    async def spec(self, symbol: str) -> dict[str, float]:
        return await self._run(self._spec, symbol)

    def _send(self, mt5, req: dict) -> Any:
        res = mt5.order_send(req)
        if res is None:
            raise BrokerError(f"order_send failed: {mt5.last_error()}")
        if res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL):
            raise BrokerError(f"Rejected by MetaTrader 5: {res.comment} (code {res.retcode})")
        return res

    def _filling(self, mt5, si) -> int:
        modes = si.filling_mode  # bit flags: 1 = FOK, 2 = IOC
        if modes & 1:
            return mt5.ORDER_FILLING_FOK
        if modes & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _place(self, order: OrderRequest) -> dict[str, Any]:
        mt5, _ = self._connect()
        name, si = self._sym(mt5, order.symbol)
        if order.type != "market":
            raise BrokerError("Only market orders (with stop-loss / take-profit) are supported")
        lots = math.floor(order.qty / si.trade_contract_size / si.volume_step + 1e-9) * si.volume_step
        lots = round(min(lots, si.volume_max), 8)
        if lots < si.volume_min:
            raise BrokerError(f"{order.qty:g} units is below {name}'s minimum of {si.volume_min} lots ({si.volume_min * si.trade_contract_size:g} units)")
        buy = order.side == "buy"
        # Close opposite positions first (XM accounts are hedging: a sell would otherwise open a second position).
        remaining = lots
        for p in mt5.positions_get(symbol=name) or []:
            if remaining <= 0 or (p.type == mt5.POSITION_TYPE_BUY) == buy:
                continue
            vol = min(p.volume, remaining)
            tick = mt5.symbol_info_tick(name)
            self._send(mt5, {"action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": vol, "position": p.ticket,
                             "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": tick.ask if buy else tick.bid,
                             "deviation": self.deviation, "magic": MAGIC, "comment": "ledger close", "type_filling": self._filling(mt5, si)})
            remaining = round(remaining - vol, 8)
        price = None
        if remaining > 0:
            tick = mt5.symbol_info_tick(name)
            px = tick.ask if buy else tick.bid
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": remaining, "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
                   "price": px, "deviation": self.deviation, "magic": MAGIC, "comment": "ledger", "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": self._filling(mt5, si)}
            if order.stopLoss:
                req["sl"] = round(order.stopLoss, si.digits)
            if order.takeProfit:
                req["tp"] = round(order.takeProfit, si.digits)
            res = self._send(mt5, req)
            price = res.price or px
        return {"status": "filled", "filledQty": lots * si.trade_contract_size, "avgFillPrice": price, "brokerOrderId": None,
                "message": f"{order.side} {lots} lots {name} on the MT5 demo account"}

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        try:
            return await self._run(self._place, order)
        except BrokerError as e:
            return {"status": "rejected", "filledQty": 0, "avgFillPrice": None, "brokerOrderId": None, "message": str(e)}

    def _close(self, symbol: str, reason: str) -> dict[str, Any] | None:
        mt5, _ = self._connect()
        name, si = self._sym(mt5, symbol)
        realized, qty, px, side = 0.0, 0.0, None, None
        for p in mt5.positions_get(symbol=name) or []:
            buy_pos = p.type == mt5.POSITION_TYPE_BUY
            tick = mt5.symbol_info_tick(name)
            px = tick.bid if buy_pos else tick.ask
            self._send(mt5, {"action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": p.volume, "position": p.ticket,
                             "type": mt5.ORDER_TYPE_SELL if buy_pos else mt5.ORDER_TYPE_BUY, "price": px, "deviation": self.deviation,
                             "magic": MAGIC, "comment": f"ledger {reason}"[:31], "type_filling": self._filling(mt5, si)})
            realized += p.profit + p.swap
            qty += p.volume * si.trade_contract_size
            side = "sell" if buy_pos else "buy"
        if not side:
            return None
        return {"symbol": symbol, "side": side, "qty": qty, "price": px, "realized": round(realized, 2), "reason": reason, "ts": time.time()}

    async def close_position(self, symbol: str, reason: str = "close") -> dict[str, Any] | None:
        return await self._run(self._close, symbol, reason)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"cancelled": False}

    def _reconcile(self) -> list[dict[str, Any]]:
        """Report stop-loss / take-profit exits that the broker executed."""
        from datetime import datetime, timezone
        mt5, _ = self._connect()
        deals = mt5.history_deals_get(datetime.fromtimestamp(self._since - 60, timezone.utc), datetime.now(timezone.utc)) or []
        fills = []
        for d in deals:
            if d.ticket in self._seen_deals or d.entry != mt5.DEAL_ENTRY_OUT:
                continue
            self._seen_deals.add(d.ticket)
            if d.reason in (mt5.DEAL_REASON_SL, mt5.DEAL_REASON_TP, mt5.DEAL_REASON_SO):
                reason = {mt5.DEAL_REASON_SL: "stop-loss", mt5.DEAL_REASON_TP: "take-profit", mt5.DEAL_REASON_SO: "stop-out"}[d.reason]
                fills.append({"symbol": self._app_symbol(d.symbol), "side": "sell" if d.type == mt5.DEAL_TYPE_SELL else "buy",
                              "qty": d.volume, "price": d.price, "realized": round(d.profit + d.swap + d.commission, 2), "reason": reason, "ts": d.time})
        return fills

    async def reconcile(self) -> list[dict[str, Any]]:
        try:
            return await self._run(self._reconcile)
        except BrokerError:
            return []

    # Status for the UI, without raising
    async def status(self) -> dict[str, Any]:
        def go():
            try:
                mt5, info = self._connect()
                term = mt5.terminal_info()
                return {"connected": True, "demo": True, "algoTrading": bool(term and term.trade_allowed), "login": info.login, "server": info.server, "balance": info.balance,
                        "equity": info.equity, "currency": info.currency, "leverage": info.leverage, "company": info.company}
            except BrokerError as e:
                return {"connected": False, "message": str(e)}
        try:
            return await self._run(go)
        except BrokerError as e:
            return {"connected": False, "message": str(e)}
