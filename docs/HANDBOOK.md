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

## 7b. Gold (XAUUSD) intraday and scalping research

### Data and test method

- **Data:** Dukascopy spot XAUUSD bid candles, downloaded with
  `scripts/fetch_dukascopy.py` into `data/history/` (local only). Hourly bars
  from October 2016 to September 2026 (59,122 bars). Minute data is being
  downloaded newest-first; when this was written it covered only February
  to October 2026, so the 15-minute results below are **preliminary**.
- **Engine:** `app/intraday.py`. Entries at the next bar's open; stop-loss
  and take-profit tested against each bar's high/low, **stop first** when both
  are touched in one bar; gaps fill at the open; buys pay the spread.
- **Costs:** $0.30/oz spread plus $0.035/oz commission per side; leverage
  1:100; minimum size 1 oz (0.01 lot); 1% risk per trade.
- **No cherry-picking:** each strategy's settings are chosen on 2016–2023
  only (in-sample, 70%) and then reported on 2023–2026 (out-of-sample, 30%),
  which played no part in the choice. Reproduce with
  `python scripts/research.py --symbol XAUUSD --tf 60`. Full results:
  [`research-xauusd-60m-2026-10-10.json`](research-xauusd-60m-2026-10-10.json).

### Results on 10 years of hourly gold (account of 10,000, 1% risk)

| Strategy | Win rate 2016–23 → 2023–26 | Profit factor 2016–23 → 2023–26 | Net 2016–23 | Net 2023–26 | Verdict |
|---|---:|---:|---:|---:|---|
| **Asian-hours drift** (long 18:00 → ~03:00 New York, 5×ATR stop) | 51.9% → 57.1% | **1.30 → 1.64** | **+3,833** | **+3,759** | Profitable in both periods |
| Asian-range breakout at London open | 38.5% → 44.7% | 0.95 → 1.12 | −3,555 | +2,463 | Lost for 7 years; recent gain only |
| Trend pullback (EMA 20/50/200) | 43.3% → 48.2% | 0.86 → 1.08 | −6,190 | +1,717 | Lost for 7 years |
| Fade failed Asian-range break | 60.4% → 57.4% | 0.77 → 0.76 | −3,854 | −1,412 | Loses |
| RSI(2) dip, small target / wide stop | 68.3% → 65.3% | 0.92 → 0.81 | −955 | −781 | Loses |

### About a 90% win rate

The best win rate any tested setting reached was **85%** (RSI(2) dip, 0.3×ATR
target, 6×ATR stop), and it **lost money** (profit factor 0.92 out-of-sample).
None reached 90%. A very high win rate comes from a target much smaller than
the stop: you win small almost every time and occasionally lose big. After
spread and commission that usually loses. Vendor robots advertising 85–90%
win rates seldom show results after costs, or out-of-sample. Judge a strategy
by **profit factor, expectancy and drawdown**, not win rate.

### What $100 and $1,000 accounts would have done (Asian-hours drift, 10 years)

| Start | Rule | Trades | Result | Yearly | Worst drawdown | Note |
|---|---|---:|---:|---:|---:|---|
| $1,000 | 1% risk, max 5% with the 1 oz minimum | 1,944 | **$2,716** | 10.6% | 19% | 1 oz usually risks 4–5% here, so the real risk is about 4–5× the 1% target |
| $100 | 1% risk, max 5% | 0 | $100 | – | – | Every trade skipped: 1 oz always risks more than 5% of $100 |
| $100 | 1% risk, max 25% | 1,978 | $2,612 | 38.8% | 39% | Very aggressive: up to a quarter of the account per trade; it survived only because the edge held |
| $10,000 | 1% risk | 1,998 | $18,680 | 6.5% | 4.6% | Sizing works as intended |

Year by year ($1,000 account): 2017 +49, 2018 −22, 2019 −1, 2020 +169,
2021 +184, 2022 +73, 2023 +268, 2024 +363, 2025 +336, 2026 (to September)
+273. The edge was flat in 2018–2019 and strongest during the 2020–2026 gold
bull market.

**Small accounts:** with a standard account, gold's 1 oz minimum makes
proper risk control impossible below about $2,000–5,000. A broker with
smaller minimum sizes (a "cent" or micro account) fixes this; set
`minQty` on the account to match it.

**Costs decide it:** at a $0.80/oz spread (common around the daily reopen at
some brokers) the 2016–2023 period turns slightly negative. Check your
broker's real spread at 18:00 New York before trading this live.

### 15-minute scalping (preliminary, about 8 months of data)

On the minute data available so far (February–October 2026), the Asian-hours
drift was again the strongest. Breakout and trend pullback were mixed, and the
fade and RSI(2) setups lost. About 8 months is far too short to rely on. Let
`fetch_dukascopy.py --tf minute` finish (it resumes where it stopped) and
re-run `python scripts/research.py --symbol XAUUSD --tf 15` for a multi-year 15-minute test.

### Live paper bot

`gold-asia-drift` runs on the `paper-gold` CFD account ($1,000, 1:100):

- Symbol **GC=F** (COMEX gold futures from Yahoo, about 10 minutes delayed)
  as a live stand-in for spot XAUUSD. Futures trade a few dollars above spot.
- Signal on the hourly bar that closes at 17:00 New York (Monday–Thursday).
  The bot waits for the 18:00 reopen, buys with a 5×ATR stop-loss and a
  far take-profit, and exits after the ninth hourly bar (about 03:00 New York).
- Risk 1% per trade, at most 5% when the 1 oz minimum forces more; the bot
  halts if the account loses more than 5% in a day.
- Live stops are checked once a minute against delayed quotes, which is
  coarser than the backtest. Expect live results to differ.

Create other gold bots from the **GOLD SCALP** tab: run a backtest, then
**Run as paper bot**.

## 7c. Scalping research: gold, US30, NAS100 and the "70–90% win rate, 30–40% a month" claims

### What was tested

The strategies shown most often in trading reels, plus research-backed ones,
on three markets, with the same method as §7b (settings picked on the first
70% of the history, judged on the last 30%):

| Strategy | Idea |
|---|---|
| New York opening-range breakout (`ny_orb`) | Range of the first 5/15/30 minutes after 09:30 New York; trade the first close outside it; stop at the other side or the middle; target 0.5–2× the stop |
| EMA-200 + RSI pullback scalp (`ema_rsi_scalp`) | With the 200 EMA trend, enter when RSI turns back from 25–35; stop 1–2×ATR; target 0.5–2× |
| RSI(2) dip, small target / wide stop (`rsi2_highwin`) | The classic "90% win rate" design |
| Asian-range breakout, trend pullback | London-session breakout; EMA 20/50/200 pullback |
| Asian-hours / overnight drift (`asia_session_drift`) | Long from the 18:00 New York reopen for ~9 hours (indices also tested to 09:30) |

Costs: gold $0.30/oz spread plus commission, NAS100 1.0 and US30 2.0 points
of spread, estimated overnight swap at the 17:00 New York rollover, 1:100
leverage, minimum size 1 oz (gold) and 0.1 (indices).

Data: gold 10 years hourly and 2 years of minute bars (5- and 15-minute
tests); US30 and NAS100 10 years hourly. NAS100 and US30 minute data was still
downloading when this was written, so the index opening-range test (5-minute)
covers only about 4½ months and is **preliminary**.

### Results (out-of-sample = the last 30%, never used to choose settings)

| Market · bars | Strategy | Win rate in → out | Profit factor in → out | Verdict |
|---|---|---:|---:|---|
| Gold · 1h · 10y | **Asian-hours drift** | 52% → 57% | **1.29 → 1.64** | Robust |
| Gold · 15m · 2y | **Asian-hours drift** | 42% → 35% | **1.41 → 1.34** | Holds (strong gold years) |
| Gold · 5m · 2y | Opening-range breakout | 45% → 44% | 1.07 → 0.95 | No edge |
| Gold · 5m · 2y | Trend pullback | 40% → 39% | 0.88 → 0.88 | Loses (costs) |
| Gold · 15m · 2y | RSI(2) high-win | 76% → 68% | 1.10 → 0.94 | No edge |
| NAS100 · 1h · 10y | **RSI(2) high-win** | **91% → 92%** | 0.97 → 1.08 | 90% win rate, ~no profit |
| NAS100 · 1h · 10y | Overnight drift | 53% → 54% | 1.08 → 1.10 | Tiny edge |
| US30 · 1h · 10y | Overnight drift | 51% → 51% | 1.15 → 1.09 | Tiny edge |
| US30 · 1h · 10y | All others | 40–73% | 0.92–1.03 → 0.97–1.29 | No reliable edge |
| NAS100 · 5m · 4½ months | Opening-range breakout | 71% → 60% | 1.22 → 0.75 | Failed out-of-sample (small sample) |
| NAS100 · 5m · 4½ months | RSI(2) high-win | 85% → 83% | 1.00 → 0.77 | Loses |

**The 70–90% win rates are real, and they don't make money.** NAS100's RSI(2)
setup won about 91% of over a thousand trades in 10 years, and $1,000 ended at
$997. A target much smaller than the stop gives many small wins and a few large
losses; spread does the rest.

### Can a strategy make 30–40% a month?

Return per month is set mostly by **risk per trade**, not by the strategy.
The risk simulation replays each strategy's real trades (costs included) at
higher risk. For the best strategy found (gold Asian-hours drift, 10 years):

| Risk per trade | Typical month | Bad month (1 in 10) | Chance the account halves within a year |
|---:|---:|---:|---:|
| 1% | +0.6% | +0.2% | 0% |
| 5% | +2.9% | +0.7% | 0% |
| 10% | +5.4% | +0.9% | 1% |
| 20% | +9.4% | −0.2% | 33% |

On the 2024–26 15-minute data, gold's strongest stretch, the same strategy
gave +3.5% a typical month at 1% risk and +14% at 5%, but at 5% the account
halved in 55% of simulated years and at 10% in almost all of them.

**30–40% a month (about 2,000–5,000% a year) did not appear for any
strategy, market or risk level in 10 years of data.** Getting near it needs
20%+ risk per trade, where the simulations show the account halving or being
wiped out most of the time. Accounts that show such months in reels are
usually short lucky streaks, demo results, or survivors of many that blew up.

### Practical settings

- Gold Asian-hours drift, 1–2% risk per trade: historically about
  0.6–1.2% a month over 10 years (more in strong gold years), with small
  drawdowns. This is what the `gold-asia-drift` paper bot runs.
- Below about $2,000 the 1 oz gold minimum forces 4–25% risk per trade. Use
  a broker with micro lots and set `minQty` to match.
- Re-run everything when more minute data has downloaded:
  `python scripts/research.py --symbol NAS100 --tf 5` (and `US30`, `XAUUSD`).

## 7d. YouTube and Instagram strategies, tested

### Sources read

Twelve popular YouTube videos were read through their transcripts, and about
40 Instagram reel captions under #goldscalping, #xauusdstrategy,
#us30strategy, #nas100strategy and #nas100scalping. Every strategy with
mechanical rules was coded exactly as described and tested with the same
method as §7b/7c (settings chosen on the first 70%, judged on the last 30%,
real spreads, commission, swaps and minimum sizes).

| Video / reel (views) | What it teaches | Coded as |
|---|---|---|
| [This 1 Minute Scalping Strategy Works Everyday](https://www.youtube.com/watch?v=bITIVwysCzM) (1.7M) | 15-min opening range, 5-min close outside, 1-min fair value gap entry, 1:2 | `orb_fvg` |
| [The ONLY US30 Trading Strategy (1K a Day)](https://www.youtube.com/watch?v=UX09Cl-FN5Y) (1.7M) | Supply/demand zone retest | `supply_demand` |
| [This Gold Trading Strategy Makes $1,000 a Day](https://www.youtube.com/watch?v=AY7fWmE1CBk) (1.7M) | 1h supply/demand zones, 15m entry, ~1:4 | `supply_demand` (1h, rr up to 4) |
| [BEST Gold Scalping Strategy](https://www.youtube.com/watch?v=en8RMFRqSME) (945k) | 1h + 15m trend aligned, demand zones, liquidity | `supply_demand` + trend filter |
| [XAUUSD Scalping Strategy](https://www.youtube.com/watch?v=oDOCBTYgCQM) (1.6M) | SMMA 21/50/200 fan, RSI vs 50, three-line strike engulfing, out within 6 candles | `three_line_strike` |
| [The BEST 5 Minute Scalping Strategy Ever](https://www.youtube.com/watch?v=O5eC5lY7ZXY) (1.9M) | First 4h candle range, close outside then back inside = fade, 1:2 | `range4h_fade` |
| [My Simple "9:30AM Candle" Scalping Strategy](https://www.youtube.com/watch?v=jq-fpkPv3-A) (437k) | 9:30 5-min candle, break and retest, 1:2. Claims 1,308 trades, 59% win, PF 4.15 | `orb_retest` |
| [1 Minute Fibonacci Scalping Strategy Backtested 100 Times](https://www.youtube.com/watch?v=2GAAK_JhNW0) (385k) | 0.5–0.618 pullback in a trend, target previous high (≈1:1.6) | `fib_pullback` |
| [3 Step BEGINNER Scalping Strategy (500 Trade Backtest)](https://www.youtube.com/watch?v=kFyD3H6I1I8) | 7/8 AM candle engulfing bias + 9:30–9:45 range | `bias_orb` |
| [Complete NAS100 Strategy in Under 10 Minutes](https://www.youtube.com/watch?v=S9QGUIzWk2c) (170k) | 10:00 window, Fibonacci + imbalance | `silver_bullet`, `fib_pullback` |
| [How to Trade Gold – Ride Massive Trends](https://www.youtube.com/watch?v=MKJz4yURCEs) (591k) | Heikin-Ashi colour change, hold until it flips | `heikin_ashi_trend` |
| [Powerful XAUUSD Gold Trading Strategy](https://www.youtube.com/watch?v=3E7k47dZeWk) (2.2M) | A paid indicator ("319 trades, 81%") | Not testable |
| Instagram "UT Bot Alerts" reel (18K likes) | ATR trailing-stop flip indicator | `ut_bot` |
| Instagram NAS100 "V-pattern" reel | Previous-day high/low retest with rejection candle | `pdhl_rejection` |
| Instagram "London Session Gold Bracket" | Range breakout | `asian_breakout` |

Most other reels ("XAUUSD morning strategy" 36K likes, "Gold 6:30 Strategy
80% accuracy", "comment GOLD for details", "$100 to $5,000 in 60 days")
keep the rules in the video, a paid group or a Telegram channel, or sell a
bot. Their claims could not be tested.

### Results

All tests, best first by out-of-sample result (generated by
`python -X utf8 scripts/summarize_research.py --markdown`):

| Market | Bars | Strategy | Win rate in → out | Profit factor in → out | Out-of-sample trades | Verdict |
|---|---|---|---:|---:|---:|---|
| NAS100 | 5m | Asian-range breakout at London open | 40% → 50% | 1.29 → 1.99 | 30 | holds up |
| XAUUSD | 1h | Asian-hours drift (long from the 18:00 New York reopen, ~9 hours) | 52% → 57% | 1.29 → 1.64 | 601 | holds up |
| XAUUSD | 15m | Asian-hours drift (long from the 18:00 New York reopen, ~9 hours) | 42% → 35% | 1.41 → 1.34 | 116 | holds up |
| XAUUSD | 1h | Supply/demand zone retest (YouTube US30 strategy) | 42% → 46% | 1.10 → 1.22 | 24 | holds up |
| US30 | 1h | EMA-200 trend + RSI pullback scalp (1:2) | 49% → 57% | 1.02 → 1.29 | 87 | tiny edge |
| NAS100 | 1h | Asian-hours drift (long from the 18:00 New York reopen, ~9 hours) | 53% → 54% | 1.08 → 1.10 | 369 | tiny edge |
| US30 | 1h | Asian-hours drift (long from the 18:00 New York reopen, ~9 hours) | 51% → 51% | 1.15 → 1.09 | 360 | tiny edge |
| NAS100 | 5m | 4-hour range false breakout (fade) | 36% → 38% | 1.02 → 1.08 | 139 | tiny edge |
| US30 | 5m | ICT Silver Bullet (FVG, 10:00-11:00 New York) | 40% → 43% | 1.05 → 1.08 | 111 | tiny edge |
| US30 | 5m | Previous-day high/low rejection ('V-pattern' reel) | 32% → 29% | 1.15 → 1.07 | 185 | tiny edge |
| XAUUSD | 5m | ICT Silver Bullet (FVG, 10:00-11:00 New York) | 43% → 39% | 1.20 → 0.96 | 92 | fit the past, failed later |
| NAS100 | 5m | Opening range + fair-value gap (YouTube 1-minute scalp) | 40% → 35% | 1.15 → 0.94 | 144 | fit the past, failed later |
| XAUUSD | 15m | New York opening-range breakout (09:30) | 49% → 48% | 1.18 → 0.92 | 107 | fit the past, failed later |
| XAUUSD | 5m | Three-line strike trend scalp (SMMA 21/50/200 + RSI) | 42% → 38% | 1.14 → 0.89 | 101 | fit the past, failed later |
| NAS100 | 5m | New York opening-range breakout (09:30) | 58% → 49% | 1.35 → 0.89 | 150 | fit the past, failed later |
| XAUUSD | 5m | Asian-hours drift (long from the 18:00 New York reopen, ~9 hours) | 20% → 14% | 1.43 → 0.89 | 50 | fit the past, failed later |
| XAUUSD | 5m | EMA-200 trend + RSI pullback scalp (1:2) | 57% → 47% | 1.27 → 0.85 | 30 | fit the past, failed later |
| US30 | 5m | New York opening-range breakout (09:30) | 46% → 39% | 1.19 → 0.75 | 152 | fit the past, failed later |
| NAS100 | 1m | Opening range + fair-value gap (YouTube 1-minute scalp) | 30% → 19% | 1.15 → 0.68 | 155 | fit the past, failed later |
| US30 | 5m | Opening range + fair-value gap (YouTube 1-minute scalp) | 34% → 25% | 1.14 → 0.67 | 131 | fit the past, failed later |
| NAS100 | 5m | 9:30 candle break and retest | 37% → 25% | 1.12 → 0.67 | 114 | fit the past, failed later |
| XAUUSD | 1h | EMA-200 trend + RSI pullback scalp (1:2) | 72% → 43% | 1.48 → 0.41 | 21 | fit the past, failed later |
| XAUUSD | 15m | Asian-range breakout at London open | 38% → 60% | 1.00 → 2.47 | 55 | no edge |
| NAS100 | 5m | Fibonacci 0.5-0.618 pullback in trend | 53% → 58% | 1.00 → 1.20 | 349 | no edge |
| NAS100 | 5m | Three-line strike trend scalp (SMMA 21/50/200 + RSI) | 47% → 55% | 0.85 → 1.13 | 308 | no edge |
| NAS100 | 5m | EMA-200 trend + RSI pullback scalp (1:2) | 31% → 37% | 0.89 → 1.13 | 60 | no edge |
| XAUUSD | 1h | Fibonacci 0.5-0.618 pullback in trend | 49% → 54% | 0.83 → 1.13 | 204 | no edge |
| XAUUSD | 5m | 4-hour range false breakout (fade) | 33% → 39% | 0.83 → 1.11 | 126 | no edge |
| XAUUSD | 5m | RSI(2) dip, small target / wide stop (high win rate) | 72% → 73% | 0.92 → 1.09 | 434 | no edge |
| NAS100 | 1h | RSI(2) dip, small target / wide stop (high win rate) | 91% → 92% | 0.97 → 1.08 | 359 | no edge |
| XAUUSD | 1h | Asian-range breakout at London open | 40% → 46% | 0.91 → 1.08 | 479 | no edge |
| NAS100 | 5m | Supply/demand zone retest (YouTube US30 strategy) | 22% → 28% | 0.70 → 1.05 | 102 | no edge |
| US30 | 5m | Fibonacci 0.5-0.618 pullback in trend | 50% → 56% | 0.85 → 1.05 | 408 | no edge |
| US30 | 5m | EMA-200 trend + RSI pullback scalp (1:2) | 46% → 51% | 0.86 → 1.04 | 127 | no edge |
| US30 | 1h | Asian-range breakout at London open | 40% → 42% | 0.92 → 1.03 | 521 | no edge |
| US30 | 5m | Trend pullback (EMA 20/50/200) | 38% → 42% | 0.89 → 1.03 | 772 | no edge |
| XAUUSD | 1m | Opening range + fair-value gap (YouTube 1-minute scalp) | 24% → 26% | 0.87 → 1.02 | 141 | no edge |
| NAS100 | 5m | Heikin-Ashi trend ride | 33% → 37% | 0.87 → 1.01 | 973 | no edge |
| XAUUSD | 5m | Previous-day high/low rejection ('V-pattern' reel) | 43% → 51% | 0.75 → 1.01 | 154 | no edge |
| NAS100 | 5m | Trend pullback (EMA 20/50/200) | 41% → 42% | 0.97 → 0.99 | 739 | no edge |
| US30 | 1h | RSI(2) dip, small target / wide stop (high win rate) | 71% → 73% | 1.01 → 0.97 | 535 | no edge |
| US30 | 1h | Trend pullback (EMA 20/50/200) | 43% → 44% | 0.98 → 0.97 | 536 | no edge |
| US30 | 5m | RSI(2) dip, small target / wide stop (high win rate) | 68% → 72% | 0.78 → 0.96 | 922 | no edge |
| XAUUSD | 5m | New York opening-range breakout (09:30) | 45% → 44% | 1.07 → 0.95 | 119 | no edge |
| US30 | 5m | Heikin-Ashi trend ride | 32% → 34% | 0.81 → 0.94 | 1060 | no edge |
| XAUUSD | 15m | RSI(2) dip, small target / wide stop (high win rate) | 76% → 68% | 1.10 → 0.94 | 257 | no edge |
| XAUUSD | 5m | Fibonacci 0.5-0.618 pullback in trend | 54% → 51% | 1.06 → 0.94 | 307 | no edge |
| XAUUSD | 5m | UT Bot Alerts (ATR trailing stop flip) | 35% → 36% | 0.92 → 0.94 | 1039 | no edge |
| NAS100 | 1h | Trend pullback (EMA 20/50/200) | 49% → 47% | 0.96 → 0.94 | 769 | no edge |
| NAS100 | 1h | Asian-range breakout at London open | 34% → 36% | 0.87 → 0.93 | 431 | no edge |
| NAS100 | 5m | RSI(2) dip, small target / wide stop (high win rate) | 71% → 71% | 0.95 → 0.93 | 921 | no edge |
| NAS100 | 5m | ICT Silver Bullet (FVG, 10:00-11:00 New York) | 43% → 41% | 0.99 → 0.92 | 112 | no edge |
| XAUUSD | 15m | Trend pullback (EMA 20/50/200) | 44% → 41% | 1.03 → 0.92 | 340 | no edge |
| XAUUSD | 1h | Trend pullback (EMA 20/50/200) | 44% → 49% | 0.79 → 0.89 | 708 | no edge |
| XAUUSD | 5m | Trend pullback (EMA 20/50/200) | 40% → 39% | 0.88 → 0.88 | 787 | no edge |
| XAUUSD | 5m | Supply/demand zone retest (YouTube US30 strategy) | 23% → 24% | 0.79 → 0.86 | 49 | no edge |
| XAUUSD | 5m | Heikin-Ashi trend ride | 32% → 32% | 0.74 → 0.86 | 942 | no edge |
| US30 | 5m | UT Bot Alerts (ATR trailing stop flip) | 35% → 35% | 1.02 → 0.85 | 727 | no edge |
| XAUUSD | 5m | 9:30 candle break and retest | 24% → 22% | 0.84 → 0.84 | 98 | no edge |
| US30 | 5m | Asian-range breakout at London open | 48% → 45% | 0.92 → 0.84 | 126 | no edge |
| NAS100 | 5m | Previous-day high/low rejection ('V-pattern' reel) | 45% → 46% | 0.82 → 0.83 | 253 | no edge |
| NAS100 | 5m | UT Bot Alerts (ATR trailing stop flip) | 36% → 35% | 1.06 → 0.81 | 686 | no edge |
| XAUUSD | 1h | Heikin-Ashi trend ride | 30% → 32% | 0.84 → 0.80 | 382 | no edge |
| XAUUSD | 1h | Three-line strike trend scalp (SMMA 21/50/200 + RSI) | 44% → 42% | 1.02 → 0.80 | 50 | no edge |
| XAUUSD | 1h | RSI(2) dip, small target / wide stop (high win rate) | 75% → 74% | 0.83 → 0.79 | 264 | no edge |
| XAUUSD | 5m | Opening range + fair-value gap (YouTube 1-minute scalp) | 35% → 30% | 1.05 → 0.78 | 128 | no edge |
| US30 | 5m | 9:30 candle break and retest | 27% → 20% | 1.04 → 0.71 | 95 | no edge |
| US30 | 5m | 4-hour range false breakout (fade) | 48% → 39% | 0.94 → 0.66 | 147 | no edge |
| US30 | 5m | Three-line strike trend scalp (SMMA 21/50/200 + RSI) | 46% → 38% | 1.00 → 0.66 | 104 | no edge |
| US30 | 5m | Supply/demand zone retest (YouTube US30 strategy) | 24% → 13% | 0.98 → 0.42 | 89 | no edge |
| XAUUSD | 15m | EMA-200 trend + RSI pullback scalp (1:2) | 73% → 70% | 1.48 → 1.22 | 10 | too few trades |
| NAS100 | 1h | EMA-200 trend + RSI pullback scalp (1:2) | 72% → 58% | 1.50 → 0.78 | 19 | too few trades |
| XAUUSD | 5m | Asian-range breakout at London open | 44% → 33% | 1.16 → 0.69 | 6 | too few trades |
| NAS100 | 5m | 7/8 AM engulfing bias + 9:30 opening range | 54% → 38% | 1.04 → 0.59 | 13 | too few trades |
| XAUUSD | 5m | 7/8 AM engulfing bias + 9:30 opening range | 41% → 14% | 1.45 → 0.52 | 14 | too few trades |
| US30 | 5m | 7/8 AM engulfing bias + 9:30 opening range | 57% → 25% | 2.41 → 0.32 | 12 | too few trades |

76 tests: 4 holds up, 6 tiny edge, 12 fit the past, failed later, 48 no edge, 6 too few trades

### The YouTube survey (252 videos)

Twenty searches ("gold scalping strategy", "US30 scalping strategy",
"NAS100 strategy 90% win rate", and so on) returned 252 distinct videos.
By teaching style: ICT / Silver Bullet / fair value gaps 59, opening range 19,
session breakouts 16, supply and demand 15, indicators (RSI, MACD, UT Bot,
Heikin-Ashi) 15, moving averages 14, price action 5, bots or paid indicators 4,
Fibonacci 1, and 104 with general titles whose rules fall into the same groups.
36 titles state a win rate; none of those videos show a long, cost-included
backtest that can be checked.

### What this means

* **Best found:** gold's Asian-hours drift (buy at the 18:00 New York reopen,
  stop 5× hourly ATR, out after ~9 hours). Profitable in 2016–2023 and again in
  2023–2026 (profit factor 1.29 → 1.64). About +10% a year at 1% risk.
* **80–90% win rates exist** (NAS100 RSI(2): 92% on later data) but the
  profit factor is about 1.08: tiny wins, rare large losses, roughly
  break-even after costs.
* **30–40% a month was not reached** by any strategy. The goal check in the
  web app shows the risk it would take; past ~15% risk per trade most
  simulated accounts halve within a year.
* The most-viewed YouTube scalps (opening range + FVG, 9:30 candle retest,
  Silver Bullet, three-line strike) looked fine on the first 70% of data and
  failed on the last 30%.

### The web app

`docs/index.html` (GitHub Pages) loads every tested strategy's full trade
list from `docs/data/` (written by `scripts/export_trades.py`) and re-sizes
the trades in the browser for any capital, risk %, account type (standard or
micro) and period, so you can see month-by-month and per-year results, the
leaderboard, and a goal check. Its Paper Trading tab keeps a paper account in
the browser's local storage, with a live gold feed and the Asian-drift bot.


## 8. Accounts and brokers

| Account | Type | Status | Notes |
|---|---|---|---|
| `paper-main` | Local simulator | Enabled | Section 5 |
| `paper-nas100`, `paper-us30` | Local CFD simulators for NAS100 / US30: $1,000, 1:100, minimum 0.1 | Enabled | For index intraday bots |
| `paper-gold` | Local CFD simulator: 1:100 leverage, long and short, stop-loss/take-profit, $1,000 | Enabled | For gold intraday bots; `brokerConfig` sets leverage, spread, commission and minimum size |
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
