"""Per-platform locations for config, cache and log files.

| kind   | Linux                         | macOS                        | Windows                         |
|--------|-------------------------------|------------------------------|---------------------------------|
| config | $XDG_CONFIG_HOME/tunhub       | ~/.config/tunhub             | %APPDATA%\\tunhub               |
| cache  | $XDG_CACHE_HOME/tunhub        | ~/Library/Caches/tunhub      | %LOCALAPPDATA%\\tunhub          |
| log    | (journald via systemd)        | ~/Library/Logs/tunhub.log    | %LOCALAPPDATA%\\tunhub\\tunhub.log |

macOS uses ~/.config like most CLI tools, so the config is easy to find and to
keep in a dotfiles repo.
"""

from __future__ import annotations

import functools
import os
import socket
import subprocess
import sys
from pathlib import Path

APP_NAME = "tunhub"
CONFIG_DIR_ENV = "TUNHUB_CONFIG_DIR"
CACHE_DIR_ENV = "TUNHUB_CACHE_DIR"
MACHINE_ENV = "TUNHUB_MACHINE"


def machine_name(explicit: str | None = None) -> str:
    """This machine's name, which picks its override file `local.<name>.yaml`.

    `explicit` (the --machine option) if given, else $TUNHUB_MACHINE if set,
    else the OS host name (on macOS the LocalHostName, which does not change
    with the network). Lowercased, and only the part before the first dot:
    `Laptop.lan` -> `laptop`.
    """
    return normalize_machine_name(explicit or os.environ.get(MACHINE_ENV) or _os_machine_name())


def normalize_machine_name(name: str) -> str:
    return name.strip().lower().split(".")[0]


@functools.cache
def _os_machine_name() -> str:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5, check=True
            )
            if result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname()


def _env_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def default_config_dir() -> Path:
    if os.environ.get(CONFIG_DIR_ENV):
        return Path(os.environ[CONFIG_DIR_ENV])
    if sys.platform == "win32":
        return _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / ".config" / APP_NAME
    return _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / APP_NAME


def cache_dir() -> Path:
    if os.environ.get(CACHE_DIR_ENV):
        return Path(os.environ[CACHE_DIR_ENV])
    if sys.platform == "win32":
        return _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    return _env_path("XDG_CACHE_HOME", Path.home() / ".cache") / APP_NAME


def icon_cache_dir() -> Path:
    return cache_dir() / "icons"


def default_log_file() -> Path:
    """Where output goes when there is no console (pythonw, launchd)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    return cache_dir() / f"{APP_NAME}.log"
