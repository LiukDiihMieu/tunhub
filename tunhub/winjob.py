"""Windows only: make the ssh processes die together with tunhub.

Windows does not end child processes when their parent ends. If tunhub is
killed (Task Manager, the scheduled task being stopped, a crash), its ssh.exe
children keep running and keep the fixed local ports, so every later start
fails with "port busy".

tunhub therefore puts itself into a job object that kills all its processes
when the job is closed. Child processes join their parent's job by default,
so every ssh (and anything ssh starts, such as a ProxyCommand) is covered.
tunhub holds the only handle to the job; Windows closes it when tunhub ends,
however it ends, and then ends the rest of the job.

On Linux and macOS this is a no-op: systemd and launchd already stop the
whole process group of the service.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("tunhub")

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

# Kept open for the lifetime of the process: closing it would end the job.
_job_handle: int | None = None


def kill_children_on_exit() -> bool:
    """Put this process into a kill-on-close job. Returns True on success."""
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return _job_handle is not None

    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning("could not create a job object (error %d); ssh may outlive tunhub", ctypes.get_last_error())
        return False

    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ) and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    if not ok:
        # E.g. an enclosing job forbids nesting: keep running as before.
        logger.warning("could not join a job object (error %d); ssh may outlive tunhub", ctypes.get_last_error())
        kernel32.CloseHandle(job)
        return False

    _job_handle = job
    return True
