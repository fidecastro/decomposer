"""The engine process, behind one handle and one config.

Before this existed the engine was configured two ways — command-line
arguments at spawn and text lines over its control socket at runtime — and
the two drifted: a restarted engine came back with whatever subset happened
to be on its argv, silently losing the rest. Now both are projections of a
single EngineConfig (core.model): argv via engine_cli_args, runtime changes
via engine_delta_lines, and `apply()` decides which projection a change
needs. There is no other way to talk to the engine.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Optional

from opal_c1.core.model import (
    EngineConfig,
    engine_cli_args,
    engine_delta_lines,
)

LOG_LINES = 12
READY_TIMEOUT = 8.0


class EngineHandle:
    def __init__(self, binary: str, control_path: Path, preview_path: Path):
        self._binary = binary
        self._control = control_path
        self._preview = preview_path
        self._proc: Optional[subprocess.Popen] = None
        self._log: list = []
        self._log_lock = threading.Lock()
        self.config: Optional[EngineConfig] = None
        self.started_at = 0.0

    # -- observability ----------------------------------------------------

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    def log_lines(self) -> list:
        with self._log_lock:
            return list(self._log)

    def log_text(self) -> str:
        return " | ".join(self.log_lines())

    @property
    def stdin(self):
        return self._proc.stdin if self._proc is not None else None

    # -- lifecycle --------------------------------------------------------

    def start(self, config: EngineConfig) -> None:
        """One spawn attempt. Raises RuntimeError with the engine's own words
        if it dies or fails to open its control socket in time."""
        # A stale socket file would make the readiness check pass against a
        # process that is already dead.
        with suppress(OSError):
            self._control.unlink()
        with self._log_lock:
            self._log = []

        cmd = (
            [self._binary]
            + engine_cli_args(config)
            + ["--control", str(self._control), "--preview", str(self._preview)]
        )
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if config.input == "-" else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        threading.Thread(
            target=self._drain, args=(self._proc,), daemon=True
        ).start()
        self.started_at = time.time()
        self.config = config

        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    "engine exited immediately: " + (self.log_text() or "no output")
                )
            if self._control.exists():
                return
            time.sleep(0.1)
        raise RuntimeError(
            f"engine did not open its control socket within {READY_TIMEOUT:.0f}s: "
            + (self.log_text() or "no output")
        )

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        with suppress(Exception):
            if proc.stdin:
                proc.stdin.close()
        with suppress(Exception):
            proc.terminate()
            proc.wait(timeout=5)
        self._proc = None
        with suppress(OSError):
            self._control.unlink()

    # -- the chokepoint ---------------------------------------------------

    def apply(self, desired: EngineConfig) -> None:
        """Make the running engine match `desired`, restarting only if a
        restart-only field changed."""
        if not self.alive() or self.config is None:
            self.start(desired)
            return
        if desired.needs_restart_from(self.config):
            self.stop()
            self.start(desired)
            return
        for line in engine_delta_lines(self.config, desired):
            self._send(line)
        self.config = desired

    def apply_live(self, **live_fields) -> None:
        """Change live fields only, never restarting.

        Used by look/mirror/overlay setters: they must not be able to bounce
        the engine (and take /dev/video10 with it) no matter what the current
        input-node discovery would say.
        """
        if not self.alive() or self.config is None:
            return  # the next start()'s argv will carry the state
        self.apply(replace(self.config, **live_fields))

    # -- internals --------------------------------------------------------

    def _send(self, line: str) -> None:
        with suppress(OSError):
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2.0)
            s.connect(str(self._control))
            s.sendall((line + "\n").encode())
            s.close()

    def _drain(self, proc: subprocess.Popen) -> None:
        """Keep the last few stderr lines: a crash must explain itself, and
        an undrained pipe would eventually block the engine."""
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            with self._log_lock:
                self._log.append(line)
                del self._log[:-LOG_LINES]
