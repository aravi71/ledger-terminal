"""Market data: quotes, OHLCV history, symbol search and news.

Two providers share one interface:
- ``YahooMarketData``: live (delayed) data from Yahoo Finance's public chart,
  search and RSS endpoints. No API key. Covers equities, ETFs, crypto
  (``BTC-USD``), futures (``GC=F``) and FX (``EURUSD=X``).
- ``DemoMarketData``: deterministic synthetic prices so the app and its tests
  run offline.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-=^]{1,20}$")
RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
INTERVALS = {"1m", "5m", "15m", "30m", "60m", "1h", "1d", "1wk", "1mo"}
_UA = {"User-Agent": "Mozilla/5.0 (LedgerTerminal; +local)"}


class MarketDataError(Exception):
    pass


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise MarketDataError(f"Invalid symbol: {symbol!r}")
    return s


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (time.monotonic() + ttl, value)


class YahooMarketData:
    mode = "yahoo"
    CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
    RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"

    def __init__(self, quote_ttl: float = 20, history_ttl: float = 300):
        self._client = httpx.AsyncClient(headers=_UA, timeout=12, follow_redirects=True)
        self._cache = _TTLCache()
        self.quote_ttl = quote_ttl
        self.history_ttl = history_ttl

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chart(self, symbol: str, range_: str, interval: str) -> dict[str, Any]:
        try:
            r = await self._client.get(self.CHART.format(symbol=symbol), params={"range": range_, "interval": interval})
        except httpx.HTTPError as e:
            raise MarketDataError(f"Network error fetching {symbol}: {e}") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        chart = data.get("chart") or {}
        if r.status_code != 200 or chart.get("error") or not chart.get("result"):
            msg = (chart.get("error") or {}).get("description") or f"HTTP {r.status_code}"
            raise MarketDataError(f"No data for {symbol}: {msg}")
        return chart["result"][0]

    async def history(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        if range_ not in RANGES or interval not in INTERVALS:
            raise MarketDataError("Unsupported range/interval")
        key = f"h:{symbol}:{range_}:{interval}"
        if (hit := self._cache.get(key)) is not None:
            return hit
        res = await self._chart(symbol, range_, interval)
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        bars = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            bars.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v or 0})
        self._cache.set(key, bars, self.history_ttl if interval in ("1d", "1wk", "1mo") else 60)
        return bars

    async def quote(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        key = f"q:{symbol}"
        if (hit := self._cache.get(key)) is not None:
            return hit
        res = await self._chart(symbol, "5d", "1d")
        meta = res.get("meta") or {}
        closes = [c for c in (((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []) if c is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        if price is None:
            raise MarketDataError(f"No price for {symbol}")
        prev = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose") or price
        quote = {
            "symbol": symbol,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "price": float(price),
            "prevClose": float(prev),
            "change": float(price) - float(prev),
            "changePct": (float(price) / float(prev) - 1) * 100 if prev else 0.0,
            "dayHigh": meta.get("regularMarketDayHigh"),
            "dayLow": meta.get("regularMarketDayLow"),
            "volume": meta.get("regularMarketVolume"),
            "high52w": meta.get("fiftyTwoWeekHigh"),
            "low52w": meta.get("fiftyTwoWeekLow"),
            "currency": meta.get("currency") or "USD",
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
            "instrumentType": meta.get("instrumentType") or "",
            "marketTime": meta.get("regularMarketTime"),
            "source": "yahoo (delayed)",
        }
        self._cache.set(key, quote, self.quote_ttl)
        return quote

    async def search(self, query: str) -> list[dict[str, Any]]:
        query = (query or "").strip()[:60]
        if not query:
            return []
        try:
            r = await self._client.get(self.SEARCH, params={"q": query, "quotesCount": 8, "newsCount": 0})
            quotes = r.json().get("quotes", [])
        except (httpx.HTTPError, ValueError) as e:
            raise MarketDataError(f"Search failed: {e}") from e
        return [
            {
                "symbol": q.get("symbol"),
                "name": q.get("longname") or q.get("shortname") or q.get("symbol"),
                "type": q.get("typeDisp") or q.get("quoteType"),
                "exchange": q.get("exchDisp") or q.get("exchange"),
            }
            for q in quotes
            if q.get("symbol")
        ]

    async def news(self, symbol: str | None = None, limit: int = 15) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol) if symbol else "^GSPC"
        key = f"n:{symbol}"
        if (hit := self._cache.get(key)) is not None:
            return hit[:limit]
        try:
            r = await self._client.get(self.RSS, params={"s": symbol, "region": "US", "lang": "en-US"})
            root = ET.fromstring(r.text)
        except (httpx.HTTPError, ET.ParseError) as e:
            raise MarketDataError(f"News failed: {e}") from e
        items = []
        for item in root.iter("item"):
            pub = item.findtext("pubDate")
            try:
                ts = parsedate_to_datetime(pub).timestamp() if pub else None
            except (TypeError, ValueError):
                ts = None
            items.append(
                {
                    "title": (item.findtext("title") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "summary": (item.findtext("description") or "").strip()[:400],
                    "published": ts,
                    "source": "Yahoo Finance",
                }
            )
        self._cache.set(key, items, 300)
        return items[:limit]


# ---------------------------------------------------------------------------
# Offline demo provider
# ---------------------------------------------------------------------------

_DEMO_UNIVERSE = {
    "SPY": ("SPDR S&P 500 ETF", "ETF", 560),
    "QQQ": ("Invesco QQQ Trust", "ETF", 480),
    "AAPL": ("Apple Inc.", "Equity", 225),
    "MSFT": ("Microsoft Corporation", "Equity", 430),
    "NVDA": ("NVIDIA Corporation", "Equity", 120),
    "AMZN": ("Amazon.com, Inc.", "Equity", 185),
    "TSLA": ("Tesla, Inc.", "Equity", 240),
    "BTC-USD": ("Bitcoin USD", "Cryptocurrency", 62000),
    "ETH-USD": ("Ethereum USD", "Cryptocurrency", 2600),
    "GC=F": ("Gold Futures", "Futures", 2650),
    "EURUSD=X": ("EUR/USD", "Currency", 1.09),
}


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)


class DemoMarketData:
    """Geometric-Brownian-motion prices, seeded per symbol, stable within a day."""

    mode = "demo"

    async def aclose(self) -> None:
        return None

    def _bars(self, symbol: str, days: int = 3700) -> list[dict[str, Any]]:
        name, _type, base = _DEMO_UNIVERSE.get(symbol, (symbol, "Equity", 20 + _seed(symbol) % 300))
        rng = random.Random(_seed(symbol))
        vol = 0.035 if _type == "Cryptocurrency" else 0.006 if _type == "Currency" else 0.016
        drift = 0.0003
        end = datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0)
        price = base * 0.8
        bars = []
        for i in range(days):
            day = end - timedelta(days=days - 1 - i)
            if _type not in ("Cryptocurrency",) and day.weekday() >= 5:
                continue
            ret = drift + vol * rng.gauss(0, 1)
            o = price
            c = max(0.01, price * math.exp(ret))
            h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            l = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            bars.append({"t": int(day.timestamp()), "o": o, "h": h, "l": l, "c": c, "v": int(1e6 * (1 + rng.random()))})
            price = c
        # Anchor the path so the latest close sits at the symbol's reference price.
        k = base / bars[-1]["c"]
        for b in bars:
            b["o"], b["h"], b["l"], b["c"] = b["o"] * k, b["h"] * k, b["l"] * k, b["c"] * k
        return bars

    async def history(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)
        if range_ not in RANGES or interval not in INTERVALS:
            raise MarketDataError("Unsupported range/interval")
        days = {"1d": 2, "5d": 5, "1mo": 22, "3mo": 66, "6mo": 128, "ytd": 200, "1y": 252, "2y": 504, "5y": 1260, "10y": 2520}.get(range_, 2520)
        return self._bars(symbol)[-days:]

    async def quote(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        bars = self._bars(symbol)
        last, prev = bars[-1], bars[-2]
        name = _DEMO_UNIVERSE.get(symbol, (symbol,))[0]
        year = bars[-252:]
        return {
            "symbol": symbol,
            "name": name,
            "price": last["c"],
            "prevClose": prev["c"],
            "change": last["c"] - prev["c"],
            "changePct": (last["c"] / prev["c"] - 1) * 100,
            "dayHigh": last["h"],
            "dayLow": last["l"],
            "volume": last["v"],
            "high52w": max(b["h"] for b in year),
            "low52w": min(b["l"] for b in year),
            "currency": "USD",
            "exchange": "DEMO",
            "instrumentType": _DEMO_UNIVERSE.get(symbol, ("", "Equity"))[1],
            "marketTime": last["t"],
            "source": "demo (synthetic)",
        }

    async def search(self, query: str) -> list[dict[str, Any]]:
        q = (query or "").strip().upper()
        return [
            {"symbol": s, "name": n, "type": t, "exchange": "DEMO"}
            for s, (n, t, _b) in _DEMO_UNIVERSE.items()
            if q and (q in s or q in n.upper())
        ]

    async def news(self, symbol: str | None = None, limit: int = 15) -> list[dict[str, Any]]:
        sym = normalize_symbol(symbol) if symbol else "MARKET"
        now = time.time()
        templates = [
            "{s} trades in focus as investors weigh rate outlook",
            "Analysts revisit {s} estimates ahead of earnings",
            "Options activity in {s} picks up into week's end",
            "{s}: what the latest macro data means for positioning",
        ]
        return [
            {"title": t.format(s=sym), "link": "", "summary": "Synthetic headline (demo mode).", "published": now - i * 3600, "source": "Demo"}
            for i, t in enumerate(templates)
        ][:limit]


def create_market_data(mode: str) -> YahooMarketData | DemoMarketData:
    return DemoMarketData() if mode == "demo" else YahooMarketData()
