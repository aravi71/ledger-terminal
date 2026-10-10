# Ledger Terminal: Operator Handbook

How trades happen (manually and automatically), what the AI does, how
paper fills are simulated, how strategies were backtested, how to run the
forward paper test, and how to deploy to a server.

> Not financial advice. Ledger Terminal is a research, backtesting and
> order-management tool. **Automated trading is restricted to paper
> accounts**: the local simulator and Alpaca's paper API. There is no
> live-money broker in this codebase.

---

## 1. What this app is

Ledger Terminal merges the two reference projects into **one dashboard**:

| Area of the screen | Comes from | What it does |
|---|---|---|
| Command bar, chart, watchlist, ticker tape, quote and technicals, news, DCF, portfolio risk | Fincept-style terminal | Research and analysis |
| Investor panel (five AI lenses) | Fincept's AI investor agents | Multi-perspective AI analysis of one symbol |
| AGENT tab | OpenAlice agent | Chat with Claude, which can research and *propose* trades |
| TRADE tab: staged, awaiting push, log | OpenAlice Trading-as-Git | Order lifecycle with human approval |
| Guards | OpenAlice guards | Pre-trade safety limits per account |
| EVENTS tab: heartbeat and event log | OpenAlice heartbeat and event log | Monitoring and audit trail |
| BACKTEST tab | Added | Test rule-based strategies on up to 10+ years of daily data against buy-and-hold |
| AUTO tab: bots, kill switch | Added | Automated paper trading by strategy bots or an AI autopilot |

## 2. Manual or automated?

There are **three ways an order gets executed**. All three use the same
stage → commit → push pipeline and the same guards.

| Mode | Who decides | Who pushes | Turned on by |
|---|---|---|---|
| **Manual** | You | You (PUSH button + confirm) | Always available |
| **Agent-assisted** | The AI agent proposes (stage + commit) | You | AGENT tab chat |
| **Automated: strategy bot** | A rule-based strategy (sma_cross, rsi_reversion, breakout, …) | The bot (`approver: auto:<bot>`) | AUTO tab → ENABLE |
| **Automated: AI autopilot** | The Claude agent, on a schedule | The bot | AUTO tab → enable `ai-autopilot` (needs `ANTHROPIC_API_KEY`) |

Safety rules for automation (all enforced in code, see `app/automation.py`):

- **Paper accounts only.** A bot refuses to run on any account type other
  than `paper` or `alpaca` (and the Alpaca adapter can only reach the paper
  endpoint).
- **Guards still apply** at push: order size, position size, cooldown, daily
  order count, whitelist, no shorting.
- **Daily loss limit per bot.** If account equity falls more than
  `maxDailyLossPct` below the day's starting equity, the bot **halts** and
  stays halted until a person clicks RESUME.
- **Kill switch.** One button stops every bot immediately. It persists across
  restarts.
- **Market hours.** US equities only trade 09:30–16:00 ET on weekdays; FX and
  futures on weekdays; crypto 24/7. There is no exchange-holiday calendar.
- **A bot only trades what it owns.** It rebuilds its holdings from its own
  commit history and only ever sells those, so it never closes a position you
  opened by hand. It also commits only its own staged orders, never ones a
  person staged.
- **AI autopilot is fenced.** It may only trade its listed symbols. Any commit
  touching another symbol is reverted, not pushed. Each run starts a fresh
  conversation, and every tool call is logged.
- **Full audit trail.** Every automated order is a normal commit authored
  `bot:<id>`, approved `auto:<id>`, with the bot's rationale, guard results,
  fills and an account snapshot.

## 3. How a trade happens, step by step

```
 ┌────────────┐   stage    ┌──────────┐  commit   ┌───────────────┐   push (human)   ┌────────┐
 │ You, agent │ ─────────▶ │  STAGED  │ ────────▶ │  COMMITTED    │ ───────────────▶ │ GUARDS │
 │ or a bot   │            │ (preview │  message  │  hash a1b2c3d4│  confirm dialog  │ re-run │
 └────────────┘            │  guards) │           │  awaiting push│                  └───┬────┘
                           └──────────┘           └──────┬────────┘                      │
                                ▲  unstage               │ revert                 any fail│ all pass
                                └──────────              ▼                               ▼        ▼
                                                     REVERTED                     REJECTED   BROKER
                                                                              (nothing sent)    │
                                                                                                ▼
                                                                         PUSHED / PARTIAL / FAILED
                                                                         + fills + account snapshot
```

### Step 1: Stage

**Who:** you (order ticket, or `BUY 10 AAPL` in the command bar) or the AI
agent (when you ask it for a trade or a proposal).

**What happens:**
- The order is validated: symbol, side, quantity > 0, and a limit price for
  limit orders.
- Guards are **previewed** against the current price and your current
  position. Violations appear as red ⚠ warnings, but you can still stage.
- The staged item records who staged it (`USER` / `AGENT`), the rationale,
  and the price when it was staged.

### Step 2: Commit

**Who:** you (`COMMIT <message>` or the COMMIT button) or the agent.

**What happens:**
- All staged orders are bundled into one commit with your message.
- The commit gets an 8-character hash (SHA-256 of parent + message + orders +
  time) that chains to the previous commit, like `git log`.
- Status becomes **committed**. It waits in *AWAITING PUSH*.

### Step 3: Review and push

**Who:** a person (manual and agent-assisted modes), or the bot that created the commit (automated mode, paper accounts only; see section 2).

**What happens when you click PUSH and confirm:**
1. Guards **run again** using fresh prices, because prices move between
   staging and pushing.
2. If **any** order in the commit violates a guard, the whole commit becomes
   **rejected** and **nothing** is sent to the broker.
3. Otherwise each order goes to the broker in sequence.
4. The commit status becomes:
   - **pushed**: every order accepted or filled
   - **partial**: some orders were rejected by the broker (e.g. not enough
     cash)
   - **failed**: all orders were rejected by the broker
5. The account is snapshotted (cash, equity, positions) and stored on the
   commit.
6. A `trade.push` or `trade.rejected` event is written to the event log.

### Step 4: Audit

Click any line in **LOG** to see the full commit: author, approver, time
pushed, each order and its rationale, guard results, broker fills, and the
account snapshot. Unpushed commits can be **reverted**.

## 4. Guards (safety limits)

Configured per account in `data/config/accounts.json`. A new account does
**not** inherit another account's guards.

| Guard | Default (paper-main) | Rule |
|---|---|---|
| `maxOrderValue` | $5,000 | Order quantity × price must not exceed this |
| `maxPositionValue` | $15,000 | The position after the order must not exceed this. Orders that *reduce* a position are always allowed |
| `cooldownSeconds` | 60 | Minimum time since the last pushed trade **in the same symbol** |
| `maxOrdersPerDay` | 20 | Maximum orders pushed per UTC day, per account |
| `symbolWhitelist` | empty (any) | If set, only these symbols can trade |
| `allowShort` | false | Blocks sells that would leave a negative position |

Price used for guard checks: the limit price for limit orders, or the live
quote for market orders.

**Emergency stop:** set `"maxOrdersPerDay": 0` on the account (blocks every
push) or `"enabled": false` (hides the account), then restart.

## 5. How paper fills are simulated

The `paper` broker is a local simulator. Its rules:

| Situation | What happens |
|---|---|
| Market buy | Fills immediately at the current quote **+ 2 bps** slippage |
| Market sell | Fills immediately at the current quote **− 2 bps** |
| Limit order, already marketable | Fills immediately at the current quote |
| Limit order, not marketable | Rests as an open order. Every 60 s the server checks the quote and fills it at that quote once it crosses the limit. You can cancel it from *OPEN ORDERS* |
| Buy costs more than cash | Rejected ("Insufficient cash"). There is no margin |
| Sell more than you hold | Rejected (no shorting unless `allowShort`) |
| Commission | $0 by default (`brokerConfig.commissionPerOrder`) |
| Starting cash | $100,000 (`brokerConfig.startingCash`) |
| Fractional quantity | Allowed (useful for crypto) |

**Know the realism limits before you trust the P&L:**
- Quotes come from Yahoo Finance and are **delayed** (often about 15 minutes
  for US equities).
- **Market hours are not enforced.** A market order on a weekend fills at the
  last price, which a real broker would not do.
- There is no partial-fill, liquidity or spread model beyond the 2 bps of
  slippage.
- For more realistic paper fills, use the **Alpaca paper** account (real
  market hours, real order book simulation). See section 8.

To reset the paper account: stop the app, delete
`data/brokers/paper-main.json` and `data/utas/paper-main.json`, then start it
again.

## 6. What the AI agent does

| Feature | Model calls | Can trade? |
|---|---|---|
| AGENT chat | Claude (`claude-opus-5-5`) with tools: search, quotes, technicals, history, news, accounts, trade log, **stage_order**, **commit_staged** | Stage and commit only |
| Investor panel | Five parallel calls, one per lens (value, growth, technical, macro, risk), each returning structured JSON | No |
| Heartbeat | Claude with **read-only** tools, returns `{notify, headline, details}` | No |
| AI autopilot (bot) | Claude with research + stage/commit tools, fresh context each run, prompt in `data/brain/autopilot.md` | Yes, automatically, on paper, only its listed symbols, within guards and the loss limit |

Its behaviour is shaped by `data/brain/persona.md`, which you can edit. Every
tool call it makes is written to the event log. The agent can be confidently
wrong, so read the rationale before you push.

## 7. Backtesting

### How the backtester works (`app/backtest.py`)

- Daily bars; long-only; one position per symbol.
- **No look-ahead:** the signal is computed at bar *t*'s close and filled at
  bar *t+1*'s open. Tests check that no strategy's past signals change when
  future bars are added.
- Costs: slippage in basis points on every fill (default 2 bps), plus an
  optional flat commission.
- Sizing: a fixed dollar allocation per position (same as the bots), whole
  shares, or fractional for crypto.
- Benchmark: buy-and-hold simulated with **the same sizing and costs**.
- Metrics: return (whole account and on allocated capital), CAGR, Sharpe (4%
  risk-free), max drawdown, volatility, trade count, win rate, average trade,
  profit factor and time in market. Includes the equity curve and the full
  trade list.

**Live bots use exactly the same strategy code** and act on the last
*completed* daily bar, so a live bot sees the same information the
backtester did.

### Results: 10 years of daily data, run 2026-10-10

Yahoo Finance daily bars from 2016-10-10 to 2026-10-09 (BTC to 2026-10-10).
$100,000 fully allocated, 2 bps slippage, $0 commission, default parameters
(not optimised). Raw numbers are in
[`backtest-results-2026-10-10.json`](backtest-results-2026-10-10.json).

| Symbol | Strategy | Total return | CAGR | Sharpe | Max drawdown | Trades | Win rate | Time in market |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| SPY | **buy & hold** | 260.6% | 13.7% | 0.58 | 34.1% | 0 | – | 100% |
| SPY | sma_cross 50/200 | 86.7% | 6.4% | 0.23 | 32.4% | 4 | 75% | 75% |
| SPY | rsi_reversion | 23.8% | 2.2% | -0.14 | 23.7% | 12 | 92% | 10% |
| SPY | breakout 20/10 | 85.9% | 6.4% | 0.35 | **6.9%** | 46 | 57% | 58% |
| QQQ | **buy & hold** | 530.4% | 20.2% | 0.75 | 35.6% | 0 | – | 100% |
| QQQ | sma_cross | 181.3% | 10.9% | 0.50 | 25.1% | 4 | 100% | 76% |
| QQQ | rsi_reversion | 51.2% | 4.2% | 0.06 | 17.9% | 12 | 92% | 10% |
| QQQ | breakout | 115.9% | 8.0% | 0.47 | **13.9%** | 44 | 50% | 54% |
| AAPL | **buy & hold** | 1043.6% | 27.6% | 0.85 | 38.7% | 0 | – | 100% |
| AAPL | sma_cross | 225.6% | 12.5% | 0.47 | 41.8% | 5 | 60% | 74% |
| AAPL | rsi_reversion | 66.2% | 5.2% | 0.15 | 23.3% | 14 | 86% | 12% |
| AAPL | breakout | 180.1% | 10.9% | 0.67 | **16.5%** | 35 | 66% | 55% |
| NVDA | **buy & hold** | 13610.0% | 63.6% | 1.15 | 66.4% | 0 | – | 100% |
| NVDA | sma_cross | 1000.7% | 27.1% | 0.81 | 35.9% | 3 | 100% | 74% |
| NVDA | rsi_reversion | -21.2% | -2.4% | -0.22 | 48.5% | 6 | 50% | 9% |
| NVDA | breakout | 296.6% | 14.8% | 0.59 | 33.8% | 43 | 42% | 52% |
| BTC-USD | **buy & hold** | 13237.7% | 63.1% | 1.01 | 83.4% | 0 | – | 100% |
| BTC-USD | sma_cross | 832.5% | 25.0% | 0.69 | 65.0% | 9 | 67% | 53% |
| BTC-USD | rsi_reversion | 192.4% | 11.3% | 0.49 | 23.2% | 23 | 65% | 20% |
| BTC-USD | breakout | 824.8% | 24.9% | **1.07** | **25.5%** | 53 | 47% | 47% |

**What the results say, plainly:**

1. **No strategy beat buy-and-hold on total return** in any of the five
   markets. This decade was a strong bull market, and being out of the market
   for 25–90% of the time cost a lot of upside.
2. **Breakout was the best risk reducer.** It cut the worst drawdown sharply:
   SPY 6.9% vs 34.1%, AAPL 16.5% vs 38.7%, BTC 25.5% vs 83.4%. On BTC its
   Sharpe ratio (1.07) was slightly better than buy-and-hold (1.01).
3. **RSI mean-reversion was weak.** It was in the market only about 10% of
   the time and lost money on NVDA.
4. The sma_cross results rest on only 3–9 trades each, which is too few to
   be statistically meaningful.
5. Caveats: survivorship bias (these symbols were picked knowing they did
   well), no dividends in the price series, free delayed data, one parameter
   set, no out-of-sample split. **A backtest is not a forecast.**

Re-run any of these yourself in the BACKTEST tab: pick a symbol, strategy,
parameters, history length and costs, then click RUN BACKTEST.

### Year-by-year results across 14 markets (run 2026-10-10)

US stocks and ETFs, gold, Bitcoin, Ethereum, the NIFTY 50 and NIFTY Bank
indices, and four large Indian stocks. 10 years of daily prices, 10,000 of
capital fully invested, slippage of 2 bps (US), 5 bps (India) and 10 bps
(crypto). Reproduce it with `python scripts/yearly_report.py`. The full
per-year numbers are in
[`yearly-report-2026-10-10.json`](yearly-report-2026-10-10.json).

| Strategy | Median yearly return | ≈ per 10,000 a year | Median worst fall | Beat buy & hold (of 14) | Up years |
|---|---:|---:|---:|---:|---:|
| Buy and hold | 15.2% | 1,522 | 43.1% | – | 80.0% |
| SMA crossover 50/200 | 8.2% | 817 | 36.4% | 2 | 71.2% |
| Breakout 20/10 | 6.1% | 607 | 20.8% | 0 | 71.2% |
| RSI mean reversion | 2.4% | 241 | 23.6% | 0 | 56.0% |

Compound yearly return per market:

| Market | Buy & hold | SMA cross | Breakout | RSI | Worst fall: hold → breakout |
|---|---:|---:|---:|---:|---:|
| S&P 500 ETF (SPY) | 13.7% | 6.5% | 6.4% | 2.2% | 34.1% → 6.9% |
| Nasdaq-100 ETF (QQQ) | 20.2% | 10.9% | 8.0% | 4.2% | 35.6% → 13.9% |
| Apple (AAPL) | 27.6% | 12.5% | 10.9% | 5.2% | 38.7% → 16.5% |
| Microsoft (MSFT) | 24.9% | 12.8% | 7.8% | 3.1% | 37.6% → 22.3% |
| NVIDIA (NVDA) | 63.6% | 27.1% | 14.8% | -2.4% | 66.4% → 33.8% |
| Gold ETF (GLD) | 12.4% | 8.4% | 3.6% | 1.0% | 26.4% → 18.4% |
| Bitcoin (BTC-USD) | 63.1% | 25.0% | 24.8% | 11.2% | 83.4% → 25.8% |
| Ethereum (ETH-USD) | 25.8% | 26.4% | 22.4% | -15.5% | 94.0% → 42.7% |
| NIFTY 50 (index) (^NSEI) | 10.0% | 2.5% | 2.5% | -0.3% | 38.4% → 20.6% |
| NIFTY Bank (index) (^NSEBANK) | 11.1% | 1.3% | 4.0% | 2.5% | 47.9% → 17.0% |
| Reliance Industries (RELIANCE.NS) | 16.7% | 5.0% | 5.7% | 2.3% | 45.1% → 19.6% |
| Tata Consultancy Services (TCS.NS) | 6.2% | 8.0% | 3.6% | 5.7% | 56.5% → 21.0% |
| HDFC Bank (HDFCBANK.NS) | 8.3% | 2.7% | 3.2% | 1.4% | 41.1% → 27.0% |
| Infosys (INFY.NS) | 7.2% | 2.0% | 5.1% | 2.8% | 50.7% → 22.0% |

**What can you expect to earn?** Over this decade, buying and holding a
broad index earned roughly 10–20% a year: NIFTY 50 about 10%, S&P 500 about
14%, Nasdaq-100 about 20%. That is about 1,000–2,000 a year per 10,000 in the
first year, compounding after that. Single stocks and crypto were spread much
wider, from about 6% a year (TCS) to about 64% (NVIDIA, Bitcoin), and fell
37–94% at their worst. The trading strategies earned less
(median 2–8% a year) because they sit in cash for part of the time. In
exchange, breakout roughly halved the worst falls. These are past results
from a strong decade, before tax and brokerage fees. They are not a forecast
or a promise.

### Forward paper test (what is running now)

Backtests show the past. The forward paper test checks that the live bots
behave exactly as the backtest assumes. The bots started on **2026-10-10**
on `paper-main`:

| Bot | Strategy | Symbol | $/position | First run |
|---|---|---|---:|---|
| `btc-trend` | sma_cross 50/200 | BTC-USD | 3,000 | Signal LONG → bought 0.03631 BTC (commit `b96739dd`) |
| `btc-breakout` | breakout 20/10 | BTC-USD | 3,000 | Signal FLAT → holding cash |
| `spy-trend` | sma_cross 50/200 | SPY | 5,000 | Market closed (Saturday); acts at Monday's open |
| `aapl-breakout` | breakout 20/10 | AAPL | 5,000 | Market closed; acts at Monday's open |

The bots only run while the app is running. Each week, compare the AUTO tab's
per-bot P&L and the commit log against a fresh backtest over the same dates.
They should match apart from slippage and fill timing.

**AI autopilot cannot be backtested honestly** (look-ahead from its training
data, non-repeatable answers, API cost per decision). Evaluate it only by
forward paper trading, and against the strategy bots.

## 8. Accounts and brokers

| Account | Type | Status | Notes |
|---|---|---|---|
| `paper-main` | Local simulator | Enabled | Section 5 |
| `alpaca-paper` | Alpaca **paper** API | Disabled | Put `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` in `.env`, set `"enabled": true`, restart. The base URL is fixed to `paper-api.alpaca.markets`, so it cannot place live-money orders |

There is **no live-money broker** in this codebase.

## 9. Not included (possible next steps)

| Feature | Notes |
|---|---|
| Exchange-holiday calendar | Market-hours checks know weekends and session times, not holidays. On a holiday, equity bots see no new bar and simply hold |
| Market-hours enforcement for *manual* paper orders | Bots respect hours. Manual paper orders still fill at the last price at any time |
| Parameter optimisation / walk-forward testing | Backtests use one parameter set over the whole period |
| Dividends and splits in backtests | Prices are not dividend-adjusted, so buy-and-hold is slightly understated |
| Short selling and multi-position portfolios in the backtester | Long-only, one symbol at a time |
| Multi-user accounts | One login (HTTP Basic) per deployment |
| Live-money brokers | Deliberately not included |

## 10. Daily operating routine

1. Open the dashboard and check the pills: **DATA**, **AI**, **HB**, **AUTO**
   (number of active bots, or **KILLED**).
2. **AUTO tab:** for each bot, check the last run, last action and P&L. Look
   for any **HALTED** bot, read why, and decide whether to RESUME it.
3. **AWAITING PUSH:** review the agent's proposals, then push or revert.
4. **EVENTS:** look for `bot.halted`, `bot.error`, `trade.rejected` and
   `system.error`.
5. **PORTFOLIO:** check exposure, VaR and drawdown.
6. Once a week: compare each bot's live P&L with a backtest over the same
   dates (section 7).

If anything looks wrong, press **KILL SWITCH** first and investigate second.

## 11. Deploying to a server (paper trading)

### Free and low-cost hosting options

| Option | Cost | Always on (bots keep trading)? | Keeps history? | How |
|---|---|---|---|---|
| **Render** (included `render.yaml`) | Free plan | No: sleeps after ~15 min without visitors | No: resets on each deploy (paid disk fixes this) | Push to GitHub → Render → New → Blueprint → pick the repo. The password is generated for you under Environment |
| **Hugging Face Spaces** (Docker) | Free | Sleeps after ~48 h without visitors | No (paid storage fixes this) | New Space → Docker → push this repo. Set `LEDGER_PASSWORD` as a secret; the app reads `PORT` |
| **Oracle Cloud Always Free VM** | Free | Yes | Yes | Create an Ubuntu VM, install Docker, follow the steps below |
| **Any small VPS** (Hetzner, DigitalOcean, Lightsail) | ~$4–6 / month | Yes | Yes | Follow the steps below |

For **paper-trading bots that run 24/7**, use a VM or VPS. The free web hosts
are fine for showing the dashboard and running backtests.

### Requirements

A Linux VM (1 vCPU / 1 GB RAM is enough), Docker with the compose plugin, a
domain name for HTTPS, and outbound internet access (Yahoo Finance and the
Claude API).

### Steps

```bash
git clone <your-repo-url> ledger-terminal && cd ledger-terminal
cp .env.example .env
# Edit .env:
#   ANTHROPIC_API_KEY=...            (optional, enables AI)
#   LEDGER_USERNAME=admin
#   LEDGER_PASSWORD=<long random>    (REQUIRED on a server)
openssl rand -base64 24             # one way to generate a password
docker compose up -d --build
curl http://127.0.0.1:3002/healthz   # -> {"ok":true}
```

The app is bound to `127.0.0.1:3002` on the server. Put HTTPS in front of
it, because Basic auth over plain HTTP sends the password in the clear. With
**Caddy** (automatic Let's Encrypt certificates), the whole `/etc/caddy/Caddyfile` is:

```
ledger.example.com {
    reverse_proxy 127.0.0.1:3002 {
        flush_interval -1     # keeps the live event stream (SSE) flowing
    }
}
```

Then `sudo systemctl reload caddy`.

**Without Docker** (systemd):

```ini
# /etc/systemd/system/ledger.service
[Unit]
Description=Ledger Terminal
After=network-online.target

[Service]
WorkingDirectory=/opt/ledger-terminal
EnvironmentFile=/opt/ledger-terminal/.env
ExecStart=/opt/ledger-terminal/.venv/bin/python run.py
Restart=on-failure
User=ledger

[Install]
WantedBy=multi-user.target
```

### Safety rules built into the server setup

- `run.py` **refuses to start** on a non-localhost address unless
  `LEDGER_PASSWORD` is set.
- Every route (UI, API, event stream) requires the login. Only `/healthz` is
  open.
- docker-compose publishes the port on `127.0.0.1` only. Expose it publicly
  through the HTTPS proxy, never directly.

### Operations

| Task | How |
|---|---|
| Logs | `docker compose logs -f ledger` |
| Update | `git pull && docker compose up -d --build` |
| Backup | Back up the `ledger-data` volume (config, commit history, paper account, event log) |
| Rotate password | Edit `.env`, then `docker compose up -d` |
| Emergency stop | `docker compose stop`, or set `maxOrdersPerDay: 0` (section 4) |

The Docker files are included but have **not been built or run yet** (Docker
wasn't available in the environment where the app was written). The first
deployment should start with `docker compose up --build` in the foreground.

## 12. Publishing it as open source

Before pushing to a public repository:

- [ ] `.env` is gitignored. Never commit API keys or passwords.
- [ ] `data/` is gitignored. It holds your trade history and account state.
- [x] `LICENSE`: MIT. Ledger Terminal is original code and does not inherit
  OpenAlice's or Fincept's AGPL licence. Change it before publishing if you
  prefer another licence.
- [x] CI: `.github/workflows/ci.yml` runs the test suite on every push and
  pull request.
- [ ] Keep the "not financial advice" notice in the README.
- [ ] Yahoo Finance data is for personal use under Yahoo's terms. A
  public or commercial deployment should switch to a licensed data provider.

## 13. Where things are stored

| Path | Contents |
|---|---|
| `data/config/*.json` | Accounts, guards, AI, heartbeat, watchlist settings |
| `data/brain/persona.md`, `heartbeat.md` | Your editable agent prompts |
| `data/utas/<account>.json` | Staged orders and the full commit history |
| `data/brokers/<account>.json` | Paper account cash, positions, open orders, fills |
| `data/events.jsonl` | Append-only event log (every stage, commit, push, AI call, bot run, alert) |
| `data/config/automation.json` | Bots and the kill switch |
| `data/automation_state.json` | Each bot's last run, last action, halt status, start-of-day equity |
| `data/brain/autopilot.md` | Your editable AI autopilot prompt |
| `docs/backtest-results-*.json` | Saved backtest result sets |

Everything is stored **locally** in the project folder. Nothing is sent
anywhere except market-data requests (Yahoo), Claude API calls (only if
configured) and Alpaca paper orders (only if enabled).
