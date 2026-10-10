"use strict";
/* Ledger Terminal (GitHub Pages edition): strategy explorer, goal check and browser paper trading. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const NAMES = { XAUUSD: "Gold (XAUUSD)", NAS100: "Nasdaq 100 (NAS100)", US30: "Dow 30 (US30)" };
const store = {
  get(k, d) { try { const v = localStorage.getItem("lt." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("lt." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (x) => (x === null || x === undefined || !isFinite(x) ? "—" : (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const pct = (x, d = 1) => (x === null || x === undefined || !isFinite(x) ? "—" : (x >= 0 ? "+" : "") + (x * 100).toFixed(d) + "%");
const pctPlain = (x, d = 0) => (x === null || x === undefined || !isFinite(x) ? "—" : (x * 100).toFixed(d) + "%");
const num = (x, d = 2) => (x === null || x === undefined || !isFinite(x) ? "—" : Number(x).toFixed(d));
const cls = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const VERDICT_CLASS = { "holds up": "up", "tiny edge": "muted", "fit the past, failed later": "down", "no edge": "down", "too few trades": "muted" };
const YEAR = 365.25 * 86400;

// ---------------------------------------------------------------- tabs + clock
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]"); if (!b) return;
  $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + b.dataset.tab));
  store.set("tab", b.dataset.tab);
  if (b.dataset.tab === "paper") initPaper();
});
setInterval(() => {
  const ny = new Date().toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false });
  $("#clock").textContent = "New York " + ny;
}, 1000);

// ---------------------------------------------------------------- data
let INDEX = [];
const CACHE = {};
async function loadFile(f) { if (!CACHE[f]) CACHE[f] = await (await fetch("data/" + f)).json(); return CACHE[f]; }

// ---------------------------------------------------------------- simulation (mirrors app/intraday.py sizing)
function sizingOpts(file) {
  const micro = $("#s-acct").value === "micro";
  const c = file.costs;
  return {
    capital: Number($("#s-capital").value) || 1000, risk: Number($("#s-risk").value) || 1, maxRisk: Number($("#s-maxrisk").value) || 5,
    minQty: micro ? 0.01 : c.min_qty, step: micro ? 0.01 : c.qty_step, lev: c.leverage,
  };
}
function periodStart(file, strat) {
  const tr = strat.trades; if (!tr.length) return 0;
  const p = $("#s-period").value;
  if (p === "oos") return file.splitTime;
  if (p === "12m") return tr[tr.length - 1][0] - YEAR;
  return tr[0][0] - 3600;
}
function simulate(trades, o, from) {
  let eq = o.capital, peak = o.capital, mdd = 0, n = 0, wins = 0, gw = 0, gl = 0, skipped = 0, streak = 0, worst = 0, blown = false;
  const curve = [], rs = [];
  for (const [t, r, u, , entry] of trades) {
    if (t < from) continue;
    if (blown) break;
    let q = Math.floor(eq * o.risk / 100 / u / o.step + 1e-9) * o.step;
    if (q < o.minQty) {
      if (o.minQty * u <= eq * o.maxRisk / 100) q = o.minQty;
      else { skipped++; if (o.minQty * entry / o.lev > eq) blown = true; continue; }
    }
    const qMargin = Math.floor(eq * o.lev / entry / o.step + 1e-9) * o.step;
    if (q > qMargin) q = qMargin;
    if (q < o.minQty) { skipped++; blown = true; continue; }
    const pnl = r * u * q;
    eq += pnl; n++; rs.push(r);
    if (pnl > 0) { wins++; gw += pnl; streak = 0; } else { gl -= pnl; streak++; worst = Math.max(worst, streak); }
    peak = Math.max(peak, eq); mdd = Math.max(mdd, peak > 0 ? (peak - eq) / peak : 0);
    curve.push([t, eq]);
    if (eq <= 0) { eq = 0; blown = true; }
  }
  const lastT = curve.length ? curve[curve.length - 1][0] : from;
  const years = Math.max((lastT - from) / YEAR, 1 / 12);
  // month-end equity
  const months = []; let prevEq = o.capital, curKey = null, lastEq = o.capital;
  for (const [t, e] of curve) {
    const d = new Date(t * 1000), key = d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0");
    if (curKey && key !== curKey) { months.push({ m: curKey, r: prevEq > 0 ? lastEq / prevEq - 1 : 0 }); prevEq = lastEq; }
    curKey = key; lastEq = e;
  }
  if (curKey) months.push({ m: curKey, r: prevEq > 0 ? lastEq / prevEq - 1 : 0 });
  const mr = months.map((x) => x.r).sort((a, b) => a - b);
  return {
    final: eq, n, wins, winRate: n ? wins / n : null, pf: gl > 0 ? gw / gl : null, mdd, skipped, blown, worst, curve, months, rs,
    years, cagr: eq > 0 && years > 0.2 ? Math.pow(eq / o.capital, 1 / years) - 1 : null,
    avgMonth: mr.length ? mr.reduce((a, b) => a + b, 0) / mr.length : null, medMonth: mr.length ? mr[Math.floor(mr.length / 2)] : null,
    posMonths: mr.length ? mr.filter((x) => x > 0).length / mr.length : null, best: mr[mr.length - 1], worstM: mr[0],
    perMonthTrades: n / Math.max(years * 12, 1), expectancy: n ? (eq - o.capital) / n : null,
  };
}
function riskSim(rs, tradesPerMonth, levels, months = 12, sims = 1200) {
  const n = Math.max(1, Math.round(tradesPerMonth * months));
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  return levels.map((risk) => {
    const f = risk / 100, monthly = []; let halved = 0, ruined = 0;
    for (let s = 0; s < sims; s++) {
      let eq = 1, peak = 1, half = false, dead = false;
      for (let k = 0; k < n; k++) {
        eq *= Math.max(0, 1 + f * rs[Math.floor(rnd() * rs.length)]);
        peak = Math.max(peak, eq); if (eq <= 0.5 * peak) half = true;
        if (eq < 0.1) { dead = true; break; }
      }
      halved += half; ruined += dead; monthly.push(dead ? -1 : Math.pow(Math.max(eq, 1e-9), 1 / months) - 1);
    }
    monthly.sort((a, b) => a - b);
    return { risk, med: monthly[Math.floor(sims / 2)], p10: monthly[Math.floor(sims / 10)], halved: halved / sims, ruined: ruined / sims };
  });
}

// ---------------------------------------------------------------- strategies tab
let chart = null, series = null;
function fillSelect(sel, items, value) {
  sel.innerHTML = items.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  if (value !== undefined && items.some(([v]) => v === value)) sel.value = value;
}
function currentFileName() {
  const e = INDEX.find((x) => x.symbol === $("#s-market").value && String(x.tf) === $("#s-tf").value);
  return e && e.file;
}
async function onMarket() {
  const sym = $("#s-market").value;
  const tfs = INDEX.filter((x) => x.symbol === sym).map((x) => [String(x.tf), x.tf >= 60 ? "1 hour (10 years)" : `${x.tf} minutes (2 years)`]);
  fillSelect($("#s-tf"), tfs, store.get("tf", "60"));
  await onTf();
}
async function onTf() {
  const f = await loadFile(currentFileName());
  const items = Object.entries(f.strategies)
    .sort((a, b) => (a[1].verdict === "holds up" ? -1 : 0) - (b[1].verdict === "holds up" ? -1 : 0) || (b[1].oosPF || 0) - (a[1].oosPF || 0))
    .map(([k, s]) => [k, `${s.label} — ${s.verdict}, win ${pctPlain(s.oosWin)} (later data)`]);
  fillSelect($("#s-strategy"), items, store.get("strategy"));
  render();
}
async function render() {
  const file = await loadFile(currentFileName());
  const key = $("#s-strategy").value, strat = file.strategies[key];
  if (!strat) return;
  ["market", "tf", "strategy"].forEach((k) => store.set(k, $("#s-" + k).value));
  const o = sizingOpts(file), from = periodStart(file, strat), r = simulate(strat.trades, o, from);
  const good = strat.verdict === "holds up";
  $("#s-verdict").className = "verdict " + (good ? "good" : "bad");
  $("#s-verdict").innerHTML = `<b>${esc(strat.label)}</b> on ${esc(NAMES[file.symbol])} · ${file.tf >= 60 ? "1h" : file.tf + "m"} bars ·
    verdict <span class="tag ${VERDICT_CLASS[strat.verdict]}">${esc(strat.verdict)}</span> · profit factor ${num(strat.isPF)} on the data used to choose it → ${num(strat.oosPF)} on later data ·
    settings ${esc(Object.entries(strat.params).filter(([k]) => !/flat|max_bars/.test(k)).map(([k, v]) => k + " " + v).join(", "))}
    ${r.blown ? '<br><span class="down">The account could not keep trading (ran out of margin for the minimum size).</span>' : ""}
    ${r.skipped ? `<br><span class="down">${r.skipped} trades skipped: the minimum size would have risked more than ${o.maxRisk}% — try the micro account type.</span>` : ""}`;
  const kpi = (k, v, s = "", c = "") => `<div class="kpi"><span class="k">${k}</span><span class="v ${c}">${v}</span><span class="s">${s}</span></div>`;
  $("#kpis").innerHTML = [
    kpi("Final balance", money(r.final), `from ${money(o.capital)}`, cls(r.final - o.capital)),
    kpi("Per month (average)", pct(r.avgMonth, 2), `typical month ${pct(r.medMonth, 2)}`, cls(r.avgMonth)),
    kpi("Per year (compounded)", pct(r.cagr), `${num(r.years, 1)} years`, cls(r.cagr)),
    kpi("Win rate", pctPlain(r.winRate, 1), `${r.n} trades · ${num(r.perMonthTrades, 1)}/month`),
    kpi("Profit factor", num(r.pf), "above 1 = made money", cls((r.pf || 0) - 1)),
    kpi("Worst drawdown", pctPlain(r.mdd, 1), `losing streak ${r.worst}`, r.mdd > 0.3 ? "down" : ""),
    kpi("Profitable months", pctPlain(r.posMonths), `best ${pct(r.best)} · worst ${pct(r.worstM)}`),
    kpi("Per trade (average)", money(r.expectancy), `at ${o.risk}% risk`, cls(r.expectancy)),
  ].join("");
  drawEquity(r.curve, o.capital, from);
  $("#eq-note").textContent = `· ${$("#s-period").selectedOptions[0].textContent.toLowerCase()}`;
  drawMonths(r.months);
  drawBoard(file, o);
  $("#g-out").innerHTML = "";
  updateGoalYear();
}
function drawEquity(curve, capital, from) {
  const LW = window.LightweightCharts; const el = $("#eq-chart");
  if (!LW) { el.textContent = "Chart library unavailable."; return; }
  if (!chart) {
    const dark = getComputedStyle(document.documentElement).colorScheme !== "light";
    chart = LW.createChart(el, { autoSize: true, layout: { background: { color: "transparent" }, textColor: dark ? "#7a8696" : "#5d6878", fontFamily: "JetBrains Mono, monospace" },
      grid: { vertLines: { color: dark ? "#141a22" : "#eef1f5" }, horzLines: { color: dark ? "#141a22" : "#eef1f5" } }, rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false } });
    series = chart.addAreaSeries({ lineColor: "#ffb000", topColor: "rgba(255,176,0,.25)", bottomColor: "rgba(255,176,0,0)", lineWidth: 2 });
  }
  const pts = [{ time: from, value: capital }]; let last = from;
  for (const [t, e] of curve) { const tt = t <= last ? last + 1 : t; pts.push({ time: tt, value: e }); last = tt; }
  series.setData(pts); chart.timeScale().fitContent();
}
function drawMonths(months) {
  const byYear = {}; for (const m of months) { const [y, mm] = m.m.split("-"); (byYear[y] ||= {})[Number(mm)] = m.r; }
  const names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const yearRet = (row) => Object.values(row).reduce((a, r) => a * (1 + r), 1) - 1;
  $("#months").innerHTML = `<thead><tr><th>Year</th>${names.map((n) => `<th>${n}</th>`).join("")}<th>Year</th></tr></thead><tbody>${
    Object.keys(byYear).sort().reverse().map((y) => `<tr><td>${y}</td>${names.map((_, i) => { const v = byYear[y][i + 1]; return `<td class="${cls(v)}">${v === undefined ? "" : (v * 100).toFixed(1)}</td>`; }).join("")}<td class="${cls(yearRet(byYear[y]))}"><b>${(yearRet(byYear[y]) * 100).toFixed(1)}%</b></td></tr>`).join("")}</tbody>`;
}
function drawBoard(file, o) {
  const rows = Object.entries(file.strategies).map(([k, s]) => ({ k, s, r: simulate(s.trades, o, periodStart(file, s)) }))
    .sort((a, b) => (b.r.final - a.r.final));
  const sel = $("#s-strategy").value;
  $("#board").innerHTML = `<thead><tr><th>Strategy</th><th>Verdict</th><th>Trades</th><th>Win rate</th><th>Profit factor</th><th>Avg month</th><th>Per year</th><th>Worst drawdown</th><th>Final</th></tr></thead><tbody>${
    rows.map(({ k, s, r }) => `<tr class="click ${k === sel ? "sel" : ""}" data-k="${esc(k)}"><td>${esc(s.label)}</td><td><span class="tag ${VERDICT_CLASS[s.verdict]}">${esc(s.verdict)}</span></td><td>${r.n}</td><td>${pctPlain(r.winRate)}</td><td class="${cls((r.pf || 0) - 1)}">${num(r.pf)}</td><td class="${cls(r.avgMonth)}">${pct(r.avgMonth, 2)}</td><td class="${cls(r.cagr)}">${pct(r.cagr)}</td><td>${pctPlain(r.mdd)}</td><td class="${cls(r.final - o.capital)}">${money(r.final)}</td></tr>`).join("")}</tbody>`;
}
$("#board").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-k]"); if (!tr) return; $("#s-strategy").value = tr.dataset.k; render(); window.scrollTo({ top: 0, behavior: "smooth" }); });

// goal check
function updateGoalYear() {
  const m = Number($("#g-target").value) / 100;
  $("#g-year").textContent = m > 0 ? `${(m * 100).toFixed(1)}% a month compounds to ${Math.round((Math.pow(1 + m, 12) - 1) * 100).toLocaleString()}% a year.` : "";
}
$("#g-target").addEventListener("input", updateGoalYear);
$("#g-run").addEventListener("click", async () => {
  const out = $("#g-out"); out.innerHTML = '<p class="muted">Checking…</p>';
  const target = Number($("#g-target").value) / 100, winGoal = Number($("#g-win").value) / 100;
  const file = await loadFile(currentFileName()), strat = file.strategies[$("#s-strategy").value];
  const o = { ...sizingOpts(file), risk: 1, maxRisk: 100 };
  const r = simulate(strat.trades, { ...o, capital: 1e6 }, periodStart(file, strat));
  const levels = [0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 25, 30];
  const sims = r.rs.length ? riskSim(r.rs, r.perMonthTrades, levels) : [];
  const need = sims.find((x) => x.med >= target), best = sims.reduce((a, b) => (b.med > (a?.med ?? -9) ? b : a), null);
  let verdict;
  if (!r.rs.length) verdict = "No trades in this period.";
  else if (r.final <= 1e6) verdict = "This strategy lost money after costs in this period, so more risk only loses faster.";
  else if (!need) verdict = `Not reachable. The best typical month is ${pct(best.med)} at ${best.risk}% risk per trade, where the account halves in ${pctPlain(best.halved)} of simulated years.`;
  else if (need.halved >= 0.25 || need.ruined >= 0.02) verdict = `Only at ${need.risk}% risk per trade — and then the account halves in ${pctPlain(need.halved)} and is wiped out in ${pctPlain(need.ruined)} of simulated years.`;
  else verdict = `Reachable at about ${need.risk}% risk per trade, halving in ${pctPlain(need.halved)} of simulated years.`;
  // strategies meeting the win-rate goal anywhere
  const all = [];
  for (const e of INDEX) { const f = await loadFile(e.file); for (const [k, s] of Object.entries(f.strategies)) all.push({ f, k, s }); }
  const winners = all.filter((x) => (x.s.oosWin || 0) >= winGoal && (x.s.oosPF !== null)).sort((a, b) => (b.s.oosWin - a.s.oosWin)).slice(0, 8);
  out.innerHTML = `<div class="verdict ${need && need.halved < 0.25 ? "good" : "bad"}">${esc(verdict)}</div>
    <table class="tbl"><thead><tr><th>Risk / trade</th><th>Typical month</th><th>Bad month (1 in 10)</th><th>Halves in a year</th><th>Wiped out</th></tr></thead><tbody>${
      sims.map((x) => `<tr><td>${x.risk}%</td><td class="${cls(x.med)}">${pct(x.med)}</td><td class="${cls(x.p10)}">${pct(x.p10)}</td><td class="${x.halved > 0.2 ? "down" : ""}">${pctPlain(x.halved)}</td><td class="${x.ruined > 0.01 ? "down" : ""}">${pctPlain(x.ruined)}</td></tr>`).join("")}</tbody></table>
    <h4>Strategies with a win rate of ${Math.round(winGoal * 100)}%+ on later data</h4>
    ${winners.length ? `<table class="tbl"><thead><tr><th>Market</th><th>Strategy</th><th>Win rate</th><th>Profit factor</th></tr></thead><tbody>${
      winners.map((x) => `<tr><td>${x.f.symbol} ${x.f.tf >= 60 ? "1h" : x.f.tf + "m"}</td><td>${esc(x.s.label)}</td><td>${pctPlain(x.s.oosWin)}</td><td class="${cls((x.s.oosPF || 0) - 1)}">${num(x.s.oosPF)}</td></tr>`).join("")}</tbody></table>
      <p class="muted small">A profit factor near 1.00 means roughly break-even after costs, however high the win rate.</p>` : '<p class="muted">None of the tested strategies reached that win rate on later data.</p>'}`;
});

["#s-market"].forEach((s) => $(s).addEventListener("change", onMarket));
$("#s-tf").addEventListener("change", onTf);
["#s-strategy", "#s-capital", "#s-risk", "#s-acct", "#s-maxrisk", "#s-period"].forEach((s) => $(s).addEventListener("change", render));

// ---------------------------------------------------------------- paper trading
const SPEC = {
  XAUUSD: { tv: "OANDA:XAUUSD", unitsPerLot: 100, spread: 0.30, label: "Gold" },
  US30: { tv: "OANDA:US30USD", unitsPerLot: 1, spread: 2.0, label: "US30" },
  NAS100: { tv: "OANDA:NAS100USD", unitsPerLot: 1, spread: 1.0, label: "NAS100" },
};
let paperSym = "XAUUSD", paperReady = false, goldPrice = null;
const acct = () => store.get("paper", null) || resetAcct();
function resetAcct() {
  const a = { start: Number($("#p-start").value) || 1000, cash: Number($("#p-start").value) || 1000, lev: Number($("#p-lev").value) || 100, pos: [], hist: [], marks: {} };
  store.set("paper", a); return a;
}
function priceOf(sym) {
  if (sym === "XAUUSD" && goldPrice) return goldPrice;
  const a = acct(); return a.marks[sym] || null;
}
function posPnl(p, mid) {
  const exit = p.side === "buy" ? mid - SPEC[p.sym].spread / 2 : mid + SPEC[p.sym].spread / 2;
  return (exit - p.entry) * p.units * (p.side === "buy" ? 1 : -1);
}
function equityOf(a) {
  let eq = a.cash, margin = 0;
  for (const p of a.pos) { const m = priceOf(p.sym) || p.entry; eq += posPnl(p, m); margin += p.units * m / a.lev; }
  return { eq, margin };
}
function closePos(a, p, mid, reason) {
  const pnl = posPnl(p, mid);
  a.cash += pnl; a.pos = a.pos.filter((x) => x.id !== p.id);
  a.hist.unshift({ ...p, exit: p.side === "buy" ? mid - SPEC[p.sym].spread / 2 : mid + SPEC[p.sym].spread / 2, pnl, reason, closedAt: Date.now() });
  a.hist = a.hist.slice(0, 200);
}
function placeOrder(side, opts = {}) {
  const a = acct(); const sym = opts.sym || paperSym;
  const lots = opts.lots ?? Number($("#o-lots").value);
  let mid = priceOf(sym);
  if (sym !== "XAUUSD" && !opts.sym) { const m = Number($("#o-manual").value); if (m > 0) { mid = m; a.marks[sym] = m; } }
  if (!mid) return alertMsg("No price yet. For US30/NAS100 type the current price from the chart.");
  if (!(lots > 0)) return alertMsg("Enter a lot size.");
  const units = lots * SPEC[sym].unitsPerLot;
  const entry = side === "buy" ? mid + SPEC[sym].spread / 2 : mid - SPEC[sym].spread / 2;
  const sl = opts.sl ?? (Number($("#o-sl").value) || null), tp = opts.tp ?? (Number($("#o-tp").value) || null);
  if (side === "buy" && ((sl && sl >= entry) || (tp && tp <= entry))) return alertMsg("For a buy, stop-loss must be below and take-profit above the price.");
  if (side === "sell" && ((sl && sl <= entry) || (tp && tp >= entry))) return alertMsg("For a sell, stop-loss must be above and take-profit below the price.");
  const { eq, margin } = equityOf(a);
  if (margin + units * mid / a.lev > eq) return alertMsg(`Not enough margin: this needs ${money(units * mid / a.lev)}, free ${money(eq - margin)}.`);
  a.pos.push({ id: Date.now().toString(36) + Math.random().toString(36).slice(2, 5), sym, side, lots, units, entry, sl, tp, openedAt: Date.now(), bot: !!opts.bot });
  store.set("paper", a); renderPaper();
  return true;
}
function alertMsg(m) { $("#p-feed").innerHTML = `<span class="down">${esc(m)}</span>`; return false; }
function checkStops() {
  const a = acct(); let changed = false;
  for (const p of [...a.pos]) {
    const mid = priceOf(p.sym); if (!mid) continue;
    const exit = p.side === "buy" ? mid - SPEC[p.sym].spread / 2 : mid + SPEC[p.sym].spread / 2;
    if (p.sl && (p.side === "buy" ? exit <= p.sl : exit >= p.sl)) { closePos(a, p, mid, "stop-loss"); changed = true; }
    else if (p.tp && (p.side === "buy" ? exit >= p.tp : exit <= p.tp)) { closePos(a, p, mid, "take-profit"); changed = true; }
  }
  const { eq, margin } = equityOf(a);
  if (a.pos.length && eq <= margin * 0.5) { for (const p of [...a.pos]) closePos(a, p, priceOf(p.sym) || p.entry, "stop-out"); changed = true; }
  if (changed) store.set("paper", a);
}
function renderPaper() {
  const a = acct(); const { eq, margin } = equityOf(a);
  $("#p-acct").innerHTML = `<span class="k">Balance</span><span class="v">${money(a.cash)}</span><span class="k">Equity</span><span class="v ${cls(eq - a.start)}">${money(eq)}</span>
    <span class="k">Margin used</span><span class="v">${money(margin)}</span><span class="k">Total P&amp;L</span><span class="v ${cls(eq - a.start)}">${money(eq - a.start)} (${pct(eq / a.start - 1)})</span>`;
  $("#p-pos").innerHTML = a.pos.length ? `<thead><tr><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>SL / TP</th><th>P&amp;L</th><th></th></tr></thead><tbody>${
    a.pos.map((p) => { const m = priceOf(p.sym); const pl = m ? posPnl(p, m) : null; return `<tr><td>${p.sym}${p.bot ? " (bot)" : ""}</td><td class="${p.side === "buy" ? "up" : "down"}">${p.side}</td><td>${p.lots}</td><td>${num(p.entry)}</td><td>${p.sl ? num(p.sl) : "—"} / ${p.tp ? num(p.tp) : "—"}</td><td class="${cls(pl)}">${money(pl)}</td><td><button class="ghost" data-close="${p.id}">CLOSE</button></td></tr>`; }).join("")}</tbody>` : '<tbody><tr><td class="muted">No open positions</td></tr></tbody>';
  $("#p-hist").innerHTML = a.hist.length ? `<thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry → exit</th><th>Why</th><th>P&amp;L</th></tr></thead><tbody>${
    a.hist.slice(0, 40).map((h) => `<tr><td>${new Date(h.closedAt).toLocaleString()}</td><td>${h.sym}</td><td>${h.side}</td><td>${h.lots}</td><td>${num(h.entry)} → ${num(h.exit)}</td><td>${esc(h.reason)}</td><td class="${cls(h.pnl)}">${money(h.pnl)}</td></tr>`).join("")}</tbody>` : '<tbody><tr><td class="muted">No closed trades yet</td></tr></tbody>';
  const m = priceOf(paperSym);
  $("#p-price").textContent = m ? num(m) : "—";
  $("#o-manual-wrap").hidden = paperSym === "XAUUSD";
}
$("#p-pos").addEventListener("click", (e) => {
  const b = e.target.closest("[data-close]"); if (!b) return;
  const a = acct(); const p = a.pos.find((x) => x.id === b.dataset.close); if (!p) return;
  let mid = priceOf(p.sym); if (p.sym !== "XAUUSD") { const mm = Number($("#o-manual").value); if (mm > 0) mid = mm; }
  if (!mid) return alertMsg("Type the current price first.");
  closePos(a, p, mid, "closed by you"); store.set("paper", a); renderPaper();
});
$("#o-buy").addEventListener("click", () => placeOrder("buy"));
$("#o-sell").addEventListener("click", () => placeOrder("sell"));
$("#o-manual").addEventListener("change", () => { const a = acct(); const v = Number($("#o-manual").value); if (v > 0) { a.marks[paperSym] = v; store.set("paper", a); checkStops(); renderPaper(); } });
$("#p-reset").addEventListener("click", () => { resetAcct(); renderPaper(); });
$("#p-sym").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-s]"); if (!b) return;
  paperSym = b.dataset.s; $$("#p-sym button").forEach((x) => x.classList.toggle("on", x === b)); loadTv(); renderPaper();
  $("#p-feed").textContent = paperSym === "XAUUSD" ? "Live gold price: PAXG/USDT from Binance (a gold-backed token, tracks spot within a few dollars)." : "No free live feed for this index in a static page: read the price on the chart and type it in.";
});

function loadTv() {
  const make = () => { $("#tv").innerHTML = ""; new window.TradingView.widget({ container_id: "tv", symbol: SPEC[paperSym].tv, interval: "15", timezone: "America/New_York", theme: matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark", style: "1", locale: "en", autosize: true, hide_side_toolbar: false, allow_symbol_change: false }); };
  if (window.TradingView) return make();
  const s = document.createElement("script"); s.src = "https://s3.tradingview.com/tv.js"; s.onload = make; document.head.appendChild(s);
}
async function pollGold() {
  try { const j = await (await fetch("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT")).json(); goldPrice = Number(j.price) || goldPrice; }
  catch { /* keep last */ }
  checkStops(); renderPaper(); botTick();
}
function initPaper() {
  if (paperReady) return; paperReady = true;
  const a = store.get("paper", null); if (a) { $("#p-start").value = a.start; $("#p-lev").value = a.lev; }
  $("#b-on").checked = store.get("botOn", false); $("#b-risk").value = store.get("botRisk", 1);
  loadTv(); pollGold(); setInterval(pollGold, 5000);
  $("#p-feed").textContent = "Live gold price: PAXG/USDT from Binance (a gold-backed token, tracks spot within a few dollars).";
}
$("#b-on").addEventListener("change", () => { store.set("botOn", $("#b-on").checked); botTick(true); });
$("#b-risk").addEventListener("change", () => store.set("botRisk", Number($("#b-risk").value)));

// gold Asian-hours drift bot (runs while the page is open)
let atrCache = { t: 0, atr: null };
async function hourlyAtr() {
  if (Date.now() - atrCache.t < 10 * 60 * 1000 && atrCache.atr) return atrCache.atr;
  const k = await (await fetch("https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=200")).json();
  const trs = k.map((c, i) => { const h = +c[2], l = +c[3], pc = i ? +k[i - 1][4] : +c[4]; return Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)); });
  let atr = trs.slice(0, 14).reduce((a, b) => a + b, 0) / 14; for (let i = 14; i < trs.length; i++) atr = (atr * 13 + trs[i]) / 14;
  atrCache = { t: Date.now(), atr }; return atr;
}
async function botTick(force) {
  const st = $("#b-status"); if (!$("#b-on").checked) { st.textContent = "Bot is off."; return; }
  if (!goldPrice) { st.textContent = "Waiting for the gold price…"; return; }
  const ny = new Date(new Date().toLocaleString("en-US", { timeZone: "America/New_York" }));
  const dayKey = ny.toDateString(), a = acct(), mine = a.pos.find((p) => p.bot);
  if (mine) {
    const hours = (Date.now() - mine.openedAt) / 3.6e6;
    if (hours >= 9) { closePos(a, mine, goldPrice, "time exit (9h)"); store.set("paper", a); renderPaper(); st.textContent = "Closed the bot trade after ~9 hours."; return; }
    st.textContent = `In a trade since ${new Date(mine.openedAt).toLocaleTimeString()} · exits about ${(9 - hours).toFixed(1)} h from now or at the stop-loss.`; return;
  }
  const wd = ny.getDay(); // 0 Sun .. 6 Sat
  const window = ny.getHours() === 18 && ny.getMinutes() < 30 && wd >= 1 && wd <= 4;
  if (!window) { st.textContent = `Waiting for 18:00 New York (Mon–Thu). New York time now ${ny.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`; return; }
  if (store.get("botDay", "") === dayKey) { st.textContent = "Already traded today's window."; return; }
  try {
    const atr = await hourlyAtr(); const stop = 5 * atr;
    const { eq } = equityOf(a); const riskUsd = eq * (Number($("#b-risk").value) || 1) / 100;
    const oz = Math.max(1, Math.floor(riskUsd / (stop + SPEC.XAUUSD.spread))); // 0.01 lot = 1 oz
    const lots = Math.round(oz) / 100;
    const entry = goldPrice + SPEC.XAUUSD.spread / 2;
    if (placeOrder("buy", { sym: "XAUUSD", lots, sl: +(entry - stop).toFixed(2), tp: null, bot: true })) {
      store.set("botDay", dayKey); st.textContent = `Bought ${lots} lots at ~${num(entry)} with stop-loss ${num(entry - stop)} (5× ATR ${num(atr)}).`;
    }
  } catch (e) { st.textContent = "Bot could not fetch prices: " + e.message; }
}

// ---------------------------------------------------------------- research tab
const FAMILIES = [
  ["ICT / Silver Bullet / FVG / smart money", 59, "17M", "silver_bullet, orb_fvg, fib_pullback"],
  ["Opening range / first candle (9:30)", 19, "2M", "ny_orb, orb_retest, orb_fvg, bias_orb"],
  ["Session breakout (London / Asia / New York)", 16, "2M", "asian_breakout, asian_sweep_fade, range4h_fade"],
  ["Supply and demand", 15, "8M", "supply_demand (with and without trend filter)"],
  ["Indicators (RSI, MACD, VWAP, UT Bot, Heikin-Ashi…)", 15, "5M", "rsi2_highwin, ut_bot, heikin_ashi_trend"],
  ["Moving averages / EMA", 14, "3M", "ema_rsi_scalp, trend_pullback, three_line_strike"],
  ["Price action / patterns / retests", 5, "28M", "pdhl_rejection, orb_retest, three_line_strike"],
  ["Bots / signals / paid indicators", 4, "<1M", "not testable"],
  ["Fibonacci", 1, "<1M", "fib_pullback"],
  ["General titles (rules only in the video)", 104, "29M", "transcripts of the most-viewed read; all mapped to the families above"],
];
const SOURCES = [
  ["This 1 Minute Scalping Strategy Works Everyday", "bITIVwysCzM", "1.7M", "Opening range + fair value gap"],
  ["The ONLY US30 Trading Strategy (1K a Day)", "UX09Cl-FN5Y", "1.7M", "Supply and demand"],
  ["This Gold Trading Strategy Makes $1,000 a Day", "AY7fWmE1CBk", "1.7M", "Supply and demand, 1:4"],
  ["The BEST 5 Minute Scalping Strategy Ever", "O5eC5lY7ZXY", "1.9M", "4-hour range false breakout"],
  ["XAUUSD Scalping Strategy", "oDOCBTYgCQM", "1.6M", "Three-line strike, SMMA 21/50/200"],
  ["BEST Gold Scalping Strategy (Beginner to PRO)", "en8RMFRqSME", "945k", "Trend-aligned supply/demand"],
  ["How to Trade Gold – Ride Massive Trends", "MKJz4yURCEs", "591k", "Heikin-Ashi trend"],
  ["My Simple 9:30AM Candle Scalping Strategy", "jq-fpkPv3-A", "437k", "9:30 candle break and retest"],
  ["1 Minute Fibonacci Scalping Strategy Backtested 100 Times", "2GAAK_JhNW0", "385k", "Fibonacci 0.5–0.618"],
  ["Complete NAS100 Strategy in Under 10 Minutes", "S9QGUIzWk2c", "170k", "10am window, Fibonacci + imbalance"],
  ["3 Step Beginner Scalping Strategy (500 Trade Backtest)", "kFyD3H6I1I8", "69k", "7/8 AM bias + opening range"],
  ["EASY Scalping Strategy (High Winrate)", "D80KULtCbHw", "—", "EMA trend, stop at 50 EMA, 1.5R"],
  ["Powerful XAUUSD Gold Trading Strategy", "3E7k47dZeWk", "2.2M", "Paid indicator — not testable"],
];
let SURVEY = null;
async function initResearch() {
  try { SURVEY = await (await fetch("data/youtube-survey.json")).json(); } catch { SURVEY = null; }
  initResearchStatic();
  if (SURVEY) {
    $("#r-fam").innerHTML = `<thead><tr><th>Strategy type (${SURVEY.videos.toLocaleString()} videos, ${SURVEY.searches} searches)</th><th>Videos</th><th>Views</th></tr></thead><tbody>${
      SURVEY.families.map(([f, n, v]) => `<tr><td>${esc(f)}</td><td>${n}</td><td>${v}</td></tr>`).join("")}</tbody>`;
    const w = SURVEY.winClaims;
    $("#r-claims").innerHTML = `<p>${w.count} titles claim a win rate: ${w.ge90} claim 90% or more, ${w["80s"]} claim 80–89%, ${w["70s"]} claim 70–79%. None showed a long, cost-included backtest that could be checked.</p><div class="scroll"><table class="tbl"><thead><tr><th>Most-viewed win-rate claims</th><th>Views</th><th>Claim</th></tr></thead><tbody>${
      w.top.map(([id, tt, v, p]) => `<tr><td><a href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener">${esc(tt)}</a></td><td>${v.toLocaleString()}K</td><td>${p}%</td></tr>`).join("")}</tbody></table></div>`;
  }
  researchCounts();
}
async function initResearchStatic() {
  $("#r-fam").innerHTML = `<thead><tr><th>Strategy type</th><th>Videos</th><th>Views</th><th>Tested as</th></tr></thead><tbody>${
    FAMILIES.map(([f, n, v, t]) => `<tr><td>${esc(f)}</td><td>${n}</td><td>${v}</td><td class="muted">${esc(t)}</td></tr>`).join("")}</tbody>`;
  $("#r-src").innerHTML = `<thead><tr><th>Video</th><th>Views</th><th>Rules</th></tr></thead><tbody>${
    SOURCES.map(([t, id, v, r]) => `<tr><td><a href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener">${esc(t)}</a></td><td>${v}</td><td class="muted">${esc(r)}</td></tr>`).join("")}</tbody>`;
}
async function researchCounts() {
  let tests = 0, holds = 0, high = 0;
  for (const e of INDEX) { const f = await loadFile(e.file); for (const s of Object.values(f.strategies)) { tests++; holds += s.verdict === "holds up"; high += (s.oosWin || 0) >= 0.8; } }
  const kpi = (k, v) => `<div class="kpi"><span class="k">${k}</span><span class="v">${v}</span></div>`;
  const tv = SURVEY ? SURVEY.timeVideos : 0;
  $("#r-kpis").innerHTML = kpi("Videos surveyed", SURVEY ? SURVEY.videos.toLocaleString() : "252") + kpi("Videos that teach a time", tv) + kpi("Instagram reels checked", "~40") + kpi("Strategy tests", tests) + kpi("Held up on later data", holds) + kpi("Tests with 80%+ win rate", high);
}

// ---------------------------------------------------------------- timing tab
let TIMING = [];
async function initTiming() {
  try { TIMING = await (await fetch("data/timing-index.json")).json(); } catch { TIMING = []; }
  if (SURVEY) {
    $("#t-count").textContent = `(${SURVEY.timeVideos} of ${SURVEY.videos.toLocaleString()} videos)`;
    $("#t-src").innerHTML = `<thead><tr><th>Time taught</th><th>Videos</th><th>Views</th><th>Most-viewed examples</th></tr></thead><tbody>${
      SURVEY.times.map(([n, c, v, , ex]) => `<tr><td>${esc(n)}</td><td>${c}</td><td>${v}</td><td style="text-align:left;white-space:normal">${ex.map(([id, tt]) => `<a href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener">${esc(tt)}</a>`).join("<br>")}</td></tr>`).join("")}</tbody>`;
  }
  if (!TIMING.length) { $("#t-heat").innerHTML = '<tbody><tr><td class="muted">Timing results not published yet.</td></tr></tbody>'; return; }
  fillSelect($("#t-market"), [...new Set(TIMING.map((x) => x.symbol))].map((s) => [s, NAMES[s] || s]), "XAUUSD");
  const tfs = () => fillSelect($("#t-tf"), TIMING.filter((x) => x.symbol === $("#t-market").value).map((x) => [String(x.tf), x.tf >= 60 ? "1-hour bars, 10 years" : `${x.tf}-minute bars, 2 years`]));
  tfs();
  $("#t-market").addEventListener("change", () => { tfs(); drawTiming(); });
  $("#t-tf").addEventListener("change", drawTiming);
  $("#t-side").addEventListener("change", drawTiming);
  drawTiming();
}
async function drawTiming() {
  const e = TIMING.find((x) => x.symbol === $("#t-market").value && String(x.tf) === $("#t-tf").value); if (!e) return;
  const d = await loadFile(e.file), side = $("#t-side").value;
  const rows = d.heatmap.filter((h) => h.side === side);
  const holds = [...new Set(rows.map((h) => h.hold))].sort((a, b) => a - b);
  const times = [...new Set(rows.map((h) => h.time))].sort();
  const c = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
  const cell = (h) => {
    if (!h || h.isN < 30) return '<td class="muted">·</td>';
    const strong = (h.isT || 0) >= 2.5 && (h.oosT || 0) >= 2;
    return `<td class="c ${strong ? "strong" : ""}" title="${h.isN} + ${h.oosN} trades; win ${pctPlain(h.isWin)} → ${pctPlain(h.oosWin)}"><span class="${c(h.isMeanR)}">${num(h.isMeanR)}</span> → <span class="${c(h.oosMeanR)}">${num(h.oosMeanR)}</span></td>`;
  };
  const by = {}; for (const h of rows) by[h.time + "|" + h.hold] = h;
  const hl = (x) => (x < 1 ? `${Math.round(x * 60)} min` : `${x} h`);
  $("#t-heat").innerHTML = `<thead><tr><th>Enter (NY)</th>${holds.map((h) => `<th>hold ${hl(h)}</th>`).join("")}</tr></thead><tbody>${
    times.map((t) => `<tr><td>${t}</td>${holds.map((h) => cell(by[t + "|" + h])).join("")}</tr>`).join("")}</tbody>`;
  const vids = Object.entries(d.tested).sort((a, b) => (b[1].oosPF || 0) - (a[1].oosPF || 0));
  $("#t-video").innerHTML = `<thead><tr><th>Time / rule</th><th>Verdict</th><th>Profit factor earlier → later</th><th>Win rate later</th><th>Trades later</th></tr></thead><tbody>${
    vids.map(([k, s]) => `<tr class="click" data-k="${esc(k)}"><td>${esc(s.label)}</td><td><span class="tag ${VERDICT_CLASS[s.verdict]}">${esc(s.verdict)}</span></td><td class="${cls((s.oosPF || 0) - 1)}">${num(s.isPF)} → ${num(s.oosPF)}</td><td>${pctPlain(s.oosWin)}</td><td>${s.oosTrades}</td></tr>`).join("")}</tbody>`;
}
$("#t-video").addEventListener("click", async (ev) => {
  const tr = ev.target.closest("tr[data-k]"); if (!tr) return;
  $("#s-market").value = $("#t-market").value; await onMarket();
  if ([...$("#s-tf").options].some((o) => o.value === $("#t-tf").value)) { $("#s-tf").value = $("#t-tf").value; await onTf(); }
  if ([...$("#s-strategy").options].some((o) => o.value === tr.dataset.k)) { $("#s-strategy").value = tr.dataset.k; render(); }
  $('#tabs button[data-tab="strategies"]').click(); window.scrollTo({ top: 0 });
});

// ---------------------------------------------------------------- XM demo (through the Ledger server on this PC)
const xurl = () => $("#x-url").value.replace(/\/+$/, "");
async function xget(path, opts) {
  const r = await fetch(xurl() + path, { ...opts, headers: { "content-type": "application/json" } });
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}
async function xmRefresh() {
  const st = $("#x-status");
  try {
    const s = await xget("/api/broker/mt5");
    if (!s.connected) { st.className = "verdict bad"; st.textContent = "Server reached, MetaTrader 5 not ready: " + s.message; $("#x-acct").innerHTML = ""; $("#x-pos").innerHTML = ""; }
    else {
      st.className = "verdict " + (s.algoTrading ? "good" : "bad");
      st.textContent = `Connected to ${s.company || "XM"} demo · login ${s.login} · ${s.server}` + (s.algoTrading ? " · Algo Trading on" : " · Algo Trading is OFF in MetaTrader: press the Algo Trading button so bots can place orders");
      $("#x-acct").innerHTML = `<span class="k">Balance</span><span class="v">${money(s.balance)} ${esc(s.currency)}</span><span class="k">Equity</span><span class="v ${cls(s.equity - s.balance)}">${money(s.equity)}</span><span class="k">Leverage</span><span class="v">1:${s.leverage}</span>`;
      const a = await xget("/api/accounts/xm-demo"); const pos = (a.account && a.account.positions) || [];
      $("#x-pos").innerHTML = pos.length ? `<thead><tr><th>Symbol</th><th>Lots</th><th>Entry</th><th>Now</th><th>SL / TP</th><th>P&amp;L</th></tr></thead><tbody>${
        pos.map((p) => `<tr><td>${esc(p.brokerSymbol)}</td><td class="${p.qty > 0 ? "up" : "down"}">${p.qty > 0 ? "buy" : "sell"} ${p.lots}</td><td>${num(p.avgPrice)}</td><td>${num(p.marketPrice)}</td><td>${p.stopLoss ? num(p.stopLoss) : "—"} / ${p.takeProfit ? num(p.takeProfit) : "—"}</td><td class="${cls(p.unrealizedPnl)}">${money(p.unrealizedPnl)}</td></tr>`).join("")}</tbody>` : '<tbody><tr><td class="muted">No open positions</td></tr></tbody>';
    }
    const auto = await xget("/api/automation"); const bots = auto.bots.filter((b) => b.account === "xm-demo");
    const last = (b) => { const l = (b.state && b.state.log) || []; const x = l[l.length - 1]; return x ? (x.msg || x.message || JSON.stringify(x)) : ""; };
    $("#x-bots").innerHTML = `<thead><tr><th>Bot</th><th>Strategy</th><th>Risk</th><th>Last action</th><th>On</th></tr></thead><tbody>${
      bots.map((b) => `<tr><td>${esc(b.id)}</td><td>${esc(b.strategy)}</td><td>${b.riskPct}%</td><td class="muted" style="white-space:normal;text-align:left">${esc(last(b))}</td><td><input type="checkbox" data-bot="${esc(b.id)}" ${b.enabled ? "checked" : ""}></td></tr>`).join("") || '<tr><td class="muted">No bots for xm-demo</td></tr>'}</tbody>`;
    window.__xbots = bots;
  } catch (e) {
    st.className = "verdict bad";
    st.textContent = `Cannot reach the Ledger server at ${xurl()} (${e.message}). Start it on this PC: .venv\\Scripts\\python run.py`;
  }
}
$("#x-connect").addEventListener("click", () => { store.set("xurl", $("#x-url").value); xmRefresh(); clearInterval(window.__xt); window.__xt = setInterval(xmRefresh, 15000); });
$("#x-bots").addEventListener("change", async (ev) => {
  const id = ev.target.dataset.bot; if (!id) return;
  const b = (window.__xbots || []).find((x) => x.id === id); if (!b) return;
  const { state, performance, running, ...cfg } = b; cfg.enabled = ev.target.checked;
  try { await xget(`/api/automation/bots/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(cfg) }); } catch (e) { alert(e.message); }
  xmRefresh();
});
$("#x-url").value = store.get("xurl", "http://127.0.0.1:3002");

// ---------------------------------------------------------------- boot
(async function boot() {
  INDEX = await (await fetch("data/index.json")).json();
  const markets = [...new Set(INDEX.map((x) => x.symbol))].map((s) => [s, NAMES[s] || s]);
  fillSelect($("#s-market"), markets, store.get("market", "XAUUSD"));
  await onMarket();
  await initResearch();
  initTiming();
  const tab = store.get("tab", "strategies"); const b = $(`#tabs button[data-tab="${tab}"]`); if (b) b.click();
})();
