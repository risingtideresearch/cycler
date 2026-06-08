"""Siglent SDL1000X-series DC electronic load over SCPI/LAN.

Operated in constant-current (CC) mode for battery discharge: select CC,
program a sink current, switch the input on, then poll terminal voltage and
current each tick. The load measures its own terminals, so a discharge test
needs no separate multimeter.

SCPI reference (SDL1000X Programming Guide):
  :SOURce:FUNCtion CURRent            select CC (static) mode
  :SOURce:CURRent:IRANGe <a>          current measurement range (5 A or 30 A)
  :SOURce:CURRent:VRANGe <v>          voltage measurement range (36 V or 150 V)
  :SOURce:CURRent:LEVel:IMMediate <a> programmed sink current
  :SOURce:INPut:STATe {ON|OFF}        connect / disconnect the input
  :MEASure:VOLTage?  / :MEASure:CURRent?  / :MEASure:POWer?
"""
from __future__ import annotations

from .scpi import ScpiError, ScpiSocket

# The SDL1000X has two hardware ranges per quantity; we pick the smaller one
# when it comfortably covers the setpoint for better resolution, else the large
# one. A single LFP cell sits far below the low voltage range, so 36 V is always
# fine; mains-series packs would need VRANGE_HIGH instead.
IRANGE_LOW, IRANGE_HIGH = 5.0, 30.0
VRANGE_LOW = 36.0


class SiglentLoad:
    # An electronic load reads the cell's resting voltage even with its input
    # off, so it can monitor open-circuit voltage while idle.
    supports_idle_voltage = True

    def __init__(self, host: str, port: int = 5025, voltage_range: float = VRANGE_LOW) -> None:
        self.name = f"load@{host}"
        self._scpi = ScpiSocket(host, port)
        self._voltage_range = voltage_range

    def identify(self) -> str:
        return self._scpi.query("*IDN?")

    def arm(self, current: float, voltage: float | None = None) -> None:
        # voltage is unused by the load (the discharge cutoff is enforced in
        # software); kept for the shared BatterySource interface.
        irange = IRANGE_LOW if current <= IRANGE_LOW else IRANGE_HIGH
        self._scpi.write(":SOURce:FUNCtion CURRent")
        self._scpi.write(f":SOURce:CURRent:IRANGe {irange}")
        self._scpi.write(f":SOURce:CURRent:VRANGe {self._voltage_range}")
        self.set_current(current)

    def set_current(self, current: float) -> None:
        self._scpi.write(f":SOURce:CURRent:LEVel:IMMediate {current:.4f}")

    def set_active(self, on: bool) -> None:
        self._scpi.write(f":SOURce:INPut:STATe {'ON' if on else 'OFF'}")

    def measure_voltage(self) -> float:
        return self._query_float(":MEASure:VOLTage?")

    def measure_current(self) -> float:
        return self._query_float(":MEASure:CURRent?")

    def _query_float(self, command: str) -> float:
        raw = self._scpi.query(command)
        try:
            return float(raw)
        except ValueError as exc:
            raise ScpiError(f"unparseable response to {command!r}: {raw!r}") from exc

    def close(self) -> None:
        self._scpi.close()
