"""Slave terminal template manager.

A template is a one-time snapshot of a virgin MT5 install. Every new slave
terminal folder is created by copying the template, so slaves never inherit
master/broker state from the master folder.

Seeding flow (UI-driven, see gui/settings_tab.py):
    1. User picks a downloaded `mt5setup.exe`.
    2. If a default MT5 install already exists, app renames it to
       `<path>.mt5copy_backup` so the installer's default target is free.
    3. App launches the installer (UAC prompt is normal). User clicks
       Next/Next/Finish in the wizard. User MUST NOT launch terminal64.exe.
    4. App waits for confirmation, then locates the freshly installed
       folder at a known default path.
    5. App copies the fresh folder to `<slaves_dir>/_template/`.
    6. If a backup existed, fresh install is left in place (user may keep
       it as master) and backup is restored next to it under its original
       name. If user had no MT5 before, the fresh install stays.

The MetaQuotes installer does NOT reliably honor /D=<path> across all
broker-rebranded builds, so we don't try silent install with a custom dir.
"""
import logging
import shutil
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


_logger = logging.getLogger("metact").getChild("template")


# Known default MT5 install locations. Order matters - first existing wins.
DEFAULT_INSTALL_PATHS = (
    Path(r"C:\Program Files\MetaTrader 5"),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\MetaTrader 5")),
)

BACKUP_SUFFIX = ".mt5copy_backup"


def slaves_dir() -> Path:
    """Locked slaves directory.

    Windows: C:\\Program Files\\MT5Copy
    Other (dev): ~/Documents/MT5Copy

    The directory is intentionally NOT user-configurable so:
      - Slave AppData hashes (keyed by terminal exe path) stay stable
      - Template path is derivable from this constant
      - No migration is needed when settings change
    """
    if sys.platform == "win32":
        return Path(r"C:\Program Files\MT5Copy")
    return Path.home() / "Documents" / "MT5Copy"


def template_dir() -> Path:
    """Canonical template location: <slaves_dir>/_template."""
    return slaves_dir() / "_template"


def collect_slave_terminals_on_disk() -> set[str]:
    """Scan slaves_dir and return all `MetaTrader 5 *` terminal exe paths.

    Skips `_template` so we never kill the template's terminal during
    shutdown cleanup. Returns empty set if slaves_dir doesn't exist.

    Used at shutdown to catch orphan slave folders that aren't in the
    current config (e.g. user deleted a slave from settings but the folder
    on disk still has a running terminal64.exe).
    """
    paths: set[str] = set()
    sdir = slaves_dir()
    try:
        if sdir.is_dir():
            for child in sdir.iterdir():
                if not child.is_dir() or child.name == "_template":
                    continue
                for exe_name in ("terminal64.exe", "terminal.exe"):
                    cand = child / exe_name
                    if cand.is_file():
                        paths.add(str(cand))
                        break
    except Exception as e:
        _logger.warning("collect_slave_terminals_on_disk: scan failed: %s", e)
    _logger.info(
        "collect_slave_terminals_on_disk: %d path(s) under %s", len(paths), sdir
    )
    return paths


def terminal_exe(folder: Path) -> Path | None:
    """Return path to terminal64.exe (or terminal.exe) under folder, or None."""
    for name in ("terminal64.exe", "terminal.exe"):
        p = folder / name
        if p.is_file():
            return p
    return None


def find_existing_install() -> Path | None:
    """Return the first known default MT5 install folder that exists."""
    for p in DEFAULT_INSTALL_PATHS:
        if p.is_dir() and terminal_exe(p):
            _logger.info("find_existing_install: hit at %s", p)
            return p
    _logger.info("find_existing_install: no install at default paths %s", list(DEFAULT_INSTALL_PATHS))
    return None


def is_virgin(folder: Path) -> tuple[bool, str]:
    """Heuristic: has the terminal been logged into a user account?

    Returns (virgin, reason). Reason is empty when virgin=True.

    Slaves run in NON-portable mode so each terminal uses its own
    `%APPDATA%\\MetaQuotes\\Terminal\\<hash>\\` data dir. The template
    folder's `bases/`, `logs/`, etc. are effectively dead weight after the
    copytree to a slave folder — they're never read at runtime.

    The only file in the template folder that actually leaks meaningful
    user state is `config/accounts.ini` (saved account credentials). We
    used to also flag any populated `bases/<broker>/history`, but the MT5
    installer's default "Launch MetaTrader 5" checkbox is ON and a brief
    auto-launch unavoidably populates `bases/Default/history` with demo
    broker data before we can kill the process. That data is harmless.

    Indicators of real user dirt:
      - `config/accounts.ini` (saved a broker account)
      - `bases/<broker>/history` where broker is NOT `Default` — i.e. the
        user actually selected and connected to a real broker.
    """
    _logger.info("is_virgin: checking %s", folder)
    config_accounts = folder / "config" / "accounts.ini"
    if config_accounts.exists():
        _logger.warning("is_virgin: dirty — %s exists", config_accounts)
        return False, "config/accounts.ini exists (terminal saved an account)"

    bases = folder / "bases"
    if bases.is_dir():
        for child in bases.iterdir():
            if not child.is_dir():
                continue
            if child.name.lower() == "default":
                # MT5's shipped demo broker. Auto-populated by installer /
                # brief auto-launch. Not user-specific. Skip.
                _logger.debug("is_virgin: skipping bases/Default (demo broker)")
                continue
            history = child / "history"
            if history.is_dir() and any(history.iterdir()):
                _logger.warning("is_virgin: dirty — bases/%s/history populated", child.name)
                return False, f"bases/{child.name}/history populated"

    _logger.info("is_virgin: clean")
    return True, ""


def list_terminal64_pids_with_paths() -> list[tuple[int, str]]:
    """Return [(pid, normalized_lowercase_exe_path), ...] for every running terminal64.exe.

    Primary: PowerShell `Get-Process -Name terminal64` (has Path field).
    Fallback: tasklist + ctypes QueryFullProcessImageNameW when PS fails.

    Windows-only. Returns [] on other platforms.

    `Get-Process -Name terminal64 -ErrorAction SilentlyContinue` exits 1 on
    PS 5.1 when nothing matches even with the silent error action. We treat
    (exit != 0 AND empty stdout AND empty stderr) as "no matches".
    """
    if sys.platform != "win32":
        return []
    import subprocess
    creationflags = subprocess.CREATE_NO_WINDOW
    pairs: list[tuple[int, str]] = []
    try:
        ps_cmd = (
            "Get-Process -Name terminal64 -ErrorAction SilentlyContinue"
            " | Select-Object Id, Path"
            " | ForEach-Object { $_.Id.ToString() + '|' + $_.Path }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            creationflags=creationflags,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        _logger.debug(
            "list_terminal64: ps exit=%d stdout=%d chars stderr=%r",
            result.returncode, len(stdout), stderr,
        )
        if result.returncode != 0 and not stdout and not stderr:
            return []
        if stdout:
            for line in stdout.splitlines():
                if "|" not in line:
                    continue
                pid_str, exe_path = line.split("|", 1)
                pid_str = pid_str.strip()
                exe_path = exe_path.strip()
                if pid_str.isdigit() and exe_path:
                    pairs.append((int(pid_str), os.path.normpath(exe_path).lower()))
            return pairs
        _logger.warning(
            "list_terminal64: ps exit=%d stderr=%r — falling back to tasklist",
            result.returncode, stderr,
        )
    except Exception as e:
        _logger.warning("list_terminal64: ps exception %s — trying tasklist", e)

    # Fallback: tasklist gives PIDs; ctypes resolves each PID's image path.
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=creationflags,
        )
        if out.returncode != 0:
            _logger.warning(
                "list_terminal64: tasklist exit=%d stderr=%r",
                out.returncode, out.stderr.strip(),
            )
            return []
        pids: list[int] = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
        if not pids:
            return []
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        QueryFullProcessImageName = kernel32.QueryFullProcessImageNameW
        QueryFullProcessImageName.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        QueryFullProcessImageName.restype = wintypes.BOOL
        OpenProcess = kernel32.OpenProcess
        OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        OpenProcess.restype = wintypes.HANDLE
        CloseHandle = kernel32.CloseHandle
        for pid in pids:
            h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if QueryFullProcessImageName(h, 0, buf, ctypes.byref(size)):
                    pairs.append((pid, os.path.normpath(buf.value).lower()))
            finally:
                CloseHandle(h)
        return pairs
    except Exception as e:
        _logger.warning("list_terminal64: tasklist fallback exception %s", e)
        return []


def find_running_terminal_pids(exe_path: Path) -> list[int]:
    """Return PIDs of running terminal64.exe processes whose image matches exe_path."""
    if sys.platform != "win32":
        _logger.debug("find_running_terminal_pids: non-Windows, skipping")
        return []
    norm_target = os.path.normpath(str(exe_path)).lower()
    _logger.info("find_running_terminal_pids: looking for %s", norm_target)
    pairs = list_terminal64_pids_with_paths()
    pids = [pid for pid, p in pairs if p == norm_target]
    _logger.info("find_running_terminal_pids: matched %s (of %d running)", pids, len(pairs))
    return pids


def kill_pids(pids: list[int]) -> int:
    """taskkill the given PIDs on Windows. Returns count of taskkill exits 0.

    On non-Windows or empty input returns 0.
    """
    if sys.platform != "win32" or not pids:
        return 0
    import subprocess
    killed = 0
    for pid in pids:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                killed += 1
                _logger.info("kill_pids: killed PID %d", pid)
            else:
                _logger.warning(
                    "kill_pids: taskkill PID %d returned %d: %s",
                    pid, r.returncode, r.stderr.strip() if r.stderr else "",
                )
        except Exception as e:
            _logger.warning("kill_pids: PID %d exception: %s", pid, e)
    return killed


# --------------------------------------------------------- graceful shutdown

# How long a terminal gets to save its state and exit on its own before we
# resort to taskkill /F. Observed clean exits are 2-6 s; the ceiling only
# matters when a terminal is wedged.
GRACEFUL_CLOSE_TIMEOUT_S = 20.0

_WM_CLOSE = 0x0010


def pid_alive(pid: int) -> bool:
    """True if the process is still running. Windows-only; True elsewhere."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x0
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, wintypes.DWORD(pid))
    if not handle:
        # Gone, or we can't touch it — either way we can't wait on it.
        return False
    try:
        # 0 ms wait: signalled means the process object is done.
        return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def post_close_to_pid(pid: int) -> int:
    """Post WM_CLOSE to every top-level window owned by `pid`.

    Returns the number of windows messaged. PostMessage (not SendMessage) so a
    terminal that takes seconds to flush its state can't block us.
    """
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    posted = 0
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_callback(hwnd, _lparam):
            nonlocal posted
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                posted += 1
            return True

        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    except Exception as e:
        _logger.warning("post_close_to_pid: PID %d exception: %s", pid, e)
    return posted


def request_close(pids: list[int]) -> int:
    """Ask each PID to close itself. Returns count of PIDs successfully asked.

    WM_CLOSE is what File → Exit does, and it is the ONLY path on which MT5
    writes its state back to disk: chart templates (so EA inputs survive),
    terminal.ini, and the experts config. taskkill /F skips all of that, which
    is why force-killed terminals come back with yesterday's settings.

    Falls back to `taskkill` without /F for a PID with no window of its own —
    taskkill posts WM_CLOSE too, just via its own enumeration.
    """
    if sys.platform != "win32" or not pids:
        return 0
    import subprocess

    asked = 0
    for pid in pids:
        posted = post_close_to_pid(pid)
        if posted:
            _logger.info("request_close: WM_CLOSE to %d window(s) of PID %d", posted, pid)
            asked += 1
            continue
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                _logger.info("request_close: taskkill (graceful) PID %d", pid)
                asked += 1
            else:
                _logger.warning(
                    "request_close: taskkill PID %d returned %d: %s",
                    pid, r.returncode, r.stderr.strip() if r.stderr else "",
                )
        except Exception as e:
            _logger.warning("request_close: PID %d exception: %s", pid, e)
    return asked


def wait_for_exit(pids: list[int], timeout_s: float, on_wait=None) -> list[int]:
    """Poll until every PID is gone or `timeout_s` elapses. Returns survivors.

    `on_wait` is called once per 250 ms tick — the GUI passes
    `QApplication.processEvents` so the close doesn't look like a hang.
    """
    import time

    remaining = list(pids)
    if sys.platform != "win32" or not remaining:
        return remaining

    deadline = time.monotonic() + max(0.0, timeout_s)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                pass
        remaining = [pid for pid in remaining if pid_alive(pid)]
    return remaining


def stop_pids(pids: list[int], grace_s: float = GRACEFUL_CLOSE_TIMEOUT_S,
              on_wait=None) -> tuple[int, int]:
    """Close terminals cleanly, force-killing only what refuses to go.

    Returns ``(exited_cleanly, force_killed)``.
    """
    if sys.platform != "win32" or not pids:
        return 0, 0

    total = len(pids)
    _logger.info("stop_pids: asking %d terminal(s) to close: %s", total, pids)
    request_close(pids)

    survivors = wait_for_exit(pids, grace_s, on_wait=on_wait)
    clean = total - len(survivors)
    if not survivors:
        _logger.info("stop_pids: all %d terminal(s) exited cleanly", clean)
        return clean, 0

    _logger.warning(
        "stop_pids: %d terminal(s) still alive after %.0fs — force killing %s",
        len(survivors), grace_s, survivors,
    )
    forced = kill_pids(survivors)
    return clean, forced


def backup_path_for(install_path: Path) -> Path:
    """Return the backup sibling path used by rename_existing."""
    return install_path.with_name(install_path.name + BACKUP_SUFFIX)


def kill_terminals_in(folder: Path, grace_s: float = GRACEFUL_CLOSE_TIMEOUT_S,
                      on_wait=None) -> int:
    """Stop any terminal64.exe running from `folder`. Returns count stopped.

    Used by the seed wizard before renaming a folder out of the way: if a
    terminal64.exe is still running from that folder (e.g. master pre-warm
    left it alive), Windows will block the later rename-back of the backup
    because the process holds open handles.

    Asks first, force-kills second — a clean exit both saves the terminal's
    settings and releases those handles properly.
    """
    exe = terminal_exe(folder)
    if exe is None:
        _logger.info("kill_terminals_in: no terminal exe in %s", folder)
        return 0
    pids = find_running_terminal_pids(exe)
    if not pids:
        _logger.info("kill_terminals_in: no running terminal64 at %s", exe)
        return 0
    _logger.warning("kill_terminals_in: stopping PIDs %s for %s", pids, exe)
    clean, forced = stop_pids(pids, grace_s=grace_s, on_wait=on_wait)
    _logger.info("kill_terminals_in: %d clean, %d forced (of %d)",
                 clean, forced, len(pids))
    return clean + forced


def rename_existing(install_path: Path) -> Path:
    """Rename an existing install folder out of the way.

    Returns the backup path. Raises OSError if rename fails (e.g. terminal
    running, file in use).
    """
    backup = backup_path_for(install_path)
    if backup.exists():
        # Stale backup from a previous failed run — refuse to overwrite.
        _logger.error("rename_existing: stale backup blocks rename: %s", backup)
        raise OSError(
            f"Backup folder already exists: {backup}\n"
            "Resolve manually before retrying."
        )
    _logger.info("rename_existing: renaming %s → %s", install_path, backup)
    try:
        install_path.rename(backup)
    except OSError as e:
        _logger.error("rename_existing: rename failed: %s", e)
        raise
    _logger.info("rename_existing: done")
    return backup


def restore_backup(install_path: Path, replace_existing: bool = False) -> bool:
    """Rename `<install_path>.mt5copy_backup` back to install_path.

    Returns True on success, False if no backup existed.

    If install_path already exists:
      - replace_existing=False  → raise OSError (default; safe)
      - replace_existing=True   → install_path is removed first (destructive,
        used when caller has confirmed with the user)
    """
    backup = backup_path_for(install_path)
    if not backup.exists():
        _logger.info("restore_backup: no backup at %s", backup)
        return False
    if install_path.exists():
        if not replace_existing:
            _logger.error(
                "restore_backup: target %s exists and replace_existing=False", install_path
            )
            raise OSError(
                f"Cannot restore backup: target {install_path} still exists. "
                "Move or delete it first."
            )
        _logger.warning("restore_backup: removing target %s before restore", install_path)
        try:
            shutil.rmtree(install_path)
        except OSError as e:
            _logger.exception("restore_backup: rmtree failed for %s", install_path)
            raise OSError(
                f"rmtree {install_path} failed: {e}. Backup remains at {backup}."
            ) from e
    # Try kill any terminal64.exe holding handles to the backup folder, so
    # rename can succeed. Common case: app's master pre-warm spawned a
    # terminal at the original path which is now inside `<...>.mt5copy_backup`.
    kill_terminals_in(backup)
    _logger.info("restore_backup: rename %s → %s", backup, install_path)
    try:
        backup.rename(install_path)
    except OSError as e:
        _logger.exception("restore_backup: rename failed")
        raise OSError(
            f"Could not rename backup {backup} → {install_path}: {e}. "
            "Likely a terminal64.exe still holds the folder open."
        ) from e
    _logger.info("restore_backup: success")
    return True


def find_stale_backups() -> list[Path]:
    """Return any leftover .mt5copy_backup folders next to default install paths."""
    found = []
    for p in DEFAULT_INSTALL_PATHS:
        b = backup_path_for(p)
        if b.is_dir() and terminal_exe(b):
            found.append(b)
    if found:
        _logger.warning("find_stale_backups: %s", [str(p) for p in found])
    else:
        _logger.debug("find_stale_backups: none")
    return found


def copy_template(source: Path, target: Path) -> None:
    """Copy source folder to target. Target must not exist.

    Raises whatever shutil.copytree raises on failure.
    """
    if target.exists():
        _logger.error("copy_template: target exists %s", target)
        raise FileExistsError(f"Template target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _logger.info("copy_template: copytree %s → %s", source, target)
    shutil.copytree(source, target)
    _logger.info("copy_template: done")


def now_iso() -> str:
    """UTC ISO-8601 timestamp suitable for config storage."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_template(template_path: str) -> tuple[bool, str]:
    """Check that a configured template path points to a usable folder.

    Returns (ok, message). Message describes the problem when ok=False,
    or the template summary when ok=True.
    """
    if not template_path:
        _logger.warning("validate_template: empty path")
        return False, "Template not configured. Seed it from Settings."
    p = Path(template_path)
    if not p.is_dir():
        _logger.warning("validate_template: folder missing %s", p)
        return False, f"Template folder missing: {p}"
    exe = terminal_exe(p)
    if exe is None:
        _logger.warning("validate_template: no terminal exe in %s", p)
        return False, f"Template missing terminal64.exe: {p}"
    _logger.info("validate_template: OK at %s", p)
    return True, f"Template OK: {p}"