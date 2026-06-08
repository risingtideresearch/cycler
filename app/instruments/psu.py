"""Siglent SPD-series programmable DC power supply over SCPI/LAN.

Used to charge a cell with the usual CC-CV profile: set the CV voltage to the
cell's charge voltage and the current to the charge-current limit, switch the
output on, and the supply holds constant current until the cell nears the set
voltage, then holds constant voltage while the current tapers. The charge
controller terminates when that taper current drops below a threshold.

SCPI reference (SPD3303X / SPD1000X Programming Guide), per channel CHn:
  *IDN?
  CH1:VOLTage <v>          set the CV voltage setpoint
  CH1:CURRent <a>          set the CC current limit
  OUTPut CH1,{ON|OFF}      enable / disable the output
  MEASure:VOLTage? CH1     measure output voltage
  MEASure:CURRent? CH1     measure output current

A single-channel SPD (e.g. SPD1168X) still addresses CH1.
"""
from __future__ import annotations

from .scpi import ScpiError, ScpiSocket


class SiglentPSU:
    # A supply reads ~0 V at its output when off, so it cannot report the cell's
    # resting voltage; idle monitoring falls back to the load if present.
    supports_idle_voltage = False

    def __init__(self, host: str, port: int = 5025, channel: str = "CH1") -> None:
        self.name = f"psu@{host}"
        self._scpi = ScpiSocket(host, port)
        self._ch = channel

    def identify(self) -> str:
        return self._scpi.query("*IDN?")

    def arm(self, current: float, voltage: float | None = None) -> None:
        if voltage is None:
            raise ValueError("a charge voltage is required to arm the power supply")
        # SAFETY TODO: set a hardware over-voltage protection trip here as an
        # independent backstop to the software cap, e.g.
        #   self._scpi.write(f"{self._ch}:VOLTage:PROTection {ovp:.3f}")
        # where ovp = HARD_MAX_CHARGE_VOLTAGE plus a small margin. The controller
        # already refuses setpoints above the cap and a CV supply can't exceed its
        # setpoint, so this is defense-in-depth. VERIFY the exact SCPI in the SPD
        # programming manual before enabling — basic SPD single-output supplies
        # may not expose settable OVP, and an unknown command could fault the unit.
        self._scpi.write(f"{self._ch}:VOLTage {voltage:.3f}")
        self.set_current(current)

    def set_current(self, current: float) -> None:
        self._scpi.write(f"{self._ch}:CURRent {current:.3f}")

    def set_voltage(self, voltage: float) -> None:
        self._scpi.write(f"{self._ch}:VOLTage {voltage:.3f}")

    def set_active(self, on: bool) -> None:
        self._scpi.write(f"OUTPut {self._ch},{'ON' if on else 'OFF'}")

    def measure_voltage(self) -> float:
        return self._query_float(f"MEASure:VOLTage? {self._ch}")

    def measure_current(self) -> float:
        return self._query_float(f"MEASure:CURRent? {self._ch}")

    def _query_float(self, command: str) -> float:
        raw = self._scpi.query(command)
        try:
            return float(raw)
        except ValueError as exc:
            raise ScpiError(f"unparseable response to {command!r}: {raw!r}") from exc

    def close(self) -> None:
        self._scpi.close()
