/* Ledger Terminal front end: vanilla JS, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  symbol: localStore("sym") || "AAPL",
  range: "1y",
  overlays: new Set(["sma50", "sma200", "rsi"]),
  status: null,
  accounts: [],
  account: localStore("acct") || "paper-main",
  side: "buy",
  bars: [],
};

// ---------------------------------------------------------------- utilities
function localStore(key, value) {
  try {
    if (value === undefined) return localStorage.getItem("ledger." + key);
    localStorage.setItem("ledger." + key, value);
  } catch { return null; }
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const abs = Math.abs(x);
  const digits = abs !== 0 && abs < 1 ? 4 : d;
  return Number(x).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function money(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const s = Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (x < 0 ? "-$" : "$") + s;
}
function pct(x, d = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x >= 0 ? "+" : "") + Number(x).toFixed(d) + "%";
}
function cls(x) { return x > 0 ? "up" : x < 0 ? "down" : ""; }
function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
function hhmmss(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour12: false }); }

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isErr ? 6000 : 3000);
}

function modal(title, html, actions = []) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  const box = $("#modal-actions");
  box.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.className = a.className || "btn";
    b.textContent = a.label;
    b.onclick = async () => {
      b.disabled = true;
      try { await a.onClick(); closeModal(); } catch (e) { toast(e.message, true); b.disabled = false; }
    };
    box.appendChild(b);
  }
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" || e.target.hasAttribute("data-close")) closeModal(); });

/* Minimal, safe markdown: escape first, then bold/code/bullets/line breaks. */
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    let line = raw.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    const bullet = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (bullet) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${bullet[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    line = line.replace(/^#{1,4}\s+(.*)$/, "<b>$1</b>");
    html += line ? line + "<br>" : "<br>";
  }
  if (inList) html += "</ul>";
  return html.replace(/(<br>)+$/, "");
}

// ---------------------------------------------------------------- tabs
function selectTab(group, name) {
  $$(`[data-tabs="${group}"] button`).forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(`[data-tabbody="${group}"] > .pane`).forEach((p) => p.classList.toggle("on", p.dataset.pane === name));
  if (group === "right") localStore("rtab", name);
  if (name === "port") loadRisk();
  if (name === "auto") loadAutomation();
}
$$(".tabs").forEach((bar) => bar.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) selectTab(bar.dataset.tabs, b.dataset.tab);
}));

// ---------------------------------------------------------------- status + clock
async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const data = $("#pill-data");
    data.textContent = s.marketData === "demo" ? "DATA DEMO" : "DATA LIVE·DELAYED";
    data.className = "pill " + (s.marketData === "demo" ? "warn" : "ok");
    const ai = $("#pill-ai");
    ai.textContent = s.ai.configured ? "AI " + s.ai.model.replace("claude-", "").toUpperCase() : "AI OFF";
    ai.className = "pill " + (s.ai.configured ? "ok" : "off");
    $("#ai-off").hidden = s.ai.configured;
    const hb = $("#pill-hb");
    hb.textContent = s.heartbeat.enabled ? `HB ${s.heartbeat.intervalMinutes}m` : "HB OFF";
    hb.className = "pill " + (s.heartbeat.enabled ? "ok" : "off");
    $("#hb-enabled").checked = s.heartbeat.enabled;
    const au = $("#pill-auto");
    const n = s.automation.enabledBots.length;
    au.textContent = s.automation.killSwitch ? "AUTO KILLED" : n ? `AUTO ${n} BOT${n > 1 ? "S" : ""}` : "AUTO OFF";
    au.className = "pill " + (s.automation.killSwitch ? "warn" : n ? "ok" : "off");
    $("#hb-interval").value = s.heartbeat.intervalMinutes;
    for (const [id, err] of Object.entries(s.accountErrors || {})) console.warn("account", id, err);
  } catch (e) { toast("Status: " + e.message, true); }
}
setInterval(() => ($("#clock").textContent = new Date().toLocaleTimeString([], { hour12: false })), 1000);

// ---------------------------------------------------------------- watchlist + tape
async function loadWatchlist() {
  const syms = state.status?.watchlist || (await api("/api/watchlist")).symbols;
  if (!syms.length) return;
  const quotes = await api("/api/quotes?symbols=" + encodeURIComponent(syms.join(",")));
  const tbody = $("#watchlist tbody");
  tbody.innerHTML = quotes.map((q) => q.error
    ? `<tr data-s="${esc(q.symbol)}"><td>${esc(q.symbol)}</td><td colspan="2" class="muted">n/a</td></tr>`
    : `<tr data-s="${esc(q.symbol)}" class="${q.symbol === state.symbol ? "on" : ""}"><td>${esc(q.symbol)}</td><td>${num(q.price)}</td><td class="${cls(q.changePct)}">${pct(q.changePct)}</td></tr>`).join("");
  const items = quotes.filter((q) => !q.error).map((q) =>
    `<span class="tape-item" data-s="${esc(q.symbol)}"><b>${esc(q.symbol)}</b>${num(q.price)} <span class="${cls(q.changePct)}">${pct(q.changePct)}</span></span>`).join("");
  $("#tape-track").innerHTML = items + items; // doubled for a seamless loop
}
$("#watchlist").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-s]"); if (tr) loadSymbol(tr.dataset.s); });
$("#tape-track").addEventListener("click", (e) => { const it = e.target.closest("[data-s]"); if (it) loadSymbol(it.dataset.s); });
$("#wl-edit").addEventListener("click", () => {
  const current = (state.status?.watchlist || []).join(", ");
  modal("EDIT WATCHLIST", `<p class="muted small">Comma-separated symbols. Stocks (AAPL), crypto (BTC-USD), futures (GC=F), FX (EURUSD=X).</p><textarea id="wl-text" rows="4" style="width:100%">${esc(current)}</textarea>`, [{
    label: "SAVE", onClick: async () => {
      const symbols = $("#wl-text").value.split(/[\s,]+/).filter(Boolean);
      await api("/api/watchlist", { method: "PUT", body: { symbols } });
      await loadStatus(); await loadWatchlist();
    },
  }]);
});

// ---------------------------------------------------------------- chart
let chart, candles, volume, lines = {}, rsiChart, rsiLine;
function initCharts() {
  const LW = window.LightweightCharts;
  if (!LW) { $("#chart").innerHTML = '<p class="muted" style="padding:12px">Chart library failed to load (offline?). Data panels still work.</p>'; return; }
  const base = {
    layout: { background: { color: "transparent" }, textColor: "#7a8696", fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
    grid: { vertLines: { color: "#141a22" }, horzLines: { color: "#141a22" } },
    rightPriceScale: { borderColor: "#1e2631" },
    timeScale: { borderColor: "#1e2631", timeVisible: true },
    crosshair: { mode: 0 },
    autoSize: true,
  };
  chart = LW.createChart($("#chart"), base);
  candles = chart.addCandlestickSeries({ upColor: "#22c55e", downColor: "#f04f4f", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#f04f4f" });
  volume = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: "#2a3442" });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const colors = { sma20: "#38bdf8", sma50: "#ffb000", sma200: "#a78bfa" };
  for (const k of Object.keys(colors)) lines[k] = chart.addLineSeries({ color: colors[k], lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  rsiChart = LW.createChart($("#rsi-chart"), { ...base, timeScale: { visible: false } });
  rsiLine = rsiChart.addLineSeries({ color: "#ffb000", lineWidth: 1, priceLineVisible: false });
  rsiLine.createPriceLine({ price: 70, color: "#f04f4f55", lineStyle: 2, axisLabelVisible: false });
  rsiLine.createPriceLine({ price: 30, color: "#22c55e55", lineStyle: 2, axisLabelVisible: false });
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => r && rsiChart.timeScale().setVisibleLogicalRange(r));
}

function smaSeries(bars, n) {
  const out = []; let sum = 0;
  bars.forEach((b, i) => { sum += b.c; if (i >= n) sum -= bars[i - n].c; if (i >= n - 1) out.push({ time: b.t, value: sum / n }); });
  return out;
}
function rsiSeries(bars, n = 14) {
  const out = []; if (bars.length <= n) return out;
  let g = 0, l = 0;
  for (let i = 1; i <= n; i++) { const d = bars[i].c - bars[i - 1].c; g += Math.max(d, 0); l += Math.max(-d, 0); }
  g /= n; l /= n;
  const val = () => (l === 0 ? 100 : 100 - 100 / (1 + g / l));
  out.push({ time: bars[n].t, value: val() });
  for (let i = n + 1; i < bars.length; i++) {
    const d = bars[i].c - bars[i - 1].c;
    g = (g * (n - 1) + Math.max(d, 0)) / n; l = (l * (n - 1) + Math.max(-d, 0)) / n;
    out.push({ time: bars[i].t, value: val() });
  }
  return out;
}
const RANGE_INTERVAL = { "1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1d", "5y": "1wk" };

async function loadChart() {
  if (!chart) return;
  try {
    const { bars } = await api(`/api/history/${encodeURIComponent(state.symbol)}?range=${state.range}&interval=${RANGE_INTERVAL[state.range]}`);
    state.bars = bars;
    candles.setData(bars.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
    volume.setData(bars.map((b) => ({ time: b.t, value: b.v, color: b.c >= b.o ? "#22c55e33" : "#f04f4f33" })));
    for (const [k, n] of [["sma20", 20], ["sma50", 50], ["sma200", 200]]) lines[k].setData(state.overlays.has(k) ? smaSeries(bars, n) : []);
    const showRsi = state.overlays.has("rsi");
    $("#rsi-chart").classList.toggle("hide", !showRsi);
    rsiLine.setData(showRsi ? rsiSeries(bars) : []);
    chart.timeScale().fitContent();
  } catch (e) { toast(`Chart ${state.symbol}: ${e.message}`, true); }
}
$("#ranges").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.range = b.dataset.r;
  $$("#ranges button").forEach((x) => x.classList.toggle("on", x === b));
  loadChart();
});
$("#overlays").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const k = b.dataset.o;
  state.overlays.has(k) ? state.overlays.delete(k) : state.overlays.add(k);
  b.classList.toggle("on", state.overlays.has(k));
  loadChart();
});

// ---------------------------------------------------------------- quote + technicals + news
async function loadQuote() {
  try {
    const [q, t] = await Promise.all([api("/api/quote/" + encodeURIComponent(state.symbol)), api("/api/technicals/" + encodeURIComponent(state.symbol))]);
    $("#sym").textContent = q.symbol;
    $("#sym-name").textContent = `${q.name} · ${q.exchange} · ${q.currency}`;
    $("#sym-px").textContent = num(q.price);
    const chg = $("#sym-chg");
    chg.textContent = `${q.change >= 0 ? "+" : ""}${num(q.change)} (${pct(q.changePct)})`;
    chg.className = "chg " + cls(q.change);
    const s = t.summary;
    const row = (k, v, c = "") => `<span class="k">${k}</span><span class="v ${c}">${v}</span>`;
    $("#quote-detail").innerHTML = `
      <div class="kv">
        ${row("Prev close", num(q.prevClose))}
        ${row("Day range", `${num(q.dayLow)} – ${num(q.dayHigh)}`)}
        ${row("52w range", `${num(q.low52w)} – ${num(q.high52w)}`)}
        ${row("Volume", q.volume ? Number(q.volume).toLocaleString() : "—")}
        ${row("SMA 20/50", `${num(s.sma20)} / ${num(s.sma50)}`)}
        ${row("SMA 200", num(s.sma200))}
        ${row("RSI 14", num(s.rsi14, 1), s.rsi14 > 70 ? "down" : s.rsi14 < 30 ? "up" : "")}
        ${row("MACD / sig", `${num(s.macd)} / ${num(s.macdSignal)}`, cls(s.macdHist))}
        ${row("ATR 14", num(s.atr14))}
        ${row("1M / 3M", `${pct(s.return1m, 1)} / ${pct(s.return3m, 1)}`)}
      </div>
      <ul class="signals">${(s.signals || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <p class="muted small">Source: ${esc(q.source)} · ${q.marketTime ? hhmmss(q.marketTime) : ""}</p>`;
    $$("#watchlist tr").forEach((tr) => tr.classList.toggle("on", tr.dataset.s === state.symbol));
  } catch (e) { $("#quote-detail").innerHTML = `<p class="down">${esc(e.message)}</p>`; }
}
async function loadNews() {
  try {
    const items = await api("/api/news?symbol=" + encodeURIComponent(state.symbol));
    $("#news").innerHTML = items.length ? items.map((n) => `<li>${n.link
      ? `<a href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>`
      : `<span>${esc(n.title)}</span>`}<div class="meta">${esc(n.source)} · ${ago(n.published)}</div></li>`).join("") : '<li class="muted">No headlines.</li>';
  } catch (e) { $("#news").innerHTML = `<li class="down">${esc(e.message)}</li>`; }
}

function loadSymbol(sym) {
  state.symbol = sym.toUpperCase();
  localStore("sym", state.symbol);
  $("#panel-sym").textContent = state.symbol;
  $("#ticket [name=symbol]").value = state.symbol;
  $("#bt-form [name=symbol]").value = state.symbol;
  $("#sym").textContent = state.symbol;
  loadQuote(); loadChart(); loadNews();
}

// ---------------------------------------------------------------- command bar
const HELP = [
  ["AAPL", "Load a symbol (stocks, BTC-USD, GC=F, EURUSD=X)"],
  ["FIND apple", "Search symbols by name"],
  ["BUY 10 AAPL", "Stage a market order in the selected account"],
  ["SELL 5 AAPL LMT 250", "Stage a limit order"],
  ["COMMIT trim tech", "Commit staged orders with a message"],
  ["ASK <question>  or  ? <question>", "Ask the AI agent"],
  ["PANEL [SYM]", "Run the AI investor panel"],
  ["NEWS · DCF · PORT · TRADE · LOG · EVENTS", "Jump to a view"],
  ["WATCH ADD TSLA · WATCH RM TSLA", "Edit the watchlist"],
  ["1D 5D 1M 3M 6M 1Y 2Y 5Y", "Change chart range"],
];
async function runCommand(raw) {
  const text = raw.trim();
  if (!text) return;
  const [head, ...rest] = text.split(/\s+/);
  const cmd = head.toUpperCase();
  const arg = rest.join(" ");
  try {
    if (cmd === "HELP") {
      return modal("COMMANDS", `<div class="kv">${HELP.map(([c, d]) => `<span class="k" style="color:var(--amber)">${esc(c)}</span><span class="v" style="text-align:left">${esc(d)}</span>`).join("")}</div>`);
    }
    if (cmd === "ASK" || cmd === "?") { selectTab("right", "agent"); return sendChat(arg); }
    if (cmd === "PANEL") { if (rest[0]) loadSymbol(rest[0]); selectTab("lower", "panel"); return runPanel(); }
    if (cmd === "NEWS") { if (rest[0]) loadSymbol(rest[0]); return selectTab("lower", "news"); }
    if (cmd === "DCF") return selectTab("lower", "dcf");
    if (cmd === "PORT") return selectTab("right", "port");
    if (cmd === "TRADE" || cmd === "LOG") return selectTab("right", "trade");
    if (cmd === "EVENTS") return selectTab("right", "events");
    if (cmd === "FIND") return showSuggest(await api("/api/search?q=" + encodeURIComponent(arg)));
    if (cmd === "COMMIT") { selectTab("right", "trade"); return commitStaged(arg); }
    if (cmd === "BUY" || cmd === "SELL") {
      const m = arg.match(/^([\d.]+)\s+(\S+)(?:\s+(?:LMT|LIMIT|@)\s*([\d.]+))?$/i);
      if (!m) throw new Error("Usage: BUY 10 AAPL  or  SELL 5 AAPL LMT 250");
      selectTab("right", "trade");
      return stageOrder({ symbol: m[2], side: cmd.toLowerCase(), qty: Number(m[1]), type: m[3] ? "limit" : "market", limitPrice: m[3] ? Number(m[3]) : null, rationale: "" });
    }
    if (cmd === "WATCH" && rest.length >= 2) {
      const sym = rest[1].toUpperCase();
      let list = state.status.watchlist.filter((s) => s !== sym);
      if (rest[0].toUpperCase() === "ADD") list = [...list, sym];
      await api("/api/watchlist", { method: "PUT", body: { symbols: list } });
      await loadStatus(); return loadWatchlist();
    }
    const rangeKey = { "1D": "1d", "5D": "5d", "1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y", "2Y": "2y", "5Y": "5y" }[cmd];
    if (rangeKey && !rest.length) return $(`#ranges [data-r="${rangeKey}"]`).click();
    if (/^[A-Z0-9.\-=^]{1,20}$/.test(cmd)) return loadSymbol(cmd);
    throw new Error(`Unknown command "${head}". Type HELP.`);
  } catch (e) { toast(e.message, true); }
}
function showSuggest(results) {
  const ul = $("#cmd-suggest");
  if (!results.length) { ul.hidden = true; return; }
  ul.innerHTML = results.map((r) => `<li data-s="${esc(r.symbol)}"><b>${esc(r.symbol)}</b><span>${esc(r.name)}</span><span class="muted">${esc(r.type || "")} ${esc(r.exchange || "")}</span></li>`).join("");
  ul.hidden = false;
}
let suggestTimer;
$("#cmd").addEventListener("input", (e) => {
  clearTimeout(suggestTimer);
  const v = e.target.value.trim();
  if (v.length < 2 || /\s/.test(v)) { $("#cmd-suggest").hidden = true; return; }
  suggestTimer = setTimeout(async () => { try { showSuggest(await api("/api/search?q=" + encodeURIComponent(v))); } catch { /* ignore */ } }, 300);
});
$("#cmd-suggest").addEventListener("mousedown", (e) => {
  const li = e.target.closest("li[data-s]"); if (!li) return;
  e.preventDefault(); $("#cmd").value = ""; $("#cmd-suggest").hidden = true; loadSymbol(li.dataset.s);
});
$("#cmd").addEventListener("blur", () => setTimeout(() => ($("#cmd-suggest").hidden = true), 150));
$("#cmd-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#cmd").value; $("#cmd").value = ""; $("#cmd-suggest").hidden = true;
  runCommand(v);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $("#cmd").focus(); }
  if (e.key === "Escape") closeModal();
});

// ---------------------------------------------------------------- agent chat
function addMsg(kind, html, tools = []) {
  const div = document.createElement("div");
  div.className = "msg " + kind;
  div.innerHTML = html;
  if (tools.length) {
    const t = document.createElement("div");
    t.className = "tools";
    t.innerHTML = tools.map((n) => `<span class="tool-chip ${/stage|commit/.test(n) ? "trade" : ""}">${esc(n)}</span>`).join("");
    div.appendChild(t);
  }
  $("#chat").appendChild(div);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return div;
}
async function sendChat(text) {
  text = (text || "").trim();
  if (!text) return;
  addMsg("user", esc(text));
  const pending = addMsg("bot typing", "");
  $("#chat-send").disabled = true;
  try {
    const out = await api("/api/agent/chat", { method: "POST", body: { message: text, channel: "web" } });
    pending.remove();
    addMsg("bot", md(out.reply || "(no reply)"), out.tools || []);
    if ((out.tools || []).some((t) => /stage|commit/.test(t))) loadAccounts();
  } catch (e) {
    pending.remove();
    addMsg("err", esc(e.message));
  } finally { $("#chat-send").disabled = false; }
}
$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#chat-input").value; $("#chat-input").value = ""; sendChat(v); });
$("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); } });
$("#chat-reset").addEventListener("click", async () => {
  await api("/api/agent/reset?channel=web", { method: "POST" });
  $("#chat").innerHTML = "";
  addMsg("bot", "New conversation. I can research tickers, explain indicators and headlines, and stage or commit paper trades for you to review. I can't push: execution is always yours.");
});

// ---------------------------------------------------------------- investor panel
async function runPanel() {
  const out = $("#panel-out");
  const btn = $("#panel-run");
  btn.disabled = true;
  out.innerHTML = `<p class="muted typing">Running five lenses on ${esc(state.symbol)} </p>`;
  try {
    const r = await api("/api/agent/panel", { method: "POST", body: { symbol: state.symbol } });
    out.innerHTML = r.lenses.map((l) => l.error
      ? `<div class="lens"><h5>${esc(l.lens.toUpperCase())}</h5><p class="down">${esc(l.error)}</p></div>`
      : `<div class="lens">
          <h5><span>${esc(l.lens.toUpperCase())}</span><span class="stance-${esc(l.stance)}">${esc(l.stance.toUpperCase())} <span class="dots">${"●".repeat(l.conviction)}${"○".repeat(5 - l.conviction)}</span></span></h5>
          <div>${esc(l.thesis)}</div>
          <ul>${l.key_points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
          <div class="muted small"><b>Risks:</b> ${l.risks.map(esc).join("; ")}</div>
          <div class="muted small"><b>Would change my mind:</b> ${esc(l.what_would_change_my_mind)}</div>
        </div>`).join("");
  } catch (e) { out.innerHTML = `<p class="down">${esc(e.message)}</p>`; }
  finally { btn.disabled = false; }
}
$("#panel-run").addEventListener("click", runPanel);

// ---------------------------------------------------------------- DCF
$("#dcf-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target).entries());
  const body = {
    freeCashFlow: +f.freeCashFlow, growthRate: +f.growthRate / 100, years: +f.years,
    discountRate: +f.discountRate / 100, terminalGrowth: +f.terminalGrowth / 100,
    netDebt: +f.netDebt || 0, sharesOutstanding: +f.sharesOutstanding,
  };
  try {
    const r = await api("/api/dcf", { method: "POST", body });
    const px = Number($("#sym-px").textContent.replace(/,/g, ""));
    const upside = px ? (r.valuePerShare / px - 1) * 100 : null;
    $("#dcf-out").innerHTML = `
      <div class="kv">
        <span class="k">PV of cash flows</span><span class="v">${num(r.pvOfCashFlows, 0)}</span>
        <span class="k">PV of terminal value</span><span class="v">${num(r.pvOfTerminalValue, 0)} (${num(r.terminalShareOfValue * 100, 0)}% of EV)</span>
        <span class="k">Enterprise value</span><span class="v">${num(r.enterpriseValue, 0)}</span>
        <span class="k">Equity value</span><span class="v">${num(r.equityValue, 0)}</span>
        <span class="k" style="color:var(--amber)">Value per share</span><span class="v" style="color:var(--amber)">${num(r.valuePerShare)}</span>
        ${upside !== null ? `<span class="k">vs ${esc(state.symbol)} @ ${num(px)}</span><span class="v ${cls(upside)}">${pct(upside, 1)}</span>` : ""}
      </div>
      <p class="muted small">Inputs are yours; the model is only as good as the growth and discount assumptions. Units: keep FCF, net debt and shares in the same scale (e.g. millions).</p>`;
  } catch (err) { $("#dcf-out").innerHTML = `<p class="down">${esc(err.message)}</p>`; }
});

// ---------------------------------------------------------------- accounts / trading-as-git
function currentAccount() { return state.accounts.find((a) => a.id === state.account) || state.accounts[0]; }
async function loadAccounts() {
  try {
    const r = await api("/api/accounts");
    state.accounts = r.accounts;
    const sel = $("#acct-select");
    sel.innerHTML = r.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.label)}</option>`).join("");
    if (!r.accounts.some((a) => a.id === state.account) && r.accounts[0]) state.account = r.accounts[0].id;
    sel.value = state.account;
    renderAccount();
  } catch (e) { toast("Accounts: " + e.message, true); }
}
function orderText(o) {
  return `<span class="${o.side}">${o.side.toUpperCase()}</span> ${num(o.qty, o.qty % 1 ? 4 : 0)} <b>${esc(o.symbol)}</b> ${o.type === "limit" ? "LMT " + num(o.limitPrice) : "MKT"}`;
}
function renderAccount() {
  const a = currentAccount();
  if (!a) { $("#acct-summary").innerHTML = '<span class="muted">No enabled accounts.</span>'; return; }
  const acct = a.account;
  const g = a.guards;
  $("#acct-summary").innerHTML = acct ? `
    <span class="k">Equity</span><span class="v">${money(acct.equity)}</span>
    <span class="k">Cash</span><span class="v">${money(acct.cash)}</span>
    <span class="k">Total P&amp;L</span><span class="v ${cls(acct.totalPnl)}">${money(acct.totalPnl)}</span>
    <span class="k">Guards</span><span class="v muted">order ≤ ${g.maxOrderValue ? money(g.maxOrderValue) : "∞"} · position ≤ ${g.maxPositionValue ? money(g.maxPositionValue) : "∞"} · cooldown ${g.cooldownSeconds}s${g.symbolWhitelist.length ? " · only " + esc(g.symbolWhitelist.join(",")) : ""}</span>`
    : `<span class="k">Error</span><span class="v down">${esc(a.error)}</span>`;

  $("#staged").innerHTML = a.staged.length ? a.staged.map((s) => `
    <li><div class="order-line"><span>${orderText(s.order)} <span class="muted">@ ~${num(s.priceAtStage)}</span></span>
      <span><span class="src ${s.source}">${esc(s.source.toUpperCase())}</span> <button class="ghost" data-unstage="${esc(s.id)}" title="Unstage">✕</button></span></div>
      ${s.rationale ? `<div class="rationale">${esc(s.rationale)}</div>` : ""}
      ${s.warnings.map((w) => `<div class="warn-line">⚠ ${esc(w)}</div>`).join("")}</li>`).join("")
    : '<li class="empty">Nothing staged</li>';

  $("#pending").innerHTML = a.pending.length ? a.pending.map((c) => `
    <li><div class="order-line"><span><span class="hash">${esc(c.hash)}</span> ${esc(c.message)}</span><span class="src ${c.author}">${esc(c.author.toUpperCase())}</span></div>
      ${c.orders.map((s) => `<div class="small">· ${orderText(s.order)}${s.warnings.length ? ' <span class="down">⚠</span>' : ""}</div>`).join("")}
      <div class="row gap" style="margin-top:6px"><button class="btn push" data-push="${esc(c.hash)}">PUSH</button><button class="ghost" data-revert="${esc(c.hash)}">REVERT</button></div></li>`).join("")
    : '<li class="empty">No commits waiting</li>';
  const badge = $("#pending-badge");
  badge.hidden = !a.pending.length; badge.textContent = a.pending.length;

  const oo = acct?.openOrders || [];
  $("#open-orders").innerHTML = oo.length ? oo.map((o) => `<li><div class="order-line"><span>${orderText(o)}</span><button class="ghost" data-cancel="${esc(o.id)}">CANCEL</button></div></li>`).join("") : '<li class="empty">None</li>';

  $("#gitlog").innerHTML = a.log.length ? a.log.map((c) => `
    <li data-show="${esc(c.hash)}"><span class="dot st-${esc(c.status)}">●</span><span class="hash">${esc(c.hash)}</span><span class="st-${esc(c.status)}">${esc(c.status)}</span>
      <span>${esc(c.message)} <span class="muted">· ${esc(c.author)} · ${c.orders} order${c.orders === 1 ? "" : "s"} · ${ago(c.pushedAt || c.createdAt)}</span></span></li>`).join("")
    : '<li class="muted">No commits yet</li>';

  $("#positions tbody").innerHTML = acct && acct.positions.length ? acct.positions.map((p) => `
    <tr><td>${esc(p.symbol)}</td><td>${num(p.qty, p.qty % 1 ? 4 : 0)}</td><td>${num(p.avgPrice)}</td><td>${num(p.marketPrice)}</td><td class="${cls(p.unrealizedPnl)}">${money(p.unrealizedPnl)} <span class="small">(${pct(p.unrealizedPnlPct, 1)})</span></td></tr>`).join("")
    : '<tr><td colspan="5" class="muted" style="text-align:center">No positions</td></tr>';
}
$("#acct-select").addEventListener("change", (e) => { state.account = e.target.value; localStore("acct", state.account); renderAccount(); loadRisk(); });
$("#acct-refresh").addEventListener("click", loadAccounts);

async function stageOrder(body) {
  const s = await api(`/api/accounts/${encodeURIComponent(state.account)}/stage`, { method: "POST", body });
  toast(s.warnings.length ? `Staged with ${s.warnings.length} guard warning(s)` : "Staged");
  await loadAccounts();
}
async function commitStaged(message) {
  if (!message.trim()) throw new Error("Enter a commit message");
  const c = await api(`/api/accounts/${encodeURIComponent(state.account)}/commit`, { method: "POST", body: { message } });
  toast(`Committed ${c.hash}. Review and push when ready.`);
  await loadAccounts();
}

$$("#ticket .side button").forEach((b) => b.addEventListener("click", () => {
  state.side = b.dataset.side;
  $$("#ticket .side button").forEach((x) => x.classList.toggle("on", x === b));
}));
$("#ticket [name=type]").addEventListener("change", (e) => { $("#ticket [name=limitPrice]").disabled = e.target.value !== "limit"; });
$("#ticket").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await stageOrder({
      symbol: f.get("symbol"), side: state.side, qty: Number(f.get("qty")), type: f.get("type"),
      limitPrice: f.get("type") === "limit" ? Number(f.get("limitPrice")) : null, rationale: f.get("rationale") || "",
      stopLoss: f.get("stopLoss") ? Number(f.get("stopLoss")) : null, takeProfit: f.get("takeProfit") ? Number(f.get("takeProfit")) : null,
    });
    e.target.qty.value = ""; e.target.rationale.value = ""; e.target.stopLoss.value = ""; e.target.takeProfit.value = "";
  } catch (err) { toast(err.message, true); }
});
$("#commit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await commitStaged($("#commit-msg").value); $("#commit-msg").value = ""; } catch (err) { toast(err.message, true); }
});

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-unstage],[data-push],[data-revert],[data-cancel],[data-show]");
  if (!t) return;
  const acct = encodeURIComponent(state.account);
  try {
    if (t.dataset.unstage) { await api(`/api/accounts/${acct}/stage/${t.dataset.unstage}`, { method: "DELETE" }); return loadAccounts(); }
    if (t.dataset.revert) { await api(`/api/accounts/${acct}/revert/${t.dataset.revert}`, { method: "POST" }); toast("Reverted"); return loadAccounts(); }
    if (t.dataset.cancel) { await api(`/api/accounts/${acct}/orders/${t.dataset.cancel}/cancel`, { method: "POST" }); return loadAccounts(); }
    if (t.dataset.push) return confirmPush(t.dataset.push);
    if (t.dataset.show) return showCommit(t.dataset.show);
  } catch (err) { toast(err.message, true); }
});

function confirmPush(hash) {
  const a = currentAccount();
  const c = a.pending.find((x) => x.hash === hash);
  const total = c.orders.reduce((s, o) => s + o.order.qty * (o.order.limitPrice || o.priceAtStage || 0), 0);
  const warnings = c.orders.flatMap((o) => o.warnings);
  modal(`PUSH ${hash} → ${a.label.toUpperCase()}`, `
    <p><b>${esc(c.message)}</b> <span class="muted">by ${esc(c.author)}</span></p>
    <ul class="list">${c.orders.map((s) => `<li>${orderText(s.order)} <span class="muted">~${money(s.order.qty * (s.order.limitPrice || s.priceAtStage || 0))}</span>${s.rationale ? `<div class="rationale">${esc(s.rationale)}</div>` : ""}</li>`).join("")}</ul>
    <p>Approx. notional <b>${money(total)}</b>. Guards run again at push; if any order fails, nothing in this commit is sent.</p>
    ${warnings.length ? `<p class="down">⚠ Staging flagged: ${warnings.map(esc).join("; ")}</p>` : ""}
    <p class="muted small">${a.type === "paper" ? "Simulated paper account: no real money moves." : "Alpaca PAPER account."}</p>`, [
    { label: "CANCEL", className: "ghost", onClick: async () => {} },
    { label: "PUSH TO BROKER", className: "btn push", onClick: async () => {
      const r = await api(`/api/accounts/${encodeURIComponent(state.account)}/push`, { method: "POST", body: { hash, confirm: true } });
      const st = r.commits[0].status;
      toast(st === "pushed" ? `Pushed ${hash}` : `${hash}: ${st}`, st !== "pushed");
      await loadAccounts();
    } },
  ]);
}
async function showCommit(hash) {
  const c = await api(`/api/accounts/${encodeURIComponent(state.account)}/commits/${hash}`);
  const lines = [
    `commit ${c.hash}${c.parent ? "  (parent " + c.parent + ")" : ""}`,
    `Author:   ${c.author}`,
    `Approver: ${c.approver || "-"}`,
    `Created:  ${new Date(c.createdAt * 1000).toLocaleString()}`,
    c.pushedAt ? `Pushed:   ${new Date(c.pushedAt * 1000).toLocaleString()}` : "",
    `Status:   ${c.status}`, "", `    ${c.message}`, "",
    ...c.orders.map((s) => `  ${s.order.side.toUpperCase()} ${s.order.qty} ${s.order.symbol} ${s.order.type}${s.order.limitPrice ? " " + s.order.limitPrice : ""}  (staged by ${s.source} @ ~${num(s.priceAtStage)})${s.rationale ? "\n      " + s.rationale : ""}`),
  ];
  if (c.guardResults) lines.push("", "Guards:", ...c.guardResults.map((g) => `  ${g.order.symbol}: ${g.violations.length ? g.violations.join("; ") : "pass"}`));
  if (c.results) lines.push("", "Broker:", ...c.results.map((r) => `  ${r.symbol} ${r.status} ${r.filledQty}@${r.avgFillPrice ? num(r.avgFillPrice) : "-"}  ${r.message}`));
  if (c.snapshot) lines.push("", `Snapshot: equity ${money(c.snapshot.equity)} · cash ${money(c.snapshot.cash)}`);
  modal(`SHOW ${hash}`, `<pre>${esc(lines.filter((l) => l !== null).join("\n"))}</pre>`);
}

async function loadRisk() {
  try {
    const r = await api(`/api/accounts/${encodeURIComponent(state.account)}/risk`);
    $("#risk").innerHTML = r.note ? `<span class="k">Note</span><span class="v muted">${esc(r.note)}</span>` : `
      <span class="k">Gross exposure</span><span class="v">${money(r.grossExposure)}</span>
      <span class="k">Annualized vol</span><span class="v">${pct(r.annualizedVolatility * 100, 1).replace("+", "")}</span>
      <span class="k">Sharpe (rf 4%)</span><span class="v">${r.sharpe === null ? "—" : num(r.sharpe)}</span>
      <span class="k">1-day VaR 95%</span><span class="v down">${money(r.var95OneDayValue)} (${num(r.var95OneDayPct * 100, 2)}%)</span>
      <span class="k">Max drawdown</span><span class="v down">${num(r.maxDrawdown * 100, 1)}%</span>
      <span class="k">Period return</span><span class="v ${cls(r.periodReturn)}">${pct(r.periodReturn * 100, 1)}</span>
      <span class="k">Observations</span><span class="v muted">${r.observations} days</span>`;
  } catch (e) { $("#risk").innerHTML = `<span class="k">Error</span><span class="v down">${esc(e.message)}</span>`; }
}
$("#risk-refresh").addEventListener("click", loadRisk);


// ---------------------------------------------------------------- backtest
let strategyCatalog = [];
let btChart = null;
async function loadStrategies() {
  try {
    strategyCatalog = await api("/api/strategies");
    $("#bt-strategy").innerHTML = strategyCatalog.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
    renderStrategyParams();
  } catch (e) { toast("Strategies: " + e.message, true); }
}
function renderStrategyParams() {
  const spec = strategyCatalog.find((s) => s.id === $("#bt-strategy").value);
  if (!spec) return;
  $("#bt-desc").textContent = spec.description;
  $("#bt-params").innerHTML = Object.entries(spec.params).map(([k, r]) =>
    `<label>${esc(k.replace(/_/g, " "))}<input data-param="${esc(k)}" type="number" step="any" min="${r.min}" max="${r.max}" value="${r.default}"></label>`).join("");
}
$("#bt-strategy").addEventListener("change", renderStrategyParams);

function btParams() {
  return Object.fromEntries($$("#bt-params [data-param]").map((i) => [i.dataset.param, Number(i.value)]));
}
const fmtPct = (x) => (x === null || x === undefined ? "—" : (x * 100).toFixed(1) + "%");
const fmtNum = (x, d = 2) => (x === null || x === undefined ? "—" : Number(x).toFixed(d));

$("#bt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    symbol: f.get("symbol"), strategy: f.get("strategy"), params: btParams(), range: f.get("range"),
    initialCash: Number(f.get("initialCash")), allocation: Number(f.get("allocation")) || null,
    slippageBps: Number(f.get("slippageBps")), commission: Number(f.get("commission")),
  };
  const out = $("#bt-out");
  $("#bt-run").disabled = true;
  out.innerHTML = '<p class="muted typing">Running backtest </p>';
  try {
    const r = await api("/api/backtest", { method: "POST", body });
    const m = r.metrics, b = r.benchmark;
    const beat = m.returnOnAllocation > b.returnOnAllocation;
    const smallSample = m.trades < 10;
    const row = (label, a, bb, good) => `<tr><td>${label}</td><td class="${good === undefined ? "" : good ? "up" : "down"}">${a}</td><td>${bb}</td></tr>`;
    out.innerHTML = `
      <div class="verdict ${beat ? "good" : "bad"}">
        <b>${esc(r.symbol)} · ${esc(r.strategy)}</b> ${esc(JSON.stringify(r.params))} · ${esc(r.period.start)} → ${esc(r.period.end)} (${r.period.bars} daily bars, ${esc(r.dataSource)} data)<br>
        Return on the $${num(r.assumptions.allocation || r.assumptions.initialCash, 0)} allocated: strategy ${fmtPct(m.returnOnAllocation)} vs buy-and-hold ${fmtPct(b.returnOnAllocation)}. It <b>${beat ? "beat" : "did not beat"}</b> the benchmark on return; max drawdown ${fmtPct(m.maxDrawdown)} vs ${fmtPct(b.maxDrawdown)}.
        ${m.trades === 0 && !m.openTrade ? `<br><span class="down">No position was ever opened. Either the signal never fired, or one unit costs more than the allocation (raise "Allocation per trade").</span>` : smallSample ? `<br><span class="down">Only ${m.trades} closed trades: too few to draw conclusions.</span>` : ""}
        Current signal: <b>${r.currentSignal ? "LONG" : "FLAT"}</b>.
      </div>
      <table class="bt-metrics"><thead><tr><th></th><th>STRATEGY</th><th>BUY &amp; HOLD</th></tr></thead><tbody>
        ${row("Return on allocated capital", fmtPct(m.returnOnAllocation), fmtPct(b.returnOnAllocation), beat)}
        ${row("Total return (whole account)", fmtPct(m.totalReturn), fmtPct(b.totalReturn), beat)}
        ${row("CAGR", fmtPct(m.cagr), fmtPct(b.cagr))}
        ${row("Sharpe (rf 4%)", fmtNum(m.sharpe), fmtNum(b.sharpe), (m.sharpe ?? -9) > (b.sharpe ?? -9))}
        ${row("Max drawdown", fmtPct(m.maxDrawdown), fmtPct(b.maxDrawdown), m.maxDrawdown < b.maxDrawdown)}
        ${row("Volatility", fmtPct(m.annualizedVolatility), fmtPct(b.annualizedVolatility))}
        ${row("Final equity", money(m.finalEquity), money(b.finalEquity))}
        ${row("Closed trades", m.trades, "—")}
        ${row("Win rate", fmtPct(m.winRate), "—")}
        ${row("Avg trade", m.avgTradeReturnPct === null ? "—" : pct(m.avgTradeReturnPct, 2), "—")}
        ${row("Profit factor", fmtNum(m.profitFactor), "—")}
        ${row("Time in market", fmtPct(m.exposure), "100%")}
      </tbody></table>
      <div id="bt-chart" class="bt-chart"></div>
      <h4>YEAR BY YEAR <span class="muted">· on the starting capital</span></h4>
      <table class="bt-metrics"><thead><tr><th>YEAR</th><th>STRATEGY</th><th>P&amp;L</th><th>BUY &amp; HOLD</th><th>P&amp;L</th></tr></thead><tbody>
        ${r.yearly.slice().reverse().map((y) => `<tr><td>${y.year}${y.partial ? ' <span class="muted">(partial)</span>' : ""}</td><td class="${cls(y.strategy)}">${pct(y.strategy * 100, 1)}</td><td class="${cls(y.strategyPnl)}">${money(y.strategyPnl)}</td><td class="${cls(y.benchmark)}">${pct(y.benchmark * 100, 1)}</td><td class="${cls(y.benchmarkPnl)}">${money(y.benchmarkPnl)}</td></tr>`).join("")}
      </tbody></table>
      <div class="row gap"><button class="btn push" id="bt-paper">PAPER TRADE THIS STRATEGY</button>
        <span class="muted small">Creates an automated bot with these settings on the paper account.</span></div>
      <h4>TRADES</h4>
      <table class="table small"><thead><tr><th>ENTRY</th><th>EXIT</th><th>QTY</th><th>IN</th><th>OUT</th><th>P&amp;L</th></tr></thead><tbody>
        ${r.trades.slice().reverse().map((t) => `<tr><td>${esc(t.entryDate)}</td><td>${t.open ? '<span class="muted">open</span>' : esc(t.exitDate)}</td><td>${fmtNum(t.qty, t.qty % 1 ? 4 : 0)}</td><td>${num(t.entryPrice)}</td><td>${num(t.exitPrice)}</td><td class="${cls(t.pnl)}">${money(t.pnl)} (${pct(t.returnPct, 1)})</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No trades</td></tr>'}
      </tbody></table>
      <p class="muted small">Assumptions: ${esc(r.assumptions.execution)}; ${r.assumptions.slippageBps} bps slippage; $${r.assumptions.commission}/order; long-only. Past performance on delayed free data does not predict future results.</p>`;
    drawBacktestChart(r.curve);
    $("#bt-paper").onclick = () => createBotFromBacktest(body);
  } catch (err) { out.innerHTML = `<p class="down">${esc(err.message)}</p>`; }
  finally { $("#bt-run").disabled = false; }
});

function drawBacktestChart(curve) {
  const LW = window.LightweightCharts;
  if (!LW) return;
  btChart = LW.createChart($("#bt-chart"), {
    layout: { background: { color: "transparent" }, textColor: "#7a8696", fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
    grid: { vertLines: { color: "#141a22" }, horzLines: { color: "#141a22" } },
    rightPriceScale: { borderColor: "#1e2631" }, timeScale: { borderColor: "#1e2631" }, autoSize: true,
  });
  btChart.addLineSeries({ color: "#5d6878", lineWidth: 1, title: "buy & hold" }).setData(curve.map((p) => ({ time: p.t, value: p.benchmark })));
  btChart.addLineSeries({ color: "#ffb000", lineWidth: 2, title: "strategy" }).setData(curve.map((p) => ({ time: p.t, value: p.equity })));
  btChart.timeScale().fitContent();
}

function createBotFromBacktest(body) {
  const id = (body.symbol + "-" + body.strategy).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
  const acct = state.account;
  modal("PAPER TRADE THIS STRATEGY", `
    <p>Create bot <b>${esc(id)}</b>: ${esc(body.strategy)} ${esc(JSON.stringify(body.params))} on <b>${esc(body.symbol.toUpperCase())}</b>, $${num(body.allocation || 0, 0)} per position, account <b>${esc(acct)}</b>.</p>
    <p>Every 15 minutes while the market is open, the bot checks the signal on the last completed daily bar, then stages, commits <b>and pushes</b> its own orders. Guards still apply, and it halts if the account loses more than 2% in a day.</p>
    <label class="row gap"><input type="checkbox" id="bot-enable" checked> Start trading now</label>`, [
    { label: "CANCEL", className: "ghost", onClick: async () => {} },
    { label: "CREATE BOT", className: "btn push", onClick: async () => {
      await api(`/api/automation/bots/${id}`, { method: "PUT", body: {
        id, type: "strategy", enabled: $("#bot-enable").checked, account: acct, strategy: body.strategy, params: body.params,
        symbols: [body.symbol.toUpperCase()], allocationPerSymbol: body.allocation || 5000, intervalMinutes: 15, maxDailyLossPct: 2, respectMarketHours: true,
      } });
      toast(`Bot ${id} created`);
      selectTab("right", "auto");
      loadAutomation(); loadStatus();
    } },
  ]);
}


// ---------------------------------------------------------------- gold scalp (intraday) backtest
let scalpCatalog = { strategies: [], history: {} };
async function loadScalp() {
  try {
    scalpCatalog = await api("/api/intraday/strategies");
    $("#sc-strategy").innerHTML = scalpCatalog.strategies.map((s) => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
    renderScalpParams();
    renderScalpAvail();
  } catch (e) { $("#sc-avail").textContent = "History: " + e.message; }
}
function renderScalpAvail() {
  const sym = $("#sc-symbol").value;
  const h = (scalpCatalog.history || {})[sym] || {};
  const inst = (scalpCatalog.instruments || {})[sym] || {};
  const line = (tf, label) => h[tf] && h[tf].files ? `${label}: ${h[tf].files} files, ${esc(h[tf].from)} → ${esc(h[tf].to)}` : `${label}: none downloaded yet`;
  $("#sc-avail").innerHTML = `${esc(sym)} history (Dukascopy). ${line("60m", "Hourly")} · ${line("minute", "Minute")}. Default spread ${inst.spread ?? "?"}, minimum size ${inst.minQty ?? "?"} (${esc(inst.unit || "")}), live paper symbol ${esc(inst.live || "?")}. Fetch more with <code>python scripts/fetch_dukascopy.py</code>.`;
}
function renderScalpParams() {
  const spec = scalpCatalog.strategies.find((s) => s.id === $("#sc-strategy").value);
  if (!spec) return;
  $("#sc-params").innerHTML = Object.entries(spec.params).map(([k, v]) =>
    `<label>${esc(k.replace(/_/g, " "))}<input data-sparam="${esc(k)}" type="number" step="any" value="${v}"></label>`).join("");
}
$("#sc-strategy").addEventListener("change", renderScalpParams);
$("#sc-symbol").addEventListener("change", renderScalpAvail);

$("#sc-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    symbol: f.get("symbol"), strategy: f.get("strategy"), timeframe: f.get("timeframe"), years: Number(f.get("years")),
    capital: Number(f.get("capital")), riskPct: Number(f.get("riskPct")), maxRiskPct: Number(f.get("maxRiskPct")),
    spread: f.get("spread") === "" ? null : Number(f.get("spread")), leverage: Number(f.get("leverage")),
    accountType: f.get("accountType"),
    params: Object.fromEntries($$("#sc-params [data-sparam]").map((i) => [i.dataset.sparam, Number(i.value)])),
  };
  const out = $("#sc-out");
  $("#sc-run").disabled = true;
  out.innerHTML = '<p class="muted typing">Running backtest </p>';
  try {
    const r = await api("/api/intraday/backtest", { method: "POST", body });
    const m = r.metrics;
    const good = m.netProfit > 0 && (m.profitFactor ?? 0) > 1;
    const row = (k, v, c = "") => `<tr><td>${k}</td><td class="${c}">${v}</td></tr>`;
    out.innerHTML = `
      <div class="verdict ${good ? "good" : "bad"}">
        <b>${esc(r.symbol)} ${esc(r.timeframe)} · ${esc(r.label)}</b> · ${esc(r.period.start)} → ${esc(r.period.end)} (${r.period.years} years)<br>
        ${money(r.assumptions.capital)} became <b>${money(m.finalEquity)}</b> (${pct(m.totalReturn * 100, 1)}) over ${m.trades} trades.
        Win rate <b>${fmtPct(m.winRate)}</b>, profit factor <b>${fmtNum(m.profitFactor)}</b>, worst drawdown <b>${fmtPct(m.maxDrawdown)}</b>.
        ${m.winRate >= 0.85 && !good ? '<br><span class="down">High win rate but it loses money: the few losses are much bigger than the many wins.</span>' : ""}
        ${m.blown ? '<br><span class="down">The account ran out of margin and stopped trading.</span>' : ""}
        ${m.skippedMinSize ? `<br><span class="down">${m.skippedMinSize} signals skipped: the broker's minimum size would have risked more than ${r.assumptions.riskPct > 0 ? body.maxRiskPct : "?"}% of the account.</span>` : ""}
      </div>
      <table class="bt-metrics"><tbody>
        ${row("Net profit", money(m.netProfit), cls(m.netProfit))}
        ${row("Yearly return (CAGR)", m.cagr === null ? "—" : pct(m.cagr * 100, 1), cls(m.cagr))}
        ${row("Trades / per week", `${m.trades} / ${fmtNum(m.tradesPerWeek, 1)}`)}
        ${row("Win rate", fmtPct(m.winRate))}
        ${row("Average win / loss", `${money(m.avgWin)} / ${money(m.avgLoss)}`)}
        ${row("Expectancy per trade", money(m.expectancy), cls(m.expectancy))}
        ${row("Average result in R (risk units)", fmtNum(m.avgR), cls(m.avgR))}
        ${row("Worst losing streak", m.worstLosingStreak)}
        ${row("Typical month (median) / average", `${m.medianMonth === null ? "—" : pct(m.medianMonth * 100, 1)} / ${m.avgMonth === null ? "—" : pct(m.avgMonth * 100, 1)}`, cls(m.medianMonth))}
        ${row("Best / worst month", `${m.bestMonth === null ? "—" : pct(m.bestMonth * 100, 1)} / ${m.worstMonth === null ? "—" : pct(m.worstMonth * 100, 1)}`)}
        ${row("Profitable months", fmtPct(m.positiveMonths))}
        ${row("Months of +30% or more", m.monthsAbove30pct)}
        ${row("Costs modelled", `spread $${r.assumptions.spread}/oz, commission $${r.assumptions.commissionPerUnitSide}/oz/side, 1:${r.assumptions.leverage}, min ${r.assumptions.minQty} oz`)}
      </tbody></table>
      <div id="sc-chart" class="bt-chart"></div>
      <div class="row gap"><button class="btn push" id="sc-bot">RUN AS PAPER BOT</button>
        <span class="muted small">Trades ${esc(r.live)} live on a paper CFD account with these settings.</span></div>
      <h4>WHAT HIGHER RISK DOES <span class="muted">· 2,000 simulated years from this strategy's own trades</span></h4>
      <table class="bt-metrics"><thead><tr><th>RISK / TRADE</th><th>TYPICAL MONTH</th><th>BAD CASE (1 IN 10)</th><th>HALVES IN A YEAR</th><th>WIPED OUT</th></tr></thead><tbody>
        ${(r.riskSimulation || []).map((x) => `<tr><td>${x.riskPct}%</td><td class="${cls(x.medianMonthly)}">${pct(x.medianMonthly * 100, 1)}</td><td class="${cls(x.p10Monthly)}">${pct(x.p10Monthly * 100, 1)}</td><td class="${x.probHalved > 0.2 ? "down" : ""}">${fmtPct(x.probHalved)}</td><td class="${x.probRuin > 0.01 ? "down" : ""}">${fmtPct(x.probRuin)}</td></tr>`).join("")}
      </tbody></table>
      <p class="muted small">Ignores minimum-size limits. "Halves" = the account falls 50% from a peak at some point in the year.</p>
      <h4>YEAR BY YEAR</h4>
      <table class="bt-metrics"><thead><tr><th>YEAR</th><th>TRADES</th><th>WIN RATE</th><th>PROFIT</th></tr></thead><tbody>
        ${r.yearly.slice().reverse().map((y) => `<tr><td>${y.year}</td><td>${y.trades}</td><td>${fmtPct(y.winRate)}</td><td class="${cls(y.pnl)}">${money(y.pnl)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No trades</td></tr>'}
      </tbody></table>
      <h4>LAST TRADES</h4>
      <table class="table small"><thead><tr><th>OPENED (UTC)</th><th>SIDE</th><th>OZ</th><th>ENTRY</th><th>SL / TP</th><th>EXIT</th><th>WHY</th><th>P&amp;L</th></tr></thead><tbody>
        ${r.trades.slice(-40).reverse().map((t) => `<tr><td>${new Date(t.entryTime * 1000).toISOString().slice(0, 16).replace("T", " ")}</td><td class="${t.side === "buy" ? "up" : "down"}">${t.side}</td><td>${t.qty}</td><td>${num(t.entry)}</td><td>${num(t.sl)} / ${num(t.tp)}</td><td>${num(t.exit)}</td><td>${esc(t.reason)}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td></tr>`).join("")}
      </tbody></table>`;
    const LW = window.LightweightCharts;
    if (LW && r.curve.length) {
      const ch = LW.createChart($("#sc-chart"), {
        layout: { background: { color: "transparent" }, textColor: "#7a8696", fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
        grid: { vertLines: { color: "#141a22" }, horzLines: { color: "#141a22" } }, autoSize: true,
        rightPriceScale: { borderColor: "#1e2631" }, timeScale: { borderColor: "#1e2631" },
      });
      ch.addLineSeries({ color: "#ffb000", lineWidth: 2, title: "equity" }).setData(r.curve.map(([t, v]) => ({ time: t, value: v })));
      ch.timeScale().fitContent();
    }
    $("#sc-bot").onclick = () => createScalpBot(body, r.live);
  } catch (err) { out.innerHTML = `<p class="down">${esc(err.message)}</p>`; }
  finally { $("#sc-run").disabled = false; }
});

function createScalpBot(body, live) {
  const account = { XAUUSD: "paper-gold", NAS100: "paper-nas100", US30: "paper-us30" }[body.symbol];
  const id = (body.symbol.toLowerCase() + "-" + body.strategy).replace(/_/g, "-").slice(0, 40);
  modal("RUN STRATEGY AS PAPER BOT", `
    <p>Bot <b>${esc(id)}</b> will trade <b>${esc(live)}</b> (futures, Yahoo, delayed) on the <b>${esc(account)}</b> CFD account with
    ${esc(body.strategy)} on ${esc(body.timeframe)} bars, risking ${body.riskPct}% per trade (max ${body.maxRiskPct}% with the 1 oz minimum),
    stop-loss and take-profit on every order.</p>
    <p class="muted small">It checks every minute, enters on the last completed bar, exits on stop-loss, take-profit, time limit or 21:00 UTC.
    Live stops are checked once a minute against delayed quotes, so live fills will differ from the backtest.</p>
    <label class="row gap"><input type="checkbox" id="sc-enable" checked> Start trading now</label>`, [
    { label: "CANCEL", className: "ghost", onClick: async () => {} },
    { label: "CREATE BOT", className: "btn push", onClick: async () => {
      await api(`/api/automation/bots/${id}`, { method: "PUT", body: {
        id, type: "intraday", enabled: $("#sc-enable").checked, account, strategy: body.strategy, params: body.params,
        symbols: [live], timeframe: body.timeframe, riskPct: body.riskPct, maxRiskPct: body.maxRiskPct,
        allocationPerSymbol: 1000, intervalMinutes: 1, maxDailyLossPct: 5, respectMarketHours: true,
      } });
      toast(`Bot ${id} created`);
      selectTab("right", "auto"); loadAutomation(); loadStatus();
    } },
  ]);
}


// ---------------------------------------------------------------- goal planner
function updateGoalYear() {
  const m = Number($("#goal-pct").value) / 100;
  $("#goal-year").textContent = m > 0 ? `${Math.round(((1 + m) ** 12 - 1) * 100).toLocaleString()}% a year (compounded)` : "—";
}
$("#goal-pct").addEventListener("input", updateGoalYear);
updateGoalYear();

function scalpBody() {
  const f = new FormData($("#sc-form"));
  return {
    symbol: f.get("symbol"), strategy: f.get("strategy"), timeframe: f.get("timeframe"), years: Number(f.get("years")),
    capital: Number(f.get("capital")), riskPct: Number(f.get("riskPct")), maxRiskPct: Number(f.get("maxRiskPct")),
    spread: f.get("spread") === "" ? null : Number(f.get("spread")), leverage: Number(f.get("leverage")),
    accountType: f.get("accountType"), targetMonthlyPct: Number($("#goal-pct").value),
    params: Object.fromEntries($$("#sc-params [data-sparam]").map((i) => [i.dataset.sparam, Number(i.value)])),
  };
}
function goalCell(x) { return x ? `${x.riskPct}%` : "not reachable"; }
function riskRows(table) {
  return table.map((x) => `<tr><td>${x.riskPct}%</td><td class="${cls(x.medianMonthly)}">${pct(x.medianMonthly * 100, 1)}</td><td class="${cls(x.p10Monthly)}">${pct(x.p10Monthly * 100, 1)}</td><td class="${x.probHalved > 0.2 ? "down" : ""}">${fmtPct(x.probHalved)}</td><td class="${x.probRuin > 0.01 ? "down" : ""}">${fmtPct(x.probRuin)}</td></tr>`).join("");
}
async function runGoal(scan) {
  const out = $("#goal-out");
  const body = scalpBody();
  $("#goal-one").disabled = $("#goal-scan").disabled = true;
  out.innerHTML = `<p class="muted typing">${scan ? "Scanning every strategy (this can take a minute)" : "Checking"} </p>`;
  try {
    if (!scan) {
      const r = await api("/api/intraday/plan", { method: "POST", body });
      const ok = r.needed && r.needed.probHalved < 0.25 && r.needed.probRuin < 0.02;
      out.innerHTML = `
        <div class="verdict ${ok ? "good" : "bad"}"><b>${esc(r.label)}</b> · ${esc(r.symbol)} ${esc(r.timeframe)} · target ${body.targetMonthlyPct}%/month (${Math.round(r.targetYearly * 100).toLocaleString()}%/year)<br>${esc(r.verdict)}</div>
        <p class="small muted">${r.trades} trades (${fmtNum(r.tradesPerMonth, 1)}/month), win rate ${fmtPct(r.winRate)}, profit factor ${fmtNum(r.profitFactor)}, average ${fmtNum(r.avgR)} R per trade.</p>
        <table class="bt-metrics"><thead><tr><th>RISK / TRADE</th><th>TYPICAL MONTH</th><th>BAD CASE (1 IN 10)</th><th>HALVES IN A YEAR</th><th>WIPED OUT</th></tr></thead><tbody>${riskRows(r.table)}</tbody></table>`;
    } else {
      const r = await api("/api/intraday/scan", { method: "POST", body });
      out.innerHTML = `
        <p class="small">Target <b>${body.targetMonthlyPct}%/month</b> on ${esc(r.symbol)} ${esc(r.timeframe)}. Safest first. "Needs" = smallest risk per trade whose typical month reaches the target.</p>
        <div class="tablebox"><table class="table small"><thead><tr><th>STRATEGY</th><th>TRADES/MO</th><th>WIN</th><th>PF</th><th>MONTH @1%</th><th>NEEDS</th><th>HALVES</th><th>WIPED</th><th>BEST MONTH POSSIBLE</th></tr></thead><tbody>
        ${r.strategies.map((x) => x.error ? `<tr><td>${esc(x.label)}</td><td colspan="8" class="muted">${esc(x.error)}</td></tr>` : `<tr title="${esc(x.verdict)}">
          <td>${esc(x.label)}</td><td>${fmtNum(x.tradesPerMonth, 1)}</td><td>${fmtPct(x.winRate)}</td><td class="${(x.profitFactor || 0) > 1 ? "up" : "down"}">${fmtNum(x.profitFactor)}</td>
          <td class="${cls(x.medianMonthAt1pct)}">${x.medianMonthAt1pct === null ? "—" : pct(x.medianMonthAt1pct * 100, 1)}</td>
          <td class="${x.needed ? "" : "down"}">${goalCell(x.needed)}</td>
          <td class="${x.needed && x.needed.probHalved > 0.2 ? "down" : ""}">${x.needed ? fmtPct(x.needed.probHalved) : "—"}</td>
          <td class="${x.needed && x.needed.probRuin > 0.01 ? "down" : ""}">${x.needed ? fmtPct(x.needed.probRuin) : "—"}</td>
          <td>${x.best ? `${pct(x.best.medianMonthly * 100, 1)} @ ${x.best.riskPct}% (halves ${fmtPct(x.best.probHalved)})` : "—"}</td></tr>`).join("")}
        </tbody></table></div>
        <p class="muted small">Hover a row for its verdict. Simulations replay each strategy's own backtest trades (costs included) and ignore minimum-size limits.</p>`;
    }
  } catch (e) { out.innerHTML = `<p class="down">${esc(e.message)}</p>`; }
  finally { $("#goal-one").disabled = $("#goal-scan").disabled = false; }
}
$("#goal-one").addEventListener("click", () => runGoal(false));
$("#goal-scan").addEventListener("click", () => runGoal(true));

// ---------------------------------------------------------------- automation
let automation = { killSwitch: false, bots: [] };
async function loadAutomation() {
  try {
    automation = await api("/api/automation");
    $(".kill-box").classList.toggle("killed", automation.killSwitch);
    $("#kill").textContent = automation.killSwitch ? "RESUME AUTOMATION" : "KILL SWITCH";
    $("#kill").className = automation.killSwitch ? "btn push" : "btn danger";
    const active = automation.bots.filter((b) => b.enabled && !b.state.halted).length;
    const badge = $("#auto-badge");
    badge.hidden = !active; badge.textContent = active;
    $("#bots").innerHTML = automation.bots.length ? automation.bots.map(renderBot).join("") : '<li class="empty">No bots</li>';
  } catch (e) { toast("Automation: " + e.message, true); }
}
function renderBot(b) {
  const st = b.state || {}, p = b.performance || {};
  const status = st.halted ? '<span class="tag halt">HALTED</span>' : b.enabled ? '<span class="tag on">ON</span>' : '<span class="tag">OFF</span>';
  const what = b.type === "ai" ? '<span class="tag ai">AI AUTOPILOT</span>'
    : b.type === "intraday" ? `<span class="tag">SCALP ${esc(b.timeframe)}</span> ${esc(b.strategy)} · risk ${b.riskPct}%/trade (max ${b.maxRiskPct}%) · SL/TP on every order`
    : `${esc(b.strategy)} ${esc(JSON.stringify(b.params))}`;
  const holdings = Object.entries(p.holdings || {}).map(([s, h]) => `${esc(s)} ${fmtNum(h.qty, h.qty % 1 ? 4 : 0)} @ ${num(h.avgCost)}`).join(", ") || "flat";
  return `<li>
    <div class="bot-head"><span><b>${esc(b.id)}</b> ${status}${b.running ? ' <span class="tag">RUNNING</span>' : ""}</span><span class="muted small">${esc(b.account)}</span></div>
    <div class="bot-meta">${what} · ${esc(b.symbols.join(", "))} · $${num(b.allocationPerSymbol, 0)}/symbol · every ${b.intervalMinutes}m · loss limit ${b.maxDailyLossPct}%/day${b.respectMarketHours ? " · market hours" : ""}</div>
    <div class="bot-state">P&amp;L <span class="${cls(p.totalPnl)}">${money(p.totalPnl ?? 0)}</span> <span class="muted">(realized ${money(p.realizedPnl ?? 0)}, open ${money(p.unrealizedPnl ?? 0)}) · holding ${holdings}</span></div>
    <div class="bot-state muted">${st.lastRun ? "last run " + ago(st.lastRun) + ": " : "never run"}${esc(st.lastAction || "")}</div>
    ${st.halted ? `<div class="warn-line">⚠ ${esc(st.haltReason)}</div>` : ""}
    <div class="bot-actions">
      <button class="btn ${b.enabled ? "danger" : "push"}" data-bot-toggle="${esc(b.id)}">${b.enabled ? "DISABLE" : "ENABLE"}</button>
      <button class="ghost" data-bot-run="${esc(b.id)}">RUN NOW</button>
      ${st.halted ? `<button class="ghost" data-bot-resume="${esc(b.id)}">RESUME</button>` : ""}
      <button class="ghost" data-bot-edit="${esc(b.id)}">EDIT</button>
      <button class="ghost" data-bot-delete="${esc(b.id)}">DELETE</button>
    </div></li>`;
}
function botConfig(b) {
  const { state: _s, performance: _p, running: _r, ...cfg } = b;
  return cfg;
}
async function saveBot(cfg) {
  await api(`/api/automation/bots/${encodeURIComponent(cfg.id)}`, { method: "PUT", body: cfg });
  await loadAutomation(); loadStatus();
}
$("#kill").addEventListener("click", async () => {
  const on = !automation.killSwitch;
  try {
    await api("/api/automation/kill", { method: "POST", body: { on } });
    toast(on ? "Kill switch ON: all bots stopped" : "Automation resumed", on);
    loadAutomation(); loadStatus();
  } catch (e) { toast(e.message, true); }
});
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-bot-toggle],[data-bot-run],[data-bot-resume],[data-bot-edit],[data-bot-delete]");
  if (!t) return;
  const id = t.dataset.botToggle || t.dataset.botRun || t.dataset.botResume || t.dataset.botEdit || t.dataset.botDelete;
  const bot = automation.bots.find((b) => b.id === id);
  try {
    if (t.dataset.botToggle) {
      const cfg = botConfig(bot);
      if (cfg.enabled) { cfg.enabled = false; await saveBot(cfg); return toast(`${id} disabled`); }
      return modal(`ENABLE ${id.toUpperCase()}`, `<p>This bot will <b>place and push orders automatically</b> on <b>${esc(cfg.account)}</b> (paper) every ${cfg.intervalMinutes} minutes${cfg.type === "ai" ? ", using Claude to decide (each run costs API tokens)" : ""}.</p><p class="muted small">Guards, the ${cfg.maxDailyLossPct}% daily loss limit and the kill switch still apply.</p>`, [
        { label: "CANCEL", className: "ghost", onClick: async () => {} },
        { label: "ENABLE", className: "btn push", onClick: async () => { cfg.enabled = true; await saveBot(cfg); toast(`${id} enabled`); } },
      ]);
    }
    if (t.dataset.botRun) {
      t.disabled = true;
      const r = await api(`/api/automation/bots/${encodeURIComponent(id)}/run`, { method: "POST" });
      const summary = (r.actions || []).map((a) => `${a.symbol || a.hash} ${a.action}${a.reason ? " (" + a.reason + ")" : ""}`).join("; ") || "no action";
      toast(r.error ? `${id}: ${r.error}` : r.halted ? `${id} halted: ${r.reason}` : `${id}: ${summary}`, !!(r.error || r.halted));
      loadAutomation(); loadAccounts();
    }
    if (t.dataset.botResume) { await api(`/api/automation/bots/${encodeURIComponent(id)}/resume`, { method: "POST" }); loadAutomation(); }
    if (t.dataset.botDelete) {
      return modal(`DELETE ${id.toUpperCase()}`, "<p>Delete this bot's configuration? Its commit history and any open positions stay in the account.</p>", [
        { label: "CANCEL", className: "ghost", onClick: async () => {} },
        { label: "DELETE", className: "btn danger", onClick: async () => { await api(`/api/automation/bots/${encodeURIComponent(id)}`, { method: "DELETE" }); loadAutomation(); loadStatus(); } },
      ]);
    }
    if (t.dataset.botEdit) {
      return modal(`EDIT ${id.toUpperCase()}`, `<textarea id="bot-json" rows="16" style="width:100%;font-family:var(--mono)">${esc(JSON.stringify(botConfig(bot), null, 2))}</textarea>`, [
        { label: "SAVE", onClick: async () => { await saveBot(JSON.parse($("#bot-json").value)); toast(`${id} saved`); } },
      ]);
    }
  } catch (err) { toast(err.message, true); } finally { t.disabled = false; }
});

// ---------------------------------------------------------------- heartbeat
async function saveHeartbeat() {
  try {
    await api("/api/heartbeat", { method: "PUT", body: { enabled: $("#hb-enabled").checked, intervalMinutes: Number($("#hb-interval").value) } });
    loadStatus();
  } catch (e) { toast(e.message, true); }
}
$("#hb-enabled").addEventListener("change", saveHeartbeat);
$("#hb-interval").addEventListener("change", saveHeartbeat);
$("#hb-run").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/heartbeat/run", { method: "POST" });
    toast((r.notify ? "⚑ " : "✓ ") + r.headline, false);
  } catch (err) { toast(err.message, true); } finally { e.target.disabled = false; }
});

// ---------------------------------------------------------------- events (SSE)
function eventSummary(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "trade.stage": return `${p.account}: ${p.order.side} ${p.order.qty} ${p.order.symbol} (${p.source})`;
    case "trade.commit": return `${p.account}: ${p.hash} "${p.message}" by ${p.author}`;
    case "trade.push": return `${p.account}: ${p.hash} ${p.status} · equity ${money(p.equity)}`;
    case "trade.rejected": return `${p.account}: ${p.hash} blocked by guards: ${(p.violations || []).join("; ")}`;
    case "trade.fill": return `${p.account}: ` + (p.fills || []).map((f) => f.reason ? `${f.reason} ${f.symbol} @ ${num(f.price)} (${num(f.realized)})` : `filled ${f.symbol}`).join("; ");
    case "agent.user": return `you: ${p.text}`;
    case "agent.reply": return `agent: ${(p.text || "").slice(0, 160)}`;
    case "agent.tool": return `tool ${p.tool} ${JSON.stringify(p.input).slice(0, 120)}`;
    case "agent.panel": return `panel ${p.symbol}: ${Object.entries(p.stances || {}).map(([k, v]) => k + "=" + v).join(" ")}`;
    case "bot.run": return `${p.bot}: ${p.action}`;
    case "bot.trade": return `${p.bot}: ${p.side} ${p.qty} ${p.symbol} pushed (${p.hash})`;
    case "bot.halted": return `${p.bot} HALTED: ${p.reason}`;
    case "bot.exit": return `${p.bot}: ${p.reason} ${p.side} ${p.qty} ${p.symbol} @ ${num(p.price)} (${p.realized >= 0 ? "+" : ""}${num(p.realized)})`;
    case "bot.error": return `${p.bot}: ${p.action}`;
    case "backtest.run": return `${p.symbol} ${p.strategy}: ${(p.totalReturn * 100).toFixed(1)}% vs B&H ${(p.benchmark * 100).toFixed(1)}%`;
    case "automation.killswitch": return p.on ? "KILL SWITCH ON" : "kill switch off";
    case "heartbeat.alert": case "heartbeat.quiet": return `${p.headline}${p.details ? " · " + p.details.slice(0, 200) : ""}`;
    default: return JSON.stringify(p).slice(0, 200);
  }
}
function addEvent(ev, prepend = true) {
  const li = document.createElement("li");
  const kind = ev.type.split(".")[0];
  li.innerHTML = `<span class="t">${hhmmss(ev.ts)}</span><span class="ty ${kind} ${/error|rejected|halted/.test(ev.type) ? "error" : ""}">${esc(ev.type)}</span><span class="p">${esc(eventSummary(ev))}</span>`;
  const list = $("#events");
  prepend ? list.prepend(li) : list.appendChild(li);
  while (list.children.length > 300) list.lastChild.remove();
}
async function initEvents() {
  try { (await api("/api/events?limit=100")).reverse().forEach((ev) => addEvent(ev, false)); } catch { /* ignore */ }
  const es = new EventSource("/api/events/stream");
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    addEvent(ev);
    $("#last-event").textContent = `${hhmmss(ev.ts)} ${ev.type} · ${eventSummary(ev).slice(0, 90)}`;
    if (ev.type.startsWith("trade.")) loadAccounts();
    if (ev.type.startsWith("bot.") || ev.type.startsWith("automation.")) loadAutomation();
    if (ev.type === "bot.halted") toast("⚠ " + eventSummary(ev), true);
    if (ev.type === "heartbeat.alert") toast("⚑ Heartbeat: " + ev.payload.headline);
  };
}

// ---------------------------------------------------------------- boot
(async function boot() {
  initCharts();
  await loadStatus();
  selectTab("right", localStore("rtab") || "agent");
  loadSymbol(state.symbol);
  loadWatchlist();
  loadAccounts();
  loadStrategies();
  loadScalp();
  loadAutomation();
  initEvents();
  if (!$("#chat").children.length) addMsg("bot", "Ask me about any ticker, or ask for a trade idea. I can stage and commit orders; you review and push. Type <code>HELP</code> in the command bar for shortcuts.");
  setInterval(loadWatchlist, 20000);
  setInterval(loadQuote, 15000);
  setInterval(loadAccounts, 30000);
  setInterval(loadAutomation, 30000);
})();
