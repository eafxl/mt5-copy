"""Retention: the app's own output files age out, and nothing else does.

WHY THIS FILE EXISTS
    A cleanup that deletes too little is a disk that fills up over months —
    annoying, visible, fixable. A cleanup that deletes too much destroys the
    evidence of the incident the user is investigating, or a file they put
    there themselves, and it does so silently. The tests are weighted
    accordingly: most of them pin what must SURVIVE a sweep.
"""
import time
from pathlib import Path

import pytest

from src.config import RetentionConfig
from src.retention import (
    SECONDS_PER_DAY,
    RetentionSweeper,
    SweepReport,
    sweep,
    sweep_directory,
)

NOW = 1_700_000_000.0


@pytest.fixture
def tree(tmp_path):
    """A data root with the three swept directories."""
    dirs = {}
    for name in ("traces", "logs", "dumps"):
        d = tmp_path / name
        d.mkdir()
        dirs[name] = d
    return dirs


def write(directory: Path, name: str, age_days: float, body: str = "x") -> Path:
    """A file whose last write was ``age_days`` ago."""
    path = directory / name
    path.write_text(body, encoding="utf-8")
    stamp = NOW - age_days * SECONDS_PER_DAY
    import os
    os.utime(path, (stamp, stamp))
    return path


# ==================== what it removes ====================

def test_a_trace_older_than_the_window_goes(tree):
    old = write(tree["traces"], "20240101_traces.jsonl", age_days=9)

    report = sweep(7, now=NOW, dirs=tree)

    assert not old.exists()
    assert report.count == 1


def test_a_trace_inside_the_window_stays(tree):
    recent = write(tree["traces"], "20240101_traces.jsonl", age_days=6.9)

    sweep(7, now=NOW, dirs=tree)

    assert recent.exists()


def test_every_swept_directory_is_visited(tree):
    write(tree["traces"], "20240101_traces.jsonl", age_days=30)
    write(tree["logs"], "copier_20240101_120000.log", age_days=30)
    write(tree["logs"], "worker_20240101.log", age_days=30)
    write(tree["dumps"], "20240101_debug.jsonl", age_days=30)

    report = sweep(7, now=NOW, dirs=tree)

    assert report.count == 4


def test_bytes_freed_is_reported(tree):
    write(tree["traces"], "20240101_traces.jsonl", age_days=30, body="x" * 500)

    report = sweep(7, now=NOW, dirs=tree)

    assert report.bytes_freed == 500


# ==================== what it must NOT remove ====================

def test_an_unrecognised_file_is_never_deleted(tree):
    """The directories are the user's to open — and to drop things into."""
    note = write(tree["traces"], "notes-about-the-crash.txt", age_days=400)
    export = write(tree["logs"], "for-support.zip", age_days=400)

    sweep(7, now=NOW, dirs=tree)

    assert note.exists()
    assert export.exists()


def test_config_and_cache_are_not_swept_directories(tmp_path):
    """The calendar cache is STATE — refetching it costs news protection."""
    from src.retention import SWEPT

    assert "cache" not in SWEPT
    assert "config" not in SWEPT


def test_zero_days_keeps_everything(tree):
    ancient = write(tree["traces"], "20200101_traces.jsonl", age_days=3650)

    report = sweep(0, now=NOW, dirs=tree)

    assert ancient.exists()
    assert report.count == 0


def test_a_file_being_written_survives_its_own_age(tree):
    """A clock that jumped backwards must not cost us the live log."""
    live = write(tree["logs"], "copier_20240101_120000.log", age_days=30)

    sweep(7, now=NOW, dirs=tree, keep=[live])

    assert live.exists()


def test_a_subdirectory_is_left_alone(tree):
    nested = tree["traces"] / "20240101_traces.jsonl"
    nested.mkdir()                       # a directory wearing a matching name

    sweep(7, now=NOW, dirs=tree)

    assert nested.is_dir()


# ==================== failure is reported, never raised ====================

def test_an_undeletable_file_is_reported_not_raised(tree, monkeypatch):
    write(tree["traces"], "20240101_traces.jsonl", age_days=30)

    def refuse(self, *a, **k):
        raise PermissionError("file is open in another process")

    monkeypatch.setattr(Path, "unlink", refuse)

    report = sweep(7, now=NOW, dirs=tree)

    assert report.count == 0
    assert len(report.failed) == 1
    assert "another process" in report.failed[0][1]


def test_a_missing_directory_is_not_an_error(tmp_path):
    report = sweep_directory(tmp_path / "nope", ("*.log",), SECONDS_PER_DAY)

    assert report.count == 0


def test_describe_reads_as_a_log_line(tree):
    write(tree["traces"], "20240101_traces.jsonl", age_days=30, body="x" * 1_000_000)

    assert "removed 1 file(s)" in sweep(7, now=NOW, dirs=tree).describe()
    assert "nothing to remove" in SweepReport().describe()


# ==================== the schedule ====================

def test_the_first_call_is_always_due():
    assert RetentionSweeper(7, clock=lambda: NOW).due()


def test_it_does_not_run_twice_in_a_day(tree, monkeypatch):
    clock = {"t": NOW}
    sweeper = RetentionSweeper(7, clock=lambda: clock["t"])
    monkeypatch.setattr("src.retention.sweep", lambda *a, **k: SweepReport())

    sweeper.run()
    clock["t"] += 3600 * 23

    assert sweeper.run_if_due() is None


def test_it_runs_again_the_next_day(tree, monkeypatch):
    clock = {"t": NOW}
    sweeper = RetentionSweeper(7, clock=lambda: clock["t"])
    monkeypatch.setattr("src.retention.sweep", lambda *a, **k: SweepReport())

    sweeper.run()
    clock["t"] += SECONDS_PER_DAY + 1

    assert sweeper.run_if_due() is not None


def test_disabled_retention_is_never_due():
    assert not RetentionSweeper(0, clock=lambda: NOW).due()


# ==================== config ====================

def test_seven_days_is_the_default():
    assert RetentionConfig().days == 7
    assert RetentionConfig().effective_days == 7


def test_disabling_reads_as_keep_forever():
    """The engine asks one question — how many days — so 'off' must answer 0."""
    assert RetentionConfig(enabled=False, days=7).effective_days == 0


def test_a_negative_day_count_is_clamped_not_inverted():
    """days=-1 must not become a cutoff in the FUTURE that deletes everything."""
    assert RetentionConfig(days=-1).days == 0


def test_config_yaml_carries_the_section(tmp_path):
    import yaml
    from src.config import Config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "master": {"terminal_path": "x"},
        "slave_terminal": {},
        "slaves": [],
        "retention": {"enabled": True, "days": 3},
    }), encoding="utf-8")

    assert Config.load(path).retention.days == 3


def test_an_old_config_without_the_section_gets_the_default(tmp_path):
    import yaml
    from src.config import Config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "master": {"terminal_path": "x"},
        "slave_terminal": {},
        "slaves": [],
    }), encoding="utf-8")

    assert Config.load(path).retention.days == 7
