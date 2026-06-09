"""Owns the electronic load and power supply and runs battery tests: a
constant-current **discharge** to a cutoff voltage, a CC-CV **charge** to a taper
cutoff, and **cycles** that alternate the two.

Like the multimeter Controller, all instrument I/O lives in one background loop
so SCPI access stays serialized. The loop has two modes:

  idle     — nothing driving the cell; slowly poll its resting voltage (via the
             load, which reads its terminals with the input off)
  running  — the active source drives current; poll V & I fast, log both,
             integrate charge (Ah) and energy (Wh), and stop at the target.

Safety first: the active source is switched **off** on reaching the target, on a
safety cap, on any loop error, and on shutdown. A discharge is refused if the
cell is already at/below the cutoff; a charge is refused above the supply's
voltage cap (and, if the cell's voltage can be read, if it is already full).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import HARD_MAX_CHARGE_VOLTAGE, HARD_MAX_LOAD_CURRENT, HARD_MAX_PSU_CURRENT
from .db import Database
from .instruments.base import BatterySource, Multimeter

log = logging.getLogger("siglent.battery")

Broadcast = Callable[[dict], Awaitable[None]]

# DB quantity keys for the two logged battery channels (shared by both modes).
Q_VOLTAGE = "battery_voltage"
Q_CURRENT = "battery_current"

MODE_DISCHARGE = "discharge"
MODE_CHARGE = "charge"
MODE_REST = "rest"        # drives no current; logs the cell relaxing for a set duration

IDLE_INTERVAL = 2.0   # seconds between resting-voltage polls when no test is running
RUN_INTERVAL = 1.0    # seconds between samples during a test
PERSIST_EVERY = 15.0  # seconds between writing running Ah/Wh totals to the session row

# A charge holds CV while the current tapers; only terminate once we're actually
# in the CV region (near the set voltage) and after a short settling time, so a
# momentary low current at switch-on can't end the charge prematurely.
CV_VOLTAGE_EPS = 0.05
MIN_CHARGE_SECONDS = 5.0

# Below this a "voltage" almost certainly means nothing is connected.
MIN_PLAUSIBLE_VOLTAGE = 0.5

# After switching a source off, current above this means it didn't actually stop.
VERIFY_OFF_CURRENT = 0.05


@dataclass
class Step:
    """One step of a program: a charge, a discharge, or a rest. For a rest,
    set_current/target_voltage are 0 and max_seconds is the rest duration."""
    mode: str
    set_current: float
    target_voltage: float                 # discharge: cutoff; charge: CV setpoint
    termination_current: float | None = None  # charge only
    max_seconds: float | None = None      # optional cap; for a rest, the duration
    max_ah: float | None = None
    note: str = ""


class BatteryController:
    def __init__(
        self,
        load: BatterySource,
        psu: BatterySource,
        db: Database,
        broadcast: Broadcast,
        voltmeter: Multimeter | None = None,
        max_current: float = 10.0,
        max_charge_voltage: float = 3.8,
        psu_max_current: float = 5.0,
        default_discharge_current: float = 1.0,
        default_cutoff: float = 2.5,
        default_charge_current: float = 1.0,
        default_charge_voltage: float = 3.65,
        default_termination_current: float = 0.05,
    ) -> None:
        self._load = load
        self._psu = psu
        self._db = db
        self._broadcast = broadcast
        # Optional standalone DMM wired Kelvin at the cell terminals. When set, it
        # is the source of truth for cell voltage (run, idle, and pre-step OCV),
        # so voltage excludes lead IR drop and DCIR is accurate. Current always
        # comes from the active load/PSU.
        self._voltmeter = voltmeter
        self.voltmeter_idn: str | None = None
        self._source_for = {MODE_DISCHARGE: load, MODE_CHARGE: psu}
        # Prefer the load for resting-voltage monitoring; fall back to anything
        # that can read its terminals while inactive.
        self._idle_source: BatterySource | None = (
            load if load.supports_idle_voltage else
            (psu if psu.supports_idle_voltage else None)
        )

        # Clamp every cap to the hard, non-configurable safety ceilings, so even a
        # bad caller (or stale config) cannot raise a limit past what's safe.
        self.max_current = min(max_current, HARD_MAX_LOAD_CURRENT)        # load: discharge cap
        self.psu_max_current = min(psu_max_current, HARD_MAX_PSU_CURRENT)  # PSU: charge cap
        self.max_charge_voltage = min(max_charge_voltage, HARD_MAX_CHARGE_VOLTAGE)
        self.defaults = {
            "discharge_current": default_discharge_current,
            "cutoff": default_cutoff,
            "charge_current": default_charge_current,
            "charge_voltage": default_charge_voltage,
            "termination_current": default_termination_current,
        }

        self.status = "idle"            # idle | running | complete | aborted | error
        self.mode: str | None = None    # discharge | charge (active or last)
        self.load_status = "starting"
        self.psu_status = "starting"
        self.load_idn: str | None = None
        self.psu_idn: str | None = None
        self.last_error: str | None = None

        self.voltage: float | None = None
        self.current: float | None = None

        # Active/last step parameters and accumulators.
        self.session_id: int | None = None
        self.step: Step | None = None
        self.started_ts: float | None = None
        self.ended_ts: float | None = None
        self.charge_ah = 0.0
        self.energy_wh = 0.0
        self.stop_reason: str | None = None
        # DCIR support: rested terminal voltage captured at step start, and the
        # DC internal resistance computed from the first loaded sample.
        self.open_circuit_v: float | None = None
        self.dcir: float | None = None

        # The program being run (or the last one run, kept for display). None
        # until the first run. Holds the step list, the current index, a
        # per-step results array, and a rest-resume timestamp.
        self._seq: dict | None = None

        self._source: BatterySource | None = None
        self._last_ts: float | None = None
        self._last_persist = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # --- snapshot -------------------------------------------------------
    def snapshot(self) -> dict:
        elapsed = None
        if self.started_ts is not None:
            end = self.ended_ts if self.ended_ts is not None else time.time()
            elapsed = end - self.started_ts
        s = self.step
        seq = self._seq
        active_step = (
            seq["index"]
            if seq is not None and (self.status == "running" or seq["resume_at"] is not None)
            else None
        )
        return {
            "type": "battery_state",
            "status": self.status,
            "mode": self.mode,
            "load_status": self.load_status,
            "psu_status": self.psu_status,
            "load_idn": self.load_idn,
            "psu_idn": self.psu_idn,
            "voltmeter_idn": self.voltmeter_idn,
            "last_error": self.last_error,
            "voltage": self.voltage,
            "current": self.current,
            "power": (self.voltage * self.current)
            if (self.voltage is not None and self.current is not None) else None,
            "session_id": self.session_id,
            "set_current": s.set_current if s else None,
            "target_voltage": s.target_voltage if s else None,
            "termination_current": s.termination_current if s else None,
            "max_seconds": s.max_seconds if s else None,
            "max_ah": s.max_ah if s else None,
            "note": s.note if s else "",
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "elapsed": elapsed,
            "charge_ah": self.charge_ah,
            "energy_wh": self.energy_wh,
            "stop_reason": self.stop_reason,
            "run_id": seq["id"] if seq else None,
            "active_step": active_step,
            "step_count": len(seq["steps"]) if seq else 0,
            "resting": bool(seq and seq["resume_at"] is not None),
            "sequence": self._sequence_view(),
            "max_current": self.max_current,
            "psu_max_current": self.psu_max_current,
            "max_charge_voltage": self.max_charge_voltage,
            "defaults": self.defaults,
        }

    def _sequence_view(self) -> list[dict]:
        """One dict per step in the current/last program: planned parameters plus
        live status, session id, and running totals so the UI can render a card
        (and fetch per-step plot data) for each."""
        seq = self._seq
        if seq is None:
            return []
        running_idx = seq["index"] if self.status == "running" else None
        out: list[dict] = []
        for i, st in enumerate(seq["steps"]):
            res = seq["results"][i]
            status = res["status"] if res else "pending"
            view = {
                "index": i,
                "kind": st.mode,
                "set_current": st.set_current,
                "target_voltage": st.target_voltage,
                "termination_current": st.termination_current,
                "max_seconds": st.max_seconds,
                "max_ah": st.max_ah,
                "note": st.note,
                "status": status,
                "session_id": res["session_id"] if res else None,
                "started_ts": res["started_ts"] if res else None,
                "ended_ts": res["ended_ts"] if res else None,
                "charge_ah": res["charge_ah"] if res else None,
                "energy_wh": res["energy_wh"] if res else None,
                "stop_reason": res["stop_reason"] if res else None,
                "open_circuit_v": res.get("open_circuit_v") if res else None,
                "dcir": res.get("dcir") if res else None,
            }
            # Keep the running step's totals fresh from the live accumulators.
            if i == running_idx:
                view["charge_ah"] = self.charge_ah
                view["energy_wh"] = self.energy_wh
                view["dcir"] = self.dcir
                view["ended_ts"] = None
            out.append(view)
        return out

    async def _emit_state(self) -> None:
        await self._broadcast(self.snapshot())

    # --- public control -------------------------------------------------
    async def start_discharge(
        self, set_current: float, cutoff: float,
        max_seconds: float | None = None, max_ah: float | None = None, note: str = "",
    ) -> dict:
        step = Step(MODE_DISCHARGE, set_current, cutoff, None, max_seconds, max_ah, note)
        return await self.start_sequence([step])

    async def start_charge(
        self, set_current: float, voltage: float, termination_current: float,
        max_seconds: float | None = None, max_ah: float | None = None, note: str = "",
    ) -> dict:
        step = Step(MODE_CHARGE, set_current, voltage, termination_current,
                    max_seconds, max_ah, note)
        return await self.start_sequence([step])

    async def start_sequence(self, steps: list[Step]) -> dict:
        """Run a program: an ordered list of charge / discharge / rest steps. They
        run one at a time, back to back. Add a rest step to let the cell relax (a
        rest at the start gives a clean open-circuit voltage for the next step's
        DCIR). Each step is a logged session, all sharing one run id."""
        async with self._lock:
            if self.status == "running" or (self._seq and self._seq["resume_at"] is not None):
                raise ValueError("a test is already running")
            if not steps:
                raise ValueError("the program has no steps")
            for st in steps:
                self._validate(st)
            run_id = await asyncio.to_thread(self._db.next_cycle_id)
        self._seq = {
            "id": run_id, "steps": list(steps), "index": 0,
            "resume_at": None, "results": [None] * len(steps),
        }
        await self._begin(steps[0])
        return self.snapshot()

    async def stop(self, reason: str = "manual stop") -> dict:
        """User-requested stop: aborts the running step and cancels the rest of
        the program (remaining steps are marked skipped)."""
        async with self._lock:
            if self.status != "running":
                # Not mid-step: may be resting between steps — cancel the program.
                if self._seq is not None:
                    self._seq["resume_at"] = None
                    self._mark_remaining_skipped()
                    await self._emit_state()
                return self.snapshot()
            await self._finalize_locked(reason, "aborted")
            self._record_result("aborted", reason)
            if self._seq is not None:
                self._seq["resume_at"] = None
                self._mark_remaining_skipped()
            closing = self._closing_text("aborted", reason)
        await self._log_event(closing)
        await self._emit_state()
        return self.snapshot()

    # --- start a step (validation + arm), holding the lock --------------
    async def _begin(self, step: Step) -> None:
        cycle_id = self._seq["id"] if self._seq else None
        async with self._lock:
            if self.status == "running":
                raise ValueError("a test is already running")
            self._validate(step)
            source = None if step.mode == MODE_REST else self._source_for[step.mode]

            present = await self._read_present_voltage()
            if step.mode == MODE_DISCHARGE:
                if present is None:
                    raise ValueError("cannot read the cell voltage to start a discharge")
                if present < MIN_PLAUSIBLE_VOLTAGE:
                    raise ValueError(f"measured only {present:.3f} V — is a cell connected?")
                if present <= step.target_voltage:
                    raise ValueError(
                        f"cell is already at {present:.3f} V, at or below the "
                        f"{step.target_voltage:g} V cutoff — nothing to discharge")
            elif step.mode == MODE_CHARGE:
                if present is not None and present >= step.target_voltage:
                    raise ValueError(
                        f"cell is already at {present:.3f} V, at or above the "
                        f"{step.target_voltage:g} V charge voltage — nothing to charge")

            if source is not None:  # rest steps drive nothing
                try:
                    await asyncio.to_thread(source.arm, step.set_current, step.target_voltage)
                    await asyncio.to_thread(source.set_active, True)
                except Exception as exc:
                    try:
                        await asyncio.to_thread(source.set_active, False)
                    except Exception:
                        pass
                    raise ValueError(f"failed to start the {step.mode}: {exc}") from exc

            now = time.time()
            self._source = source
            self.mode = step.mode
            self.step = step
            self.started_ts = now
            self.ended_ts = None
            self.charge_ah = 0.0
            self.energy_wh = 0.0
            self.stop_reason = None
            self.last_error = None
            self._last_ts = None
            self._last_persist = now
            # Rested terminal voltage before this step drives current — the OCV
            # reference for DCIR. (Meaningful only if the cell was rested first;
            # put a rest step before this one for accuracy.)
            self.open_circuit_v = present
            self.dcir = None
            self.session_id = await asyncio.to_thread(
                self._db.start_session, step.mode, now, step.set_current,
                step.target_voltage, step.termination_current, step.max_seconds,
                step.max_ah, step.note, cycle_id, present,
            )
            self.status = "running"
            if self._seq is not None:
                self._seq["resume_at"] = None
                self._seq["results"][self._seq["index"]] = {
                    "session_id": self.session_id, "status": "running",
                    "started_ts": now, "ended_ts": None,
                    "charge_ah": 0.0, "energy_wh": 0.0, "stop_reason": None,
                    "open_circuit_v": present, "dcir": None,
                }

        await self._log_event(self._opening_text(step))
        await self._emit_state()
        log.info("%s %s started: %g A, target %g V", step.mode, self.session_id,
                 step.set_current, step.target_voltage)

    def _validate(self, step: Step) -> None:
        if step.mode == MODE_REST:
            if not (step.max_seconds and step.max_seconds > 0):
                raise ValueError("a rest step needs a duration")
            return
        if not (step.set_current > 0):
            raise ValueError("current must be greater than 0 A")
        # Discharge current is sunk by the load; charge current is sourced by the
        # PSU — each has its own cap.
        cap = self.psu_max_current if step.mode == MODE_CHARGE else self.max_current
        if step.set_current > cap:
            raise ValueError(
                f"{step.mode} current {step.set_current} A exceeds the safety "
                f"limit of {cap} A")
        if not (step.target_voltage > 0):
            raise ValueError("target voltage must be greater than 0 V")
        if step.max_seconds is not None and step.max_seconds <= 0:
            raise ValueError("max duration must be greater than 0 s")
        if step.max_ah is not None and step.max_ah <= 0:
            raise ValueError("max charge must be greater than 0 Ah")
        if step.mode == MODE_CHARGE:
            if step.target_voltage > self.max_charge_voltage:
                raise ValueError(
                    f"charge voltage {step.target_voltage} V exceeds the safety "
                    f"limit of {self.max_charge_voltage} V")
            if not (step.termination_current and step.termination_current > 0):
                raise ValueError("termination current must be greater than 0 A")

    def _voltage_reader(self, fallback: BatterySource | None):
        """Sync callable that reads cell voltage: the DMM if present, else the
        given source's terminal-voltage reading (None if neither can read)."""
        if self._voltmeter is not None:
            return self._voltmeter.read
        if fallback is not None:
            return fallback.measure_voltage
        return None

    async def _read_present_voltage(self) -> float | None:
        reader = self._voltage_reader(self._idle_source)
        if reader is None:
            return None
        try:
            return await asyncio.to_thread(reader)
        except Exception as exc:
            log.warning("could not read present voltage: %s", exc)
            return None

    async def set_sources(self, load: BatterySource, psu: BatterySource,
                          voltmeter: Multimeter | None) -> None:
        """Swap the load / PSU / cell voltmeter (e.g. after changing IPs) and
        re-identify. Refused while a program is running."""
        async with self._lock:
            if self.status == "running" or (self._seq and self._seq["resume_at"] is not None):
                raise ValueError("stop the running program before changing instruments")
            old = [self._load, self._psu, self._voltmeter]
            self._load, self._psu, self._voltmeter = load, psu, voltmeter
            self._source_for = {MODE_DISCHARGE: load, MODE_CHARGE: psu}
            self._idle_source = (
                load if load.supports_idle_voltage else
                (psu if psu.supports_idle_voltage else None)
            )
            self.load_status = self.psu_status = "starting"
            self.load_idn = self.psu_idn = self.voltmeter_idn = None
            self.last_error = None
            self.voltage = self.current = None
        for dev in old:
            if dev is not None and hasattr(dev, "close"):
                try:
                    await asyncio.to_thread(dev.close)
                except Exception:
                    pass
        self.load_idn, self.load_status = await self._identify(self._load)
        self.psu_idn, self.psu_status = await self._identify(self._psu)
        await self._setup_voltmeter()
        await self._emit_state()

    # --- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="battery")

    async def shutdown(self) -> None:
        if self.status == "running":
            await self.stop("app shutdown")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        self.load_idn, self.load_status = await self._identify(self._load)
        self.psu_idn, self.psu_status = await self._identify(self._psu)
        await self._setup_voltmeter()
        await self._emit_state()

        while True:
            running = self.status == "running"
            interval = RUN_INTERVAL if running else IDLE_INTERVAL
            start = time.monotonic()
            try:
                if running:
                    if self.mode == MODE_REST:
                        await self._rest_tick()
                    else:
                        await self._run_tick()
                else:
                    await self._idle_tick()
            except Exception as exc:
                log.warning("battery sample failed: %s", exc)
                await self._note_source_error(exc)
                if self.status == "running":
                    await self._complete("instrument error: " + str(exc), "error")
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _identify(self, source: BatterySource) -> tuple[str | None, str]:
        try:
            return await asyncio.to_thread(source.identify), "connected"
        except Exception as exc:
            log.warning("%s identify failed: %s", source.name, exc)
            self.last_error = str(exc)
            return None, "error"

    async def _setup_voltmeter(self) -> None:
        """Identify the cell DMM and put it in DC-voltage mode (autorange). The
        DMM is dedicated to the cell, so this is safe to configure once."""
        if self._voltmeter is None:
            return
        try:
            self.voltmeter_idn = await asyncio.to_thread(self._voltmeter.identify)
            await asyncio.to_thread(self._voltmeter.configure, "CONF:VOLT:DC", "AUTO")
            log.info("cell voltmeter ready: %s", self.voltmeter_idn)
        except Exception as exc:
            log.warning("cell voltmeter setup failed: %s", exc)
            self.last_error = str(exc)

    # --- ticks ----------------------------------------------------------
    async def _idle_tick(self) -> None:
        # Between program steps, wait out the rest interval then start the next.
        if self._seq is not None and self._seq["resume_at"] is not None:
            if time.time() >= self._seq["resume_at"]:
                await self._start_next_step()
                return

        reader = self._voltage_reader(self._idle_source)
        if reader is not None:
            v = await asyncio.to_thread(reader)
            self.voltage, self.current = v, 0.0
            if self._idle_source is not None:
                self._mark_connected(self._idle_source)
            await self._broadcast({
                "type": "battery", "ts": time.time(), "voltage": v, "current": 0.0,
                "power": 0.0, "charge_ah": self.charge_ah, "energy_wh": self.energy_wh,
                "running": False, "mode": self.mode,
            })

    async def _run_tick(self) -> None:
        source = self._source
        assert source is not None and self.step is not None
        # Voltage from the DMM (Kelvin) when present, else the source; current
        # always from the active source.
        v = await asyncio.to_thread(self._voltage_reader(source))
        i = await asyncio.to_thread(source.measure_current)
        now = time.time()
        p = v * i

        # Trapezoidal integration of charge & energy over each interval.
        if self._last_ts is not None and self.current is not None:
            dt = now - self._last_ts
            avg_i = (self.current + i) / 2.0
            avg_p = ((self.voltage or v) * self.current + p) / 2.0
            self.charge_ah += avg_i * dt / 3600.0
            self.energy_wh += avg_p * dt / 3600.0
        self._last_ts = now
        self.voltage, self.current = v, i
        self._mark_connected(source)

        # DCIR from the first loaded sample: |OCV - V_loaded| / I. Done once,
        # near the step start, so it reflects mostly ohmic + early polarization.
        # TODO: average the first N loaded samples (e.g. 3) instead of a single
        # one, to cut measurement noise from the estimate. Keeps the same meaning.
        if self.dcir is None and self.open_circuit_v is not None and i > 0.05:
            self.dcir = abs(self.open_circuit_v - v) / i
            if self._seq is not None:
                self._seq["results"][self._seq["index"]]["dcir"] = self.dcir
            if self.session_id is not None:
                await asyncio.to_thread(self._db.update_dcir, self.session_id, self.dcir)

        # Both channels share a timestamp so they pair up exactly.
        await asyncio.to_thread(self._db.insert_reading, now, source.name, Q_VOLTAGE, v, "V")
        await asyncio.to_thread(self._db.insert_reading, now, source.name, Q_CURRENT, i, "A")

        await self._broadcast({
            "type": "battery", "ts": now, "voltage": v, "current": i, "power": p,
            "charge_ah": self.charge_ah, "energy_wh": self.energy_wh,
            "running": True, "mode": self.mode, "session_id": self.session_id,
            "dcir": self.dcir, "open_circuit_v": self.open_circuit_v,
        })

        if now - self._last_persist >= PERSIST_EVERY and self.session_id is not None:
            self._last_persist = now
            await asyncio.to_thread(
                self._db.update_session, self.session_id, self.charge_ah, self.energy_wh)

        stop = self._check_stop(v, i, now)
        if stop is not None:
            await self._complete(*stop)

    async def _rest_tick(self) -> None:
        """A rest step: drive nothing, log the cell relaxing (V, I=0) so the card
        shows the relaxation curve, and end after the configured duration."""
        step = self.step
        assert step is not None
        now = time.time()
        reader = self._voltage_reader(self._idle_source)
        v = await asyncio.to_thread(reader) if reader is not None else None
        self.voltage, self.current = v, 0.0
        if v is not None:
            if self._idle_source is not None:
                self._mark_connected(self._idle_source)
            name = self._idle_source.name if self._idle_source is not None else "rest"
            await asyncio.to_thread(self._db.insert_reading, now, name, Q_VOLTAGE, v, "V")
            await asyncio.to_thread(self._db.insert_reading, now, name, Q_CURRENT, 0.0, "A")
        await self._broadcast({
            "type": "battery", "ts": now, "voltage": v, "current": 0.0, "power": 0.0,
            "charge_ah": 0.0, "energy_wh": 0.0, "running": True, "mode": MODE_REST,
            "session_id": self.session_id, "dcir": None, "open_circuit_v": self.open_circuit_v,
        })
        if (now - self.started_ts) >= step.max_seconds:
            await self._complete(f"rested {step.max_seconds:g} s", "complete")

    def _check_stop(self, v: float, i: float, now: float) -> tuple[str, str] | None:
        step = self.step
        assert step is not None
        if step.mode == MODE_DISCHARGE:
            if v <= step.target_voltage:
                return (f"reached {step.target_voltage:g} V cutoff", "complete")
        else:  # charge: CV taper
            in_cv = v >= step.target_voltage - CV_VOLTAGE_EPS
            settled = (now - self.started_ts) >= MIN_CHARGE_SECONDS
            if settled and in_cv and i <= step.termination_current:
                return (f"current tapered to {step.termination_current:g} A "
                        f"at {step.target_voltage:g} V", "complete")
        if step.max_seconds is not None and (now - self.started_ts) >= step.max_seconds:
            return (f"reached {step.max_seconds:g} s time limit", "complete")
        if step.max_ah is not None and self.charge_ah >= step.max_ah:
            return (f"reached {step.max_ah:g} Ah limit", "complete")
        return None

    # --- stopping -------------------------------------------------------
    async def _complete(self, reason: str, status: str) -> None:
        """A step ended on its own (target/cap/error). Finalize, record its
        result, then advance to the next step (resting first) unless it errored."""
        async with self._lock:
            if self.status != "running":
                return
            await self._finalize_locked(reason, status)
            self._record_result(status, reason)
            closing = self._closing_text(status, reason)
            seq = self._seq
            if seq is not None:
                idx = seq["index"]
                if status == "complete":
                    seq["index"] += 1
                    # Steps run back to back; the idle loop starts the next one on
                    # its next tick. (Rests are explicit steps, not gaps here.)
                    seq["resume_at"] = (
                        time.time() if seq["index"] < len(seq["steps"]) else None
                    )
                else:  # error — abandon the rest of the program
                    seq["resume_at"] = None
                    self._mark_remaining_skipped()
        await self._log_event(closing)
        await self._emit_state()

    def _record_result(self, status: str, reason: str) -> None:
        """Snapshot the just-finished step's outcome into the program results."""
        seq = self._seq
        if seq is None:
            return
        seq["results"][seq["index"]] = {
            "session_id": self.session_id, "status": status,
            "started_ts": self.started_ts, "ended_ts": self.ended_ts,
            "charge_ah": self.charge_ah, "energy_wh": self.energy_wh,
            "stop_reason": reason,
            "open_circuit_v": self.open_circuit_v, "dcir": self.dcir,
        }

    def _mark_remaining_skipped(self) -> None:
        """Steps that never ran (program stopped/errored early) show as skipped."""
        seq = self._seq
        if seq is None:
            return
        for i in range(len(seq["steps"])):
            if seq["results"][i] is None:
                seq["results"][i] = {
                    "session_id": None, "status": "skipped",
                    "started_ts": None, "ended_ts": None,
                    "charge_ah": None, "energy_wh": None, "stop_reason": "skipped",
                }

    async def _finalize_locked(self, reason: str, status: str) -> None:
        if self._source is not None:
            src = self._source
            try:
                await asyncio.to_thread(src.set_active, False)
            except Exception as exc:
                log.error("failed to switch %s off: %s", src.name, exc)
                self.last_error = f"source did not switch off cleanly: {exc}"
            # Confirm the output actually dropped to ~0 A. Best-effort: if the
            # instrument is unresponsive (often why we're here), say so loudly —
            # an uncontrolled source on the cell is the thing to never miss.
            try:
                resid = await asyncio.to_thread(src.measure_current)
                if abs(resid) > VERIFY_OFF_CURRENT:
                    msg = f"{src.name} still drawing {resid:.3f} A after OFF — CHECK THE BENCH"
                    log.error(msg)
                    self.last_error = msg
            except Exception as exc:
                msg = f"could not confirm {src.name} switched off ({exc}) — CHECK THE BENCH"
                log.warning(msg)
                if self.last_error is None:
                    self.last_error = msg
        self.ended_ts = time.time()
        self.status = status
        self.stop_reason = reason
        self.current = 0.0
        self._source = None
        if self.session_id is not None:
            try:
                await asyncio.to_thread(
                    self._db.finish_session, self.session_id, self.ended_ts,
                    self.charge_ah, self.energy_wh, status, reason)
            except Exception as exc:
                log.error("failed to finalize session %s: %s", self.session_id, exc)
        log.info("%s %s ended (%s): %.4f Ah, %.4f Wh", self.mode, self.session_id,
                 reason, self.charge_ah, self.energy_wh)

    async def _start_next_step(self) -> None:
        seq = self._seq
        if seq is None:
            return
        step = seq["steps"][seq["index"]]
        try:
            await self._begin(step)
        except ValueError as exc:
            # e.g. the cell can't meet the next step's preconditions — end here.
            log.warning("program %s stopped before step %d: %s",
                        seq["id"], seq["index"] + 1, exc)
            seq["resume_at"] = None
            seq["results"][seq["index"]] = {
                "session_id": None, "status": "error",
                "started_ts": None, "ended_ts": None,
                "charge_ah": None, "energy_wh": None, "stop_reason": str(exc),
            }
            self._mark_remaining_skipped()
            self.last_error = str(exc)
            await self._log_event(f"Step {seq['index'] + 1} skipped: {exc}")
            await self._emit_state()

    # --- helpers --------------------------------------------------------
    def _mark_connected(self, source: BatterySource) -> None:
        attr = "load_status" if source is self._load else "psu_status"
        if getattr(self, attr) != "connected" or self.last_error is not None:
            setattr(self, attr, "connected")
            self.last_error = None

    async def _note_source_error(self, exc: Exception) -> None:
        src = self._source or self._idle_source
        attr = "psu_status" if src is self._psu else "load_status"
        if getattr(self, attr) != "error" or self.last_error != str(exc):
            setattr(self, attr, "error")
            self.last_error = str(exc)
            await self._emit_state()

    def _opening_text(self, step: Step) -> str:
        seq = self._seq
        prefix = (f"Step {seq['index'] + 1}/{len(seq['steps'])}: "
                  if seq and len(seq["steps"]) > 1 else "")
        if step.mode == MODE_DISCHARGE:
            body = f"Discharge started: {step.set_current:g} A → {step.target_voltage:g} V cutoff"
        elif step.mode == MODE_CHARGE:
            body = (f"Charge started: {step.set_current:g} A → {step.target_voltage:g} V, "
                    f"taper {step.termination_current:g} A")
        else:
            body = f"Rest started: {step.max_seconds:g} s"
        return prefix + body + (f" ({step.note})" if step.note else "")

    def _closing_text(self, status: str, reason: str) -> str:
        verb = (self.mode or "test").capitalize()
        if self.mode == MODE_REST:
            return f"Rest {status} ({reason})"
        return (f"{verb} {status}: {self.charge_ah:.3f} Ah, "
                f"{self.energy_wh:.3f} Wh ({reason})")

    async def _log_event(self, text: str) -> None:
        ts = time.time()
        await asyncio.to_thread(self._db.insert_event, ts, text)
        await self._broadcast({"type": "event", "ts": ts, "text": text})
