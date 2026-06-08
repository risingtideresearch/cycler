"""Siglent SDM-series digital multimeter over SCPI/LAN.

Configure-once / read-many: send CONF:<func> [range] only when the selection
changes, then READ? each sample (which triggers a fresh measurement and returns
it). This is faster and more correct than re-running MEAS? on every poll.
"""
from __future__ import annotations

from .scpi import ScpiError, ScpiSocket


class SiglentMultimeter:
    def __init__(self, host: str, port: int = 5025) -> None:
        self.name = f"dmm@{host}"
        self._scpi = ScpiSocket(host, port)

    def identify(self) -> str:
        return self._scpi.query("*IDN?")

    def configure(self, conf_command: str, range_value: str) -> None:
        if range_value and range_value != "AUTO":
            self._scpi.write(f"{conf_command} {range_value}")
        else:
            # Bare CONFigure selects the function and enables autoranging.
            self._scpi.write(conf_command)

    def read(self) -> float:
        raw = self._scpi.query("READ?")
        try:
            return float(raw)
        except ValueError as exc:
            raise ScpiError(f"unparseable reading: {raw!r}") from exc

    def close(self) -> None:
        self._scpi.close()
