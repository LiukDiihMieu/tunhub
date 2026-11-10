"""Rendering of autostart entries (nothing is registered on the machine)."""

import plistlib
import sys
from pathlib import Path

from tunhub import autostart


def test_run_command_uses_current_interpreter(tmp_path):
    assert autostart.run_command(None) == [sys.executable, "-m", "tunhub", "run"]
    argv = autostart.run_command(tmp_path / "cfg")
    assert argv[-2:] == ["--config-dir", str(tmp_path / "cfg")]


def test_systemd_unit_quotes_arguments():
    unit = autostart.render_systemd_unit(["/opt/my tools/python", "-m", "tunhub", "run"])
    assert "ExecStart='/opt/my tools/python' -m tunhub run\n" in unit
    assert "WantedBy=default.target" in unit


def test_launchd_plist(tmp_path):
    data = plistlib.loads(autostart.render_launchd_plist(["/usr/bin/python3", "-m", "tunhub", "run"], tmp_path / "t.log"))
    assert data["Label"] == autostart.LAUNCHD_LABEL
    assert data["ProgramArguments"][-1] == "run"
    assert data["RunAtLoad"] is True and data["StandardErrorPath"].endswith("t.log")


def test_windows_task_uses_pythonw_and_escapes_quotes():
    argv = ["C:/Users/O'Neil/tools/python.exe", "-m", "tunhub", "run", "--config-dir", "C:/My Config"]
    script = autostart.render_windows_task_script(argv)
    assert str(Path("C:/Users/O'Neil/tools/pythonw.exe")).replace("'", "''") in script
    assert "-Argument '-m tunhub run --config-dir \"C:/My Config\"'" in script
    assert "Register-ScheduledTask -TaskName 'TunHub'" in script
