# Siglent Lab Control

Remote monitoring and control for Siglent DC power supplies and electronic loads
over LAN, with a live web interface and SQLite data logging — focused on running
**battery test programs** on LFP cells.

**Status:** v0 — a **battery program** runner driven by a Siglent SDL electronic
load and an SPD-series DC power supply: build an ordered list of CC discharge and
CC-CV charge steps, run them in sequence, and watch a live plot + stats card for
each step (logs voltage + current, integrates Ah/Wh, computes per-step DCIR,
fails safe). An optional Siglent SDM multimeter, wired Kelvin at the cell, serves
as the precise cell voltmeter.

## Architecture

- **`app/instruments/`** — SCPI-over-TCP transport (`scpi.py`) and instrument
  drivers: `load.py` (Siglent SDL1000X electronic load), `psu.py` (SPD supply),
  and `multimeter.py` (SDM DMM, used as the cell voltmeter). `mock_load.py`,
  `mock_psu.py`, and a shared `mock_cell.py` simulate an LFP cell so the app runs
  with no hardware.
- **`app/battery.py`** — owns the load, the supply, and (optionally) the DMM.
  Runs a **program**: an ordered list of CC discharge / CC-CV charge `Step`s, one
  at a time with optional rests between, integrating Ah/Wh per step, estimating
  DCIR, and **failing safe** (source off on target, safety cap, error, or
  shutdown). Each step is a logged session; steps of one run share a run id.
- **`app/db.py`** — SQLite storage. The `readings` table is general
  (instrument / quantity / unit); the `sessions` table records each step's
  parameters and results (grouped by `cycle_id` = run id, plus open-circuit V and
  DCIR). Per-step plot data is sliced from `readings` by the session time window.
- **`app/main.py`** — FastAPI app: serves the battery UI, streams state over
  `/ws/battery`, and exposes REST endpoints (`POST /api/battery/run`, `/stop`,
  `GET /api/battery/session/{id}` and `…/export.csv`, `/sessions`).
- **`web/`** — `battery.html` is the program builder + per-step cards (each a
  dual-axis V/I plot with stats). uPlot is vendored.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --ws-ping-interval 30 --ws-ping-timeout 120
```

The `--ws-ping-*` flags relax the WebSocket keepalive. The UI's socket is
server-push (the browser never sends), so the default 20 s ping/pong timeout can
drop an idle or backgrounded tab — especially over a remote link like Tailscale —
which makes the live page look frozen. The page auto-reconnects regardless, but
the longer timeout avoids the disconnect in the first place.

Open <http://localhost:8000>. With no hardware configured it runs against a
simulated LFP cell, so you can build and run programs and see everything working.

### Battery program

On the battery page, build a **program** from three kinds of step: *+ Discharge*
(CC to a cutoff), *+ Charge* (CC-CV to a taper current), and *+ Rest* (drive
nothing for a set duration while the cell relaxes). Edit each step's settings,
and reorder or delete them. Press **Run program** and the steps execute in order,
back to back. A live card per step shows its status, stats (duration, Ah, Wh, V,
I, DCIR) and a dual-axis V/I plot (fixed 2–4 V axis), filling in as the program
runs — a rest card plots the relaxation curve. **Stop** aborts the running step
and skips the rest.

**Ah** is a trapezoidal integration of measured current over time (the real
charge moved); **DCIR** is a rough DC internal resistance estimated from a step's
first loaded sample as `|V_open_circuit − V_loaded| / I`. DCIR is only meaningful
if the cell was rested first — **put a rest step before a charge/discharge** so
its open-circuit voltage is a true rested OCV — and is most accurate with the DMM
sensing voltage at the terminals (see below), which excludes lead resistance.

Each step **fails safe** (source off on its target, a safety cap, an instrument
error, or shutdown). A step is refused if its preconditions fail — e.g. a
discharge below the cutoff, a charge above a full cell, a discharge current over
`[load] max_current`, or a charge current/voltage over `[psu] max_current` /
`[psu] max_voltage`; if a mid-program step can't start, the remaining steps are
skipped. Every step is saved in the `sessions` table (steps of one run share a
run id); download a step's paired V/I/power samples from the **CSV** link in the
history list.

### Cell voltage and DCIR (optional DMM)

The load/PSU report their own terminal voltage, which at several amps includes
the IR drop of your test leads (tens of mV) — enough to corrupt DCIR, since cell
resistance is itself only tens of mΩ. For accurate voltage and DCIR, wire a
Siglent SDM multimeter's leads **Kelvin (4-wire) directly at the cell terminals**
and set `[dmm] host`. When configured, the DMM becomes the cell voltmeter for all
battery readings (current still comes from the load/PSU). With no DMM, the
battery falls back to the load/PSU voltage. The SDL1000X has no remote-sense
terminals, so the DMM is how you get 4-wire voltage here.

> To watch a mock run finish quickly, shrink the simulated cell in `config.toml`
> (`[mock_cell] ah = 0.05`), use a few amps, and/or set a small per-step Max Ah.

### Point it at real hardware

Easiest: use the **Instruments** panel at the top of the page — enter each
device's IP and press *Save & reconnect*. The app reconnects live (when no
program is running), shows each instrument's status, and writes the IPs to
`config.toml` so they persist across restarts. An empty field = simulated.

Find each IP on the device under *Utility → I/O → LAN*. Siglent SDM DMMs, SDL
loads, and SPD supplies listen for SCPI on TCP 5025. You can also set them by
hand in `config.toml` (the Instruments panel rewrites this file when you save):

```toml
# config.toml
[dmm]
host = "192.168.1.50"   # cell voltmeter (DMM, wired Kelvin at the terminals)
[load]
host = "192.168.1.51"   # electronic load
[psu]
host = "192.168.1.52"   # power supply (for charging)
```

## Configuration (`config.toml`)

Configuration lives in a TOML file. The app looks for `config.toml` in the
current working directory first, then in the project root; if neither exists, it
runs entirely on built-in defaults (all instruments simulated). Every table and
key is optional — omit anything to keep its default. An empty or absent `host`
selects the simulated instrument for that device. See `config.toml.example` for
an annotated template.

| Table        | Key                   | Default          | Description                                   |
|--------------|-----------------------|------------------|-----------------------------------------------|
| `[dmm]`      | `host`                | _(empty → none)_ | Cell-voltmeter DMM IP (Kelvin at terminals); empty → use load/PSU voltage |
| `[dmm]`      | `port`                | `5025`           | SCPI TCP port                                 |
| `[storage]`  | `db_path`             | `siglent.db`     | SQLite file for logged readings/sessions      |
| `[load]`     | `host`                | _(empty → mock)_ | Electronic load IP address                    |
| `[load]`     | `port`                | `5025`           | Load SCPI TCP port                            |
| `[load]`     | `max_current`         | `10.0`           | Discharge-current safety cap (A); hard-clamped ≤ 30 |
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
| `[mock_cell]`| `ah`                  | `3.0`            | Simulated cell capacity (mock load/PSU only)  |
| `[mock_cell]`| `rint`                | `0.08`           | Simulated cell internal resistance Ω (mock)   |

### Safety caps

The three `max_*` caps above are **hard-clamped in code** to non-configurable
ceilings (`HARD_MAX_CHARGE_VOLTAGE = 3.7 V`, `HARD_MAX_PSU_CURRENT = 5 A`,
`HARD_MAX_LOAD_CURRENT = 30 A` in `app/config.py`): a config value *above* a
ceiling is clamped down with a warning, so no config edit or typo can raise a
limit past what's safe (you can only set them *lower*). The clamp is applied both
at config load and again in `BatteryController`. A charge step is refused if its
CV voltage exceeds the (clamped) cap, and a CV supply cannot output more than its
setpoint — so the cell can never see more than the cap. The ceilings are tuned
for **LFP cells**; change them in code, deliberately, for other chemistries.

## Logged data

Every reading is stored in the `readings` table of the SQLite database. Export
or inspect with e.g.:

```bash
sqlite3 siglent.db 'SELECT ts, value, unit FROM readings ORDER BY ts DESC LIMIT 10;'
```
