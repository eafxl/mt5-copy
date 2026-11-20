"""Age out the files the app writes about itself: traces, logs, debug dumps.

WHY THIS EXISTS
    Every one of these is append-only and rolls by day or by session, so the
    directories only ever grow. A single 11-hour run produced an 11.8 MB trace
    before the tracer learned to filter; logs and dumps are smaller but just as
    endless. On a machine that runs the copier daily this is the one part of
    the install that silently consumes the disk, and the oldest of it is the
    least useful.

WHAT IT WILL AND WILL NOT DELETE
    Only files matching the patterns THIS app writes, listed per directory
    below. A sweep runs over directories the user can open and drop things
    into — a note, a saved screenshot, an exported CSV — and deleting one of
    those would be indefensible, so anything unrecognised is left alone
    forever, however old.

    ``config/`` and ``cache/`` are never swept. Config is the user's. The cache
    holds the ForexFactory calendar, which is not output but STATE: deleting it
    forces a refetch from a feed that rate-limits to roughly one request per
    five minutes, so a cleanup would cost news protection rather than disk.

AGE IS MTIME, NOT THE NAME
    A file named for the 3rd may still be open and appended to on the 4th.
    Last-write time is the only measure that cannot delete something in use,
    and the active files are additionally skipped by path.
"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

SECONDS_PER_DAY = 86400.0

#: What each directory is allowed to lose, as glob patterns matched against
#: the file name. Anything else in these directories is not ours to delete.
SWEPT: dict[str, tuple[str, ...]] = {
    "traces": ("*_traces.jsonl",),
    "logs": ("copier_*.log", "worker_*.log", "*.log.old"),
    "dumps": ("*_debug.jsonl", "*_crash.json"),
}


@dataclass
class SweepReport:
    """What one sweep did. Logged, and shown by the Diagnostics dump."""
    deleted: list[Path] = field(default_factory=list)
    bytes_freed: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)
    scanned: int = 0

    @property
    def count(self) -> int:
        return len(self.deleted)

    def describe(self) -> str:
        if not self.deleted and not self.failed:
            return f"nothing to remove ({self.scanned} file(s) checked)"
        parts = [f"removed {self.count} file(s), {self.bytes_freed / 1e6:.1f} MB"]
        if self.failed:
            parts.append(f"{len(self.failed)} could not be removed")
        return "; ".join(parts)


def sweep_directory(
    directory: Path,
    patterns: Iterable[str],
    older_than_s: float,
    now: float | None = None,
    keep: Iterable[Path] = (),
    report: SweepReport | None = None,
) -> SweepReport:
    """Delete matching files in ``directory`` last written before the cutoff.

    ``keep`` is a set of paths never deleted regardless of age — the files this
    process is writing right now. Their mtime already protects them, but a
    clock that jumps backwards would not, and a deleted open log file is a
    debugging session lost at exactly the moment one is needed.
    """
    report = report or SweepReport()
    if not directory.is_dir():
        return report

    cutoff = (time.time() if now is None else now) - older_than_s
    protected = {p.resolve() for p in keep}

    seen: set[Path] = set()
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            report.scanned += 1
            try:
                if path.resolve() in protected:
                    continue
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue
                size = stat.st_size
                path.unlink()
            except OSError as e:
                # A file held open by another process (Windows) or a permission
                # problem is normal, not a failure of the app. Recorded so it
                # shows up in Diagnostics, never raised.
                report.failed.append((path, str(e)))
                continue
            report.deleted.append(path)
            report.bytes_freed += size

    return report


def sweep(
    days: float,
    now: float | None = None,
    keep: Iterable[Path] = (),
    dirs: dict[str, Path] | None = None,
) -> SweepReport:
    """Age out every swept directory. ``days <= 0`` keeps everything.

    ``dirs`` overrides the resolved locations, for tests.
    """
    report = SweepReport()
    if days <= 0:
        return report

    if dirs is None:
        from .paths import dumps_dir, logs_dir, traces_dir
        dirs = {"traces": traces_dir(), "logs": logs_dir(), "dumps": dumps_dir()}

    older_than_s = days * SECONDS_PER_DAY
    for name, patterns in SWEPT.items():
        directory = dirs.get(name)
        if directory is None:
            continue
        sweep_directory(
            Path(directory), patterns, older_than_s,
            now=now, keep=keep, report=report,
        )
    return report


def active_files() -> list[Path]:
    """Files this process is writing: today's trace, dump, and session log.

    Resolved defensively — a sweep must never be skipped because one of these
    could not be named.
    """
    out: list[Path] = []
    try:
        from .paths import dumps_dir, logs_dir, traces_dir
        from .utils.logging import session_stamp

        stamp = session_stamp()
        out.append(traces_dir() / f"{stamp}_traces.jsonl")
        out.append(dumps_dir() / f"{stamp}_debug.jsonl")
        out.append(logs_dir() / f"copier_{stamp}.log")
        out.append(logs_dir() / f"worker_{stamp}.log")
    except Exception:
        pass
    return out


class RetentionSweeper:
    """Runs a sweep at startup and once a day after that.

    A daemon of its own would be one more thread to shut down cleanly for a
    job that takes milliseconds, so this is driven by whatever loop already
    ticks — ``due()`` is cheap and ``run_if_due()`` does nothing until a day
    has passed.
    """

    INTERVAL_S = SECONDS_PER_DAY

    def __init__(self, days: float, clock: Callable[[], float] = time.time):
        self.days = days
        self._clock = clock
        self._last_run: float | None = None
        self.last_report: SweepReport | None = None

    def due(self) -> bool:
        if self.days <= 0:
            return False
        if self._last_run is None:
            return True
        return (self._clock() - self._last_run) >= self.INTERVAL_S

    def run_if_due(self) -> SweepReport | None:
        if not self.due():
            return None
        return self.run()

    def run(self) -> SweepReport:
        now = self._clock()
        self._last_run = now
        self.last_report = sweep(self.days, now=now, keep=active_files())
        return self.last_report
