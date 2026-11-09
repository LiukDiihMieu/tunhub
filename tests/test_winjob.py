"""ssh must not outlive a killed tunhub on Windows (job object)."""

import subprocess
import sys
import textwrap

import pytest

from tunhub.winjob import kill_children_on_exit

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

# Stands in for `tunhub run`: optionally joins the job, starts a long-lived
# child (like ssh.exe), reports its pid and waits to be killed.
PARENT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    from tunhub.winjob import kill_children_on_exit
    if sys.argv[1] == "job":
        assert kill_children_on_exit()
    child = subprocess.Popen(["ping", "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)
    print(os.getpid(), child.pid, flush=True)
    time.sleep(120)
    """
)


def _exits_within(pid: int, seconds: float) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


@windows_only
@pytest.mark.parametrize("mode, child_dies", [("job", True), ("no-job", False)])
def test_child_dies_when_parent_is_killed(mode, child_dies):
    launcher = subprocess.Popen([sys.executable, "-c", PARENT, mode], stdout=subprocess.PIPE, text=True)
    # In a venv, sys.executable is a launcher that starts the real interpreter;
    # kill the interpreter itself (the process that joined the job).
    parent_pid, child_pid = map(int, launcher.stdout.readline().split())
    try:
        # TerminateProcess, like "End task" in Task Manager (no /T: not the tree)
        subprocess.run(["taskkill", "/F", "/PID", str(parent_pid)], capture_output=True, check=True)
        assert _exits_within(parent_pid, 10)
        # Without the job the child survives: the bug the job object fixes.
        assert _exits_within(child_pid, 5) is child_dies
    finally:
        for pid in (child_pid, launcher.pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the no-op on Linux and macOS")
def test_no_op_outside_windows():
    assert kill_children_on_exit() is False
