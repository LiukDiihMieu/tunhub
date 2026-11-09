"""Running the system OpenSSH client as a port-forwarding subprocess.

subprocess.Popen + a reader thread is used instead of asyncio subprocesses,
because the latter depend on the event loop flavour (notably on Windows).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Callable

from .config import Route, TunnelSettings

# Always applied, but placed after user options: ssh uses the first value it
# sees, so a user can still override them through tunnel.ssh_options.
FIXED_SSH_OPTIONS = (
    ("ExitOnForwardFailure", "yes"),
    ("BatchMode", "yes"),
)


def build_ssh_command(settings: TunnelSettings, route: Route, local_port: int) -> list[str]:
    remote_host = f"[{route.target_host}]" if ":" in route.target_host else route.target_host
    forward = f"127.0.0.1:{local_port}:{remote_host}:{route.target_port}"

    argv = [*settings.ssh_command, "-N", "-T"]
    for key, value in (*settings.ssh_options, *FIXED_SSH_OPTIONS):
        argv += ["-o", f"{key}={value}"]
    argv += ["-L", forward, str(route.ssh_host)]
    return argv


class SshProcess:
    def __init__(self, argv: list[str], on_line: Callable[[str], None]) -> None:
        self.argv = argv
        self._on_line = on_line
        self._proc: subprocess.Popen[str] | None = None
        self.last_line = ""

    def start(self) -> None:
        extra: dict = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **extra,
        )
        threading.Thread(target=self._read_stderr, daemon=True).start()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 3) -> None:
        """Blocking; call through asyncio.to_thread from async code."""
        if not self.alive():
            return
        assert self._proc is not None
        self._proc.terminate()
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            line = line.rstrip()
            if line:
                self.last_line = line
                self._on_line(line)
