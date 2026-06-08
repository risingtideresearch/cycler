"""Simulated power supply, charging a shared MockCell with a CC-CV profile, so
the charge test (and full cycles) run without hardware. See mock_cell.py.

Operating point at the current state of charge: the supply pushes its full
current limit (CC) until the cell's terminal voltage would exceed the set
voltage, then holds the set voltage (CV) while the current tapers as the cell
fills. Because the cell's OCV shoulder reaches the charge voltage near full, the
CV current decays to ~0, so a taper-current termination works just like real
hardware.
"""
from __future__ import annotations

import random
import time

from .mock_cell import MockCell


class MockPSU:
    supports_idle_voltage = False

    def __init__(self, cell: MockCell) -> None:
        self.name = "psu@mock"
        self._cell = cell
        self._vset = 0.0
        self._ilim = 0.0
        self._active = False
        self._last = time.monotonic()

    def identify(self) -> str:
        return ("Siglent,MOCK-SPD,SIM0001,1.0 "
                "(simulated supply — set SIGLENT_PSU_HOST for real hardware)")

    def arm(self, current: float, voltage: float | None = None) -> None:
        self._ilim = current
        if voltage is not None:
            self._vset = voltage

    def set_current(self, current: float) -> None:
        self._ilim = current

    def set_voltage(self, voltage: float) -> None:
        self._vset = voltage

    def set_active(self, on: bool) -> None:
        self._advance()
        self._active = on
        self._last = time.monotonic()

    def measure_voltage(self) -> float:
        self._advance()
        v, _ = self._operating_point()
        return v + (random.uniform(-0.002, 0.002) if self._active else 0.0)

    def measure_current(self) -> float:
        self._advance()
        _, i = self._operating_point()
        return i + (random.uniform(-0.001, 0.001) if self._active else 0.0)

    def _operating_point(self) -> tuple[float, float]:
        """Return (terminal_voltage, current) the supply sources right now."""
        if not self._active:
            return 0.0, 0.0
        ocv = self._cell.ocv()
        v_cc = ocv + self._ilim * self._cell.rint
        if v_cc <= self._vset:
            return v_cc, self._ilim          # constant-current phase
        i = max(0.0, (self._vset - ocv) / self._cell.rint)
        return self._vset, i                 # constant-voltage taper

    def _advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if self._active:
            _, i = self._operating_point()
            self._cell.advance(i, dt)        # charge

    def close(self) -> None:
        pass
