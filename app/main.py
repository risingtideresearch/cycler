"""FastAPI app: serves the web UI, streams readings + state over a WebSocket,
exposes recent history and CSV export, and accepts function/range/interval
control commands."""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import persist_config, settings
from .battery import (
    BatteryController, MODE_CHARGE, MODE_DISCHARGE, MODE_REST, Q_CURRENT, Q_VOLTAGE, Step,
)
from .db import Database
from .instruments.load import SiglentLoad
from .instruments.mock_cell import MockCell
from .instruments.mock_load import MockLoad
from .instruments.mock_psu import MockPSU
from .instruments.multimeter import SiglentMultimeter
from .instruments.psu import SiglentPSU

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("siglent")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ConnectionManager:
    """Tracks connected WebSocket clients and fans out messages to all of them."""

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
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


def _build_voltmeter_for(dmm_host: str | None):
    """The DMM, if a host is given, is the battery's cell voltmeter — wire its
    leads Kelvin (4-wire) at the cell terminals so terminal voltage excludes lead
    IR drop. With no host the battery falls back to the load/PSU's own reading."""
    if not dmm_host:
        return None
    log.info("connecting to DMM (cell voltmeter) at %s:%s", dmm_host, settings.dmm_port)
    return SiglentMultimeter(dmm_host, settings.dmm_port)


def _build_sources_for(load_host: str | None, psu_host: str | None):
    """Build the electronic load and power supply for the given hosts. When both
    are simulated they share one MockCell, so an offline cycle is self-consistent.
    An empty host selects the simulator for that device."""
    cell = MockCell(settings.mock_cell_ah, settings.mock_cell_rint)
    load = SiglentLoad(load_host, settings.load_port) if load_host else MockLoad(cell)
    psu = (SiglentPSU(psu_host, settings.psu_port, settings.psu_channel)
           if psu_host else MockPSU(cell))
    return load, psu


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    voltmeter = _build_voltmeter_for(settings.dmm_host)
    load, psu = _build_sources_for(settings.load_host, settings.psu_host)
    battery_manager = ConnectionManager()
    battery = BatteryController(
        load, psu, db, battery_manager.broadcast,
        voltmeter=voltmeter,
        discord_webhook=settings.discord_webhook,
        max_current=settings.load_max_current,
        max_charge_voltage=settings.psu_max_voltage,
        psu_max_current=settings.psu_max_current,
        default_discharge_current=settings.discharge_current,
        default_cutoff=settings.cutoff_voltage,
        default_charge_current=settings.charge_current,
        default_charge_voltage=settings.charge_voltage,
        default_termination_current=settings.termination_current,
    )
    battery.start()

    app.state.db = db
    app.state.battery_manager = battery_manager
    app.state.battery = battery
    app.state.hosts = {"dmm": settings.dmm_host or "", "load": settings.load_host or "",
                       "psu": settings.psu_host or ""}
    try:
        yield
    finally:
        await battery.shutdown()  # safely switches the load/supply off if running
        for dev in (load, psu, voltmeter):
            if dev is not None and hasattr(dev, "close"):
                dev.close()
        db.close()


app = FastAPI(title="Siglent Lab Control", lifespan=lifespan)


class ConfigBody(BaseModel):
    """Instrument IP addresses. Empty string selects the simulator for that device."""
    dmm_host: str = ""
    load_host: str = ""
    psu_host: str = ""


class DischargeBody(BaseModel):
    current: float
    cutoff: float
    max_seconds: float | None = None
    max_ah: float | None = None
    note: str = ""


class ChargeBody(BaseModel):
    current: float
    voltage: float
    termination_current: float
    max_seconds: float | None = None
    max_ah: float | None = None
    note: str = ""


class StepBody(BaseModel):
    """One step of a program. `kind` is 'discharge', 'charge', or 'rest'.
    Discharge uses `cutoff`; charge uses `voltage` + `termination_current`;
    rest uses `rest_seconds` (and needs no current)."""
    kind: str
    current: float | None = None
    cutoff: float | None = None
    voltage: float | None = None
    termination_current: float | None = None
    max_seconds: float | None = None
    max_ah: float | None = None
    rest_seconds: float | None = None


class RunBody(BaseModel):
    steps: list[StepBody]
    note: str = ""   # one note for the whole program, recorded on every step


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "battery.html")


def _config_payload() -> dict:
    snap: dict = app.state.battery.snapshot()
    h = app.state.hosts
    return {
        "dmm_host": h["dmm"], "load_host": h["load"], "psu_host": h["psu"],
        "load_status": snap["load_status"], "psu_status": snap["psu_status"],
        "load_idn": snap["load_idn"], "psu_idn": snap["psu_idn"],
        "voltmeter_idn": snap["voltmeter_idn"],
        "busy": snap["status"] == "running" or snap["resting"],
    }


@app.get("/api/config")
async def get_config() -> dict:
    return _config_payload()


@app.post("/api/config")
async def set_config(body: ConfigBody) -> dict:
    battery: BatteryController = app.state.battery
    dmm = body.dmm_host.strip() or None
    load = body.load_host.strip() or None
    psu = body.psu_host.strip() or None
    new_load, new_psu = _build_sources_for(load, psu)
    new_voltmeter = _build_voltmeter_for(dmm)
    try:
        await battery.set_sources(new_load, new_psu, new_voltmeter)
    except ValueError as exc:
        # Reconnect refused (a program is running) — drop the freshly built devices.
        for dev in (new_load, new_psu, new_voltmeter):
            if dev is not None and hasattr(dev, "close"):
                await asyncio.to_thread(dev.close)
        raise HTTPException(status_code=409, detail=str(exc))
    app.state.hosts = {"dmm": dmm or "", "load": load or "", "psu": psu or ""}
    await asyncio.to_thread(
        persist_config, {"dmm_host": dmm, "load_host": load, "psu_host": psu})
    return _config_payload()


@app.get("/api/battery")
async def battery_state() -> dict:
    return app.state.battery.snapshot()


@app.post("/api/battery/discharge")
async def battery_discharge(body: DischargeBody) -> dict:
    battery: BatteryController = app.state.battery
    try:
        return await battery.start_discharge(
            body.current, body.cutoff, body.max_seconds, body.max_ah, body.note.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/battery/charge")
async def battery_charge(body: ChargeBody) -> dict:
    battery: BatteryController = app.state.battery
    try:
        return await battery.start_charge(
            body.current, body.voltage, body.termination_current,
            body.max_seconds, body.max_ah, body.note.strip()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _step_from_body(b: StepBody, note: str) -> Step:
    if b.kind == MODE_REST:
        if not b.rest_seconds or b.rest_seconds <= 0:
            raise HTTPException(status_code=400, detail="rest step needs a duration")
        return Step(MODE_REST, 0.0, 0.0, None, b.rest_seconds, None, note)
    if b.current is None:
        raise HTTPException(status_code=400, detail=f"{b.kind} step needs a current")
    if b.kind == MODE_DISCHARGE:
        if b.cutoff is None:
            raise HTTPException(status_code=400, detail="discharge step needs a cutoff voltage")
        return Step(MODE_DISCHARGE, b.current, b.cutoff, None, b.max_seconds, b.max_ah, note)
    if b.kind == MODE_CHARGE:
        if b.voltage is None or b.termination_current is None:
            raise HTTPException(status_code=400,
                                detail="charge step needs a voltage and termination current")
        return Step(MODE_CHARGE, b.current, b.voltage, b.termination_current,
                    b.max_seconds, b.max_ah, note)
    raise HTTPException(status_code=400, detail=f"unknown step kind {b.kind!r}")


@app.post("/api/battery/run")
async def battery_run(body: RunBody) -> dict:
    battery: BatteryController = app.state.battery
    note = body.note.strip()
    steps = [_step_from_body(b, note) for b in body.steps]
    try:
        return await battery.start_sequence(steps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/battery/stop")
async def battery_stop() -> dict:
    battery: BatteryController = app.state.battery
    return await battery.stop()


@app.get("/api/battery/sessions")
async def battery_sessions(limit: int = 50) -> list[dict]:
    db: Database = app.state.db
    return await asyncio.to_thread(db.recent_sessions, limit)


@app.get("/api/battery/session/{session_id}")
async def battery_session(session_id: int) -> dict:
    """A single step's session row plus its logged V/I points, for the per-step
    plot. Pairs voltage & current by their shared timestamp."""
    db: Database = app.state.db
    sess = await asyncio.to_thread(db.get_session, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="no such session")
    volts = await asyncio.to_thread(db.readings_between, Q_VOLTAGE, sess["started_ts"], sess["ended_ts"])
    amps = await asyncio.to_thread(db.readings_between, Q_CURRENT, sess["started_ts"], sess["ended_ts"])
    amp_by_ts = {r["ts"]: r["value"] for r in amps}
    points = [
        {"ts": r["ts"], "voltage": r["value"], "current": amp_by_ts.get(r["ts"])}
        for r in volts
    ]
    return {"session": sess, "points": points}


def _readings_csv(volts: list[dict], amp_by_ts: dict[float, float]) -> StreamingResponse:
    """Stream paired voltage/current readings (oldest first) as CSV."""
    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)

        def flush() -> str:
            data = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return data

        writer.writerow(["timestamp_iso", "epoch_seconds", "voltage_v", "current_a", "power_w"])
        yield flush()
        for r in volts:
            iso = datetime.datetime.fromtimestamp(r["ts"], tz=datetime.timezone.utc).isoformat()
            v = r["value"]
            i = amp_by_ts.get(r["ts"])
            p = v * i if i is not None else ""
            writer.writerow([iso, r["ts"], v, "" if i is None else i, p])
            yield flush()

    return StreamingResponse(generate(), media_type="text/csv")


@app.get("/api/battery/session/{session_id}/export.csv")
async def battery_session_export_csv(session_id: int) -> StreamingResponse:
    """CSV of one session's (one step's) paired V/I/power samples."""
    db: Database = app.state.db
    sess = await asyncio.to_thread(db.get_session, session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="no such session")
    volts = await asyncio.to_thread(db.readings_between, Q_VOLTAGE, sess["started_ts"], sess["ended_ts"])
    amps = await asyncio.to_thread(db.readings_between, Q_CURRENT, sess["started_ts"], sess["ended_ts"])
    amp_by_ts = {r["ts"]: r["value"] for r in amps}
    resp = _readings_csv(volts, amp_by_ts)
    filename = f"siglent_session_{session_id}_{sess['kind']}.csv"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.websocket("/ws/battery")
async def ws_battery(ws: WebSocket) -> None:
    manager: ConnectionManager = app.state.battery_manager
    battery: BatteryController = app.state.battery
    await manager.connect(ws)
    try:
        # Snapshot carries the full program state; per-step plot data is fetched
        # per session, so no bulk history is pushed here.
        await ws.send_json(battery.snapshot())
        while True:
            await ws.receive_text()  # client doesn't send; this just detects disconnect
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# Static assets (vendored uPlot, battery.js), mounted last so it doesn't shadow routes.
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
