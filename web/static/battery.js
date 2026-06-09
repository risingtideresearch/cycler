"use strict";

// Battery program UI: build an ordered list of charge/discharge steps, run them,
// and show a live plot + stats card per step as the program executes.

const el = (id) => document.getElementById(id);
const statusEl = el("status"), progressEl = el("progress"), idnsEl = el("idns");
const stepsEl = el("steps"), cardsEl = el("cards"), formErrorEl = el("formError");
const runBtn = el("run"), stopBtn = el("stop"), noteIn = el("note");
const dmmHostIn = el("dmmHost"), loadHostIn = el("loadHost"), psuHostIn = el("psuHost");
const saveInstrBtn = el("saveInstruments"), instErrorEl = el("instError");

// ---- model ---------------------------------------------------------------
let program = [];                 // editable list of step objects
let defaults = {};                // server-provided default settings
let maxAmps = 10;                 // A-axis bound = max(load cap, psu cap), from the server
let state = { status: "idle" };   // latest battery snapshot
let shownRunId = null;            // run id whose cards are currently rendered
const cards = new Map();          // step index -> { root, plot, xs, vs, is_, sessionId, finalLoaded, seeded }
const sessionToIndex = new Map(); // session id -> step index (for routing live ticks)

const FINISHED = new Set(["complete", "aborted", "error", "skipped"]);

// ---- formatting ----------------------------------------------------------
function fmt(v, d = 3) { return (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d); }
function fmtMilliohm(ohms) { return (ohms == null || Number.isNaN(ohms)) ? "—" : (ohms * 1000).toFixed(1) + " mΩ"; }
function fmtDuration(sec) {
  if (sec == null || Number.isNaN(sec)) return "—";
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function num(v) { const n = parseFloat(v); return Number.isNaN(n) ? null : n; }

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return r.json();
}

// ---- builder -------------------------------------------------------------
function newStep(kind) {
  if (kind === "rest") {
    return { kind: "rest", minutes: 10 };
  }
  if (kind === "charge") {
    return { kind, current: defaults.charge_current ?? 1, voltage: defaults.charge_voltage ?? 3.65,
      term: defaults.termination_current ?? 0.05, maxHours: "", maxAh: "" };
  }
  return { kind: "discharge", current: defaults.discharge_current ?? 1, cutoff: defaults.cutoff ?? 2.5,
    maxHours: "", maxAh: "" };
}

function numField(label, idx, field, value, step = "any") {
  return `<div class="f"><label>${label}</label>` +
    `<input type="number" min="0" step="${step}" data-idx="${idx}" data-field="${field}" value="${value ?? ""}"></div>`;
}

function rowHtml(s, idx) {
  const head = `<div class="kind">${s.kind}</div>`;
  let main;
  if (s.kind === "rest") {
    main = numField("Duration (min)", idx, "minutes", s.minutes, "1");
  } else if (s.kind === "charge") {
    main = numField("Current (A)", idx, "current", s.current, "0.1") +
      numField("Voltage (V)", idx, "voltage", s.voltage, "0.05") +
      numField("Term (A)", idx, "term", s.term, "0.01") +
      numField("Max h", idx, "maxHours", s.maxHours, "0.5") +
      numField("Max Ah", idx, "maxAh", s.maxAh, "0.1");
  } else {
    main = numField("Current (A)", idx, "current", s.current, "0.1") +
      numField("Cutoff (V)", idx, "cutoff", s.cutoff, "0.05") +
      numField("Max h", idx, "maxHours", s.maxHours, "0.5") +
      numField("Max Ah", idx, "maxAh", s.maxAh, "0.1");
  }
  const tail = "";
  const ctl = `<div class="rowctl">` +
    `<button data-action="up" data-idx="${idx}" title="Move up">↑</button>` +
    `<button data-action="down" data-idx="${idx}" title="Move down">↓</button>` +
    `<button class="rm" data-action="rm" data-idx="${idx}" title="Delete">✕</button></div>`;
  return `<div class="row ${s.kind}">${head}${main}${tail}${ctl}</div>`;
}

function renderBuilder() {
  if (!program.length) {
    stepsEl.innerHTML = '<div class="empty">No steps yet — add a discharge or charge step above.</div>';
  } else {
    stepsEl.innerHTML = program.map(rowHtml).join("");
  }
  setBuilderEnabled(!isBusy());
}

stepsEl.addEventListener("input", (e) => {
  const t = e.target, idx = t.dataset.idx, field = t.dataset.field;
  if (idx == null || !field) return;
  program[idx][field] = t.value;
});
stepsEl.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-action]");
  if (!b) return;
  const idx = +b.dataset.idx, act = b.dataset.action;
  if (act === "rm") program.splice(idx, 1);
  else if (act === "up" && idx > 0) [program[idx - 1], program[idx]] = [program[idx], program[idx - 1]];
  else if (act === "down" && idx < program.length - 1) [program[idx + 1], program[idx]] = [program[idx], program[idx + 1]];
  renderBuilder();
});

el("addDischarge").addEventListener("click", () => { program.push(newStep("discharge")); renderBuilder(); });
el("addCharge").addEventListener("click", () => { program.push(newStep("charge")); renderBuilder(); });
el("addRest").addEventListener("click", () => { program.push(newStep("rest")); renderBuilder(); });

function isBusy() { return state.status === "running" || state.resting; }

function setBuilderEnabled(enabled) {
  el("addDischarge").disabled = !enabled;
  el("addCharge").disabled = !enabled;
  el("addRest").disabled = !enabled;
  runBtn.disabled = !enabled || program.length === 0;
  stopBtn.disabled = enabled;
  noteIn.disabled = !enabled;
  for (const node of stepsEl.querySelectorAll("input, button")) node.disabled = !enabled;
}

// ---- run / stop ----------------------------------------------------------
function buildRunBody() {
  const steps = program.map((s) => {
    if (s.kind === "rest") {
      const minutes = num(s.minutes);
      if (minutes == null || minutes <= 0) throw new Error("Rest steps need a duration in minutes.");
      return { kind: "rest", rest_seconds: minutes * 60 };
    }
    const current = num(s.current);
    if (current == null) throw new Error("Charge/discharge steps need a current.");
    const b = { kind: s.kind, current };
    if (s.kind === "discharge") {
      const cutoff = num(s.cutoff);
      if (cutoff == null) throw new Error("Discharge steps need a cutoff voltage.");
      b.cutoff = cutoff;
    } else {
      const voltage = num(s.voltage), term = num(s.term);
      if (voltage == null || term == null) throw new Error("Charge steps need a voltage and termination current.");
      b.voltage = voltage; b.termination_current = term;
    }
    if (num(s.maxHours) > 0) b.max_seconds = num(s.maxHours) * 3600;
    if (num(s.maxAh) > 0) b.max_ah = num(s.maxAh);
    return b;
  });
  return { steps, note: noteIn.value || "" };
}

runBtn.addEventListener("click", async () => {
  formErrorEl.textContent = "";
  let body;
  try { body = buildRunBody(); } catch (e) { formErrorEl.textContent = e.message; return; }
  runBtn.disabled = true;
  try { await post("/api/battery/run", body); }   // state arrives over the websocket
  catch (e) { formErrorEl.textContent = e.message; runBtn.disabled = false; }
});

stopBtn.addEventListener("click", async () => {
  stopBtn.disabled = true;
  try { await post("/api/battery/stop"); }
  catch (e) { formErrorEl.textContent = e.message; }
});

// ---- instruments (IP config) --------------------------------------------
let appliedHosts = { dmm: "", load: "", psu: "" };

function setStat(role, host, status, idn) {
  const n = document.querySelector(`[data-role="${role}"]`);
  if (!n) return;
  let text, cls;
  if (!host) { text = "simulated"; cls = "sim"; }
  else if (status === "connected" || idn) { text = "connected"; cls = "ok"; }
  else if (status === "error") { text = "error"; cls = "err"; }
  else { text = status || "…"; cls = ""; }
  n.textContent = text;
  n.className = "iststat " + cls;
}

// Status reflects the *applied* hosts, not what's currently typed in the field.
function refreshInstStatuses(d) {
  setStat("dmmStat", appliedHosts.dmm, d.voltmeter_idn ? "connected" : "error", d.voltmeter_idn);
  setStat("loadStat", appliedHosts.load, d.load_status, d.load_idn);
  setStat("psuStat", appliedHosts.psu, d.psu_status, d.psu_idn);
  idnsEl.innerHTML = [["Load", d.load_idn], ["PSU", d.psu_idn], ["DMM", d.voltmeter_idn]]
    .map(([k, v]) => `<div><span class="idnk">${k}</span> ${v ? escapeHtml(v) : "—"}</div>`)
    .join("");
}

function applyConfig(c) {
  appliedHosts = { dmm: c.dmm_host || "", load: c.load_host || "", psu: c.psu_host || "" };
  dmmHostIn.value = appliedHosts.dmm;
  loadHostIn.value = appliedHosts.load;
  psuHostIn.value = appliedHosts.psu;
  refreshInstStatuses(c);
}

async function loadConfig() {
  try { applyConfig(await (await fetch("/api/config")).json()); } catch (e) {}
}

saveInstrBtn.addEventListener("click", async () => {
  instErrorEl.textContent = "";
  saveInstrBtn.disabled = true;
  saveInstrBtn.textContent = "Reconnecting…";
  try {
    applyConfig(await post("/api/config", {
      dmm_host: dmmHostIn.value.trim(),
      load_host: loadHostIn.value.trim(),
      psu_host: psuHostIn.value.trim(),
    }));
  } catch (e) {
    instErrorEl.textContent = e.message;
  } finally {
    saveInstrBtn.textContent = "Save & reconnect";
    saveInstrBtn.disabled = isBusy();
  }
});

// ---- per-step cards ------------------------------------------------------
function plotWidth(card) {
  const w = card.querySelector(".plot").clientWidth;
  return Math.max(240, w || 380);
}

function makePlot(container, width, rest) {
  if (rest) {
    // Rest: voltage only, autoscaled, so the relaxation curve fills the plot.
    return new uPlot({
      width, height: 180,
      scales: { x: { time: true }, V: {} },
      legend: { show: true },
      series: [
        {},
        { label: "V", scale: "V", stroke: "#60a5fa", width: 1.5, value: (u, v) => (v == null ? "—" : fmt(v) + " V") },
      ],
      axes: [
        { stroke: "#8a939e", grid: { stroke: "#23272e" } },
        { scale: "V", stroke: "#60a5fa", size: 52, grid: { stroke: "#23272e" }, values: (u, vals) => vals.map((v) => v) },
      ],
    }, [[], []], container);
  }
  return new uPlot({
    width, height: 180,
    scales: { x: { time: true }, V: { range: [2, 4] }, A: { range: [0, maxAmps] } },
    legend: { show: true },
    series: [
      {},
      { label: "V", scale: "V", stroke: "#4ade80", width: 1.5, value: (u, v) => (v == null ? "—" : fmt(v) + " V") },
      { label: "A", scale: "A", stroke: "#fbbf24", width: 1.5, value: (u, v) => (v == null ? "—" : fmt(v) + " A") },
    ],
    axes: [
      { stroke: "#8a939e", grid: { stroke: "#23272e" } },
      { scale: "V", stroke: "#4ade80", size: 46, grid: { stroke: "#23272e" }, values: (u, vals) => vals.map((v) => v) },
      { scale: "A", side: 1, stroke: "#fbbf24", size: 46, grid: { show: false }, values: (u, vals) => vals.map((v) => v) },
    ],
  }, [[], [], []], container);
}

function rebuildCards(seq) {
  for (const cs of cards.values()) { try { cs.plot.destroy(); } catch (e) {} }
  cards.clear();
  sessionToIndex.clear();
  cardsEl.innerHTML = "";
  el("runHead").hidden = seq.length === 0;
  for (const v of seq) {
    const rest = v.kind === "rest";
    const root = document.createElement("div");
    root.className = `card ${v.kind}`;
    // Rest steps show only duration + voltage; charge/discharge show the full set.
    const stats = rest
      ? `<div class="stat"><span class="k">Duration</span><span class="v" data-role="dur">—</span></div>` +
        `<div class="stat volt"><span class="k">Voltage</span><span class="v" data-role="v">—</span></div>`
      : `<div class="stat"><span class="k">Duration</span><span class="v" data-role="dur">—</span></div>` +
        `<div class="stat"><span class="k">Charge</span><span class="v" data-role="ah">—</span></div>` +
        `<div class="stat"><span class="k">Energy</span><span class="v" data-role="wh">—</span></div>` +
        `<div class="stat volt"><span class="k">Voltage</span><span class="v" data-role="v">—</span></div>` +
        `<div class="stat amp"><span class="k">Current</span><span class="v" data-role="i">—</span></div>` +
        `<div class="stat"><span class="k">DCIR</span><span class="v" data-role="dcir">—</span></div>`;
    root.innerHTML =
      `<div class="chead"><span class="ctitle">Step ${v.index + 1} · ${v.kind}</span>` +
      `<span class="badge" data-role="badge">pending</span></div>` +
      `<div class="cset" data-role="set"></div>` +
      `<div class="stats">${stats}</div>` +
      `<div class="plot"></div>` +
      `<div class="creason" data-role="reason"></div>`;
    cardsEl.appendChild(root);
    const cs = { root, rest, xs: [], vs: [], is_: [], sessionId: null, startedTs: null, finalLoaded: false, seeded: false };
    cs.plot = makePlot(root.querySelector(".plot"), plotWidth(root), rest);
    cards.set(v.index, cs);
  }
}

function setText(root, role, text) {
  const n = root.querySelector(`[data-role="${role}"]`);
  if (n) n.textContent = text;   // null-safe: rest cards omit some stat tiles
}

function settingsSummary(v) {
  if (v.kind === "rest") {
    return `rest ${fmtDuration(v.max_seconds)}`;
  }
  if (v.kind === "charge") {
    return `${fmt(v.set_current, 2)} A → ${fmt(v.target_voltage, 2)} V, taper ${fmt(v.termination_current, 3)} A`;
  }
  let s = `${fmt(v.set_current, 2)} A → ${fmt(v.target_voltage, 2)} V cutoff`;
  if (v.max_ah) s += `, max ${fmt(v.max_ah, 3)} Ah`;
  return s;
}

function redraw(cs) { cs.plot.setData(cs.rest ? [cs.xs, cs.vs] : [cs.xs, cs.vs, cs.is_]); }

function lastVals(cs) {
  const n = cs.xs.length;
  return n ? { v: cs.vs[n - 1], i: cs.is_[n - 1] } : { v: null, i: null };
}

function stepDuration(v, cs) {
  // Use server-stamped time only — the recorded end, or the latest sample's ts
  // for a running step. Never the browser clock (Date.now()), which can be
  // skewed from the server's started_ts and would yield a negative duration.
  if (v.started_ts != null) {
    const end = v.ended_ts != null ? v.ended_ts
      : (cs.xs.length ? cs.xs[cs.xs.length - 1] : null);
    if (end != null) return Math.max(0, end - v.started_ts);
  }
  if (cs.xs.length >= 2) return Math.max(0, cs.xs[cs.xs.length - 1] - cs.xs[0]);
  return null;
}

function updateCard(v, activeStep) {
  const cs = cards.get(v.index);
  if (!cs) return;
  cs.startedTs = v.started_ts;   // so live ticks can update the duration (server clock)
  cs.root.classList.toggle("active", v.index === activeStep && state.status === "running");
  const badge = cs.root.querySelector('[data-role="badge"]');
  badge.textContent = v.status;
  badge.className = "badge " + v.status;
  setText(cs.root, "set", settingsSummary(v));

  if (v.session_id != null) {
    cs.sessionId = v.session_id;
    sessionToIndex.set(v.session_id, v.index);
    if (FINISHED.has(v.status) && v.status !== "skipped" && !cs.finalLoaded) {
      cs.finalLoaded = true;                 // optimistic guard against duplicate fetches
      loadSession(v.index, v.session_id).catch(() => { cs.finalLoaded = false; });
    } else if (v.status === "running" && !cs.seeded && !cs.xs.length) {
      cs.seeded = true;
      loadSession(v.index, v.session_id).catch(() => { cs.seeded = false; });
    }
  }

  const { v: lv, i: li } = lastVals(cs);
  // Rest cards omit the ah/wh/i/dcir tiles; setText no-ops on the missing ones.
  setText(cs.root, "dur", fmtDuration(stepDuration(v, cs)));
  setText(cs.root, "ah", v.charge_ah != null ? fmt(v.charge_ah) : "—");
  setText(cs.root, "wh", v.energy_wh != null ? fmt(v.energy_wh) : "—");
  setText(cs.root, "v", fmt(lv));
  setText(cs.root, "i", fmt(li));
  setText(cs.root, "dcir", fmtMilliohm(v.dcir));
  setText(cs.root, "reason", v.stop_reason && FINISHED.has(v.status) ? v.stop_reason : "");
}

async function loadSession(index, sessionId) {
  const data = await (await fetch(`/api/battery/session/${sessionId}`)).json();
  const cs = cards.get(index);
  if (!cs || cs.sessionId !== sessionId) return;
  cs.xs = data.points.map((p) => p.ts);
  cs.vs = data.points.map((p) => p.voltage);
  cs.is_ = data.points.map((p) => (p.current == null ? null : p.current));
  redraw(cs);
  // Refresh the V/I readouts from the freshly loaded series.
  const { v: lv, i: li } = lastVals(cs);
  setText(cs.root, "v", fmt(lv));
  setText(cs.root, "i", fmt(li));
}

function renderCards() {
  const seq = state.sequence || [];
  if (state.run_id !== shownRunId || seq.length !== cards.size) {
    shownRunId = state.run_id;
    rebuildCards(seq);
  }
  for (const v of seq) updateCard(v, state.active_step);
}

// A live sample for the running step: append and redraw its card.
function onTick(msg) {
  if (msg.session_id == null) return;
  const idx = sessionToIndex.get(msg.session_id);
  if (idx == null) return;
  const cs = cards.get(idx);
  if (!cs || cs.finalLoaded) return;
  if (cs.xs.length && msg.ts <= cs.xs[cs.xs.length - 1]) return;  // dedup at seed boundary
  cs.xs.push(msg.ts); cs.vs.push(msg.voltage); cs.is_.push(msg.current);
  redraw(cs);
  // Tick the duration live from server time (msg.ts), so it counts up without
  // waiting for the next state snapshot and without browser-clock skew.
  if (cs.startedTs != null) setText(cs.root, "dur", fmtDuration(Math.max(0, msg.ts - cs.startedTs)));
  // Missing tiles (rest cards) are no-ops via null-safe setText.
  setText(cs.root, "v", fmt(msg.voltage));
  setText(cs.root, "i", fmt(msg.current));
  setText(cs.root, "ah", fmt(msg.charge_ah));
  setText(cs.root, "wh", fmt(msg.energy_wh));
  if (msg.dcir != null) setText(cs.root, "dcir", fmtMilliohm(msg.dcir));
}

// ---- top-level state -----------------------------------------------------
function applyState(s) {
  state = s;
  if (s.defaults) defaults = s.defaults;
  const bound = Math.max(s.max_current || 0, s.psu_max_current || 0);
  if (bound > 0) maxAmps = bound;   // A-axis bound for plots built this run

  // Overall status badge + progress line.
  let label = s.status, cls = s.status;
  if (s.resting) { label = "resting"; cls = "resting"; }
  statusEl.textContent = label;
  statusEl.className = "badge " + cls;

  if (s.step_count) {
    const at = s.active_step != null ? s.active_step + 1 : s.step_count;
    const cur = (s.sequence || [])[s.active_step];
    const what = cur ? ` (${cur.kind})` : "";
    progressEl.textContent = s.status === "running" || s.resting
      ? `step ${at}/${s.step_count}${what}` : `program ${s.status} · ${s.step_count} steps`;
  } else {
    progressEl.textContent = "";
  }

  setBuilderEnabled(!isBusy());
  // Instrument fields are locked while a program is running; statuses stay live.
  const busy = isBusy();
  dmmHostIn.disabled = loadHostIn.disabled = psuHostIn.disabled = saveInstrBtn.disabled = busy;
  refreshInstStatuses(s);
  renderCards();

  if (s.last_error && (s.load_status === "error" || s.psu_status === "error")) {
    formErrorEl.textContent = s.last_error;
  }
}

// ---- sessions history ----------------------------------------------------
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }

async function loadSessions() {
  let sessions = [];
  try { sessions = await (await fetch("/api/battery/sessions")).json(); }
  catch (e) { return; }
  const body = el("sessionsBody");
  if (!sessions.length) { body.innerHTML = '<div class="empty">No sessions logged yet.</div>'; return; }
  const rows = sessions.map((s) => {
    const dur = s.ended_ts && s.started_ts ? fmtDuration(s.ended_ts - s.started_ts) : "—";
    const rest = s.kind === "rest";
    return `<tr>
      <td>${fmtTime(s.started_ts)}</td>
      <td class="mode-${s.kind}">${s.kind}</td>
      <td>${s.cycle_id != null ? "#" + s.cycle_id : ""}</td>
      <td>${rest ? "—" : fmt(s.set_current, 2) + " A"}</td>
      <td>${rest ? "—" : fmt(s.target_voltage, 2) + " V"}</td>
      <td>${dur}</td>
      <td>${rest ? "—" : fmt(s.charge_ah) + " Ah"}</td>
      <td>${rest ? "—" : fmt(s.energy_wh) + " Wh"}</td>
      <td>${rest ? "—" : fmtMilliohm(s.dcir_ohms)}</td>
      <td>${s.status}</td>
      <td class="note">${s.note ? escapeHtml(s.note) : ""}</td>
      <td><a class="csv" href="/api/battery/session/${s.id}/export.csv">⬇ CSV</a></td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table><thead><tr>
    <th>Started</th><th>Mode</th><th>Run</th><th>Current</th><th>Target</th><th>Duration</th>
    <th>Charge</th><th>Energy</th><th>DCIR</th><th>Status</th><th>Note</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

// ---- websocket -----------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/battery`);
  let wasRunning = false;
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "battery_state") {
      const running = msg.status === "running" || msg.resting;
      applyState(msg);
      if (wasRunning && !running) loadSessions();   // a run just ended
      wasRunning = running;
    } else if (msg.type === "battery") {
      onTick(msg);
    }
    // battery_history is ignored: each step's plot is fetched per session.
  };
  ws.onclose = () => {
    statusEl.textContent = "disconnected — reconnecting…";
    statusEl.className = "badge error";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

window.addEventListener("resize", () => {
  for (const cs of cards.values()) cs.plot.setSize({ width: plotWidth(cs.root), height: 180 });
});

// ---- boot ----------------------------------------------------------------
renderBuilder();
loadConfig();
loadSessions();
connect();
