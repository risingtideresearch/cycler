"""A shared simulated LFP cell.

The mock electronic load and mock power supply both hold a reference to one of
these, so an offline charge/discharge cycle is self-consistent: the load drains
the same state-of-charge the supply fills. Only one instrument is ever active at
a time (load input on XOR supply output on), so only the active one advances the
cell's state.

The open-circuit-voltage curve is a simplified LFP shape: a short shoulder up to
~3.65 V at full, a long flat plateau around 3.2-3.3 V, and a steep knee toward
empty. A single internal resistance gives sag under discharge and rise under
charge, which (with the shoulder reaching the charge voltage) makes the CC-CV
charge current taper to zero near full, just like a real cell.
"""
from __future__ import annotations


class MockCell:
    def __init__(self, capacity_ah: float = 3.0, internal_ohms: float = 0.08,
                 soc: float = 1.0) -> None:
        self.capacity_ah = max(capacity_ah, 1e-3)
        self.rint = internal_ohms
        self.soc = max(0.0, min(soc, 1.0))

    def ocv(self) -> float:
        """Open-circuit (rested) terminal voltage at the current state of charge."""
        s = self.soc
        if s >= 0.90:
            return 3.30 + (s - 0.90) / 0.10 * (3.65 - 3.30)
        if s >= 0.10:
            return 3.20 + (s - 0.10) / 0.80 * (3.30 - 3.20)
        return 2.30 + (s / 0.10) * (3.20 - 2.30)

    def advance(self, signed_current: float, dt: float) -> None:
        """Integrate charge over dt seconds. signed_current > 0 charges the cell
        (current flowing in), < 0 discharges it."""
        self.soc = max(0.0, min(1.0, self.soc + signed_current * dt / 3600.0 / self.capacity_ah))

    def terminal_voltage(self, signed_current: float) -> float:
        """Terminal voltage under a signed current (charge raises it above OCV,
        discharge pulls it below)."""
        return max(0.0, self.ocv() + signed_current * self.rint)
