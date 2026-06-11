"""Standalone DCIR tester — a separate app (default port 8002).

Quickly screens cells (e.g. after a top balance) by measuring DC internal
resistance with a single load pulse, hands-free:

  no_cell   — watch the voltage; ~0 V means nothing is connected
  settling  — a cell appeared; wait until its voltage holds steady so the
              open-circuit reference is a true rested value
  pulsing   — sink one constant-current pulse on the load and sample the
              loaded voltage near the end of the pulse
  done      — show + log DCIR = (V_open − V_loaded) / I; hold until the cell
              is removed, then go back to watching for the next one

It just measures and logs — no pass/fail judgement. Uses the SECOND instrument
set ([dmm2]/[load2] in config.toml): the DMM wired Kelvin at the test fixture is
the voltage source of truth; the load sinks the pulse and reports current. With
neither configured it simulates cells being swapped in and out, so the page
works with no hardware.

Results go to their own SQLite file ([dcir] db_path) so this app never contends
with the cycler's database across processes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import HARD_MAX_LOAD_CURRENT, persist_config, settings
from app.instruments.load import SiglentLoad
from app.instruments.multimeter import SiglentMultimeter
from app.notify import discord_post

log = logging.getLogger("siglent.dcir")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CELL_PRESENT_VOLTAGE = 1.0   # below this the fixture is considered empty
IDLE_INTERVAL = 0.5          # seconds between voltage polls while watching/settling
PULSE_SAMPLE_INTERVAL = 0.25  # seconds between V/I samples during the pulse
VERIFY_OFF_CURRENT = 0.05    # residual amps after OFF that mean it didn't stop
HISTORY = 200                # recent results kept in memory for the UI


# --- storage ----------------------------------------------------------------
class DcirDb:
    """Tiny dedicated store: one row per completed DCIR test."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dcir_tests (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL NOT NULL,   -- unix epoch seconds
                    ocv        REAL NOT NULL,   -- rested open-circuit voltage (V)
                    v_loaded   REAL NOT NULL,   -- voltage under the pulse (V)
                    current    REAL NOT NULL,   -- measured pulse current (A)
                    dcir_ohms  REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    def insert(self, ts: float, ocv: float, v_loaded: float,
               current: float, dcir_ohms: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO dcir_tests (ts, ocv, v_loaded, current, dcir_ohms) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, ocv, v_loaded, current, dcir_ohms),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent(self, limit: int = HISTORY) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dcir_tests ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- simulated bench ---------------------------------------------------------
class MockDcirBench:
    """Simulated fixture for demo: every ~50 s a 'new cell' (different OCV and
    internal resistance each time) is placed in the fixture, sits a while, and is
    removed. The mock meter and mock load both read from this shared state."""

    EMPTY_S, OCCUPIED_S = 8.0, 45.0

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self.load_on = False
        self.set_current = 0.0

    def _cell(self) -> tuple[bool, float, float]:
        t = time.monotonic() - self._t0
        period = self.EMPTY_S + self.OCCUPIED_S
        n = int(t // period)
        present = (t % period) >= self.EMPTY_S
        # Deterministic per-insertion variety: 18–32 mΩ, OCV 3.32–3.36 V.
        rint = 0.018 + 0.014 * ((n * 3) % 5) / 4.0
        ocv = 3.32 + 0.04 * ((n * 7) % 5) / 4.0
        return present, ocv, rint

    def read_voltage(self) -> float:
        present, ocv, rint = self._cell()
        if not present:
            return 0.002
        i = self.set_current if self.load_on else 0.0
        return ocv - i * rint

    def read_current(self) -> float:
        present, _, _ = self._cell()
        return self.set_current if (self.load_on and present) else 0.0


class MockMeter:
    name = "mock-dmm2"

    def __init__(self, bench: MockDcirBench) -> None:
        self._bench = bench

    def identify(self) -> str:
        return "Siglent,MOCK-SDM,dcir-sim,1.0"

    def configure(self, *args) -> None:
        pass

    def read(self) -> float:
        return self._bench.read_voltage()


class MockPulseLoad:
    name = "mock-load2"
    supports_idle_voltage = True

    def __init__(self, bench: MockDcirBench) -> None:
        self._bench = bench

    def identify(self) -> str:
        return "Siglent,MOCK-SDL,dcir-sim,1.0"

    def arm(self, current: float, voltage: float | None = None) -> None:
        self._bench.set_current = current

    def set_active(self, on: bool) -> None:
        self._bench.load_on = on

    def measure_voltage(self) -> float:
        return self._bench.read_voltage()

    def measure_current(self) -> float:
        return self._bench.read_current()


def _build_devices(dmm_host: str | None, load_host: str | None):
    """Load + meter for the second instrument set. The meter (voltage source of
    truth) is the DMM when configured, else the load's own terminal reading; with
    no real load at all, both come from one simulated bench."""
    if load_host:
        load = SiglentLoad(load_host, settings.load2_port)
    else:
        load = MockPulseLoad(MockDcirBench())
    if dmm_host:
        meter = SiglentMultimeter(dmm_host, settings.dmm2_port)
    else:
        meter = None  # fall back to the load's terminal voltage
    mock = not load_host
    return load, meter, mock


# --- websocket fan-out --------------------------------------------------------
class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            clients = list(self._clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


# --- the tester ----------------------------------------------------------------
class DcirTester:
    def __init__(self, load, meter, mock: bool, db: DcirDb,
                 manager: ConnectionManager) -> None:
        self._load = load
        self._meter = meter
        self.mock = mock
        self._db = db
        self._manager = manager
        # Pulse parameters from config, current clamped to the hard load ceiling
        # (config already clamps; clamp again here so no caller can raise it).
        self.pulse_current = min(settings.dcir_pulse_current, HARD_MAX_LOAD_CURRENT)
        self.pulse_seconds = settings.dcir_pulse_seconds
        self.settle_seconds = settings.dcir_settle_seconds
        self.settle_band = settings.dcir_settle_band
        self.min_voltage = settings.dcir_min_voltage
        self._webhook = settings.discord_webhook

        self.state = "starting"   # no_cell | settling | pulsing | done | error
        self.voltage: float | None = None
        self.connected = False
        self.last_error: str | None = None
        self.load_idn: str | None = None
        self.meter_idn: str | None = None
        self.result: dict | None = None      # the cell-in-fixture's test result
        self.history: list[dict] = []        # recent results, newest first
        self._window: list[tuple[float, float]] = []  # (ts, v) settle window
        self._task: asyncio.Task | None = None
        self._notify_tasks: set[asyncio.Task] = set()

    # --- snapshots / broadcast ------------------------------------------
    def snapshot(self) -> dict:
        return {
            "type": "dcir", "state": self.state, "voltage": self.voltage,
            "connected": self.connected, "mock": self.mock,
            "last_error": self.last_error,
            "load_idn": self.load_idn, "meter_idn": self.meter_idn,
            "result": self.result,
            "pulse_current": self.pulse_current, "pulse_seconds": self.pulse_seconds,
            "settle_seconds": self.settle_seconds, "settle_band": self.settle_band,
            "min_voltage": self.min_voltage,
            "settle_progress": self._settle_progress(),
            "ts": time.time(),
        }

    def _settle_progress(self) -> float | None:
        if self.state != "settling" or not self._window:
            return None
        return min(1.0, (self._window[-1][0] - self._window[0][0]) / self.settle_seconds)

    async def _emit(self) -> None:
        await self._manager.broadcast(self.snapshot())

    # --- voltage source ---------------------------------------------------
    def _read_voltage_sync(self) -> float:
        if self._meter is not None:
            return self._meter.read()
        return self._load.measure_voltage()

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="dcir")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Belt and braces: make sure the load isn't left sinking.
        try:
            await asyncio.to_thread(self._load.set_active, False)
        except Exception:
            pass
        for dev in (self._load, self._meter):
            if dev is not None and hasattr(dev, "close"):
                try:
                    dev.close()
                except Exception:
                    pass

    async def set_devices(self, load, meter, mock: bool) -> None:
        """Swap the load/meter (after an IP change). Refused mid-pulse."""
        if self.state == "pulsing":
            raise ValueError("a pulse is in progress — try again in a moment")
        old = [self._load, self._meter]
        self._load, self._meter, self.mock = load, meter, mock
        self._window.clear()
        self.result = None
        self.state = "starting"
        self.last_error = None
        self.load_idn = self.meter_idn = None
        for dev in old:
            if dev is not None and hasattr(dev, "close"):
                try:
                    await asyncio.to_thread(dev.close)
                except Exception:
                    pass
        await self._identify()
        await self._emit()

    async def retest(self) -> None:
        """Re-measure the cell currently in the fixture without unplugging it."""
        if self.state == "pulsing":
            raise ValueError("a pulse is already in progress")
        self._window.clear()
        self.result = None
        if self.state == "done":
            self.state = "settling"
        await self._emit()

    async def _identify(self) -> None:
        try:
            self.load_idn = await asyncio.to_thread(self._load.identify)
            if self._meter is not None:
                self.meter_idn = await asyncio.to_thread(self._meter.identify)
                await asyncio.to_thread(self._meter.configure, "CONF:VOLT:DC", "AUTO")
            # Make sure the load is off and quiescent before we watch anything.
            await asyncio.to_thread(self._load.set_active, False)
            self.connected = True
            self.last_error = None
        except Exception as exc:
            log.warning("dcir device setup failed: %s", exc)
            self.connected = False
            self.last_error = str(exc)

    async def _run(self) -> None:
        await self._identify()
        self.history = await asyncio.to_thread(self._db.recent)
        await self._emit()
        await self._manager.broadcast({"type": "dcir_history", "tests": self.history})
        while True:
            start = time.monotonic()
            try:
                await self._tick()
            except Exception as exc:
                log.warning("dcir tick failed: %s", exc)
                if self.connected or self.last_error != str(exc):
                    self.connected = False
                    self.last_error = str(exc)
                    await self._emit()
            await asyncio.sleep(max(0.0, IDLE_INTERVAL - (time.monotonic() - start)))

    # --- state machine -------------------------------------------------------
    async def _tick(self) -> None:
        v = await asyncio.to_thread(self._read_voltage_sync)
        now = time.time()
        if not self.connected:
            self.connected = True
            self.last_error = None
        self.voltage = v

        if v < CELL_PRESENT_VOLTAGE:
            # Fixture empty. Forget the previous cell so the next one re-tests.
            if self.state != "no_cell":
                self.state = "no_cell"
                self.result = None
                self._window.clear()
        elif self.state in ("starting", "no_cell"):
            self.state = "settling"
            self._window = [(now, v)]
        elif self.state == "settling":
            self._window.append((now, v))
            cutoff = now - self.settle_seconds
            # Keep one point older than the window so its span is provably full.
            while len(self._window) > 2 and self._window[1][0] <= cutoff:
                self._window.pop(0)
            if self._window[0][0] <= cutoff:
                vs = [p[1] for p in self._window]
                if max(vs) - min(vs) <= self.settle_band:
                    if v < self.min_voltage:
                        # Steady but too low to pulse safely; sit here and say so.
                        self.last_error = (
                            f"cell resting at {v:.3f} V — below the "
                            f"{self.min_voltage:g} V minimum, not pulsing")
                    else:
                        self.last_error = None
                        await self._pulse(sum(vs) / len(vs))
                        return
        # done: hold the result until the cell is removed.
        await self._emit()

    async def _pulse(self, ocv: float) -> None:
        """Fire one CC pulse and compute DCIR from the rested OCV and the mean
        loaded V/I over the second half of the pulse (past the initial transient)."""
        self.state = "pulsing"
        await self._emit()
        samples: list[tuple[float, float, float]] = []  # (t_rel, v, i)
        try:
            await asyncio.to_thread(self._load.arm, self.pulse_current, None)
            await asyncio.to_thread(self._load.set_active, True)
            t0 = time.monotonic()
            while (t := time.monotonic() - t0) < self.pulse_seconds:
                sv = await asyncio.to_thread(self._read_voltage_sync)
                si = await asyncio.to_thread(self._load.measure_current)
                samples.append((t, sv, si))
                self.voltage = sv
                await asyncio.sleep(PULSE_SAMPLE_INTERVAL)
        except Exception as exc:
            self.last_error = f"pulse failed: {exc}"
            log.warning("dcir pulse failed: %s", exc)
        finally:
            await self._switch_off_verified()

        loaded = [s for s in samples if s[0] >= self.pulse_seconds / 2 and s[2] > 0.1]
        if not loaded:
            self.state = "settling"
            self._window.clear()
            if self.last_error is None:
                self.last_error = "no loaded samples during the pulse — re-settling"
            await self._emit()
            return

        v_loaded = sum(s[1] for s in loaded) / len(loaded)
        i_loaded = sum(s[2] for s in loaded) / len(loaded)
        dcir = (ocv - v_loaded) / i_loaded
        result = {
            "ts": time.time(), "ocv": round(ocv, 5), "v_loaded": round(v_loaded, 5),
            "current": round(i_loaded, 4), "dcir_ohms": round(dcir, 6),
        }
        await asyncio.to_thread(
            self._db.insert, result["ts"], result["ocv"], result["v_loaded"],
            result["current"], result["dcir_ohms"])
        self.result = result
        self.history.insert(0, result)
        del self.history[HISTORY:]
        self.state = "done"
        self.last_error = None
        log.info("DCIR: %.2f mΩ (OCV %.4f V, %.4f V @ %.3f A)",
                 dcir * 1000, ocv, v_loaded, i_loaded)
        await self._emit()
        await self._manager.broadcast({"type": "dcir_test", "test": result})

    async def _switch_off_verified(self) -> None:
        """Switch the load off (retrying) and confirm the current actually
        dropped — an uncontrolled sink on a small cell is the thing to never miss."""
        off_exc: Exception | None = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(self._load.set_active, False)
                off_exc = None
                break
            except Exception as exc:
                off_exc = exc
                if hasattr(self._load, "close"):
                    try:
                        await asyncio.to_thread(self._load.close)
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
        try:
            resid = await asyncio.to_thread(self._load.measure_current)
            if abs(resid) > VERIFY_OFF_CURRENT:
                self._alarm(f"load still sinking {resid:.3f} A after OFF — CHECK THE BENCH")
        except Exception as exc:
            self._alarm(f"could not confirm the load switched off ({exc}) — CHECK THE BENCH")
        if off_exc is not None:
            self._alarm(f"load did not switch off cleanly ({off_exc}) — CHECK THE BENCH")

    def _alarm(self, msg: str) -> None:
        log.error(msg)
        self.last_error = msg
        if self._webhook:
            task = asyncio.create_task(self._post(f"🚨 DCIR tester: {msg}"))
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    async def _post(self, content: str) -> None:
        try:
            await asyncio.to_thread(discord_post, self._webhook, content)
        except Exception as exc:
            log.warning("dcir Discord notify failed: %s", exc)


# --- FastAPI app -----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DcirDb(settings.dcir_db_path)
    manager = ConnectionManager()
    load, meter, mock = _build_devices(settings.dmm2_host, settings.load2_host)
    tester = DcirTester(load, meter, mock, db, manager)
    tester.start()
    app.state.manager = manager
    app.state.tester = tester
    app.state.db = db
    app.state.hosts = {"dmm2": settings.dmm2_host or "", "load2": settings.load2_host or ""}
    try:
        yield
    finally:
        await tester.stop()
        db.close()


app = FastAPI(title="RTBW DCIR Tester", lifespan=lifespan)


class ConfigBody(BaseModel):
    """Second-set instrument IPs. Empty string simulates that device."""
    dmm2_host: str = ""
    load2_host: str = ""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "dcir.html")


def _config_payload() -> dict:
    t: DcirTester = app.state.tester
    h = app.state.hosts
    return {
        "dmm2_host": h["dmm2"], "load2_host": h["load2"],
        "load_idn": t.load_idn, "meter_idn": t.meter_idn,
        "connected": t.connected, "busy": t.state == "pulsing",
    }


@app.get("/api/config")
async def get_config() -> dict:
    return _config_payload()


@app.post("/api/config")
async def set_config(body: ConfigBody) -> dict:
    tester: DcirTester = app.state.tester
    dmm2 = body.dmm2_host.strip() or None
    load2 = body.load2_host.strip() or None
    load, meter, mock = _build_devices(dmm2, load2)
    try:
        await tester.set_devices(load, meter, mock)
    except ValueError as exc:
        for dev in (load, meter):
            if dev is not None and hasattr(dev, "close"):
                await asyncio.to_thread(dev.close)
        raise HTTPException(status_code=409, detail=str(exc))
    app.state.hosts = {"dmm2": dmm2 or "", "load2": load2 or ""}
    await asyncio.to_thread(persist_config, {"dmm2_host": dmm2, "load2_host": load2})
    return _config_payload()


@app.get("/api/dcir")
async def dcir_state() -> dict:
    return app.state.tester.snapshot()


@app.post("/api/dcir/retest")
async def dcir_retest() -> dict:
    tester: DcirTester = app.state.tester
    try:
        await tester.retest()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return tester.snapshot()


@app.get("/api/dcir/tests")
async def dcir_tests(limit: int = HISTORY) -> list[dict]:
    db: DcirDb = app.state.db
    return await asyncio.to_thread(db.recent, limit)


@app.websocket("/ws/dcir")
async def ws_dcir(ws: WebSocket) -> None:
    manager: ConnectionManager = app.state.manager
    tester: DcirTester = app.state.tester
    await manager.connect(ws)
    try:
        await ws.send_json(tester.snapshot())
        await ws.send_json({"type": "dcir_history", "tests": list(tester.history)})
        while True:
            await ws.receive_text()  # client doesn't send; detects disconnect
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
