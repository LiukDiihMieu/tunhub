"""Start tunhub at login: systemd (Linux), launchd (macOS), Task Scheduler (Windows).

The render_* functions are pure so they can be tested on any platform; the
install / uninstall / status functions apply them to the current machine.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from .paths import default_log_file

SYSTEMD_UNIT = "tunhub.service"
LAUNCHD_LABEL = "io.github.liukdiihmieu.tunhub"
WINDOWS_TASK = "TunHub"


class AutostartError(RuntimeError):
    pass


def run_command(config_dir: Path | None) -> list[str]:
    """The command the service runs: this interpreter, so uv/pipx tool venvs work."""
    argv = [sys.executable, "-m", "tunhub", "run"]
    if config_dir is not None:
        # Absolute, but symlinks kept: a dotfiles symlink may be re-pointed later.
        argv += ["--config-dir", os.path.abspath(config_dir)]
    return argv


# ---------------------------------------------------------------------------
# Rendering


def render_systemd_unit(argv: list[str]) -> str:
    return (
        "[Unit]\n"
        "Description=tunhub (local dashboard for SSH tunnels and LAN services)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={' '.join(shlex.quote(part) for part in argv)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launchd_plist(argv: list[str], log_file: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(log_file),
            "StandardErrorPath": str(log_file),
        }
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_windows_task_script(argv: list[str]) -> str:
    """PowerShell that registers a logon task running tunhub without a console."""
    # pythonw.exe sits next to python.exe in every CPython install and venv.
    executable = Path(argv[0])
    if executable.name.lower() == "python.exe":
        executable = executable.with_name("pythonw.exe")
    arguments = subprocess.list2cmdline(argv[1:])
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute {_ps_quote(str(executable))} "
            f"-Argument {_ps_quote(arguments)}",
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
            "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited",
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)",
            f"Register-ScheduledTask -TaskName {_ps_quote(WINDOWS_TASK)} -Action $action -Trigger $trigger "
            "-Principal $principal -Settings $settings -Description 'tunhub dashboard' -Force | Out-Null",
            f"Start-ScheduledTask -TaskName {_ps_quote(WINDOWS_TASK)}",
        ]
    )


# ---------------------------------------------------------------------------
# Paths


def systemd_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / SYSTEMD_UNIT


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


# ---------------------------------------------------------------------------
# Apply


def _run(argv: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        raise AutostartError(f"command not found: {argv[0]}") from None
    if check and result.returncode != 0:
        raise AutostartError(f"{shlex.join(argv)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result


def _powershell(script: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check)


def install(config_dir: Path | None) -> str:
    """Install and start the autostart entry; return a human-readable summary."""
    argv = run_command(config_dir)
    if sys.platform.startswith("linux"):
        unit = systemd_unit_path()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(render_systemd_unit(argv), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        return f"installed {unit}\nlogs: journalctl --user -u tunhub -f"

    if sys.platform == "darwin":
        plist = launchd_plist_path()
        log_file = default_log_file()
        plist.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(render_launchd_plist(argv, log_file))
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=False)
        _run(["launchctl", "bootstrap", domain, str(plist)])
        return f"installed {plist}\nlogs: tail -f {log_file}"

    if sys.platform == "win32":
        _powershell(render_windows_task_script(argv))
        return f"registered scheduled task '{WINDOWS_TASK}'\nlogs: {default_log_file()}"

    raise AutostartError(f"unsupported platform: {sys.platform}")


def uninstall() -> str:
    if sys.platform.startswith("linux"):
        unit = systemd_unit_path()
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
        unit.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        return f"removed {unit}"

    if sys.platform == "darwin":
        plist = launchd_plist_path()
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        plist.unlink(missing_ok=True)
        return f"removed {plist}"

    if sys.platform == "win32":
        task = _ps_quote(WINDOWS_TASK)
        _powershell(f"Stop-ScheduledTask -TaskName {task} -ErrorAction SilentlyContinue; "
                    f"Unregister-ScheduledTask -TaskName {task} -Confirm:$false")
        return f"removed scheduled task '{WINDOWS_TASK}'"

    raise AutostartError(f"unsupported platform: {sys.platform}")


def installed() -> bool:
    """Whether the login autostart entry exists (not whether tunhub is running)."""
    if sys.platform.startswith("linux"):
        return systemd_unit_path().exists()
    if sys.platform == "darwin":
        return launchd_plist_path().exists()
    if sys.platform == "win32":
        result = _powershell(
            f"if (Get-ScheduledTask -TaskName {_ps_quote(WINDOWS_TASK)} -ErrorAction SilentlyContinue) {{ 'yes' }}",
            check=False,
        )
        return result.stdout.strip() == "yes"
    raise AutostartError(f"unsupported platform: {sys.platform}")


def status() -> str:
    if sys.platform.startswith("linux"):
        result = _run(["systemctl", "--user", "status", SYSTEMD_UNIT, "--no-pager"], check=False)
    elif sys.platform == "darwin":
        result = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
    elif sys.platform == "win32":
        result = _powershell(
            f"Get-ScheduledTask -TaskName {_ps_quote(WINDOWS_TASK)} | Format-List TaskName,State", check=False
        )
    else:
        raise AutostartError(f"unsupported platform: {sys.platform}")
    output = (result.stdout + result.stderr).strip()
    return output or "not installed"
