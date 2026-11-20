"""Seed Template wizard.

Walks the user through capturing a virgin MT5 install as the slave template:
    1. Pick mt5setup.exe
    2. Detect existing default MT5 install; if found, rename it aside
       (so the new installer can use its default path without collision)
    3. Launch installer (UAC pops as needed). User clicks through wizard.
       App locks UI behind a modal "Click OK when wizard finished" prompt.
    4. Verify the freshly installed folder is present + virgin
    5. Copy to <slaves_dir>/_template
    6. Restore (or keep) the renamed install per user choice, then save
       template_path / template_source / template_created_at to config.

Failure modes are surfaced via QMessageBox; on hard failure the dialog
exits the app per the user's spec.
"""
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..template_manager import (
    DEFAULT_INSTALL_PATHS,
    find_existing_install,
    find_running_terminal_pids,
    find_stale_backups,
    is_virgin,
    kill_pids,
    kill_terminals_in,
    now_iso,
    rename_existing,
    restore_backup,
    slaves_dir as slaves_dir_fn,
    terminal_exe,
)


# Inherit from the project logger so wizard events land in logs/ when
# setup_logging has run, and to stderr otherwise (root fallback).
_logger = logging.getLogger("metact").getChild("template.wizard")


class _CopyThread(QThread):
    """Walks a directory tree and copies it file-by-file in a background thread.

    Emits:
        progress(int copied_bytes, int total_bytes)
        done(str error_or_empty)
    """

    progress = pyqtSignal(int, int)
    done = pyqtSignal(str)

    def __init__(self, source: Path, target: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.source = source
        self.target = target

    def run(self) -> None:
        try:
            _logger.info("CopyThread: starting %s → %s", self.source, self.target)
            if self.target.exists():
                _logger.error("CopyThread: target exists, aborting")
                self.done.emit(f"Target already exists: {self.target}")
                return

            # Compute total bytes for accurate progress.
            total = 0
            file_count = 0
            for root, _dirs, files in os.walk(self.source):
                for name in files:
                    file_count += 1
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
            _logger.info("CopyThread: %d files, %.1f MB total", file_count, total / (1024 * 1024))
            if total == 0:
                total = 1  # avoid div/0 in UI

            copied = 0
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.mkdir(parents=False, exist_ok=False)

            # Throttle progress signal emissions: at most ~10 Hz / 4 MB delta.
            # MT5 templates have thousands of small files; one signal per copy
            # would flood the Qt event queue and slow the dialog.
            EMIT_BYTES = 4 * 1024 * 1024
            EMIT_INTERVAL = 0.1
            next_emit_bytes = 0
            next_emit_time = 0.0

            src_root = str(self.source)
            for root, _dirs, files in os.walk(self.source):
                rel = os.path.relpath(root, src_root)
                dst_dir = self.target if rel == "." else self.target / rel
                dst_dir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    src_file = os.path.join(root, name)
                    dst_file = os.path.join(str(dst_dir), name)
                    try:
                        shutil.copy2(src_file, dst_file)
                        copied += os.path.getsize(src_file)
                    except OSError as e:
                        _logger.error("CopyThread: copy failed at %s: %s", src_file, e)
                        self.done.emit(f"copy failed at {src_file}: {e}")
                        return
                    now = time.monotonic()
                    if copied >= next_emit_bytes or now >= next_emit_time:
                        self.progress.emit(copied, total)
                        next_emit_bytes = copied + EMIT_BYTES
                        next_emit_time = now + EMIT_INTERVAL
            # Final tick so the bar definitely shows 100%.
            self.progress.emit(copied, total)
            _logger.info("CopyThread: done, copied %.1f MB", copied / (1024 * 1024))
            self.done.emit("")
        except Exception as e:  # safety net
            _logger.exception("CopyThread: unexpected failure")
            self.done.emit(str(e))


def _fatal_and_exit(parent: QWidget | None, title: str, message: str) -> None:
    """Show a fatal error dialog and quit the app on OK.

    Flushes file log handlers BEFORE sys.exit so the FATAL line is durable
    on disk — without this, the async handler's in-memory queue may be
    discarded when the interpreter shuts down before its 200ms flush tick.
    """
    _logger.error("FATAL: %s — %s", title, message.replace("\n", " | "))
    QMessageBox.critical(parent, title, message)
    app = QApplication.instance()
    if app is not None:
        app.quit()
    _logger.info("FATAL: sys.exit(2) — flushing handlers")
    try:
        logging.shutdown()
    except Exception:
        pass
    sys.exit(2)


class SeedTemplateDialog(QDialog):
    """Modal wizard that seeds the slave template.

    Result on success: self.result_data = {
        "template_path": str,
        "template_source": str,
        "template_created_at": str,
    }
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.slaves_dir = slaves_dir_fn()
        self.result_data: dict[str, str] | None = None

        self.setWindowTitle("Seed Slave Template")
        self.setModal(True)
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.info_label = QLabel(
            "This wizard captures a virgin MT5 installation as the template "
            "used to create every new slave terminal.\n\n"
            "Steps:\n"
            "  1. Pick mt5setup.exe (downloaded from MetaQuotes / your broker).\n"
            "  2. If a default MT5 install exists, it is renamed aside\n"
            "     (you can restore it later).\n"
            "  3. The installer launches. Click through the wizard.\n"
            "     IMPORTANT: on the final page, UNCHECK \"Launch MetaTrader 5\"\n"
            "     before pressing Finish. If you forget, this wizard will\n"
            "     detect the running terminal and offer to close it.\n"
            "  4. The fresh folder is copied to:\n"
            f"     {self.slaves_dir / '_template'}\n"
        )
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.status_label = QLabel("Ready.")
        self.status_label.setStyleSheet("color: #444; font-size: 11px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.start_btn = QPushButton("Pick installer and start...")
        self.start_btn.clicked.connect(self._run_flow)
        layout.addWidget(self.start_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(self.cancel_btn)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        QApplication.processEvents()

    def _pump_sleep(self, seconds: float) -> None:
        """Sleep `seconds` while keeping the Qt event loop responsive.

        Used during retry / settle waits so the dialog repaints, scrollbars
        work, and follow-up QMessageBoxes render without a frozen UI.
        """
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            QApplication.processEvents()
            time.sleep(0.05)

    def _run_flow(self) -> None:
        # Lock buttons immediately so the user can't re-trigger mid-flow
        # (e.g. spam-clicking Start while QFileDialog is open).
        _logger.info("=== Seed Template wizard: started ===")
        _logger.info("slaves_dir=%s, target=%s", self.slaves_dir, self.slaves_dir / "_template")
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        try:
            self._run_flow_inner()
            _logger.info("=== Seed Template wizard: returned normally ===")
        except SystemExit:
            _logger.info("=== Seed Template wizard: SystemExit propagating ===")
            raise
        except Exception:
            _logger.exception("=== Seed Template wizard: unhandled exception ===")
            raise
        finally:
            # Re-enable only if dialog is still open. accept()/reject() and
            # _fatal_and_exit close/kill before we get here in success/fatal
            # paths, so this only takes effect on user-cancel early returns.
            if self.isVisible():
                self.start_btn.setEnabled(True)
                self.cancel_btn.setEnabled(True)

    def _run_flow_inner(self) -> None:
        _logger.info("step 1: handle stale backups")
        if not self._handle_stale_backups():
            _logger.info("step 1: cancelled by user")
            return  # user cancelled — finally re-enables buttons

        _logger.info("step 2: pick installer")
        installer = self._pick_installer()
        if not installer:
            _logger.info("step 2: cancelled by user (no installer picked)")
            return
        _logger.info("step 2: installer=%s", installer)

        target = self.slaves_dir / "_template"
        if target.exists():
            _logger.warning("step 3: existing template at %s — asking user to re-seed", target)
            reply = QMessageBox.question(
                self,
                "Template exists",
                f"Template already exists at:\n{target}\n\n"
                "Re-seeding will DELETE it and capture a new one. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                self._set_status(f"Removing existing template at {target}…")
                shutil.rmtree(target)
            except Exception as e:
                _fatal_and_exit(
                    self,
                    "Cannot remove existing template",
                    f"Failed to remove {target}:\n{e}\n\nApp will close.",
                )

        # Snapshot ALL pre-existing MT5 installs at default paths. The one we
        # rename aside is `existing`; the rest are recorded so _find_fresh_install
        # later doesn't mistake an untouched master install for the fresh one.
        pre_existing_others: list[Path] = [
            p for p in DEFAULT_INSTALL_PATHS if p.is_dir() and terminal_exe(p)
        ]
        _logger.info("step 4: pre-existing MT5 installs (all defaults): %s",
                     [str(p) for p in pre_existing_others])
        existing = find_existing_install()
        if existing is not None and existing in pre_existing_others:
            pre_existing_others.remove(existing)
        _logger.info("step 4: rename target=%s, other_existing=%s",
                     existing, [str(p) for p in pre_existing_others])
        backup_made: Path | None = None
        if existing is not None:
            # Pre-flight: any terminal64.exe still running from `existing`
            # would lock the folder and block both the rename here AND the
            # later rename-back. Master pre-warm at app startup often leaves
            # one running. Ask user, then force-kill.
            running_at_existing = find_running_terminal_pids(
                terminal_exe(existing) or existing / "terminal64.exe"
            )
            if running_at_existing:
                _logger.warning(
                    "step 4: terminal64.exe running at %s (PIDs %s) — must be killed",
                    existing, running_at_existing,
                )
                reply = QMessageBox.question(
                    self,
                    "Master MT5 is running",
                    f"MT5 is running at:\n  {existing}\n  PIDs: {running_at_existing}\n\n"
                    "Seeding requires the folder to be free of locks. "
                    "If you're using this terminal as master, stop the Copy "
                    "service (Service tab → Stop) before seeding, then retry.\n\n"
                    "Click Yes to FORCE-KILL the terminal and continue, "
                    "or Cancel to abort.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    _logger.info("step 4: user cancelled at running-terminal prompt")
                    return
                from PyQt6.QtWidgets import QApplication
                killed = kill_terminals_in(
                    existing, on_wait=QApplication.processEvents,
                )
                _logger.info("step 4: stopped %d terminal(s) at %s", killed, existing)
                # Brief settle so Windows releases handles before rename.
                self._pump_sleep(1.0)

            self._set_status(f"Existing MT5 found at {existing}. Renaming aside…")
            try:
                backup_made = rename_existing(existing)
            except OSError as e:
                _logger.exception("step 4: rename_existing failed")
                _fatal_and_exit(
                    self,
                    "Cannot rename existing MT5",
                    f"{e}\n\nClose MT5 if running, then retry. App will close.",
                )
            _logger.info("step 4: backup at %s", backup_made)
        else:
            _logger.info("step 4: no existing MT5 to rename")

        # Launch installer. User clicks through wizard (UAC prompt expected).
        self._set_status("Launching MT5 installer…")
        _logger.info("step 5: launching installer subprocess: %s", installer)
        try:
            installer_proc = subprocess.Popen([str(installer)])
            _logger.info("step 5: installer launched, PID=%s", installer_proc.pid)
        except Exception as e:
            _logger.exception("step 5: Popen failed")
            self._restore_or_keep(existing, backup_made, restore=True)
            _fatal_and_exit(
                self,
                "Could not launch installer",
                f"{e}\n\nApp will close.",
            )

        # Block until user confirms installer done.
        QMessageBox.information(
            self,
            "Finish installer",
            "When the MT5 installer wizard has finished:\n\n"
            "  - DO NOT launch terminal64.exe\n"
            "  - Click OK below to continue.\n\n"
            "If the installer was cancelled, click OK and the app will fail "
            "with a clear message.",
        )

        # Log installer poll() purely for diagnostics. We deliberately do NOT
        # treat a nonzero exit as failure: the MT5 installer is a wrapper
        # that often exits with code 1 even on a successful install (it
        # spawns the real NSIS installer and exits early). The real source
        # of truth is whether the fresh install exists on disk (step 7).
        rc = installer_proc.poll()
        _logger.info("step 6: user clicked OK; installer poll()=%s (informational only)", rc)

        # Locate the freshly installed folder. Prefer the renamed slot
        # (installer's default target) and skip pre-existing untouched
        # installs that aren't this fresh one. Retry briefly: NSIS post-
        # install scripts can still be writing files when the user clicks
        # OK on our modal, and is_dir/terminal_exe may miss for ~1s.
        self._set_status("Looking for freshly installed MT5…")
        fresh = None
        for attempt in range(5):
            fresh = self._find_fresh_install(
                expected_path=existing,
                exclude=pre_existing_others,
            )
            _logger.info("step 7 attempt %d/5: fresh=%s", attempt + 1, fresh)
            if fresh is not None:
                break
            self._pump_sleep(1.0)
        if fresh is None:
            # Fallback: ask user to point at the installed folder. Broker-
            # rebranded installers can land under custom paths we don't track.
            fresh = self._ask_user_for_install_folder()
            if fresh is None:
                self._restore_or_keep(existing, backup_made, restore=True)
                _fatal_and_exit(
                    self,
                    "Install not found",
                    f"Could not locate the freshly installed MT5 at any "
                    f"default path.\n\n"
                    f"Installer process exit code: {rc}\n"
                    f"  (exit 0 = clean, 1 = often the NSIS wrapper's normal\n"
                    f"   detached-success code, anything else may indicate\n"
                    f"   cancel/crash).\n\n"
                    f"Wizard may have been cancelled or install went to "
                    f"a non-default path.\n\nApp will close.",
                )

        # MT5 installer typically launches terminal64.exe at the end (the
        # "Launch MetaTrader 5" checkbox is on by default). Block here until
        # the terminal is closed — either by the user from MT5's UI, or by
        # forcing taskkill. Loop: poll PIDs, prompt user, repeat.
        fresh_exe = terminal_exe(fresh)
        _logger.info("step 8: terminal exe at %s", fresh_exe)
        if fresh_exe is not None:
            while True:
                running = find_running_terminal_pids(fresh_exe)
                _logger.info("step 8: running PIDs=%s", running)
                if not running:
                    break
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setWindowTitle("Close MT5 to continue")
                msg.setText(
                    f"terminal64.exe is running for the freshly installed MT5\n"
                    f"  PIDs: {', '.join(str(p) for p in running)}\n"
                    f"  Path: {fresh_exe}\n\n"
                    "The wizard cannot copy a running terminal.\n\n"
                    "Recommended: close MT5 manually (File → Exit), then\n"
                    "click Recheck. Or click Force Kill to terminate it.\n"
                    "Cancel aborts the wizard."
                )
                recheck_btn = msg.addButton("Recheck", QMessageBox.ButtonRole.AcceptRole)
                kill_btn = msg.addButton("Force Kill", QMessageBox.ButtonRole.DestructiveRole)
                cancel_btn = msg.addButton(QMessageBox.StandardButton.Cancel)
                msg.exec()
                choice = msg.clickedButton()
                if choice is cancel_btn:
                    _logger.info("step 8: user cancelled — aborting wizard")
                    self._restore_or_keep(existing, backup_made, restore=True)
                    _fatal_and_exit(
                        self, "Aborted",
                        "Cannot continue while terminal64.exe is running. App will close.",
                    )
                elif choice is kill_btn:
                    _logger.warning("step 8: user chose Force Kill on PIDs %s", running)
                    kill_pids(running)
                    # brief settle so Windows releases handles before next probe
                    self._pump_sleep(1.0)
                else:
                    _logger.info("step 8: user chose Recheck — re-polling")
                    # loop continues; next iteration re-runs find_running_terminal_pids

        # Verify it's virgin. If not (likely user picked the wrong folder,
        # e.g. their master), let them re-pick before giving up.
        virgin, reason = is_virgin(fresh)
        _logger.info("step 9: is_virgin(%s) → %s, %s", fresh, virgin, reason)
        while not virgin:
            _logger.warning("step 9: NOT virgin — asking user to re-pick")
            reply = QMessageBox.question(
                self,
                "Install is not virgin",
                f"Detected use in:\n  {fresh}\n\n"
                f"Reason: {reason}\n\n"
                "Likely you picked the wrong folder. Pick another folder?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._restore_or_keep(existing, backup_made, restore=True)
                _fatal_and_exit(
                    self,
                    "Install is not virgin",
                    "Reinstall MT5 without launching or logging in afterwards.\n\n"
                    "App will close.",
                )
            picked = self._ask_user_for_install_folder()
            if picked is None:
                self._restore_or_keep(existing, backup_made, restore=True)
                _fatal_and_exit(
                    self, "Aborted",
                    "No folder picked. App will close.",
                )
            fresh = picked
            virgin, reason = is_virgin(fresh)
            _logger.info("step 9 re-pick: is_virgin(%s) → %s, %s", fresh, virgin, reason)

        # Copy fresh install → template, off the UI thread.
        _logger.info("step 10: copy %s → %s", fresh, target)
        self._set_status(f"Copying {fresh} → {target}…")
        copy_error = self._copy_with_progress(fresh, target)
        _logger.info("step 10: copy_error=%r", copy_error)
        if copy_error:
            self._restore_or_keep(existing, backup_made, restore=True)
            _fatal_and_exit(
                self,
                "Template copy failed",
                f"Could not copy template:\n{copy_error}\n\nApp will close.",
            )

        # Cleanup choice: keep both, restore backup, or remove fresh.
        # Rule (per user spec):
        #   - If a backup exists → fresh install is the "new MT5" the user
        #     installed for this purpose; leave it. Backup is also kept,
        #     just inform the user where it landed.
        #   - If no backup → fresh install becomes the default MT5; leave it.
        self._set_status("Saving template metadata…")
        self.result_data = {
            "template_path": str(target),
            "template_source": str(fresh),
            "template_created_at": now_iso(),
        }
        _logger.info("step 11: result=%s", self.result_data)

        msg_lines = [f"Template saved to:\n  {target}\n"]
        msg_lines.append(f"Source install kept at:\n  {fresh}")
        if backup_made is not None:
            msg_lines.append(
                f"\nYour previous MT5 install was renamed to:\n  {backup_made}\n\n"
                "Move/rename it back manually if you need it under its "
                "original name."
            )

        QMessageBox.information(self, "Template seeded", "\n".join(msg_lines))
        self.accept()

    def _handle_stale_backups(self) -> bool:
        """Resolve leftover .mt5copy_backup folders. Returns True to continue."""
        stale = find_stale_backups()
        if not stale:
            _logger.info("_handle_stale_backups: no stale backups")
            return True
        _logger.warning("_handle_stale_backups: stale=%s", [str(p) for p in stale])

        listing = "\n".join(f"  {p}" for p in stale)
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Stale backup detected")
        msg.setText(
            "A previous seed attempt left these backup folders behind:\n\n"
            f"{listing}\n\n"
            "What should I do?"
        )
        restore = msg.addButton("Restore (rename back)", QMessageBox.ButtonRole.AcceptRole)
        delete = msg.addButton("Delete backup", QMessageBox.ButtonRole.DestructiveRole)
        cancel = msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()
        choice = msg.clickedButton()
        choice_name = "restore" if choice is restore else "delete" if choice is delete else "cancel"
        _logger.info("_handle_stale_backups: user picked %s", choice_name)
        if choice is cancel:
            return False

        for backup in stale:
            # backup = `<original>.mt5copy_backup`; original = strip suffix
            original = backup.with_name(backup.name[: -len(".mt5copy_backup")])
            try:
                if choice is restore:
                    if original.exists():
                        rep = QMessageBox.question(
                            self,
                            "Replace existing?",
                            f"{original} already exists. Replace it with the backup?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        if rep != QMessageBox.StandardButton.Yes:
                            continue
                        self._set_status(f"Restoring {backup} → {original} (overwriting)…")
                        restore_backup(original, replace_existing=True)
                    else:
                        self._set_status(f"Restoring {backup} → {original}…")
                        restore_backup(original)
                elif choice is delete:
                    self._set_status(f"Deleting {backup}…")
                    shutil.rmtree(backup)
            except Exception as e:
                _logger.exception("_handle_stale_backups: cleanup failed at %s", backup)
                QMessageBox.warning(self, "Cleanup failed", f"{backup}: {e}")
                return False
        _logger.info("_handle_stale_backups: cleanup done")
        self._set_status("Stale backup cleanup done.")
        return True

    def _pick_installer(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select MT5 installer (mt5setup.exe)",
            str(Path.home()),
            "MT5 installer (mt5setup.exe);;Executable (*.exe);;All Files (*)",
        )
        if not path:
            _logger.info("_pick_installer: cancelled")
            return None
        p = Path(path)
        if not p.is_file():
            _logger.warning("_pick_installer: not a file %s", p)
            QMessageBox.warning(self, "Bad path", f"Not a file: {p}")
            return None
        _logger.info("_pick_installer: %s", p)
        return p

    def _copy_with_progress(self, src: Path, dst: Path) -> str:
        """Run _CopyThread, drive progress UI, return empty string on success."""
        _logger.info("_copy_with_progress: src=%s dst=%s", src, dst)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        result: dict[str, str] = {"error": "", "finished": ""}
        thread = _CopyThread(src, dst, self)

        def on_progress(copied: int, total: int) -> None:
            pct = int(copied * 100 / max(total, 1))
            self.progress_bar.setValue(min(pct, 100))
            self._set_status(
                f"Copying… {copied / (1024 * 1024):.0f} / {total / (1024 * 1024):.0f} MB"
            )

        def on_done(error: str) -> None:
            result["error"] = error
            result["finished"] = "yes"

        thread.progress.connect(on_progress)
        thread.done.connect(on_done)
        thread.start()

        # Pump events while thread runs. processEvents avoids freezing the
        # dialog and lets QMessageBox/repaints work mid-copy.
        while not result["finished"]:
            QApplication.processEvents()
            thread.wait(50)

        self.progress_bar.setVisible(False)
        _logger.info("_copy_with_progress: finished, error=%r", result["error"])
        return result["error"]

    def _ask_user_for_install_folder(self) -> Path | None:
        """Prompt user to point at the freshly installed MT5 folder."""
        _logger.info("_ask_user_for_install_folder: prompting user")
        QMessageBox.information(
            self,
            "Locate fresh MT5",
            "Could not auto-detect the freshly installed MT5.\n"
            "Some broker-rebranded installers use custom paths.\n\n"
            "On the next dialog, pick the folder that contains terminal64.exe.",
        )
        path = QFileDialog.getExistingDirectory(
            self,
            "Select freshly installed MT5 folder",
            r"C:\Program Files" if sys.platform == "win32" else str(Path.home()),
        )
        if not path:
            _logger.info("_ask_user_for_install_folder: cancelled")
            return None
        p = Path(path)
        if terminal_exe(p) is None:
            _logger.warning("_ask_user_for_install_folder: %s has no terminal exe", p)
            QMessageBox.warning(self, "No terminal", f"No terminal64.exe under: {p}")
            return None
        _logger.info("_ask_user_for_install_folder: picked %s", p)
        return p

    def _find_fresh_install(
        self,
        expected_path: Path | None,
        exclude: list[Path] | None = None,
    ) -> Path | None:
        """Locate freshly installed MT5 in known default paths.

        Tries the expected path first (where we renamed an existing install
        aside, so the installer should put the new one), then other defaults,
        skipping anything in `exclude` (e.g. pre-existing untouched installs
        that would otherwise look "fresh" but are actually the user's master).
        """
        ex_norm = {Path(p).resolve() for p in (exclude or [])}

        def usable(p: Path) -> bool:
            if not (p.is_dir() and terminal_exe(p)):
                return False
            try:
                return p.resolve() not in ex_norm
            except OSError:
                return True

        if expected_path is not None and usable(expected_path):
            return expected_path
        for candidate in DEFAULT_INSTALL_PATHS:
            if usable(candidate):
                return candidate
        return None

    def _restore_or_keep(
        self,
        original: Path | None,
        backup: Path | None,
        restore: bool,
    ) -> None:
        _logger.info(
            "_restore_or_keep: original=%s backup=%s restore=%s",
            original, backup, restore,
        )
        """Rollback helper used on failure paths.

        If we renamed an existing install and the new flow failed, give the
        user back their original. If a partial fresh install sits at the
        same path, ask whether to overwrite it with the backup or keep
        both side-by-side.
        """
        if not restore or original is None or backup is None:
            return
        try:
            if original.exists():
                reply = QMessageBox.question(
                    self,
                    "Restore backup?",
                    f"Original MT5 is at:\n  {backup}\n\n"
                    f"A new install also sits at:\n  {original}\n\n"
                    "Replace the new install with the original (Yes) or "
                    "keep both side-by-side (No)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    _logger.info("_restore_or_keep: user chose to overwrite %s", original)
                    restore_backup(original, replace_existing=True)
                else:
                    _logger.info("_restore_or_keep: user chose keep-both")
                return
            restore_backup(original)
        except Exception as e:
            _logger.exception(
                "_restore_or_keep: restore failed backup=%s original=%s", backup, original
            )
            QMessageBox.warning(
                self,
                "Restore failed",
                f"Could not restore backup {backup} → {original}: {e}\n\n"
                "Check the session log file for full details. "
                "You can manually rename the .mt5copy_backup folder once "
                "all terminal64.exe processes are stopped.",
            )