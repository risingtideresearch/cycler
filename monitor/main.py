"""Read-only top-balance voltage monitor — a separate app (default port 8001).

Polls cell/pack voltage from the DMM and streams it to a small web page that
shows the voltage, plots it, and alerts (on-screen + browser beep + optional
Discord) as it approaches the balance target. With no DMM configured it
simulates a balance charge ramping toward the target, so the page works with no
hardware.

It is PURELY PASSIVE — it never arms, sets, or switches any instrument. Note the
DMM allows a single connection, so run this when the cycler isn't using the DMM.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.instruments.multimeter import SiglentMultimeter
from app.notify import discord_post

log = logging.getLogger("siglent.monitor")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Alert state machine, in increasing severity. An alert fires when the state
# escalates (moves to a higher index).
STATES = ["normal", "approaching", "at_target", "over"]
OVER_MARGIN = 0.02   # volts above target that counts as "over"
HISTORY = 3600       # points kept for the plot (~1 h at 1 Hz)


class MockBalanceMeter:
    """Simulates a cell on a CV balance charge: voltage rises and eases toward a
    little above the target, so warn / at-target / over all trigger in a demo."""
    name = "mock-dmm"

    def __init__(self, start: float = 3.30, top: float = 3.67, seconds: float = 90.0) -> None:
        self._start, self._top, self._span = start, top, seconds
        self._t0 = time.monotonic()

    def identify(self) -> str:
        return "Siglent,MOCK-SDM,balance-sim,1.0"

    def configure(self, *args) -> None:
        pass

    def read(self) -> float:
        f = min(1.0, (time.monotonic() - self._t0) / self._span)
        # ease-out: quick at first, tapering near the top (like CV current taper)
        return self._start + (self._top - self._start) * (1 - (1 - f) ** 2)


def _build_meter():
    if settings.dmm_host:
        log.info("monitor reading DMM at %s:%s", settings.dmm_host, settings.dmm_port)
        return SiglentMultimeter(settings.dmm_host, settings.dmm_port), False
    log.info("no DMM configured — monitor simulating a balance charge")
    return MockBalanceMeter(), True


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


class Monitor:
    def __init__(self, meter, mock: bool, manager: ConnectionManager) -> None:
        self._meter = meter
        self.mock = mock
        self._manager = manager
        self.target = settings.monitor_target_voltage
        self.warn = settings.monitor_warn_voltage
        self.over = self.target + OVER_MARGIN
        self.interval = settings.monitor_interval
        self._webhook = settings.discord_webhook
        self.voltage: float | None = None
        self.state = "normal"
        self.connected = False
        self.idn: str | None = None
        self.last_error: str | None = None
        self.history: list[dict] = []
        self._task: asyncio.Task | None = None
        self._notify_tasks: set[asyncio.Task] = set()

    def classify(self, v: float) -> str:
        if v >= self.over:
            return "over"
        if v >= self.target:
            return "at_target"
        if v >= self.warn:
            return "approaching"
        return "normal"

    def snapshot(self) -> dict:
        return {
            "type": "monitor", "connected": self.connected, "voltage": self.voltage,
            "state": self.state, "target": self.target, "warn": self.warn, "over": self.over,
            "idn": self.idn, "last_error": self.last_error, "mock": self.mock,
            "interval": self.interval,
        }

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if hasattr(self._meter, "close"):
            try:
                self._meter.close()
            except Exception:
                pass

    async def _run(self) -> None:
        try:
            self.idn = await asyncio.to_thread(self._meter.identify)
            await asyncio.to_thread(self._meter.configure, "CONF:VOLT:DC", "AUTO")
            self.connected = True
        except Exception as exc:
            log.warning("monitor meter setup failed: %s", exc)
            self.last_error = str(exc)
        await self._manager.broadcast(self.snapshot())
        while True:
            start = time.monotonic()
            await self._tick()
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - start)))

    async def _tick(self) -> None:
        try:
            v = await asyncio.to_thread(self._meter.read)
        except Exception as exc:
            if self.connected or self.last_error != str(exc):
                self.connected = False
                self.last_error = str(exc)
                await self._manager.broadcast(self.snapshot())
            return
        now = time.time()
        if not self.connected:
            self.connected = True
            self.last_error = None
        self.voltage = v
        new = self.classify(v)
        alert = None
        if STATES.index(new) > STATES.index(self.state):
            alert = new
            self._fire_alert(new, v)
        self.state = new
        self.history.append({"ts": now, "v": v})
        if len(self.history) > HISTORY:
            self.history = self.history[-HISTORY:]
        await self._manager.broadcast({
            "type": "monitor", "ts": now, "voltage": v, "state": new, "alert": alert,
            "connected": True, "target": self.target, "warn": self.warn, "over": self.over,
        })

    def _fire_alert(self, level: str, v: float) -> None:
        text = {
            "approaching": f"⚠️ Approaching {self.target:g} V — now {v:.4f} V",
            "at_target": f"🔔 Reached {self.target:g} V — now {v:.4f} V",
            "over": f"🚨 OVER {self.target:g} V — now {v:.4f} V, check the charge",
        }.get(level)
        if not text:
            return
        log.info("monitor alert: %s", text)
        if self._webhook:
            task = asyncio.create_task(self._post(text))
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    async def _post(self, content: str) -> None:
        try:
            await asyncio.to_thread(discord_post, self._webhook, content)
        except Exception as exc:
            log.warning("monitor Discord notify failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = ConnectionManager()
    meter, mock = _build_meter()
    monitor = Monitor(meter, mock, manager)
    monitor.start()
    app.state.manager = manager
    app.state.monitor = monitor
    try:
        yield
    finally:
        await monitor.stop()


app = FastAPI(title="RTBW Top-Balance Monitor", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "monitor.html")


@app.get("/api/monitor")
async def monitor_state() -> dict:
    return app.state.monitor.snapshot()


@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket) -> None:
    manager: ConnectionManager = app.state.manager
    monitor: Monitor = app.state.monitor
    await manager.connect(ws)
    try:
        await ws.send_json(monitor.snapshot())
        await ws.send_json({"type": "monitor_history", "points": list(monitor.history)})
        while True:
            await ws.receive_text()  # client doesn't send; detects disconnect
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
