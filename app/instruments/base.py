"""Instrument abstractions.

The Multimeter protocol uses a configure-once / read-many model: `configure`
selects the function and range (sent only when it changes), then `read` triggers
and returns one measurement. Power supplies and electronic loads (with
set/control methods) will implement sibling protocols sharing ScpiSocket.
"""
from __future__ import annotations

from typing import Protocol


class Multimeter(Protocol):
    name: str

    def identify(self) -> str:
        """Return the instrument's *IDN? string (or a description for mocks)."""
        ...

    def configure(self, conf_command: str, range_value: str) -> None:
        """Select a measurement function and range. range_value 'AUTO' autoranges."""
        ...

    def read(self) -> float:
        """Trigger and return one measurement in the configured function's unit."""
        ...


class BatterySource(Protocol):
    """A device wired to a battery that both drives current and measures the
    cell's terminal voltage and current — either an electronic load (discharge,
    constant current) or a power supply (charge, constant current / constant
    voltage). Sharing one interface lets a single controller run both
    directions and chain them into charge/discharge cycles.

    Sign convention for measure_current: positive amps, regardless of direction
    (the controller knows whether it is charging or discharging from context).
    """

    name: str
    # True if the device reads a meaningful terminal voltage while inactive
    # (an electronic load sees the cell's resting voltage with its input off; a
    # power supply reads ~0 at its output when off).
    supports_idle_voltage: bool

    def identify(self) -> str:
        """Return the instrument's *IDN? string (or a description for mocks)."""
        ...

    def arm(self, current: float, voltage: float | None = None) -> None:
        """Program the operating point without activating: a load sets its CC
        sink current (voltage ignored); a supply sets its CC current limit and
        CV voltage. Call set_active(True) to actually drive current."""
        ...

    def set_active(self, on: bool) -> None:
        """Connect (True) or disconnect (False) the device from the cell
        (load input / supply output)."""
        ...

    def measure_voltage(self) -> float:
        """Measure terminal voltage (volts)."""
        ...

    def measure_current(self) -> float:
        """Measure current magnitude (amps); ~0 when inactive."""
        ...
