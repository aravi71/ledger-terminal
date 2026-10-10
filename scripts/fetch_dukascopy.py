"""Download historical candles from Dukascopy's public datafeed into a local cache.

Usage:
    python scripts/fetch_dukascopy.py --symbol XAUUSD --tf hour   --start 2016-10 --end 2026-10
    python scripts/fetch_dukascopy.py --symbol XAUUSD --tf minute --start 2021-10 --end 2026-10

Files land in data/history/<SYMBOL>/<h1|m1>/ as CSV (time,open,high,low,close,volume,
UTC epoch seconds, BID prices). Re-running resumes: cached files are skipped.
The feed throttles aggressively (HTTP 503); the script backs off and retries
politely, and lists anything it could not fetch so you can re-run.
"""

from __future__ import annotations

import argparse
import calendar
import lzma
import struct
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://datafeed.dukascopy.com/datafeed"
POINT = {"XAUUSD": 1000, "XAGUSD": 1000, "EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 1000}


def decode(blob: bytes, start: datetime, point: int) -> list[tuple]:
    if not blob:
        return []
    raw = lzma.decompress(blob)
    rows = []
    t0 = int(start.timestamp())
    for i in range(len(raw) // 24):
        off, o, c, lo, hi, vol = struct.unpack(">5if", raw[i * 24 : (i + 1) * 24])
        if vol <= 0:  # filler candles while the market is closed
            continue
        rows.append((t0 + off, o / point, hi / point, lo / point, c / point, round(vol, 4)))
    return rows


def fetch(client: httpx.Client, url: str, tries: int = 8) -> bytes | None:
    delay = 4.0
    for _ in range(tries):
        try:
            r = client.get(url)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                return b""
        except httpx.HTTPError:
            pass
        time.sleep(delay)
        delay = min(delay * 1.8, 60)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", choices=["hour", "minute"], default="hour")
    ap.add_argument("--start", default="2016-10", help="YYYY-MM")
    ap.add_argument("--end", default=date.today().strftime("%Y-%m"), help="YYYY-MM (inclusive)")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    sym = args.symbol.upper()
    point = POINT.get(sym, 1000)
    out = ROOT / "data" / "history" / sym / ("h1" if args.tf == "hour" else "m1")
    out.mkdir(parents=True, exist_ok=True)
    y0, m0 = map(int, args.start.split("-"))
    y1, m1 = map(int, args.end.split("-"))
    today = datetime.now(timezone.utc).date()

    jobs: list[tuple[str, Path, datetime]] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        if args.tf == "hour":
            start = datetime(y, m, 1, tzinfo=timezone.utc)
            if start.date() <= today:
                jobs.append((f"{BASE}/{sym}/{y}/{m - 1:02d}/BID_candles_hour_1.bi5", out / f"{y}-{m:02d}.csv", start))
        else:
            for d in range(1, calendar.monthrange(y, m)[1] + 1):
                day = date(y, m, d)
                if day.weekday() == 5 or day >= today:  # Saturday has no trading
                    continue
                start = datetime(y, m, d, tzinfo=timezone.utc)
                jobs.append((f"{BASE}/{sym}/{y}/{m - 1:02d}/{d:02d}/BID_candles_min_1.bi5", out / f"{day.isoformat()}.csv", start))
        m += 1
        if m == 13:
            y, m = y + 1, 1

    # Newest first, so a partial run still leaves the most recent unbroken stretch.
    todo = [j for j in reversed(jobs) if not j[1].exists()]
    print(f"{sym} {args.tf}: {len(jobs)} files, {len(jobs) - len(todo)} cached, {len(todo)} to fetch", flush=True)
    failed = []
    with httpx.Client(timeout=60, headers={"User-Agent": "Mozilla/5.0 (LedgerTerminal research)"}) as client:
        for n, (url, path, start) in enumerate(todo, 1):
            blob = fetch(client, url)
            if blob is None:
                failed.append(path.name)
                print(f"  failed {path.name}", flush=True)
                continue
            rows = decode(blob, start, point)
            tmp = path.with_suffix(".tmp")
            tmp.write_text("time,open,high,low,close,volume\n" + "".join(",".join(map(str, r)) + "\n" for r in rows), encoding="utf-8")
            tmp.replace(path)
            if n % 25 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)} done ({path.name}, {len(rows)} bars)", flush=True)
            time.sleep(args.pause)
    if failed:
        print(f"{len(failed)} files failed; re-run the same command to retry them.", flush=True)
        sys.exit(1)
    print("complete", flush=True)


if __name__ == "__main__":
    main()
