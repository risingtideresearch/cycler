"use strict";

// Read-only top-balance monitor UI: live voltage readout, plot with warn/target
// reference lines, a colored state banner, and a browser beep on escalation.

const el = (id) => document.getElementById(id);
const connEl = el("conn"), mockEl = el("mock"), idnEl = el("idn"), errEl = el("err");
const bannerEl = el("banner"), readingEl = el("reading");
const stateLabelEl = el("stateLabel"), stateSubEl = el("stateSub");
const targetEl = el("target"), warnEl = el("warn"), updatedEl = el("updated");
const soundBtn = el("soundBtn");

const STATE_LABEL = {
  normal: "Charging", approaching: "Approaching target",
  at_target: "At target", over: "OVER target",
};
const MAX_POINTS = 5000;
let target = null, warn = null, over = null;
let xs = [], vs = [];
let lastTs = 0;

// ---- formatting ----------------------------------------------------------
function fmt(v, d = 4) { return (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d); }
function fmtClock(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : "—"; }

// ---- sound (Web Audio; needs a user gesture to start) --------------------
let audio = null;
soundBtn.addEventListener("click", () => {
  if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
  audio.resume();
  beep(880, 0.12);                    // confirmation blip
  soundBtn.classList.add("on");
  soundBtn.textContent = "🔊 Sound on";
});

function tone(freq, when, dur) {
  const o = audio.createOscillator(), g = audio.createGain();
  o.type = "sine"; o.frequency.value = freq;
  o.connect(g); g.connect(audio.destination);
  g.gain.setValueAtTime(0.0001, when);
  g.gain.exponentialRampToValueAtTime(0.3, when + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
  o.start(when); o.stop(when + dur);
}
function beep(freq, dur, count = 1, gap = 0.18) {
  if (!audio) return;
  for (let i = 0; i < count; i++) tone(freq, audio.currentTime + i * gap, dur);
}
function alertSound(level) {
  if (level === "approaching") beep(660, 0.15, 1);
  else if (level === "at_target") beep(880, 0.15, 2);
  else if (level === "over") beep(1175, 0.18, 4, 0.16);
}

// ---- plot ----------------------------------------------------------------
function chartWidth() { return Math.min(window.innerWidth - 48, 1100); }

const chart = new uPlot({
  width: chartWidth(), height: 420,
  scales: { x: { time: true }, y: {} },
  legend: { show: true },
  series: [
    {},
    { label: "Voltage", scale: "y", stroke: "#60a5fa", width: 2, value: (u, v) => (v == null ? "—" : fmt(v) + " V") },
    { label: "Target", scale: "y", stroke: "#4ade80", width: 1, dash: [6, 4], value: (u, v) => (v == null ? "—" : fmt(v, 2) + " V") },
    { label: "Warn", scale: "y", stroke: "#fbbf24", width: 1, dash: [3, 4], value: (u, v) => (v == null ? "—" : fmt(v, 2) + " V") },
  ],
  axes: [
    { stroke: "#8a939e", grid: { stroke: "#2a2f36" } },
    { scale: "y", stroke: "#8a939e", grid: { stroke: "#2a2f36" }, size: 60, values: (u, vals) => vals.map((v) => v + " V") },
  ],
}, [[], [], [], []], el("chart"));

window.addEventListener("resize", () => chart.setSize({ width: chartWidth(), height: 420 }));

function redraw() {
  const t = xs.map(() => target), w = xs.map(() => warn);
  chart.setData([xs, vs, t, w]);
}
function setHistory(points) {
  xs = points.map((p) => p.ts); vs = points.map((p) => p.v);
  redraw();
}
function pushPoint(ts, v) {
  xs.push(ts); vs.push(v);
  if (xs.length > MAX_POINTS) { xs = xs.slice(-MAX_POINTS); vs = vs.slice(-MAX_POINTS); }
  redraw();
}

// ---- apply state ---------------------------------------------------------
function applyConn(connected, mock) {
  connEl.textContent = connected ? "connected" : "DMM unreachable";
  connEl.className = "badge " + (connected ? "ok" : "err");
  mockEl.hidden = !mock;
}

function applyState(m) {
  if (m.target != null) { target = m.target; targetEl.textContent = fmt(m.target, 2) + " V"; }
  if (m.warn != null) { warn = m.warn; warnEl.textContent = fmt(m.warn, 2) + " V"; }
  if (m.over != null) over = m.over;
  if (m.idn != null) idnEl.textContent = m.idn;
  if ("connected" in m) applyConn(m.connected, m.mock);
  if (m.last_error) errEl.textContent = m.last_error; else if (m.connected) errEl.textContent = "";

  if (m.voltage != null) readingEl.textContent = fmt(m.voltage);
  const state = m.state || "normal";
  bannerEl.className = "banner " + state + (m.connected === false ? " stale" : "");
  stateLabelEl.textContent = STATE_LABEL[state] || state;
  stateSubEl.textContent = target != null
    ? (state === "over" ? `above the ${fmt(target, 2)} V target` : `target ${fmt(target, 2)} V`)
    : "";

  if (m.ts != null) {
    lastTs = m.ts;
    updatedEl.textContent = fmtClock(m.ts);
    if (m.voltage != null) pushPoint(m.ts, m.voltage);
  }
  if (m.alert) alertSound(m.alert);
}

// ---- websocket -----------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/monitor`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "monitor") applyState(m);
    else if (m.type === "monitor_history") setHistory(m.points);
  };
  ws.onclose = () => {
    connEl.textContent = "disconnected — reconnecting…";
    connEl.className = "badge err";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

// Mark the reading stale if updates stop arriving.
setInterval(() => {
  if (lastTs && Date.now() / 1000 - lastTs > 5) bannerEl.classList.add("stale");
}, 2000);

connect();
