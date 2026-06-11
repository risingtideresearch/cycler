"""Runtime configuration, read from a TOML file.

The app looks for ``config.toml`` first in the current working directory, then
next to the project root (alongside the README). If neither exists, every value
falls back to a sensible default so the app boots with no setup at all. To point
at real instruments, copy ``config.toml.example`` to ``config.toml`` and fill in
the host addresses (see README).

The file is grouped into tables; any table or key may be omitted to keep its
default. Leaving a ``host`` empty or absent selects the simulated instrument.
"""
from __future__ import annotations

import dataclasses
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("siglent.config")

# config.toml is searched for here, in order; the first that exists wins.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEARCH_PATHS = (Path("config.toml"), _PROJECT_ROOT / "config.toml")

# Absolute safety ceilings — NOT configurable. config.toml values above these are
# clamped down (with a warning) at load, and BatteryController clamps again, so no
# config edit or typo can command a dangerous setpoint. Tuned for LFP cells on the
# SPD1305X supply + SDL1000X load. Change these in code, deliberately, only if the
# chemistry or hardware changes.
HARD_MAX_CHARGE_VOLTAGE = 3.7   # LFP fully charges ~3.65 V; never apply more
HARD_MAX_PSU_CURRENT = 5.0      # SPD1305X rating
HARD_MAX_LOAD_CURRENT = 30.0    # SDL1000X-E rating


@dataclass(frozen=True)
class Settings:
    # DMM connection. When set, the DMM is the battery's cell voltmeter (wire it
    # Kelvin at the terminals). When None, the battery uses the load/PSU voltage.
    dmm_host: str | None = None
    dmm_port: int = 5025

    # Where to store logged readings.
    db_path: str = "siglent.db"

    # Electronic load connection (for battery discharge tests). If unset we fall
    # back to a simulated LFP cell + load so the test UI works without hardware.
    load_host: str | None = None
    load_port: int = 5025

    # Power supply connection (for charging). If unset we fall back to a
    # simulated supply driving the same simulated cell as the mock load.
    psu_host: str | None = None
    psu_port: int = 5025
    psu_channel: str = "CH1"

    # Discharge defaults and safety cap (current the load may be told to sink).
    discharge_current: float = 1.0
    cutoff_voltage: float = 2.5
    load_max_current: float = 30.0

    # Charge defaults and safety caps (the CV voltage and current the supply may
    # be set to). SPD1305X is 30 V / 5 A.
    charge_current: float = 1.0
    charge_voltage: float = 3.65
    termination_current: float = 0.05
    psu_max_voltage: float = 3.7
    psu_max_current: float = 5.0

    # Second instrument set, used by the standalone DCIR tester app (the cycler
    # uses the [dmm]/[load]/[psu] set above). Same semantics: empty → simulate.
    dmm2_host: str | None = None
    dmm2_port: int = 5025
    load2_host: str | None = None
    load2_port: int = 5025

    # DCIR tester app (the separate app on port 8002): load-pulse parameters.
    # It waits for a connected cell to settle, fires one CC pulse on load 2, and
    # logs DCIR = (V_open - V_loaded) / I. Results go in their own DB file so the
    # two apps never contend for one SQLite database across processes.
    dcir_pulse_current: float = 10.0   # CC pulse amplitude (A); clamped ≤ load ceiling
    dcir_pulse_seconds: float = 2.0    # pulse length (s)
    dcir_settle_seconds: float = 10.0  # voltage must hold steady this long before a pulse
    dcir_settle_band: float = 0.003    # "steady" = spread within this many volts
    dcir_min_voltage: float = 2.8      # never pulse a cell resting below this (V)
    dcir_db_path: str = "dcir.db"

    # Optional Discord webhook for run notifications (step start/end, errors).
    # Empty/absent disables it. The URL is a credential — config.toml is gitignored.
    discord_webhook: str | None = None

    # Top-balance monitor (the separate read-only app). Alerts as voltage nears
    # the target; reuses the [dmm] host and [discord] webhook above.
    monitor_target_voltage: float = 3.65
    monitor_warn_voltage: float = 3.60
    monitor_interval: float = 1.0

    # Simulated-cell parameters (only used when load_host/psu_host are unset).
    mock_cell_ah: float = 3.0
    mock_cell_rint: float = 0.08

    def __post_init__(self) -> None:
        # Clamp the safety caps to the hard, non-configurable ceilings.
        for field, ceiling in (
            ("psu_max_voltage", HARD_MAX_CHARGE_VOLTAGE),
            ("psu_max_current", HARD_MAX_PSU_CURRENT),
            ("load_max_current", HARD_MAX_LOAD_CURRENT),
            ("dcir_pulse_current", HARD_MAX_LOAD_CURRENT),
        ):
            if getattr(self, field) > ceiling:
                log.warning("config %s=%g exceeds the hard safety ceiling %g — clamping to %g",
                            field, getattr(self, field), ceiling, ceiling)
                object.__setattr__(self, field, ceiling)  # frozen dataclass

    @property
    def use_mock(self) -> bool:
        return self.dmm_host is None

    @property
    def use_mock_load(self) -> bool:
        return self.load_host is None

    @property
    def use_mock_psu(self) -> bool:
        return self.psu_host is None


# Maps a Settings field to its (table, key) location in config.toml. Grouping the
# flat dataclass into tables keeps the file readable without complicating the
# rest of the app, which only ever sees the flat Settings object.
_LAYOUT: dict[str, tuple[str, str]] = {
    "dmm_host": ("dmm", "host"),
    "dmm_port": ("dmm", "port"),
    "db_path": ("storage", "db_path"),
    "load_host": ("load", "host"),
    "load_port": ("load", "port"),
    "load_max_current": ("load", "max_current"),
    "psu_host": ("psu", "host"),
    "psu_port": ("psu", "port"),
    "psu_channel": ("psu", "channel"),
    "psu_max_voltage": ("psu", "max_voltage"),
    "psu_max_current": ("psu", "max_current"),
    "discharge_current": ("discharge", "current"),
    "cutoff_voltage": ("discharge", "cutoff"),
    "charge_current": ("charge", "current"),
    "charge_voltage": ("charge", "voltage"),
    "termination_current": ("charge", "termination_current"),
    "dmm2_host": ("dmm2", "host"),
    "dmm2_port": ("dmm2", "port"),
    "load2_host": ("load2", "host"),
    "load2_port": ("load2", "port"),
    "dcir_pulse_current": ("dcir", "pulse_current"),
    "dcir_pulse_seconds": ("dcir", "pulse_seconds"),
    "dcir_settle_seconds": ("dcir", "settle_seconds"),
    "dcir_settle_band": ("dcir", "settle_band"),
    "dcir_min_voltage": ("dcir", "min_voltage"),
    "dcir_db_path": ("dcir", "db_path"),
    "discord_webhook": ("discord", "webhook_url"),
    "monitor_target_voltage": ("monitor", "target_voltage"),
    "monitor_warn_voltage": ("monitor", "warn_voltage"),
    "monitor_interval": ("monitor", "interval"),
    "mock_cell_ah": ("mock_cell", "ah"),
    "mock_cell_rint": ("mock_cell", "rint"),
}

# Fields where an empty string means "unset" (→ None / disabled): instrument
# hosts select the simulator, an empty webhook disables notifications. Written
# back as `key = ""` so the generated file shows where to fill them in.
_BLANK_FIELDS = {"dmm_host", "load_host", "psu_host", "dmm2_host", "load2_host",
                 "discord_webhook"}


def _find_config() -> Path | None:
    for path in _SEARCH_PATHS:
        if path.is_file():
            return path
    return None


def load_settings(path: Path | None = None) -> Settings:
    """Build Settings from a TOML file. With no path, search the default
    locations; if nothing is found, return all-default Settings."""
    if path is None:
        path = _find_config()
    if path is None:
        log.info("no config.toml found — using built-in defaults (simulated instruments)")
        return Settings()

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    log.info("loaded configuration from %s", path)

    overrides: dict[str, object] = {}
    for name, (table, key) in _LAYOUT.items():
        section = data.get(table)
        if not isinstance(section, dict) or key not in section:
            continue
        value = section[key]
        if name in _BLANK_FIELDS and value == "":
            continue  # empty → keep the default (None: simulate / disabled)
        overrides[name] = value

    _warn_unknown(data)
    return Settings(**overrides)


def _warn_unknown(data: dict) -> None:
    """Log keys in the file that don't map to any setting, to catch typos."""
    known: dict[str, set[str]] = {}
    for table, key in _LAYOUT.values():
        known.setdefault(table, set()).add(key)
    for table, section in data.items():
        if not isinstance(section, dict):
            log.warning("ignoring unexpected top-level key %r in config.toml", table)
            continue
        for key in section:
            if key not in known.get(table, set()):
                log.warning("ignoring unknown config key [%s].%s", table, key)


# Table order for the generated file (keeps it readable).
_TABLE_ORDER = ["dmm", "storage", "load", "psu", "dmm2", "load2", "discharge",
                "charge", "dcir", "discord", "monitor", "mock_cell"]


def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(v) if isinstance(v, float) else str(v)


def _render_toml(values: dict) -> str:
    by_table: dict[str, list[tuple[str, str]]] = {}
    for field, (table, key) in _LAYOUT.items():
        by_table.setdefault(table, []).append((key, field))
    lines = ["# Written by the app's Instruments panel. Edit by hand if you like;",
             "# the Instruments panel rewrites this file when you save.", ""]
    for table in _TABLE_ORDER:
        if table not in by_table:
            continue
        lines.append(f"[{table}]")
        for key, field in by_table[table]:
            v = values.get(field)
            if v is None:
                if field in _BLANK_FIELDS:
                    lines.append(f'{key} = ""')   # show the key, blank = unset
                continue
            lines.append(f"{key} = {_toml_scalar(v)}")
        lines.append("")
    return "\n".join(lines)


def persist_config(updates: dict) -> Path:
    """Write config.toml from the current settings plus `updates` (Settings field
    names). Returns the path written. Does not mutate the running `settings`."""
    values = dataclasses.asdict(settings)
    values.update(updates)
    target = _find_config() or (_PROJECT_ROOT / "config.toml")
    target.write_text(_render_toml(values))
    log.info("wrote configuration to %s", target)
    return target


settings = load_settings()
