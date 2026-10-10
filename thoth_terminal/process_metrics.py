"""One-shot native resource counters, with no sampler or external commands.

Windows private bytes are private *commit*, not private resident memory. CPU
cycles are cumulative integer cycles, never seconds or a clock-frequency estimate.
Creation time is an identity counter: Windows FILETIME (100 ns since 1601), or
Linux start ticks since boot; compare it only within the same host/platform.
Unavailable counters stay None and each failed native call is recorded in errors.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from typing import Any

_PROCESS_FIELDS = (
    "creation_time", "private_bytes", "working_set_bytes", "peak_working_set_bytes",
    "user_time_s", "kernel_time_s", "cpu_time_s", "cpu_cycles", "handle_count",
    "io_read_bytes", "io_write_bytes", "io_other_bytes", "io_read_operations", "io_write_operations", "io_other_operations",
)


def _result(fields: tuple[str, ...], source: str) -> dict[str, Any]:
    return {**dict.fromkeys(fields), "source": source, "errors": {}, "availability": {}}


def _finish(result: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result["availability"] = {key: result[key] is not None for key in fields}
    return result


def _error(result: dict[str, Any], operation: str, error: OSError) -> None:
    result["errors"][operation] = {
        "code": getattr(error, "winerror", None) or error.errno,
        "message": str(error),
    }


if os.name == "nt":
    from ctypes import wintypes as wt

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
        ]

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD)] + [
            (name, ctypes.c_uint64) for name in (
                "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual",
            )
        ]

    class _PerformanceInfo(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD)] + [(name, ctypes.c_size_t) for name in (
            "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
            "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize",
        )] + [(name, wt.DWORD) for name in ("HandleCount", "ProcessCount", "ThreadCount")]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    def _bind(dll: Any, name: str, args: list[Any], returns: Any) -> Any:
        function = getattr(dll, name)
        function.argtypes = args
        function.restype = returns
        return function

    _open_process = _bind(_kernel, "OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
    _close_handle = _bind(_kernel, "CloseHandle", [wt.HANDLE], wt.BOOL)
    _get_times = _bind(_kernel, "GetProcessTimes", [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4, wt.BOOL)
    _get_cycles = _bind(_kernel, "QueryProcessCycleTime", [wt.HANDLE, ctypes.POINTER(ctypes.c_uint64)], wt.BOOL)
    _get_handles = _bind(_kernel, "GetProcessHandleCount", [wt.HANDLE, ctypes.POINTER(wt.DWORD)], wt.BOOL)
    _get_io = _bind(_kernel, "GetProcessIoCounters", [wt.HANDLE, ctypes.POINTER(_IoCounters)], wt.BOOL)
    _get_memory = _bind(_psapi, "GetProcessMemoryInfo", [wt.HANDLE, ctypes.POINTER(_MemoryCounters), wt.DWORD], wt.BOOL)
    _snapshot = _bind(_kernel, "CreateToolhelp32Snapshot", [wt.DWORD, wt.DWORD], wt.HANDLE)
    _process_first = _bind(_kernel, "Process32FirstW", [wt.HANDLE, ctypes.POINTER(_ProcessEntry)], wt.BOOL)
    _process_next = _bind(_kernel, "Process32NextW", [wt.HANDLE, ctypes.POINTER(_ProcessEntry)], wt.BOOL)
    _memory_status = _bind(_kernel, "GlobalMemoryStatusEx", [ctypes.POINTER(_MemoryStatus)], wt.BOOL)
    _performance_info = _bind(_psapi, "GetPerformanceInfo", [ctypes.POINTER(_PerformanceInfo), wt.DWORD], wt.BOOL)

    def _win_error(result: dict[str, Any], operation: str) -> None:
        _error(result, operation, ctypes.WinError(ctypes.get_last_error()))

    def _filetime(value: wt.FILETIME) -> int:
        return (value.dwHighDateTime << 32) | value.dwLowDateTime


def sample_process_resources(pid: int) -> dict[str, Any]:
    """Return cumulative counters for a PID; disappearance/access denial is explicit.

    Sampling owns a single PROCESS_QUERY_LIMITED_INFORMATION handle on Windows.
    Linux exposes resident memory, CPU time and storage I/O from /proc, but does
    not pretend those sources supply private commit, handles, or CPU cycles.
    """
    result = _result(_PROCESS_FIELDS, "win32" if os.name == "nt" else "procfs")
    result["pid"] = pid
    result["creation_time_unit"] = "100ns_since_1601" if os.name == "nt" else "ticks_since_boot"
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid > 0xFFFFFFFF:
        result["errors"]["pid"] = {"code": "invalid_pid", "message": "PID must be a positive DWORD integer"}
        return _finish(result, _PROCESS_FIELDS)
    if os.name != "nt":
        return _sample_procfs(pid, result)
    handle = _open_process(0x1000, False, pid)
    if not handle:
        _win_error(result, "OpenProcess")
        return _finish(result, _PROCESS_FIELDS)
    try:
        creation, exit_time, kernel, user = (wt.FILETIME() for _ in range(4))
        if _get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            result.update(creation_time=_filetime(creation), user_time_s=_filetime(user) / 10_000_000,
                          kernel_time_s=_filetime(kernel) / 10_000_000,
                          cpu_time_s=(_filetime(user) + _filetime(kernel)) / 10_000_000)
        else:
            _win_error(result, "GetProcessTimes")
        memory = _MemoryCounters()
        memory.cb = ctypes.sizeof(memory)
        if _get_memory(handle, ctypes.byref(memory), memory.cb):
            result.update(private_bytes=memory.PrivateUsage, working_set_bytes=memory.WorkingSetSize,
                          peak_working_set_bytes=memory.PeakWorkingSetSize)
        else:
            _win_error(result, "GetProcessMemoryInfo")
        cycles, handles = ctypes.c_uint64(), wt.DWORD()
        for operation, function, counter, key in (
            ("QueryProcessCycleTime", _get_cycles, cycles, "cpu_cycles"),
            ("GetProcessHandleCount", _get_handles, handles, "handle_count"),
        ):
            if function(handle, ctypes.byref(counter)):
                result[key] = counter.value
            else:
                _win_error(result, operation)
        io = _IoCounters()
        if _get_io(handle, ctypes.byref(io)):
            result.update(io_read_bytes=io.ReadTransferCount, io_write_bytes=io.WriteTransferCount,
                          io_other_bytes=io.OtherTransferCount, io_read_operations=io.ReadOperationCount,
                          io_write_operations=io.WriteOperationCount, io_other_operations=io.OtherOperationCount)
        else:
            _win_error(result, "GetProcessIoCounters")
    finally:
        if not _close_handle(handle):
            _win_error(result, "CloseHandle")
    return _finish(result, _PROCESS_FIELDS)


def _proc_stat(pid: int) -> tuple[str, list[str]]:
    raw = (Path("/proc") / str(pid) / "stat").read_text()
    return raw[raw.index("(") + 1:raw.rindex(")")], raw[raw.rindex(")") + 2:].split()


def _sample_procfs(pid: int, result: dict[str, Any]) -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        result["source"] = "unavailable"
        result["creation_time_unit"] = None
        result["errors"]["platform"] = {"code": "unsupported", "message": "Native counters unavailable on this platform"}
        return _finish(result, _PROCESS_FIELDS)
    try:
        _, stat = _proc_stat(pid)
        ticks = os.sysconf("SC_CLK_TCK")
        result.update(creation_time=int(stat[19]), user_time_s=int(stat[11]) / ticks,
                      kernel_time_s=int(stat[12]) / ticks,
                      cpu_time_s=(int(stat[11]) + int(stat[12])) / ticks,
                      working_set_bytes=int(stat[21]) * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, IndexError) as error:
        _error(result, "proc/stat", error if isinstance(error, OSError) else OSError(str(error)))
        return _finish(result, _PROCESS_FIELDS)
    try:
        status = dict(line.split(":", 1) for line in (Path("/proc") / str(pid) / "status").read_text().splitlines())
        if "VmHWM" in status:
            result["peak_working_set_bytes"] = int(status["VmHWM"].split()[0]) * 1024
    except (OSError, ValueError) as error:
        _error(result, "proc/status", error if isinstance(error, OSError) else OSError(str(error)))
    try:
        io = dict(line.split(":", 1) for line in (Path("/proc") / str(pid) / "io").read_text().splitlines())
        result.update(io_read_bytes=int(io["read_bytes"]), io_write_bytes=int(io["write_bytes"]))
    except (OSError, ValueError, KeyError) as error:
        _error(result, "proc/io", error if isinstance(error, OSError) else OSError(str(error)))
    # Reject mixed counters if the process exited or its PID was reused mid-read.
    try:
        if int(_proc_stat(pid)[1][19]) != result["creation_time"]:
            raise OSError("PID was reused during resource sampling")
    except (OSError, ValueError, IndexError) as error:
        for key in _PROCESS_FIELDS:
            result[key] = None
        _error(result, "proc/identity", error if isinstance(error, OSError) else OSError(str(error)))
    return _finish(result, _PROCESS_FIELDS)


def list_processes() -> list[dict[str, Any]]:
    """Enumerate process identity metadata; enumeration failure raises OSError."""
    rows: list[dict[str, Any]] = []
    if os.name == "nt":
        snapshot = _snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            succeeded = _process_first(snapshot, ctypes.byref(entry))
            while succeeded:
                rows.append({"pid": entry.th32ProcessID, "ppid": entry.th32ParentProcessID,
                             "name": entry.szExeFile, "thread_count": entry.cntThreads})
                succeeded = _process_next(snapshot, ctypes.byref(entry))
            error = ctypes.get_last_error()
            if error != 18:  # ERROR_NO_MORE_FILES is the only successful terminator.
                raise ctypes.WinError(error)
        finally:
            if not _close_handle(snapshot):
                raise ctypes.WinError(ctypes.get_last_error())
    elif sys.platform.startswith("linux"):
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                name, stat = _proc_stat(int(path.name))
                rows.append({"pid": int(path.name), "ppid": int(stat[1]), "name": name,
                             "thread_count": int(stat[17])})
            except (OSError, ValueError, IndexError):
                continue  # Exits/access-denied entries do not invalidate the snapshot.
    else:
        raise OSError("Native process enumeration unavailable on this platform")
    return rows


def sample_system_resources() -> dict[str, Any]:
    """Physical availability and total commit/limit bytes (not process pagefile quota)."""
    fields = ("available_physical_bytes", "committed_bytes", "commit_limit_bytes")
    result = _result(fields, "win32" if os.name == "nt" else "procfs")
    if os.name == "nt":
        memory = _MemoryStatus()
        memory.dwLength = ctypes.sizeof(memory)
        if _memory_status(ctypes.byref(memory)):
            result["available_physical_bytes"] = memory.ullAvailPhys
        else:
            _win_error(result, "GlobalMemoryStatusEx")
        performance = _PerformanceInfo()
        performance.cb = ctypes.sizeof(performance)
        if _performance_info(ctypes.byref(performance), performance.cb):
            result.update(committed_bytes=performance.CommitTotal * performance.PageSize,
                          commit_limit_bytes=performance.CommitLimit * performance.PageSize)
        else:
            _win_error(result, "GetPerformanceInfo")
    elif sys.platform.startswith("linux"):
        try:
            values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            for key, native in (("available_physical_bytes", "MemAvailable"),
                                ("committed_bytes", "Committed_AS"), ("commit_limit_bytes", "CommitLimit")):
                if native in values:
                    result[key] = int(values[native].split()[0]) * 1024
        except (OSError, ValueError) as error:
            _error(result, "proc/meminfo", error if isinstance(error, OSError) else OSError(str(error)))
    else:
        result["source"] = "unavailable"
        result["errors"]["platform"] = {"code": "unsupported", "message": "Native counters unavailable on this platform"}
    return _finish(result, fields)
