"""tunhub's own per-machine state, kept apart from the configuration.

Things tunhub remembers by itself, such as the card order or the dashboard
address of the last configuration that loaded. Losing the file only loses
these conveniences. It lives in the state directory (see paths.py), which is
never synced, so each machine has its own.

Format: ``{"version": 1, "configs": {"<config dir>": {<name>: <value>}}}``,
one entry per config directory, since `--config-dir` can point tunhub at
several on one machine.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .paths import state_dir

logger = logging.getLogger("tunhub")

FILE_NAME = "state.json"
FORMAT_VERSION = 1


class StateStore:
    """Values remembered for one config directory. Best effort: never raises."""

    def __init__(self, config_dir: Path, path: Path | None = None) -> None:
        self.path = path if path is not None else state_dir() / FILE_NAME
        self.key = os.path.abspath(config_dir)

    def get(self, name: str, default: Any = None) -> Any:
        return self._read().get("configs", {}).get(self.key, {}).get(name, default)

    def set(self, name: str, value: Any) -> None:
        """Store `value` (JSON); None removes it."""
        data = self._read()
        configs = data.setdefault("configs", {})
        entry = configs.setdefault(self.key, {})
        if value is None:
            entry.pop(name, None)
        else:
            entry[name] = value
        if not entry:
            configs.pop(self.key)
        data["version"] = FORMAT_VERSION
        self._write(data)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("state: cannot read %s (%s); starting from scratch", self.path, exc)
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("configs", {}), dict):
            logger.warning("state: %s has an unexpected format; starting from scratch", self.path)
            return {}
        data["configs"] = {k: v for k, v in data.get("configs", {}).items() if isinstance(v, dict)}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        # Write a temporary file and rename it: a crash never leaves half a file.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                os.replace(temp, self.path)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("state: cannot write %s (%s)", self.path, exc)
