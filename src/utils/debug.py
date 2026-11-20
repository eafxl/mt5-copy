"""Comprehensive debugging utilities for production/Nuitka deployment.

Provides system info, thread dumps, crash handlers, and memory stats
for debugging compiled applications where standard debugging isn't available.

All debug writes are ASYNC - never blocks the copy path.
"""
import atexit
import gc
import json
import os
import platform
import signal
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .async_writer import AsyncFileWriter


class DebugDumper:
    """Comprehensive debug information dumper.

    Captures system state, threads, memory, and crash info.
    All writes are async (never blocks copy path).
    """

    def __init__(self, dump_dir: Path | None = None):
        if dump_dir is None:
            from ..paths import dumps_dir
            dump_dir = dumps_dir()

        self.dump_dir = dump_dir
        self.dump_dir.mkdir(parents=True, exist_ok=True)

        self._writer = AsyncFileWriter(
            get_file_path=self._get_dump_path,
            max_queue_size=1000,
            flush_interval=0.5,
        )

        self._start_time = datetime.now()
        self._crash_count = 0

    def _get_dump_path(self) -> Path:
        """This RUN's dump file — same session stamp as the log and the trace."""
        from .logging import session_stamp

        return self.dump_dir / f"{session_stamp()}_debug.jsonl"

    def _write(self, record: dict):
        """Write a debug record (async, non-blocking)."""
        record["_ts"] = datetime.utcnow().isoformat() + "Z"
        self._writer.write(json.dumps(record, default=str))

    def dump_system_info(self) -> dict:
        """Dump comprehensive system information."""
        info = {
            "type": "system_info",
            "python": {
                "version": sys.version,
                "executable": sys.executable,
                "platform": sys.platform,
                "prefix": sys.prefix,
                "is_frozen": getattr(sys, "frozen", False),
            },
            "os": {
                "name": os.name,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "release": platform.release(),
                "version": platform.version(),
            },
            "process": {
                "pid": os.getpid(),
                "ppid": os.getppid() if hasattr(os, "getppid") else None,
                "cwd": os.getcwd(),
            },
            "env": {
                k: v for k, v in os.environ.items()
                if any(key in k.upper() for key in ["PATH", "PYTHON", "MT5", "TEMP", "HOME", "USER"])
            },
        }

        # CPU count
        try:
            import multiprocessing
            info["cpu_count"] = multiprocessing.cpu_count()
        except Exception:
            pass

        self._write(info)
        return info

    def dump_memory_stats(self) -> dict:
        """Dump memory statistics."""
        import sys

        gc.collect()

        info = {
            "type": "memory_stats",
            "gc": {
                "enabled": gc.isenabled(),
                "counts": gc.get_count(),
                "threshold": gc.get_threshold(),
                "objects": len(gc.get_objects()),
            },
            "sys": {
                "getsizeof_empty_list": sys.getsizeof([]),
                "getsizeof_empty_dict": sys.getsizeof({}),
            },
        }

        # Try to get process memory (Windows)
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            c_ulong = ctypes.c_ulong
            c_ulonglong = ctypes.c_ulonglong

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", c_ulong),
                    ("PageFaultCount", c_ulong),
                    ("PeakWorkingSetSize", c_ulonglong),
                    ("WorkingSetSize", c_ulonglong),
                    ("QuotaPeakPagedPoolUsage", c_ulonglong),
                    ("QuotaPagedPoolUsage", c_ulonglong),
                    ("QuotaPeakNonPagedPoolUsage", c_ulonglong),
                    ("QuotaNonPagedPoolUsage", c_ulonglong),
                    ("PagefileUsage", c_ulonglong),
                    ("PeakPagefileUsage", c_ulonglong),
                ]

            GetProcessMemoryInfo = ctypes.windll.psapi.GetProcessMemoryInfo
            GetCurrentProcess = kernel32.GetCurrentProcess

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            if GetProcessMemoryInfo(GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                info["process_memory"] = {
                    "working_set_mb": pmc.WorkingSetSize / (1024 * 1024),
                    "peak_working_set_mb": pmc.PeakWorkingSetSize / (1024 * 1024),
                    "pagefile_usage_mb": pmc.PagefileUsage / (1024 * 1024),
                    "page_fault_count": pmc.PageFaultCount,
                }
        except Exception:
            pass

        # Try tracemalloc if available
        try:
            import tracemalloc
            if tracemalloc.is_tracing():
                current, peak = tracemalloc.get_traced_memory()
                info["tracemalloc"] = {
                    "current_mb": current / (1024 * 1024),
                    "peak_mb": peak / (1024 * 1024),
                }
        except Exception:
            pass

        self._write(info)
        return info

    def dump_thread_info(self) -> dict:
        """Dump thread information with stack traces."""
        info = {
            "type": "thread_dump",
            "active_count": threading.active_count(),
            "main_thread_id": threading.main_thread().ident,
            "current_thread_id": threading.current_thread().ident,
            "threads": [],
        }

        for thread in threading.enumerate():
            thread_info = {
                "id": thread.ident,
                "name": thread.name,
                "daemon": thread.daemon,
                "alive": thread.is_alive(),
            }

            # Get stack trace for this thread
            if thread.ident in sys._current_frames():
                frame = sys._current_frames()[thread.ident]
                thread_info["stack"] = traceback.format_stack(frame)

            info["threads"].append(thread_info)

        self._write(info)
        return info

    def dump_exception(self, exc_type, exc_value, exc_tb, context: str = "") -> dict:
        """Dump exception with full traceback."""
        self._crash_count += 1

        info = {
            "type": "exception",
            "crash_count": self._crash_count,
            "context": context,
            "exception": {
                "type": str(exc_type.__name__) if exc_type else "Unknown",
                "message": str(exc_value) if exc_value else "",
                "traceback": traceback.format_exception(exc_type, exc_value, exc_tb),
            },
            "thread": {
                "id": threading.current_thread().ident,
                "name": threading.current_thread().name,
            },
        }

        self._write(info)
        return info

    def dump_state(self, state: dict[str, Any], label: str = "state") -> dict:
        """Dump arbitrary application state."""
        info = {
            "type": "state_dump",
            "label": label,
            "state": state,
        }
        self._write(info)
        return info

    def dump_startup_info(self) -> dict:
        """Dump startup information."""
        info = {
            "type": "startup",
            "argv": sys.argv,
            "start_time": self._start_time.isoformat(),
        }

        # Check if running under Nuitka
        if "__compiled__" in dir():
            info["nuitka"] = True
        elif getattr(sys, "frozen", False):
            info["frozen"] = True
            info["frozen_by"] = getattr(sys, "_MEIPASS", "unknown")

        self._write(info)
        self.dump_system_info()
        return info

    def dump_shutdown_info(self) -> dict:
        """Dump shutdown information."""
        runtime = datetime.now() - self._start_time

        info = {
            "type": "shutdown",
            "runtime_seconds": runtime.total_seconds(),
            "crash_count": self._crash_count,
        }

        self._write(info)
        self.dump_memory_stats()
        return info

    def get_stats(self) -> dict:
        """Get debug dumper statistics."""
        return {
            "start_time": self._start_time.isoformat(),
            "runtime_seconds": (datetime.now() - self._start_time).total_seconds(),
            "crash_count": self._crash_count,
            "writer": {
                "total_writes": self._writer.total_writes,
                "dropped": self._writer.dropped_count,
                "pending": self._writer.pending_count,
            },
        }

    def close(self):
        """Close the dumper."""
        self._writer.close()


# Global debug dumper
_dumper: DebugDumper | None = None


def setup_debug(dump_dir: Path | None = None, install_signal_handlers: bool = True) -> DebugDumper:
    """Initialize the global debug dumper with crash handlers.

    Args:
        dump_dir: Directory for debug dump files.
        install_signal_handlers: If False, skip installing SIGINT/SIGTERM
            handlers.  Set to False when running inside a GUI to avoid
            conflicts with Qt's own signal handling.
    """
    global _dumper

    _dumper = DebugDumper(dump_dir)

    # Install global exception handler
    original_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_tb):
        """Global exception handler that dumps crash info."""
        if _dumper:
            _dumper.dump_exception(exc_type, exc_value, exc_tb, context="uncaught_exception")
            _dumper.dump_thread_info()
            _dumper.dump_memory_stats()
        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    # Install threading exception handler (Python 3.8+)
    if hasattr(threading, "excepthook"):
        original_threading_excepthook = threading.excepthook

        def threading_excepthook(args):
            if _dumper:
                _dumper.dump_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                    context=f"thread_exception:{args.thread.name if args.thread else 'unknown'}"
                )
            original_threading_excepthook(args)

        threading.excepthook = threading_excepthook

    # Register shutdown handler
    def shutdown_handler():
        if _dumper:
            _dumper.dump_shutdown_info()
            _dumper.close()

    atexit.register(shutdown_handler)

    # Try to catch signals (SIGTERM, SIGINT)
    if install_signal_handlers:
        def signal_handler(signum, frame):
            if _dumper:
                _dumper.dump_state({"signal": signum}, label="signal_received")
                _dumper.dump_thread_info()
            # Re-raise to let default handler run
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
        except Exception:
            pass  # Signals may not work in all environments

    # Dump startup info
    _dumper.dump_startup_info()

    return _dumper


def get_dumper() -> DebugDumper | None:
    """Get the global debug dumper."""
    return _dumper


def dump_state(state: dict[str, Any], label: str = "state"):
    """Dump application state (convenience function)."""
    if _dumper:
        _dumper.dump_state(state, label)


def dump_thread_info():
    """Dump thread info (convenience function)."""
    if _dumper:
        _dumper.dump_thread_info()


def dump_memory():
    """Dump memory stats (convenience function)."""
    if _dumper:
        _dumper.dump_memory_stats()


def enable_tracemalloc():
    """Enable tracemalloc for detailed memory tracking."""
    try:
        import tracemalloc
        tracemalloc.start()
    except Exception:
        pass


def get_debug_stats() -> dict:
    """Get all debug statistics."""
    from .logging import get_logging_stats
    from .tracer import get_tracer

    stats = {
        "debug": _dumper.get_stats() if _dumper else {"enabled": False},
        "logging": get_logging_stats(),
        "tracer": get_tracer().get_stats(),
    }
    return stats
