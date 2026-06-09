"""Minimal SCPI-over-TCP transport for Siglent instruments.

Siglent DC power supplies, electronic loads, and SDM-series multimeters all
expose a raw SCPI socket on TCP port 5025. Commands and responses are newline
terminated. This module deliberately uses blocking sockets; callers on the async
event loop should wrap calls in asyncio.to_thread so I/O doesn't block the loop.
"""
from __future__ import annotations

import socket
import threading
import time


class ScpiError(RuntimeError):
    pass


class ScpiSocket:
    def __init__(self, host: str, port: int = 5025, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        # Serialize access so a query's write+read pair is never interleaved
        # with another caller on a shared connection.
        self._lock = threading.Lock()

    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def write(self, command: str) -> None:
        with self._lock:
            self._connect_locked()
            assert self._sock is not None
            self._sock.sendall(command.encode("ascii") + b"\n")

    def query(self, command: str, retries: int = 2) -> str:
        """Send a command and read a single newline-terminated response.

        On a timeout or socket error, drop the connection and retry (reconnecting)
        up to `retries` times, so a transient stall — instrument briefly busy, a
        LAN hiccup — doesn't abort a long run. Each retry reconnects first, so a
        stale or half-read response from the failed attempt can't desync the
        session. A sustained outage still raises after the retries are exhausted,
        so the controller can fail safe."""
        for attempt in range(retries + 1):
            with self._lock:
                self._connect_locked()
                assert self._sock is not None
                try:
                    self._sock.sendall(command.encode("ascii") + b"\n")
                    return self._read_line_locked()
                except (OSError, ScpiError):
                    # Drop the connection so the retry (or next call) reconnects.
                    if self._sock is not None:
                        self._sock.close()
                        self._sock = None
                    if attempt == retries:
                        raise
            time.sleep(0.2)  # brief backoff before reconnecting and retrying

    def _read_line_locked(self) -> str:
        assert self._sock is not None
        chunks: list[bytes] = []
        while True:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ScpiError("connection closed by instrument")
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return b"".join(chunks).decode("ascii", errors="replace").strip()
