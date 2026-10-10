# Ledger Terminal

A local, Bloomberg-style market terminal with a Claude-powered research and
trading agent. It combines the two projects in the reference guides, built
from scratch with original code:

| Reference (from the Reel guides) | What Ledger Terminal recreates |
|---|---|
| **Fincept Terminal**: open-source "Bloomberg Terminal" with equity research, DCF, portfolio analytics, AI investor agents | Dark terminal UI with a command bar, live candlestick charts, watchlist and ticker tape, technicals, news, DCF calculator, portfolio risk (volatility, Sharpe, VaR, max drawdown), and a multi-lens **AI investor panel** |
| **OpenAlice**: AI trading agent with Unified Trading Accounts, *Trading-as-Git* (stage, commit, push), guards, heartbeat, event log | Per-account **UTAs**, a git-style order workflow with 8-character commit hashes, **pre-trade guards**, a **push** approval gate, a periodic **heartbeat**, and a live **event log** |
| *(added)* | A **backtester** for rule-based strategies against buy-and-hold, and **automated paper trading**: strategy bots and an AI autopilot with a kill switch, daily loss limit and market-hours checks |

🌐 **[Web app on GitHub Pages](https://aravi71.github.io/ledger-terminal/)**: pick any tested gold / US30 / NAS100 strategy (from popular YouTube videos and Instagram reels), set capital and risk, see month-by-month and yearly results, check a monthly target, and paper trade in the browser. [Research tables](https://aravi71.github.io/ledger-terminal/research.html).

📘 **[Operator Handbook](docs/HANDBOOK.md)**: how a trade happens step by step, guards, how paper fills are simulated, backtesting status, the daily routine, and server deployment.

> **Not financial advice.** This is a technical tool. Automated trading only
> runs on paper accounts: the built-in simulator, or Alpaca's *paper*
> endpoint, which the adapter is hard-wired to. There is no live-money broker.
> In the 10-year backtests, **no included strategy beat buy-and-hold on
> return** ([results](docs/HANDBOOK.md#7-backtesting)).

---

## Quick start

Requires **Python 3.10+**. No Node.js or API keys are needed for a first run.

```bash
cd ledger-terminal
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
python run.py
```

Open **http://127.0.0.1:3002**.

Without a Claude credential, everything works except the AI agent, investor
panel and heartbeat. To turn those on, set `ANTHROPIC_API_KEY` in `.env` (or run
`ant auth login` once) and restart.

Offline or behind a firewall? Set `LEDGER_MARKET_DATA=demo` to use
deterministic synthetic prices.

---

## Using it

**Command bar** (press `/` to focus it):

| Command | Does |
|---|---|
| `AAPL`, `BTC-USD`, `GC=F`, `EURUSD=X` | Load a symbol (typing shows search suggestions) |
| `FIND apple` | Search symbols by name |
| `BUY 10 AAPL` / `SELL 5 AAPL LMT 250` | Stage a market or limit order |
| `COMMIT trim tech exposure` | Commit the staged orders |
| `ASK what moved NVDA today?` (or `? …`) | Ask the agent |
| `PANEL [SYM]` | Run the five-lens AI investor panel |
| `NEWS`, `DCF`, `PORT`, `TRADE`, `EVENTS` | Jump to a view |
| `WATCH ADD TSLA`, `WATCH RM TSLA` | Edit the watchlist |
| `1D` to `5Y` | Change the chart range |
| `HELP` | Show all commands |

**The order workflow (Trading-as-Git):**

1. **Stage.** You (order ticket or command bar) or the agent add orders to the
   staging area. Guards are previewed right away and any warnings are shown.
2. **Commit.** Staged orders are bundled under a message and get an
   8-character hash chained to the previous commit.
3. **Push.** Only a human can push, via the PUSH button and a confirmation
   dialog (the API also requires `confirm: true`). Push re-runs the guards,
   sends the orders to the broker, snapshots the account, and records the
   result. If *any* order in a commit fails a guard, nothing in that commit is
   sent.
4. **Log / show.** Click any line in the log to see the full commit: who
   staged it, the rationale, guard results, broker fills and the snapshot.
   Unpushed commits can be **reverted**.

**Backtesting (BACKTEST tab):** pick a symbol, a strategy (SMA crossover,
RSI mean reversion, Donchian breakout, or buy-and-hold), its parameters,
history (1–10 years or max) and costs. You get return on allocated capital
against buy-and-hold with the same sizing, CAGR, Sharpe, drawdown, win rate,
an equity-curve chart and every trade. The signal is computed at the close
and filled at the next open, so there is no look-ahead. **Paper trade this
strategy** turns it into a bot.

**Automated trading (AUTO tab):** bots run every N minutes and stage, commit
and push their own orders on a paper account. Guards still apply; there is a
per-bot daily loss limit (halts until you resume), a global **kill switch**,
market-hours checks, and per-bot P&L. A bot only sells what it bought itself.
The `ai-autopilot` bot lets Claude make the decisions instead (it needs an
API key, can only trade its listed symbols, and every run is logged).

**Gold scalping (GOLD SCALP tab):** intraday XAUUSD backtests on local
Dukascopy history (10 years hourly, minute data as downloaded) with
stop-loss/take-profit, spread, commission, leverage and broker minimum size,
for any starting capital ($100, $1,000, …). **Run as paper bot** trades it
live on the `paper-gold` CFD account (leverage, shorts, SL/TP brackets).
Research results, including why no tested setup reached a profitable 90% win
rate: [HANDBOOK §7b](docs/HANDBOOK.md#7b-gold-xauusd-intraday-and-scalping-research).

**The agent** can research (quotes, technicals, price history, news, account
state) and **stage and commit** orders. It has no push tool, so execution is
always yours. Each tool call shows as a chip under the reply and is written to
the event log.

**Investor panel:** five independent lenses (value, growth, technical, macro,
risk) analyse the same data snapshot in parallel. Each returns structured JSON
(stance, conviction, thesis, risks, and what would change its mind), rendered
as cards.

**Heartbeat:** off by default. Enable it in the EVENTS tab. On each interval
the agent reviews the watchlist and your positions using **read-only** tools
and only raises an alert when something matters. You can edit its rules in
`data/brain/heartbeat.md`.

---

## Configuration

All config lives in `data/config/` as JSON (created with defaults on first
run, validated with Pydantic). Edit the files and restart.

| File | Purpose |
|---|---|
| `accounts.json` | Trading accounts (UTAs): `type` (`paper` / `alpaca`), `enabled`, `guards`, `brokerConfig` |
| `ai.json` | `model` (default `claude-opus-5-5`), `effort`, `heartbeatEffort`, `maxAgentSteps` |
| `heartbeat.json` | `enabled`, `intervalMinutes` (also editable in the UI) |
| `watchlist.json` | Watchlist symbols (also editable in the UI) |
| `automation.json` | Bots (strategy, params, symbols, allocation, interval, loss limit) and the kill switch (also editable in the AUTO tab) |

**Guards** (per account; new accounts do not inherit them):

```json
"guards": {
  "maxOrderValue": 5000,        // max notional per order (null = off)
  "maxPositionValue": 15000,    // max position size after the order (reducing is always allowed)
  "cooldownSeconds": 60,        // min seconds between pushed trades in the same symbol
  "maxOrdersPerDay": 20,
  "symbolWhitelist": [],        // empty = any symbol
  "allowShort": false
}
```

**Prompts** use a default plus override pattern. The tracked defaults are in
`defaults/`. On first run they are copied to `data/brain/persona.md` and
`data/brain/heartbeat.md`, which you can edit freely.

**Alpaca paper trading** (optional): get paper keys from Alpaca, put them in
`.env` as `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, set
`"enabled": true` on the `alpaca-paper` account, and restart. The adapter's
base URL is a constant (`https://paper-api.alpaca.markets`), so it cannot be
pointed at a live account through configuration.

**Environment variables** (`.env`):

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Enables the agent. Alternatively use `ant auth login` |
| `LEDGER_MARKET_DATA` | `yahoo` | `yahoo` (live, delayed, no key) or `demo` (offline) |
| `LEDGER_HOST` / `LEDGER_PORT` | `127.0.0.1` / `3002` | Any non-localhost host requires `LEDGER_PASSWORD` |
| `LEDGER_USERNAME` / `LEDGER_PASSWORD` | `admin` / — | Turns on HTTP Basic login for the UI, API and event stream |
| `LEDGER_DATA_DIR` | `./data` | Config, event log, broker state, commit history |

---

## Architecture

```
static/ (vanilla JS + TradingView lightweight-charts)
   │  REST + Server-Sent Events
app/main.py ── FastAPI routes, SSE stream, scheduler (heartbeat + limit-order reconcile)
   ├── agent.py      Claude tool-use loop · investor panel · heartbeat · autopilot (Anthropic SDK)
   ├── automation.py bots: scheduling, kill switch, loss limit, market hours, per-bot ledger
   ├── strategies.py rule-based strategies shared by the backtester and the bots
   ├── backtest.py   bar-by-bar simulator with costs and a matched buy-and-hold benchmark
   ├── uta.py        UTAs: stage / commit / push / revert / log / show
   │    ├── guards.py      pure pre-trade checks
   │    └── brokers/       paper.py (simulated) · alpaca.py (paper-only)
   ├── market.py     Yahoo Finance chart/search/RSS · offline demo provider
   ├── indicators.py SMA, EMA, RSI, MACD, Bollinger, ATR + signal summary
   ├── analytics.py  volatility, Sharpe, historical VaR, drawdown, DCF
   ├── events.py     append-only JSONL event log with live subscribers
   └── config.py     JSON config + Pydantic validation + prompt overrides
```

Key decisions:

- **Python + FastAPI, no build step.** It installs with one `pip install` and
  runs anywhere Python runs. The UI is plain HTML/CSS/JS served by the same
  process.
- **The agent can't execute.** Removing push from the tool list is enforced by
  code, not just asked for in the prompt. Guards run again at push time
  because prices move between staging and pushing.
- **Event log as the spine.** Every stage, commit, push, rejection, agent
  message, tool call and heartbeat is a typed event in `data/events.jsonl`,
  streamed live to the UI.
- **Claude API usage:** the official `anthropic` SDK; model `claude-opus-5-5`
  with adaptive thinking and configurable effort; a cached system prompt;
  structured JSON outputs for the panel and heartbeat; and server-side
  refusal fallbacks (`fallbacks: "default"`).
- **Free data.** Yahoo Finance's public endpoints need no key but are
  unofficial and delayed. For production, swap `market.py` for a licensed
  feed (Polygon, Alpaca Market Data, IEX Cloud) behind the same four methods.

---

## Tests

```bash
pip install -r requirements.txt
python -m pytest
```

59 tests cover the strategies (including no-look-ahead checks), the backtester, the gold intraday engine (stop-before-target, spread on shorts, minimum size, no look-ahead), the CFD broker (margin, brackets, stop-out), the bots (kill switch, loss-limit halt, market hours, never touching manual positions, AI symbol fencing), the indicators, DCF and risk maths, every guard rule, the paper
broker (fills, cash and short-sale checks, resting limit orders, persistence),
the full stage → commit → push flow (including atomic rejection, cooldown and
revert), every HTTP endpoint, the login, and the agent's tool dispatch and tool-use loop
(with a scripted fake client, so no API calls or costs). The tests use the
offline demo data provider.

---

## Gold history and research

```bash
python scripts/fetch_dukascopy.py --symbol XAUUSD --tf hour --start 2016-10
python scripts/fetch_dukascopy.py --symbol XAUUSD --tf minute --start 2021-10
python scripts/research.py --symbol XAUUSD --tf 60   # also NAS100 / US30 and --tf 5, 15, 30
```

The feed throttles heavily; the downloader backs off, resumes where it
stopped, and fetches minute data newest-first.

## Reproducing the backtest report

```bash
python scripts/yearly_report.py --range 10y --capital 10000 --out docs/yearly-report.json
```

This backtests every strategy on 14 markets (US, gold, crypto, India) and
writes year-by-year returns and P&L per 10,000 of capital. A summary is in
[docs/HANDBOOK.md §7](docs/HANDBOOK.md#7-backtesting).

## Deployment

**Free hosting:** `render.yaml` deploys to Render's free plan in a few clicks
(New → Blueprint). Free hosts sleep when idle, so for bots that trade around
the clock use a VM or VPS. See the hosting options table in
[docs/HANDBOOK.md §11](docs/HANDBOOK.md#11-deploying-to-a-server-paper-trading).


For a server, use Docker and an HTTPS reverse proxy:

```bash
cp .env.example .env      # set LEDGER_PASSWORD (required) and ANTHROPIC_API_KEY
docker compose up -d --build
```

The container listens on `127.0.0.1:3002` only. Put Caddy or nginx in front
for HTTPS. `run.py` refuses to bind to a public address without
`LEDGER_PASSWORD`. Full steps (Caddyfile, systemd, backups, updates,
emergency stop) are in [docs/HANDBOOK.md §11](docs/HANDBOOK.md#11-deploying-to-a-server-paper-trading).

---

## Using the original open-source projects instead

If you want the exact tools from the guides rather than this
reimplementation:

- **OpenAlice** (AGPL-3.0, pre-release). Needs Node.js 22+, pnpm 10+ and the
  Claude Code CLI: `git clone https://github.com/TraderAlice/OpenAlice.git`,
  then `pnpm install && pnpm build && pnpm dev`, and open
  http://localhost:3002. Leave its "Evolution mode" off.
- **Fincept Terminal** (dual licensed: AGPL-3.0 for personal use, commercial
  licence required for any business use). It is a C++/Qt6 desktop build with
  pinned toolchain versions (CMake 3.27.7, Ninja 1.11.1, Qt 6.8.3, Python
  3.11.9), about 30 GB of disk space and 16 GB of RAM. Follow its README;
  Claude Code can drive the build.

Ledger Terminal is written from scratch and shares no code with either
project.
