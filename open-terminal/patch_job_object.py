"""Attach a Win32 Job Object (KILL_ON_JOB_CLOSE) to Open Terminal agent subprocesses on Windows.

Idempotent: invoked by AI\\open-terminal\\run.ps1 before open-terminal.exe starts. Ensures that if open-terminal.exe
exits or a session ends, child trees (opencode-cli, claude, node, python/scene3d) never remain orphaned.
"""
import importlib.util
import sys
from pathlib import Path

MARKER = "# local patch: Win32 Job Object kill-on-close"
HELPER = f'''
{MARKER} (AI\\open-terminal\\patch_job_object.py)
def _attach_win_job_object(proc):
    if os.name != "nt" or not getattr(proc, "pid", None):
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(f"f{{i}}", ctypes.c_ulonglong) for i in range(6)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) | JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800)
        info.BasicLimitInformation.LimitFlags = 0x2800
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        # PROCESS_SET_QUOTA (0x0100) | PROCESS_TERMINATE (0x0001)
        hproc = kernel32.OpenProcess(0x0101, False, int(proc.pid))
        if not hproc:
            kernel32.CloseHandle(job)
            return
        try:
            if kernel32.AssignProcessToJobObject(job, hproc):
                proc._win_job = job
            else:
                kernel32.CloseHandle(job)
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        pass


def _close_win_job_object(proc):
    job = getattr(proc, "_win_job", None)
    if job and os.name == "nt":
        proc._win_job = None
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
        except Exception:
            pass
'''

SPAWN_TARGET = (
    "    return await asyncio.create_subprocess_exec(\n"
    "        *args,\n"
    "        cwd=cwd,\n"
    "        env=env,\n"
    "        **kwargs,\n"
    "        start_new_session=os.name != \"nt\",\n"
    "        stdin=asyncio.subprocess.PIPE,\n"
    "        stdout=asyncio.subprocess.PIPE,\n"
    "        stderr=asyncio.subprocess.PIPE,\n"
    "        limit=LIMIT,\n"
    "    ), cwd\n"
)
SPAWN_REPLACEMENT = (
    "    proc = await asyncio.create_subprocess_exec(\n"
    "        *args,\n"
    "        cwd=cwd,\n"
    "        env=env,\n"
    "        **kwargs,\n"
    "        start_new_session=os.name != \"nt\",\n"
    "        stdin=asyncio.subprocess.PIPE,\n"
    "        stdout=asyncio.subprocess.PIPE,\n"
    "        stderr=asyncio.subprocess.PIPE,\n"
    "        limit=LIMIT,\n"
    "    )\n"
    "    _attach_win_job_object(proc)\n"
    "    return proc, cwd\n"
)

STOP_TARGET = "    await proc.wait()\n"
STOP_REPLACEMENT = "    await proc.wait()\n    _close_win_job_object(proc)\n"


def main() -> int:
    spec = importlib.util.find_spec("open_terminal")
    if not spec or not spec.submodule_search_locations:
        print("open_terminal not found in this Python environment")
        return 1
    path = Path(list(spec.submodule_search_locations)[0]) / "agents" / "sessions.py"
    if not path.exists():
        print(f"sessions.py not found at {path}")
        return 1
    raw = path.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    src = raw.replace("\r\n", "\n")
    if MARKER in src:
        print("open_terminal sessions.py: Job Object already patched")
        return 0
    if SPAWN_TARGET not in src or "\nasync def spawn(" not in src:
        print("open_terminal sessions.py: upstream code changed, patch skipped")
        return 0
    src = src.replace("\nasync def spawn(", HELPER + "\n\nasync def spawn(", 1)
    src = src.replace(SPAWN_TARGET, SPAWN_REPLACEMENT, 1)
    if src.count(STOP_TARGET) >= 1:
        src = src.replace(STOP_TARGET, STOP_REPLACEMENT, 1)
    path.write_text(src.replace("\n", nl), encoding="utf-8")
    print("open_terminal sessions.py: Job Object patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
