"""Simulated electronic load, draining a shared MockCell, so the discharge test
runs without hardware. See mock_cell.py for the cell model.

Tip: set SIGLENT_MOCK_CELL_AH small (e.g. 0.05) for a fast end-to-end demo.
"""
from __future__ import annotations

import random
import time

from .mock_cell import MockCell


class MockLoad:
    supports_idle_voltage = True

    def __init__(self, cell: MockCell) -> None:
        self.name = "load@mock"
        self._cell = cell
        self._set_current = 0.0
        self._active = False
        self._last = time.monotonic()

    def identify(self) -> str:
        return ("Siglent,MOCK-SDL,SIM0001,1.0 "
                "(simulated LFP cell — set SIGLENT_LOAD_HOST for real hardware)")

    def arm(self, current: float, voltage: float | None = None) -> None:
        self._set_current = current

    def set_current(self, current: float) -> None:
        self._set_current = current

    def set_active(self, on: bool) -> None:
        self._advance()  # settle SoC up to the switch instant
        self._active = on
        self._last = time.monotonic()

    def measure_voltage(self) -> float:
        self._advance()
        sink = self._set_current if self._active else 0.0
        # Discharge → negative signed current pulls terminal below OCV.
        return self._cell.terminal_voltage(-sink) + random.uniform(-0.002, 0.002)

    def measure_current(self) -> float:
        if not self._active:
            return random.uniform(-0.0005, 0.0005)
        return self._set_current + random.uniform(-0.001, 0.001)

    def _advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if self._active and self._set_current > 0:
            self._cell.advance(-self._set_current, dt)  # discharge

    def close(self) -> None:
        pass
