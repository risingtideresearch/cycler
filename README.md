# RTBW Cycler

Run battery test programs on LFP cells from your browser, driving Siglent bench
instruments over LAN, with live plots and SQLite logging.

Build an ordered **program** of charge, discharge, and rest steps; run it; and
watch a live plot and a stats card (Ah, Wh, DCIR, …) for each step. It drives a
Siglent **SDL** electronic load (discharge) and **SPD** power supply (charge),
and can use a Siglent **SDM** multimeter as a precise 4-wire cell voltmeter. With
no instruments configured it runs against a built-in simulated LFP cell, so you
can try the whole thing with no hardware.

> **Status:** v0. LFP-focused, with hard safety caps on charge voltage and
> current (see [Safety caps](#safety-caps)).

## Requirements

- Python ≥ 3.11 (uses the standard-library `tomllib`)
- The instruments on the same LAN — or none, in which case it simulates them

## Quick start

```bash
git clone https://github.com/risingtideresearch/cycler.git
cd cycler
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --ws-ping-interval 30 --ws-ping-timeout 120
```

Open <http://localhost:8000>. With no hardware configured it runs against a
simulated LFP cell, so you can build and run programs right away.

The `--ws-ping-*` flags relax the WebSocket keepalive: the UI socket is
server-push (the browser never sends), so the default 20 s ping/pong timeout can
drop an idle or backgrounded tab — especially over a remote link like Tailscale.
The page auto-reconnects regardless, but the longer timeout avoids the drop.

## Running a battery program

Build a **program** from three kinds of step:

- **Discharge** — constant current down to a cutoff voltage.
- **Charge** — constant current, then constant voltage as the current tapers to a
  termination threshold (CC-CV).
- **Rest** — drive nothing for a set duration while the cell relaxes.

Add steps with the *+ Discharge / + Charge / + Rest* buttons, edit each step's
settings, reorder or delete them, then press **Run program**. Steps run in order,
back to back. Each step gets a live card with a dual-axis voltage/current plot and
stats:

- charge/discharge cards show duration, Ah, Wh, last V & I, and DCIR, on a fixed
  2–4 V axis (the current axis is scaled to the configured caps);
- rest cards show duration and voltage on an autoscaled axis, plotting the
  relaxation curve.

**Stop** aborts the running step and skips the rest of the program.

A step is **refused** if its preconditions fail — e.g. discharging a cell already
below the cutoff, charging one already full, or exceeding a safety cap — and if a
mid-program step can't start, the remaining steps are marked skipped. Every step
**fails safe**: its source is switched off on reaching the target, hitting a
safety cap, an instrument error, or app shutdown.

### Ah and DCIR

- **Ah / Wh** are trapezoidal integrations of the measured current and power over
  time — the real charge and energy moved, not `current × time`.
- **DCIR** (DC internal resistance) is estimated from a step's first loaded sample
  as `|V_open_circuit − V_loaded| / I`. It's only meaningful if the cell was
  rested first, so **put a Rest step before a charge/discharge** to capture a true
  open-circuit voltage — and it's most accurate with a DMM sensing voltage at the
  terminals (see below).

Every step is saved as a session; download a step's paired V/I/power samples from
the **CSV** link in the history table.

## Cell voltage and DCIR (optional DMM)

The load and supply report their own terminal voltage, which at several amps
includes the IR drop of your test leads (tens of mV) — enough to corrupt DCIR,
since cell resistance is itself only tens of mΩ. For accurate voltage and DCIR,
wire a Siglent **SDM** multimeter's leads **Kelvin (4-wire) directly at the cell
terminals** and set its IP. The DMM then provides voltage for all battery
readings (current still comes from the load/supply). The SDL1000X has no
remote-sense terminals, so a separate DMM is how you get 4-wire voltage here. With
no DMM configured, the cycler falls back to the load/supply voltage.

## Connecting real instruments

Easiest: use the **Instruments** panel at the top of the page — enter each
device's IP and press *Save & reconnect*. It reconnects live (when no program is
running), shows each instrument's connection status, and persists the IPs to
`config.toml`. An empty field means "simulate this instrument".

Find each IP on the device under *Utility → I/O → LAN*. Siglent SDM, SDL, and SPD
instruments listen for SCPI on TCP port 5025. You can also set them by hand in
`config.toml` (the Instruments panel rewrites this file when you save):

```toml
[dmm]
host = "192.168.1.50"   # cell voltmeter (DMM, wired Kelvin at the terminals)
[load]
host = "192.168.1.51"   # electronic load
[psu]
host = "192.168.1.52"   # power supply (for charging)
```

## Configuration

Configuration lives in `config.toml`, searched in the current working directory
first, then the project root. If neither exists, the app runs entirely on
built-in defaults (all instruments simulated). Every table and key is optional —
omit anything to keep its default; an empty or absent `host` simulates that
device. See `config.toml.example` for an annotated template.

| Table        | Key                   | Default          | Description                                   |
|--------------|-----------------------|------------------|-----------------------------------------------|
| `[dmm]`      | `host`                | _(empty → none)_ | Cell-voltmeter DMM IP (Kelvin at terminals); empty → use load/PSU voltage |
| `[dmm]`      | `port`                | `5025`           | SCPI TCP port                                 |
| `[storage]`  | `db_path`             | `siglent.db`     | SQLite file for logged readings/sessions      |
| `[load]`     | `host`                | _(empty → mock)_ | Electronic load IP address                    |
| `[load]`     | `port`                | `5025`           | Load SCPI TCP port                            |
| `[load]`     | `max_current`         | `30.0`           | Discharge-current safety cap (A); hard-clamped ≤ 30 |
| `[psu]`      | `host`                | _(empty → mock)_ | Power supply IP address (charging)            |
| `[psu]`      | `port`                | `5025`           | PSU SCPI TCP port                             |
| `[psu]`      | `channel`             | `CH1`            | PSU output channel                            |
| `[psu]`      | `max_voltage`         | `3.7`            | CV charge-voltage safety cap (V); hard-clamped ≤ 3.7 (LFP) |
| `[psu]`      | `max_current`         | `5.0`            | Charge-current safety cap (A); hard-clamped ≤ 5 (SPD1305X) |
| `[discharge]`| `current`             | `1.0`            | Default discharge current (A) in the UI       |
| `[discharge]`| `cutoff`              | `2.5`            | Default discharge cutoff voltage (V)          |
| `[charge]`   | `current`             | `1.0`            | Default charge current (A) in the UI          |
| `[charge]`   | `voltage`             | `3.65`           | Default CV charge voltage (V)                 |
| `[charge]`   | `termination_current` | `0.05`           | Default taper-off current that ends a charge (A) |
| `[dmm2]`     | `host`                | _(empty → none)_ | Second DMM IP — the DCIR tester's fixture voltmeter |
| `[dmm2]`     | `port`                | `5025`           | SCPI TCP port                                 |
| `[load2]`    | `host`                | _(empty → mock)_ | Second load IP — sinks the DCIR test pulse    |
| `[load2]`    | `port`                | `5025`           | SCPI TCP port                                 |
| `[dcir]`     | `pulse_current`       | `10.0`           | DCIR pulse amplitude (A); hard-clamped ≤ 30   |
| `[dcir]`     | `pulse_seconds`       | `2.0`            | DCIR pulse length (s)                         |
| `[dcir]`     | `settle_seconds`      | `10.0`           | Voltage must hold steady this long before a pulse |
| `[dcir]`     | `settle_band`         | `0.003`          | "Steady" = spread within this many volts      |
| `[dcir]`     | `min_voltage`         | `2.8`            | Never pulse a cell resting below this (V)     |
| `[dcir]`     | `db_path`             | `dcir.db`        | DCIR tester's own results DB file             |
| `[discord]`  | `webhook_url`         | _(empty → off)_  | Discord webhook for run notifications (step start/end, errors) |
| `[monitor]`  | `target_voltage`      | `3.65`           | Balance target the monitor alerts on (V)      |
| `[monitor]`  | `warn_voltage`        | `3.60`           | Monitor starts warning at/above this (V)       |
| `[monitor]`  | `interval`            | `1.0`            | Monitor seconds between voltage reads          |
| `[mock_cell]`| `ah`                  | `3.0`            | Simulated cell capacity (mock load/PSU only)  |
| `[mock_cell]`| `rint`                | `0.08`           | Simulated cell internal resistance Ω (mock)   |

> To watch a mock run finish quickly, shrink the simulated cell
> (`[mock_cell] ah = 0.05`), use a few amps, and/or set a small per-step Max Ah.

### Safety caps

LFP-focused: the three `max_*` caps are **hard-clamped in code** to
non-configurable ceilings (`HARD_MAX_CHARGE_VOLTAGE = 3.7 V`,
`HARD_MAX_PSU_CURRENT = 5 A`, `HARD_MAX_LOAD_CURRENT = 30 A` in `app/config.py`).
A `config.toml` value above a ceiling is clamped down with a warning — you can
only set the caps *lower*, never higher — and the clamp is applied both at config
load and again in the controller. A charge step is refused if its CV voltage
exceeds the cap, and a constant-voltage supply can't output above its setpoint, so
the cell can never see more than the cap. Change the ceilings in code,
deliberately, for other chemistries.

## Top-balance monitor

A separate, **read-only** app (`monitor/main.py`, default port 8001) for watching
a top-balance charge. It polls cell/pack voltage from the DMM and shows a big live
readout and a plot with warn/target reference lines, **alerting as the voltage
nears the target** — an on-screen banner (amber → green → red), a browser beep,
and an optional Discord message. It never arms, sets, or switches any instrument.

```bash
uvicorn monitor.main:app --host 0.0.0.0 --port 8001 \
  --ws-ping-interval 30 --ws-ping-timeout 120
```

Open <http://localhost:8001>. With no DMM configured it simulates a balance charge
so you can see it work. Target/warn thresholds and the read interval are the
`[monitor]` config keys (default 3.65 / 3.60 V). Click **Enable sound** once to
allow the browser beep (browsers block audio until a user gesture).

> **The DMM allows a single connection.** Run the monitor when the cycler isn't
> using the DMM — the cycler holds the DMM whenever a `[dmm] host` is configured,
> even while idle, so the two can't read it at the same time.

## DCIR tester

A second standalone app (`dcir/main.py`, default port 8002) for quickly
screening cells — e.g. spotting a bad cell after a top balance — by measuring DC
internal resistance with a single load pulse, hands-free:

1. **Connect a cell** — the app watches the fixture voltage; a cell appearing
   starts the test automatically.
2. **Settling** — it waits until the voltage holds steady (the `[dcir]`
   `settle_*` keys) so the open-circuit reference is a true rested value.
3. **Pulse** — one constant-current pulse on the load (`pulse_current` ×
   `pulse_seconds`), sampling the loaded voltage past the initial transient.
4. **Result** — DCIR = (V_open − V_loaded) / I, shown big in mΩ and logged.
   Remove the cell and insert the next one; it re-arms by itself. A *Re-test*
   button repeats the measurement without unplugging.

It measures and logs only — no pass/fail judgement. It uses the **second
instrument set** (`[dmm2]`/`[load2]`), so it can run alongside the cycler: wire
DMM 2 Kelvin at the fixture for accurate voltage (else it falls back to the
load's own reading). Results go to their own SQLite file (`[dcir] db_path`).
The pulse current is hard-clamped to the same 30 A load ceiling, a cell resting
below `min_voltage` is never pulsed, and the load is switched off and
**verified off** after every pulse.

```bash
uvicorn dcir.main:app --host 0.0.0.0 --port 8002 \
  --ws-ping-interval 30 --ws-ping-timeout 120
```

Open <http://localhost:8002>. With no `[load2]` host configured it simulates
cells being swapped in and out so you can watch the whole flow.

## How data is logged

Readings go to the SQLite database (`siglent.db` by default). The easiest export
is the per-step **CSV** link in the history table; for ad-hoc queries:

```bash
sqlite3 siglent.db 'SELECT ts, value, unit FROM readings ORDER BY ts DESC LIMIT 10;'
```

## Architecture

- **`app/instruments/`** — SCPI-over-TCP transport (`scpi.py`) and drivers:
  `load.py` (SDL1000X load), `psu.py` (SPD supply), `multimeter.py` (SDM DMM, used
  as the cell voltmeter). `mock_load.py`, `mock_psu.py`, and a shared
  `mock_cell.py` simulate an LFP cell so the app runs with no hardware.
- **`app/battery.py`** — `BatteryController`: owns the load, supply, and optional
  DMM, and runs a program of `Step`s one at a time — integrating Ah/Wh, estimating
  DCIR, failing safe, and logging each step as a session.
- **`app/db.py`** — SQLite storage: a general `readings` table
  (instrument/quantity/unit) and a `sessions` table (per-step parameters and
  results, grouped by run id, with open-circuit V and DCIR).
- **`app/config.py`** — TOML config loading and the hard safety ceilings.
- **`app/notify.py`** — minimal Discord webhook poster, shared by both apps.
- **`monitor/`** — the standalone read-only top-balance monitor app (its own
  FastAPI app on port 8001; reuses the instrument drivers and config).
- **`dcir/`** — the standalone DCIR tester app (port 8002): auto-detects a
  connected cell, fires one load pulse on the second instrument set
  (`[dmm2]`/`[load2]`), and logs DCIR to its own database.
- **`app/main.py`** — FastAPI app: serves the UI, streams state over
  `/ws/battery`, and exposes the REST API (`/api/battery/run`, `/stop`,
  `/session/{id}` and `…/export.csv`, `/sessions`, `/config`).
- **`web/`** — `battery.html` + `static/battery.js` (cycler UI: program builder,
  per-step cards, Instruments panel) and `monitor.html` + `static/monitor.js`
  (monitor UI). uPlot is vendored.

## License

MIT — see [LICENSE](LICENSE).
