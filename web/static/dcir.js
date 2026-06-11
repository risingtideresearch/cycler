"use strict";

// DCIR tester UI: hands-free state banner (insert cell → settling → pulse →
// result in mΩ), live voltage, instrument IP config, and a history table.

const el = (id) => document.getElementById(id);
const connEl = el("conn"), mockEl = el("mock"), errEl = el("err");
const bannerEl = el("banner"), readingEl = el("reading"), unitEl = el("readingUnit");
const stateLabelEl = el("stateLabel"), stateSubEl = el("stateSub");
const settleWrap = el("settleWrap"), settleBar = el("settleBar");
const retestBtn = el("retestBtn");
const liveVEl = el("liveV"), ocvEl = el("ocv"), vloadedEl = el("vloaded"), pulseEl = el("pulseInfo");

const STATE_LABEL = {
  starting: "Starting…",
  no_cell: "Connect a cell",
  settling: "Settling",
  pulsing: "Pulsing",
  done: "DCIR",
};
const STATE_SUB = {
  no_cell: "waiting for a cell in the fixture",
  settling: "waiting for the voltage to rest",
  pulsing: "sinking the test pulse",
  done: "remove the cell for the next test",
};

let lastTs = 0;

function fmt(v, d = 4) { return (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d); }
function fmtClock(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : "—"; }
function mohm(r) { return (r == null) ? "—" : (r * 1000).toFixed(2); }

// ---- state ----------------------------------------------------------------
function applyState(m) {
  if ("connected" in m) {
    connEl.textContent = m.connected ? "connected" : "instruments unreachable";
    connEl.className = "badge " + (m.connected ? "ok" : "err");
  }
  if ("mock" in m) mockEl.hidden = !m.mock;
  errEl.textContent = m.last_error || "";

  const state = m.state || "no_cell";
  bannerEl.className = "banner " + state;
  stateLabelEl.textContent = STATE_LABEL[state] || state;
  stateSubEl.textContent = STATE_SUB[state] || "";

  // Big readout: the result in mΩ once done, the live voltage otherwise.
  if (state === "done" && m.result) {
    readingEl.textContent = mohm(m.result.dcir_ohms);
    unitEl.textContent = "mΩ";
  } else {
    readingEl.textContent = fmt(m.voltage, 3);
    unitEl.textContent = "V";
  }

  settleWrap.hidden = state !== "settling";
  if (m.settle_progress != null) settleBar.style.width = (m.settle_progress * 100).toFixed(0) + "%";
  retestBtn.hidden = state !== "done";

  liveVEl.textContent = m.voltage != null ? fmt(m.voltage) + " V" : "—";
  ocvEl.textContent = m.result ? fmt(m.result.ocv) + " V" : "—";
  vloadedEl.textContent = m.result ? fmt(m.result.v_loaded) + " V" : "—";
  if (m.result) {
    pulseEl.textContent = fmt(m.result.current, 2) + " A";
  } else if (m.pulse_current != null) {
    pulseEl.textContent = `${m.pulse_current} A × ${m.pulse_seconds} s`;
  }
  if (m.ts != null) lastTs = m.ts;
}

// ---- history ----------------------------------------------------------------
const historyBody = el("historyBody");

function historyRow(t) {
  const tr = document.createElement("tr");
  tr.innerHTML =
    `<td>${new Date(t.ts * 1000).toLocaleString()}</td>` +
    `<td>${fmt(t.ocv)}</td><td>${fmt(t.v_loaded)}</td>` +
    `<td>${fmt(t.current, 2)}</td><td><strong>${mohm(t.dcir_ohms)}</strong></td>`;
  return tr;
}
function setHistory(tests) {
  historyBody.replaceChildren(...tests.map(historyRow));
}
function pushTest(t) {
  historyBody.prepend(historyRow(t));
}

// ---- config panel -------------------------------------------------------------
const dmm2El = el("dmm2Host"), load2El = el("load2Host");
const cfgMsg = el("cfgMsg");

async function loadConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) return;
  const c = await r.json();
  dmm2El.value = c.dmm2_host || "";
  load2El.value = c.load2_host || "";
  renderIdns(c);
}
function renderIdns(c) {
  el("idns").replaceChildren(
    ...[["DMM 2", c.meter_idn], ["Load 2", c.load_idn]]
      .filter(([, idn]) => idn)
      .map(([name, idn]) => Object.assign(document.createElement("div"), { textContent: `${name}: ${idn}` }))
  );
}
el("cfgSave").addEventListener("click", async () => {
  cfgMsg.textContent = "reconnecting…"; cfgMsg.className = "";
  const r = await fetch("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dmm2_host: dmm2El.value.trim(), load2_host: load2El.value.trim() }),
  });
  if (r.ok) {
    renderIdns(await r.json());
    cfgMsg.textContent = "saved"; cfgMsg.className = "ok";
  } else {
    const d = await r.json().catch(() => ({}));
    cfgMsg.textContent = d.detail || `save failed (${r.status})`; cfgMsg.className = "err";
  }
});

retestBtn.addEventListener("click", () => fetch("/api/dcir/retest", { method: "POST" }));

// ---- websocket -----------------------------------------------------------------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/dcir`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "dcir") applyState(m);
    else if (m.type === "dcir_history") setHistory(m.tests);
    else if (m.type === "dcir_test") pushTest(m.test);
  };
  ws.onclose = () => {
    connEl.textContent = "disconnected — reconnecting…";
    connEl.className = "badge err";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

// Mark the banner stale if updates stop arriving.
setInterval(() => {
  if (lastTs && Date.now() / 1000 - lastTs > 6) bannerEl.classList.add("stale");
}, 2000);

loadConfig();
connect();
