"""SQLite storage for logged readings.

The schema is general (instrument / quantity / unit) so power-supply and load
readings can be logged into the same table later without migration.
"""
from __future__ import annotations

import sqlite3
import threading


class Database:
    def __init__(self, path: str) -> None:
        # check_same_thread=False because asyncio.to_thread may run inserts and
        # queries on different worker threads; a lock serializes access.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,   -- unix epoch seconds
                    instrument  TEXT    NOT NULL,
                    quantity    TEXT    NOT NULL,   -- e.g. 'voltage_dc'
                    value       REAL    NOT NULL,
                    unit        TEXT    NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts    REAL NOT NULL,   -- unix epoch seconds
                    text  TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind            TEXT NOT NULL,   -- 'discharge' | 'charge'
                    started_ts      REAL NOT NULL,   -- unix epoch seconds
                    ended_ts        REAL,            -- NULL while running
                    set_current     REAL NOT NULL,   -- programmed CC current (A)
                    target_voltage  REAL NOT NULL,   -- discharge: cutoff; charge: CV setpoint (V)
                    termination_current REAL,        -- charge: taper-off current (A); NULL for discharge
                    max_seconds     REAL,            -- optional safety duration cap
                    max_ah          REAL,            -- optional safety charge cap
                    charge_ah       REAL,            -- integrated charge moved (Ah)
                    energy_wh       REAL,            -- integrated energy moved (Wh)
                    status          TEXT NOT NULL,   -- running|complete|aborted|error
                    stop_reason     TEXT,
                    cycle_id        INTEGER,         -- groups steps of one cycling run
                    note            TEXT
                )
                """
            )
            # Migrate older DBs: add columns introduced after the table existed.
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            for col in ("open_circuit_v", "dcir_ohms"):
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} REAL")
            self._conn.commit()

    def insert_reading(self, ts: float, instrument: str, quantity: str, value: float, unit: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO readings (ts, instrument, quantity, value, unit) VALUES (?, ?, ?, ?, ?)",
                (ts, instrument, quantity, value, unit),
            )
            self._conn.commit()

    def recent_readings(self, quantity: str, limit: int = 600) -> list[dict]:
        """Return up to `limit` most recent readings for a quantity, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, value, unit FROM readings WHERE quantity = ? ORDER BY ts DESC LIMIT ?",
                (quantity, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def fetch_readings(self, quantity: str, limit: int | None = None) -> list[dict]:
        """Return readings for a quantity, oldest first (for CSV export)."""
        sql = "SELECT ts, instrument, quantity, value, unit FROM readings WHERE quantity = ? ORDER BY ts ASC"
        params: list = [quantity]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def insert_event(self, ts: float, text: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (ts, text) VALUES (?, ?)", (ts, text))
            self._conn.commit()

    def recent_events(self, limit: int = 500) -> list[dict]:
        """Return up to `limit` most recent events, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, text FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def fetch_events(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT ts, text FROM events ORDER BY ts ASC").fetchall()
        return [dict(r) for r in rows]

    def stats_since_event(self, quantity: str) -> dict:
        """Aggregate readings for a quantity since the most recent event.

        Returns raw aggregates (n, sum, sumsq, min, max, last_ts) so the client
        can keep them updated incrementally as new readings stream in, plus the
        event the window starts from (None if there are no events yet).
        """
        with self._lock:
            ev = self._conn.execute(
                "SELECT ts, text FROM events ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            since = ev["ts"] if ev else None
            sql = (
                "SELECT COUNT(*) n, MIN(value) mn, MAX(value) mx, "
                "SUM(value) s, SUM(value * value) ss, MAX(ts) mts "
                "FROM readings WHERE quantity = ?"
            )
            params: list = [quantity]
            if since is not None:
                sql += " AND ts >= ?"
                params.append(since)
            row = self._conn.execute(sql, params).fetchone()
        return {
            "n": row["n"],
            "min": row["mn"],
            "max": row["mx"],
            "sum": row["s"] or 0.0,
            "sumsq": row["ss"] or 0.0,
            "last_ts": row["mts"],
            "event": dict(ev) if ev else None,
        }

    # --- charge/discharge sessions --------------------------------------
    def start_session(
        self, kind: str, started_ts: float, set_current: float, target_voltage: float,
        termination_current: float | None, max_seconds: float | None,
        max_ah: float | None, note: str, cycle_id: int | None = None,
        open_circuit_v: float | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions "
                "(kind, started_ts, set_current, target_voltage, termination_current, "
                " max_seconds, max_ah, charge_ah, energy_wh, status, cycle_id, note, open_circuit_v) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'running', ?, ?, ?)",
                (kind, started_ts, set_current, target_voltage, termination_current,
                 max_seconds, max_ah, cycle_id, note, open_circuit_v),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_dcir(self, session_id: int, dcir_ohms: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET dcir_ohms = ? WHERE id = ?", (dcir_ohms, session_id)
            )
            self._conn.commit()

    def update_session(self, session_id: int, charge_ah: float, energy_wh: float) -> None:
        """Persist running totals so an interrupted test still has partial data."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET charge_ah = ?, energy_wh = ? WHERE id = ?",
                (charge_ah, energy_wh, session_id),
            )
            self._conn.commit()

    def finish_session(
        self, session_id: int, ended_ts: float, charge_ah: float, energy_wh: float,
        status: str, stop_reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_ts = ?, charge_ah = ?, "
                "energy_wh = ?, status = ?, stop_reason = ? WHERE id = ?",
                (ended_ts, charge_ah, energy_wh, status, stop_reason, session_id),
            )
            self._conn.commit()

    def recent_sessions(self, limit: int = 50) -> list[dict]:
        """Return the most recent charge/discharge sessions, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def readings_between(
        self, quantity: str, t0: float, t1: float | None = None
    ) -> list[dict]:
        """Readings for a quantity within [t0, t1], oldest first. Steps run
        sequentially and only log while active, so a step's session window
        [started_ts, ended_ts] selects exactly that step's readings."""
        sql = "SELECT ts, value FROM readings WHERE quantity = ? AND ts >= ?"
        params: list = [quantity, t0]
        if t1 is not None:
            sql += " AND ts <= ?"
            params.append(t1)
        sql += " ORDER BY ts ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def next_cycle_id(self) -> int:
        """Allocate a new cycle group id (max existing + 1)."""
        with self._lock:
            row = self._conn.execute("SELECT MAX(cycle_id) m FROM sessions").fetchone()
        return (row["m"] or 0) + 1

    def quantities(self) -> list[str]:
        """Distinct quantities that have logged readings."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT quantity FROM readings ORDER BY quantity"
            ).fetchall()
        return [r["quantity"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
