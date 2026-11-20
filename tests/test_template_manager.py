"""Unit tests for src/template_manager.py.

Covers the pure helpers (no MT5, no installer, no UI):
  - terminal_exe
  - backup_path_for
  - is_virgin (positive + negative)
  - validate_template
  - copy_template + restore_backup behavior
  - find_stale_backups (via monkeypatched DEFAULT_INSTALL_PATHS)
"""
import os
import pytest

from src import template_manager as tm


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_terminal_exe_finds_terminal64(tmp_path):
    _touch(tmp_path / "terminal64.exe")
    assert tm.terminal_exe(tmp_path) == tmp_path / "terminal64.exe"


def test_terminal_exe_falls_back_to_terminal(tmp_path):
    _touch(tmp_path / "terminal.exe")
    assert tm.terminal_exe(tmp_path) == tmp_path / "terminal.exe"


def test_terminal_exe_returns_none(tmp_path):
    assert tm.terminal_exe(tmp_path) is None


def test_backup_path_for(tmp_path):
    p = tmp_path / "MetaTrader 5"
    assert tm.backup_path_for(p).name == "MetaTrader 5" + tm.BACKUP_SUFFIX


def test_is_virgin_empty_folder(tmp_path):
    _touch(tmp_path / "terminal64.exe")
    ok, reason = tm.is_virgin(tmp_path)
    assert ok and reason == ""


def test_is_virgin_accounts_ini_dirty(tmp_path):
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "config" / "accounts.ini")
    ok, reason = tm.is_virgin(tmp_path)
    assert not ok and "accounts.ini" in reason


def test_is_virgin_history_dirty(tmp_path):
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "bases" / "BrokerX" / "history" / "EURUSD.hst")
    ok, reason = tm.is_virgin(tmp_path)
    assert not ok and "BrokerX" in reason


def test_is_virgin_ignores_default_broker(tmp_path):
    """bases/Default/history is the demo broker auto-populated by MT5 itself.

    The installer's "Launch MetaTrader 5" checkbox is on by default and a
    brief auto-launch produces this content before we can kill the process.
    Must NOT be flagged as dirty.
    """
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "bases" / "Default" / "history" / "EURUSD.hst")
    _touch(tmp_path / "bases" / "Default" / "history" / "GBPUSD.hst")
    ok, _ = tm.is_virgin(tmp_path)
    assert ok


def test_is_virgin_default_case_insensitive(tmp_path):
    """Default broker name match must be case-insensitive."""
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "bases" / "DEFAULT" / "history" / "x.hst")
    assert tm.is_virgin(tmp_path)[0]


def test_is_virgin_real_broker_with_default(tmp_path):
    """Real broker history is dirty even if Default also present."""
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "bases" / "Default" / "history" / "x.hst")
    _touch(tmp_path / "bases" / "FTMO-Server" / "history" / "EURUSD.hst")
    ok, reason = tm.is_virgin(tmp_path)
    assert not ok and "FTMO-Server" in reason


def test_is_virgin_ignores_logs(tmp_path):
    """Logs dir alone must NOT mark a folder dirty — auto-launch creates it."""
    _touch(tmp_path / "terminal64.exe")
    _touch(tmp_path / "logs" / "20260520.log")
    ok, _ = tm.is_virgin(tmp_path)
    assert ok


def test_validate_template_empty():
    ok, msg = tm.validate_template("")
    assert not ok and "not configured" in msg


def test_validate_template_missing(tmp_path):
    ok, msg = tm.validate_template(str(tmp_path / "nope"))
    assert not ok and "missing" in msg


def test_validate_template_no_exe(tmp_path):
    (tmp_path / "Templates").mkdir()
    ok, msg = tm.validate_template(str(tmp_path))
    assert not ok and "terminal64.exe" in msg


def test_validate_template_ok(tmp_path):
    _touch(tmp_path / "terminal64.exe")
    ok, msg = tm.validate_template(str(tmp_path))
    assert ok and "OK" in msg


def test_copy_template_basic(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "terminal64.exe")
    _touch(src / "MQL5" / "Indicators" / "x.ex5")
    tm.copy_template(src, dst)
    assert (dst / "terminal64.exe").is_file()
    assert (dst / "MQL5" / "Indicators" / "x.ex5").is_file()


def test_copy_template_target_exists_raises(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "terminal64.exe")
    dst.mkdir()
    with pytest.raises(FileExistsError):
        tm.copy_template(src, dst)


def test_rename_existing_and_restore(tmp_path):
    install = tmp_path / "MetaTrader 5"
    _touch(install / "terminal64.exe")
    backup = tm.rename_existing(install)
    assert backup.exists() and not install.exists()
    assert tm.restore_backup(install) is True
    assert install.exists() and not backup.exists()


def test_restore_backup_target_exists_no_replace(tmp_path):
    install = tmp_path / "MetaTrader 5"
    _touch(install / "terminal64.exe")
    backup = tm.rename_existing(install)
    _touch(install / "terminal64.exe")  # fresh install at the same path
    with pytest.raises(OSError):
        tm.restore_backup(install)


def test_restore_backup_replace_existing(tmp_path):
    install = tmp_path / "MetaTrader 5"
    _touch(install / "terminal64.exe")
    (install / "marker.txt").write_text("original")
    backup = tm.rename_existing(install)
    # Simulate fresh install at the original path
    _touch(install / "terminal64.exe")
    (install / "marker.txt").write_text("fresh")
    assert tm.restore_backup(install, replace_existing=True)
    assert (install / "marker.txt").read_text() == "original"


def test_find_stale_backups(monkeypatch, tmp_path):
    p1 = tmp_path / "MT5_a"
    _touch(tm.backup_path_for(p1) / "terminal64.exe")  # backup folder with terminal
    monkeypatch.setattr(tm, "DEFAULT_INSTALL_PATHS", (p1,))
    found = tm.find_stale_backups()
    assert tm.backup_path_for(p1) in found


def test_slaves_dir_is_path():
    """Smoke: returns a Path (Windows or POSIX), and is constant within a process."""
    d = tm.slaves_dir()
    assert d == tm.slaves_dir()
    assert d.name  # non-empty


def test_template_dir_under_slaves_dir():
    assert tm.template_dir().parent == tm.slaves_dir()
    assert tm.template_dir().name == "_template"


def test_collect_slave_terminals_on_disk_skips_template(monkeypatch, tmp_path):
    """Scan returns slave folder terminal paths but never the _template one."""
    # Point slaves_dir() at a tmp dir for the duration of this test.
    monkeypatch.setattr(tm, "slaves_dir", lambda: tmp_path)
    # Create three subfolders: two slaves with terminal64.exe, plus _template.
    (tmp_path / "MetaTrader 5 Slave1").mkdir()
    _touch(tmp_path / "MetaTrader 5 Slave1" / "terminal64.exe")
    (tmp_path / "MetaTrader 5 Bybit").mkdir()
    _touch(tmp_path / "MetaTrader 5 Bybit" / "terminal64.exe")
    (tmp_path / "_template").mkdir()
    _touch(tmp_path / "_template" / "terminal64.exe")
    # And a folder without terminal exe — should be ignored.
    (tmp_path / "MetaTrader 5 Empty").mkdir()

    out = tm.collect_slave_terminals_on_disk()
    assert str(tmp_path / "MetaTrader 5 Slave1" / "terminal64.exe") in out
    assert str(tmp_path / "MetaTrader 5 Bybit" / "terminal64.exe") in out
    # Template must NEVER be in the set — otherwise shutdown would kill it.
    assert str(tmp_path / "_template" / "terminal64.exe") not in out
    # Empty folder ignored.
    assert len(out) == 2


def test_collect_slave_terminals_on_disk_missing(monkeypatch, tmp_path):
    """Returns empty set if slaves_dir doesn't exist on disk."""
    fake = tmp_path / "does_not_exist"
    monkeypatch.setattr(tm, "slaves_dir", lambda: fake)
    assert tm.collect_slave_terminals_on_disk() == set()


def test_list_terminal64_pids_with_paths_non_windows(monkeypatch):
    """Returns [] on non-Windows platforms without invoking subprocess."""
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    assert tm.list_terminal64_pids_with_paths() == []


def test_find_running_terminal_pids_non_windows(monkeypatch, tmp_path):
    """Returns [] on non-Windows."""
    monkeypatch.setattr(tm.sys, "platform", "darwin")
    assert tm.find_running_terminal_pids(tmp_path / "terminal64.exe") == []