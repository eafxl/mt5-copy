"""Terminals tab — every MT5 terminal this app drives, and its live state.

WHAT IS IN THE TABLE
    The MASTER terminal and every copy terminal, in one list. The master was
    missing before, which made the list a half-truth: it is the terminal whose
    death stops everything, and there was nowhere to see that or to bring it
    back. It sits first and cannot be deleted — it is configuration, not a row
    to tidy away — but it can be relaunched.

ALGO TRADING
    Reported per terminal, because it is the single switch that turns a working
    copier into one that silently places no orders. Nothing in the app could
    see it, so it was only ever discovered by a trade not happening.

    Read from the terminal itself (``terminal_info().trade_allowed``) rather
    than assumed, and shown as "?" when the terminal cannot be asked — "we
    could not reach it" and "you have it switched off" are different facts, and
    only one of them is the operator's to fix.

The word "slave" survives in the code beneath this file (config keys, the
state machine, the worker protocol) where renaming it would be a wide and
risky change for no behavioural gain. It does not survive in anything the
operator reads.
"""
import os
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..paths import icons_dir as _icons_dir

_ICONS = _icons_dir()

from ..config import make_slave_id
from ..slave_state import SlaveStatus, get_slave_state
from ..events import EventType, get_ui_queue


# ---- Background warmup for a single slave ----

class _SlaveWarmupThread(QThread):
    """Warms up a single slave terminal in a background thread."""

    warmup_done = pyqtSignal(str, bool, str)  # slave_name, success, error_msg

    def __init__(self, executor, warmup_task, slave_name: str, parent=None):
        super().__init__(parent)
        self._executor = executor
        self._task = warmup_task
        self._slave_name = slave_name

    def run(self):
        try:
            results = self._executor.execute_tasks([self._task])
            if results and results[0].success:
                self.warmup_done.emit(self._slave_name, True, "")
            else:
                err = results[0].error if results else "No result"
                self.warmup_done.emit(self._slave_name, False, err or "Unknown error")
        except Exception as e:
            self.warmup_done.emit(self._slave_name, False, str(e))


class _TerminalHealthThread(QThread):
    """Checks which terminal64.exe processes are running (background thread).

    Runs a quick PowerShell query and emits the set of running terminal paths.
    Keeps the GUI responsive since the subprocess call can take 100-300ms.
    """

    result_ready = pyqtSignal(object)  # set of paths, or None when unknown

    def run(self):
        from ..parallel_worker import _get_running_terminal_paths
        self.result_ready.emit(_get_running_terminal_paths())


class _MasterRelaunchThread(QThread):
    """Starts the master terminal — in a SEPARATE PROCESS.

    `mt5.initialize` cold-starts a terminal in 11-45s (measured in production
    traces), so it can never run on the GUI thread.

    THE SUBPROCESS IS NOT AN OPTIMISATION, IT IS THE FIX
        `_warm_master_terminal` ends with `mt5.shutdown()`. The MetaTrader5
        module is a per-PROCESS singleton, so running it here — as this thread
        used to — launched the terminal and then tore down THIS process's MT5
        connection: the very one the service holds open for the master.

        The visible result was that "Relaunch master" made things worse. The
        terminal came back up, the service's link to it did not, so the
        Terminals tab kept reading Algo Trading as "?" and the copier only
        recovered if a trade happened to trigger its reconnect. Running it in
        a child process, exactly as the service's own pre-warm does, leaves
        this process's connection untouched.

    The service then notices the terminal is up on its next health check and
    reconnects on its own thread — the only thread allowed to.
    """

    done = pyqtSignal(bool, str)      # success, error

    def __init__(self, terminal_path: str, parent=None):
        super().__init__(parent)
        self._path = terminal_path

    def run(self):
        from concurrent.futures import ProcessPoolExecutor

        try:
            from ..main import _warm_master_terminal
            with ProcessPoolExecutor(max_workers=1) as pool:
                ok = pool.submit(_warm_master_terminal, self._path).result(
                    timeout=180
                )
            self.done.emit(bool(ok), "" if ok else "terminal did not start")
        except Exception as e:
            self.done.emit(False, str(e))


class _AlgoProbeThread(QThread):
    """Asks each copy terminal whether Algo Trading is on.

    Off the GUI thread because it talks to MT5. Rides the cached worker
    connections, so it costs about what a position fetch costs (0.4ms p50 in
    production traces) — cheap enough for the existing health cadence.
    """

    result_ready = pyqtSignal(dict)   # slave_id -> {"algo_trading": bool, ...}

    def __init__(self, executor, tasks, parent=None):
        super().__init__(parent)
        self._executor = executor
        self._tasks = tasks

    def run(self):
        out: dict[str, dict] = {}
        try:
            for result in self._executor.execute_tasks(self._tasks) or []:
                if result.success and result.details:
                    out[result.slave_id or result.slave_name] = dict(result.details)
        except Exception:
            # A failed probe must never be reported as "Algo off" — leaving the
            # id absent is what makes the cell read "?" instead.
            pass
        self.result_ready.emit(out)


# ---- State → UI helpers ----

_STATUS_DISPLAY = {
    SlaveStatus.ON: ("On", "#4CAF50"),
    SlaveStatus.PAUSED: ("Paused", "#FF9800"),
    SlaveStatus.OFF: ("Off", "#888"),
}

def _terminal_label(path: str, fallback: str) -> str:
    """The folder a terminal lives in — what tells two installs apart.

    Split on BOTH separators. These are Windows paths, but the GUI is also
    developed and screenshotted on POSIX, where `Path(...).parent` treats a
    backslash as an ordinary character and hands back an empty name.
    """
    if not path:
        return fallback
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return parts[-2] if len(parts) >= 2 else fallback


#: The master's row id. Not a login@server pair — the master is identified by
#: being the master, and it must never collide with a real slave id.
MASTER_ROW_ID = "\x00master"

_ALGO_DISPLAY = {
    True: ("On", "#2E7D32"),
    False: ("OFF", "#C62828"),      # loud: this is why orders vanish
    None: ("?", "#9E9E9E"),         # could not ask — not the same as "off"
}


class TerminalsTab(QWidget):
    """Every MT5 terminal this app drives, with its live state and controls."""

    settings_save_requested = pyqtSignal()

    COLUMNS = ["Terminal", "Role", "Status", "Algo Trading", "", "", ""]
    COL_NAME, COL_ROLE, COL_STATUS, COL_ALGO = 0, 1, 2, 3
    COL_PLAY, COL_STOP, COL_DELETE = 4, 5, 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._copier = None  # Set via populate()
        # slave_id -> last terminal_info seen. Absent means "not asked yet",
        # which the table renders as "?" rather than inventing a value.
        self._terminal_state: dict[str, dict] = {}
        self._algo_thread: _AlgoProbeThread | None = None
        self._master_relaunch: QThread | None = None
        # Terminal exe paths seen running by the last health check. None until
        # the first one lands, so the table can say "checking" rather than
        # announcing everything is dead a beat before it knows.
        self._running_paths: set[str] | None = None
        # The master as the SERVICE measured it (MASTER_STATUS events). None
        # until the service reports, which is why the master's row can read
        # "…" before anything has started.
        self._master_state: dict | None = None
        # Set for good when the daily loss limit trips — see lock_out().
        self._locked_out = False
        self._config_data = None  # Raw config dict
        self._warmup_threads: dict[str, _SlaveWarmupThread] = {}
        self._auto_reconnects: set[str] = set()  # Slaves being auto-reconnected (no error dialog)
        self._pending_reconnects: set[str] = set()  # Slaves that need auto-reconnect on next health check
        self._controls_enabled = False
        self._health_thread: _TerminalHealthThread | None = None
        self._removed_during_warmup: set[str] = set()  # Slaves deleted before populate()
        self._setup_ui()

        # Listen for SLAVE_DISCONNECTED events
        self._ui_queue = get_ui_queue()
        self._ui_queue.register_callback(self._on_event_from_service)

        # Periodic terminal health check (every 5s)
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._start_health_check)
        self._health_timer.setInterval(5000)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title = QLabel("Terminals")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        self._info_label = QLabel("")
        self._info_label.setStyleSheet("color: #666;")
        header.addWidget(self._info_label)
        layout.addLayout(header)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_ROLE, self.COL_STATUS, self.COL_ALGO):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for col in (self.COL_PLAY, self.COL_STOP, self.COL_DELETE):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, 40)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet("""
            QTableWidget { gridline-color: #e0e0e0; border: 1px solid #ddd; }
            QTableWidget::item { padding: 4px; }
        """)
        layout.addWidget(self.table, 1)

    # ---- Public API (called by MainWindow) ----

    def populate(self, config_data: dict, copier):
        """Populate table after warmup completes.

        Args:
            config_data: Raw config dict with 'slaves' list.
            copier: TradeCopier instance (for building warmup tasks + executor).
        """
        self._copier = copier
        self._config_data = config_data
        self._controls_enabled = True

        state = get_slave_state()
        current = state.get_all()
        slaves = config_data.get("slaves", [])
        enabled_slaves = [s for s in slaves if s.get("enabled", False)]

        # Register enabled slaves as ON, but skip ones that were
        # removed/deleted by the user during warmup
        for s in enabled_slaves:
            sid = make_slave_id(s["login"], s["server"])
            if sid in self._removed_during_warmup:
                continue  # User deleted this slave while warmup was running
            if sid not in current:
                # New slave not yet tracked — add as ON (just warmed up)
                state.add_slave(sid, SlaveStatus.ON)
            elif current[sid] == SlaveStatus.OFF:
                # Was pre-populated as OFF before warmup — now warmed, set ON
                state.resume(sid)

        # Clear the set — no longer needed after populate
        self._removed_during_warmup.clear()

        self._rebuild_table()
        self._update_info()

        # Start periodic terminal health monitoring, and take a first reading
        # now rather than waiting out the 5s interval on a freshly opened tab.
        self._health_timer.start()
        self._start_health_check()

    def lock_out(self):
        """Freeze the controls that could start copying again, for the day.

        Called when the daily stop limit trips. Stop and Pause stay live —
        they can only ever reduce what happens — as does the master relaunch,
        which is about keeping the terminal healthy while open positions run
        their course, not about opening new ones.
        """
        self._locked_out = True
        self._rebuild_table()
        self._update_info()

    def on_service_stopped(self):
        """Called when top-level Stop fires — set all to Off."""
        self._health_timer.stop()
        self._pending_reconnects.clear()
        # The probe answers are about connections that no longer exist.
        self._terminal_state.clear()
        state = get_slave_state()
        state.stop_all()
        self._controls_enabled = False
        self._rebuild_table()

    def on_service_started(self):
        """Called when service starts running."""
        self._controls_enabled = True
        self._rebuild_table()

    def on_config_changed(self, config_data: dict):
        """Called when Settings tab saves — sync state machine with config.

        Adds newly-enabled slaves (as OFF, need warmup) and removes
        terminals that were disabled from outside this tab.
        """
        self._config_data = config_data
        state = get_slave_state()
        current = state.get_all()
        slaves = config_data.get("slaves", [])
        enabled_ids = {
            make_slave_id(s["login"], s["server"])
            for s in slaves if s.get("enabled", False)
        }

        # Add newly-enabled slaves that aren't tracked yet (as OFF — need Play)
        for sid in enabled_ids:
            if sid not in current:
                state.add_slave(sid, SlaveStatus.OFF)

        # Remove slaves that were disabled externally
        for sid in list(current):
            if sid not in enabled_ids:
                state.remove_slave(sid)

        self._rebuild_table()
        self._update_info()

    # ---- Table rendering ----

    def _resolve_name(self, slave_id: str) -> str:
        """Resolve display name from config_data, falling back to slave_id."""
        if self._config_data:
            for s in self._config_data.get("slaves", []):
                sid = make_slave_id(s.get("login", 0), s.get("server", ""))
                if sid == slave_id:
                    return s.get("name", slave_id)
        return slave_id

    def _master_path(self) -> str:
        """The master terminal's exe path, or "" when not configured."""
        if self._copier is not None:
            master = getattr(self._copier, "master", None)
            path = getattr(master, "terminal_path", "") if master else ""
            if path:
                return path
        data = self._config_data or {}
        return (data.get("master", {}) or {}).get("terminal_path", "") or ""

    def _master_running(self) -> bool | None:
        """Whether the master terminal is alive. None = nobody could tell yet.

        Two sources, in order of authority:

        1. The SERVICE's own health check. It holds the master connection and
           asks the terminal directly, so a positive answer is proof.
        2. The process listing, for when the service is not running. This is
           weaker than it looks: it comes from a PowerShell query that returns
           nothing at all when it fails, and it cannot read the path of a
           process owned by another user — which the master, launched by hand,
           may well be. It is a fallback, never a contradiction of (1).
        """
        if self._master_state is not None:
            return bool(self._master_state.get("connected"))
        if self._running_paths is None:
            return None
        path = self._master_path()
        if not path:
            return False
        return os.path.normpath(path).lower() in self._running_paths

    def _algo_for(self, row_id: str) -> bool | None:
        """Algo Trading for a row, or None when it could not be asked."""
        if row_id == MASTER_ROW_ID:
            if self._master_state is None:
                return None
            return self._master_state.get("algo_trading")
        state = self._terminal_state.get(row_id)
        if not state:
            return None
        value = state.get("algo_trading")
        return None if value is None else bool(value)

    def _rebuild_table(self):
        """Rebuild the table: the master first, then every copy terminal."""
        state = get_slave_state()
        all_slaves = state.get_all()

        rows = [(MASTER_ROW_ID, None)] + list(all_slaves.items())

        self._clear_cell_widgets()
        self.table.clearContents()
        self.table.setRowCount(len(rows))

        play_icon = QIcon(str(_ICONS / "play.svg"))
        pause_icon = QIcon(str(_ICONS / "pause.svg"))
        stop_icon = QIcon(str(_ICONS / "stop.svg"))
        reload_icon = QIcon(str(_ICONS / "chevron-smooth-right.svg"))

        for row, (row_id, status) in enumerate(rows):
            if row_id == MASTER_ROW_ID:
                self._fill_master_row(row, reload_icon)
                continue
            self._fill_slave_row(
                row, row_id, status, play_icon, pause_icon, stop_icon
            )

    def _clear_cell_widgets(self):
        """Detach every cell widget from the viewport, now — not eventually.

        `removeCellWidget` only schedules the old widget for deferred deletion,
        so until the event loop gets round to it the widget is still a visible
        child of the viewport, parked at (0, 0). This table rebuilds on a 5s
        timer, so they accumulate: seven orphaned buttons were stacked in the
        corner, the topmost one — a delete "x" — painting straight over the
        master's name.

        Reparenting first removes them from the viewport immediately; the
        deleteLater then frees them whenever Qt is ready.
        """
        for row in range(self.table.rowCount()):
            for col in range(self.table.columnCount()):
                widget = self.table.cellWidget(row, col)
                if widget is None:
                    continue
                self.table.removeCellWidget(row, col)
                widget.setParent(None)
                widget.deleteLater()

    def _set_cell(self, row: int, col: int, text: str, color: str | None = None,
                  centered: bool = True, tooltip: str = ""):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(QColor(color))
        if centered:
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
            )
        if tooltip:
            item.setToolTip(tooltip)
        self.table.setItem(row, col, item)
        return item

    def _fill_master_row(self, row: int, reload_icon: QIcon):
        """The master: shown because its death stops everything, and there was
        nowhere to see that or to act on it."""
        path = self._master_path()
        name_item = QTableWidgetItem(_terminal_label(path, "Master"))
        name_item.setToolTip(path or "No master terminal configured")
        font = name_item.font()
        font.setBold(True)
        name_item.setFont(font)
        self.table.setItem(row, self.COL_NAME, name_item)

        self._set_cell(row, self.COL_ROLE, "Master", "#1565C0")

        running = self._master_running()
        if running is None:
            text, color, tip = "…", "#9E9E9E", "Checking the terminal process"
        elif running:
            text, color, tip = "On", "#4CAF50", "Terminal process is running"
        else:
            text, color, tip = (
                "Off", "#C62828",
                "Master terminal is not running — nothing can be copied.\n"
                "Use the relaunch button to start it.",
            )
        self._set_cell(row, self.COL_STATUS, text, color, tooltip=tip)

        algo = self._algo_for(MASTER_ROW_ID)
        label, color = _ALGO_DISPLAY[algo]
        self._set_cell(
            row, self.COL_ALGO, label, color,
            tooltip=self._algo_tooltip(algo, "master"),
        )

        relaunch = QToolButton()
        relaunch.setIcon(reload_icon)
        relaunch.setText("" if not reload_icon.isNull() else "↻")
        relaunch.setToolTip("Relaunch the master terminal")
        relaunch.setEnabled(
            bool(path) and self._master_relaunch is None
        )
        relaunch.clicked.connect(lambda _=False: self._on_relaunch_master())
        self.table.setCellWidget(row, self.COL_PLAY, relaunch)

        # No stop and no delete for the master: stopping it is the service's
        # own Stop button, and it is configuration rather than a row to tidy
        # away. Blank widgets so the previous row's buttons cannot show through
        # a reused cell.
        for col in (self.COL_STOP, self.COL_DELETE):
            self.table.setCellWidget(row, col, QWidget())

    @staticmethod
    def _algo_tooltip(algo: bool | None, what: str) -> str:
        if algo is True:
            return f"Algo Trading is enabled on the {what} terminal."
        if algo is False:
            return (
                f"Algo Trading is OFF on the {what} terminal.\n"
                "Orders will not be placed. Enable it in the terminal's "
                "toolbar (Tools > Options > Expert Advisors)."
            )
        return (
            f"Could not read Algo Trading from the {what} terminal — it is "
            "not connected right now.\nThis is not the same as it being off."
        )

    def _fill_slave_row(self, row, slave_id, status, play_icon, pause_icon, stop_icon):
        display_text, color = _STATUS_DISPLAY.get(status, ("?", "#888"))
        display_name = self._resolve_name(slave_id)

        name_item = QTableWidgetItem(display_name)
        name_item.setToolTip(slave_id)
        self.table.setItem(row, self.COL_NAME, name_item)

        self._set_cell(row, self.COL_ROLE, "Copy", "#666")
        self._set_cell(row, self.COL_STATUS, display_text, color)

        algo = self._algo_for(slave_id)
        label, algo_color = _ALGO_DISPLAY[algo]
        self._set_cell(
            row, self.COL_ALGO, label, algo_color,
            tooltip=self._algo_tooltip(algo, display_name),
        )

        # Play/Pause button
        pp_btn = QToolButton()
        if status == SlaveStatus.ON:
            pp_btn.setIcon(pause_icon)
            pp_btn.setToolTip("Pause")
            pp_btn.clicked.connect(lambda _, sid=slave_id: self._on_pause(sid))
        else:
            pp_btn.setIcon(play_icon)
            pp_btn.setToolTip("Resume" if status == SlaveStatus.PAUSED else "Start")
            pp_btn.clicked.connect(lambda _, sid=slave_id: self._on_play(sid))
        # Starting a terminal is starting trading, so the daily stop limit
        # takes that away. PAUSING one is still allowed — it only ever reduces
        # what can happen.
        can_press = self._controls_enabled and slave_id not in self._warmup_threads
        if self._locked_out and status != SlaveStatus.ON:
            can_press = False
            pp_btn.setToolTip(
                "Daily stop limit reached — copying is finished for today."
            )
        pp_btn.setEnabled(can_press)
        self.table.setCellWidget(row, self.COL_PLAY, pp_btn)

        # Stop button
        stop_btn = QToolButton()
        stop_btn.setIcon(stop_icon)
        stop_btn.setToolTip("Stop")
        stop_btn.setEnabled(
            self._controls_enabled
            and status in (SlaveStatus.ON, SlaveStatus.PAUSED)
        )
        stop_btn.clicked.connect(lambda _, sid=slave_id: self._on_stop(sid))
        self.table.setCellWidget(row, self.COL_STOP, stop_btn)

        # Delete button
        del_btn = QToolButton()
        del_btn.setIcon(QIcon(str(_ICONS / "close.svg")))
        del_btn.setToolTip("Remove from configuration")
        del_btn.setEnabled(status == SlaveStatus.OFF)
        del_btn.clicked.connect(lambda _, sid=slave_id: self._on_delete(sid))
        self.table.setCellWidget(row, self.COL_DELETE, del_btn)

    # ---- Actions ----

    def _on_pause(self, slave_id: str):
        """Pause a running slave (On -> Paused)."""
        state = get_slave_state()
        if state.pause(slave_id):
            self._rebuild_table()

    def _on_play(self, slave_id: str):
        """Resume or start a slave."""
        self._pending_reconnects.discard(slave_id)
        state = get_slave_state()
        cur = state.get_status(slave_id)

        if cur == SlaveStatus.PAUSED:
            # Just resume — terminal is still alive
            if state.resume(slave_id):
                self._rebuild_table()
        elif cur == SlaveStatus.OFF:
            # Need to warm up the terminal
            self._start_slave_warmup(slave_id)

    def _on_stop(self, slave_id: str):
        """Stop a slave — kill its terminal."""
        state = get_slave_state()
        self._pending_reconnects.discard(slave_id)
        if state.stop(slave_id):
            self._cancel_warmup(slave_id)
            self._kill_slave_terminal(slave_id)
            self._rebuild_table()

    def _on_delete(self, slave_id: str):
        """Delete (disable) a slave from config."""
        display_name = self._resolve_name(slave_id)
        self._pending_reconnects.discard(slave_id)
        state = get_slave_state()
        if not state.can_delete(slave_id):
            QMessageBox.warning(
                self, "Cannot Delete",
                f"Stop '{display_name}' first.",
            )
            return

        reply = QMessageBox.question(
            self, "Remove terminal",
            f"Remove '{display_name}' from the configuration?\n"
            f"Its terminal stays on disk; only the copier stops using it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Cancel any in-progress warmup and kill terminal
        self._cancel_warmup(slave_id)
        self._kill_slave_terminal(slave_id)

        # Track removal so populate() won't re-add after initial warmup
        self._removed_during_warmup.add(slave_id)

        # Disable in config data and save
        if self._config_data:
            for slave in self._config_data.get("slaves", []):
                sid = make_slave_id(slave.get("login", 0), slave.get("server", ""))
                if sid == slave_id:
                    slave["enabled"] = False
                    break

        state.remove_slave(slave_id)
        self._rebuild_table()
        self._update_info()
        self.settings_save_requested.emit()

    # ---- Warmup for single slave ----

    def _start_slave_warmup(self, slave_id: str):
        """Start warmup for a single slave terminal."""
        if not self._copier or slave_id in self._warmup_threads:
            return

        # Find slave config and build warmup task
        slave_config = self._copier._get_slave_config_by_id(slave_id)
        if not slave_config:
            display_name = self._resolve_name(slave_id)
            QMessageBox.warning(
                self, "Error",
                f"Terminal '{display_name}' is not in the configuration.",
            )
            return

        task = self._copier._build_warmup_task(slave_config)
        thread = _SlaveWarmupThread(
            self._copier.executor, task, slave_id, parent=self
        )
        thread.warmup_done.connect(self._on_slave_warmup_done)
        self._warmup_threads[slave_id] = thread
        thread.start()

        # Show "Starting..." in table
        self._rebuild_table()

    def _on_slave_warmup_done(self, slave_id: str, success: bool, error: str):
        """Handle single-slave warmup result."""
        self._warmup_threads.pop(slave_id, None)
        is_auto = slave_id in self._auto_reconnects
        self._auto_reconnects.discard(slave_id)
        state = get_slave_state()

        # Slave was deleted/stopped while warmup was running — ignore result
        cur = state.get_status(slave_id)
        if cur is None:
            return

        display_name = self._resolve_name(slave_id)
        if success and cur == SlaveStatus.OFF:
            self._pending_reconnects.discard(slave_id)
            state.resume(slave_id)
            # Warmup already read the terminal's state while connected, so the
            # Algo column is right immediately rather than blank until the next
            # poll — which is when a freshly started terminal is most worth
            # checking.
            self._start_algo_probe()
        elif not success and cur == SlaveStatus.OFF:
            if is_auto:
                # Auto-reconnect failed — will retry on next health check cycle
                import logging
                logging.getLogger("metact").warning(
                    f"[{display_name}] Auto-reconnect failed: {error}"
                )
            else:
                QMessageBox.warning(
                    self, "Terminal did not start",
                    f"Failed to start '{display_name}': {error}",
                )

        self._rebuild_table()

    def _cancel_warmup(self, slave_id: str):
        """Discard pending warmup for a slave (thread can't be killed,
        but result will be ignored via state check in _on_slave_warmup_done)."""
        self._warmup_threads.pop(slave_id, None)

    # ---- Terminal management ----

    def _kill_slave_terminal(self, slave_id: str):
        """Close the MT5 terminal for a specific slave (clean exit, then force).

        Runs on the GUI thread and waits for the terminal to save its state, so
        Qt's event loop is pumped from inside the wait.
        """
        if not self._copier:
            return
        slave_config = self._copier._get_slave_config_by_id(slave_id)
        if not slave_config:
            return
        tp = slave_config.get_terminal_path(self._copier.default_slave_terminal)
        if tp:
            from PyQt6.QtWidgets import QApplication

            from ..parallel_worker import _kill_mt5_terminals
            _kill_mt5_terminals({tp}, on_wait=QApplication.processEvents)

    # ---- Disconnect detection ----

    def _on_event_from_service(self, event):
        """UIEventQueue callback (service thread). Thread-safe via check."""
        if event.event_type == EventType.MASTER_STATUS:
            # The service measured the master. That beats every other source
            # here: the process listing can be blind (a terminal started by
            # another user, or a PowerShell query that failed), while this is
            # the same connection the copier reads positions through.
            self._master_state = {
                "connected": bool(event.master_connected),
                "algo_trading": event.master_algo,
            }
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._rebuild_table)
            return

        if event.event_type == EventType.SLAVE_DISCONNECTED:
            # Must dispatch to main thread — just update state directly
            # (SlaveStateMachine is thread-safe)
            state = get_slave_state()
            state.disconnect(event.slave_name)
            # Table rebuild + auto-reconnect must happen in GUI thread
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._rebuild_table)
            QTimer.singleShot(0, lambda: self._auto_reconnect(event.slave_name))

    # ---- Periodic terminal health check ----

    def _start_health_check(self):
        """Start a background thread to check terminal processes."""
        if self._health_thread is not None and self._health_thread.isRunning():
            return  # Previous check still running
        self._health_thread = _TerminalHealthThread(parent=self)
        self._health_thread.result_ready.connect(self._on_health_result)
        self._health_thread.start()

    def _on_health_result(self, running_paths):
        """Handle health check result — mark dead terminals as Off and auto-reconnect.

        ``running_paths`` is None when the query could not be answered (not a
        Windows host, PowerShell failed, timed out). That is not evidence that
        the terminals died, so the previous answer stands and nothing is
        disconnected: acting on it used to declare every terminal dead and
        start reconnecting them all.
        """
        if running_paths is None:
            self._rebuild_table()
            return
        # Kept even without a copier: the master's row is driven by this and is
        # worth showing before any slave has warmed up.
        self._running_paths = set(running_paths)
        if not self._copier:
            self._rebuild_table()
            return

        state = get_slave_state()
        all_slaves = state.get_all()
        disconnected: list[str] = []

        for slave_id, status in all_slaves.items():
            if status != SlaveStatus.ON:
                continue  # Only check running slaves

            # Get the terminal path for this slave
            slave_config = self._copier._get_slave_config_by_id(slave_id)
            if not slave_config:
                continue
            tp = slave_config.get_terminal_path(self._copier.default_slave_terminal)
            if not tp:
                continue

            # Normalize to match the PowerShell output
            normalized = os.path.normpath(tp).lower()
            if normalized not in running_paths:
                state.disconnect(slave_id)
                disconnected.append(slave_id)

        if disconnected:
            self._rebuild_table()

        # Auto-reconnect: newly disconnected + pending retries
        reconnect_now = set(disconnected) | {
            sid for sid in self._pending_reconnects
            if state.get_status(sid) == SlaveStatus.OFF and sid not in self._warmup_threads
        }
        for slave_id in reconnect_now:
            self._auto_reconnect(slave_id)

        self._start_algo_probe()
        self._rebuild_table()

    # ---- Algo Trading probe ----

    def _start_algo_probe(self):
        """Ask each live terminal whether Algo Trading is on.

        Only terminals believed to be up are asked: a probe against a dead one
        would make the worker try to relaunch it, turning a status read into an
        unasked-for restart.
        """
        if not self._copier or (self._algo_thread and self._algo_thread.isRunning()):
            return

        state = get_slave_state()
        tasks = []
        for slave_id, status in state.get_all().items():
            if status == SlaveStatus.OFF or slave_id in self._warmup_threads:
                self._terminal_state.pop(slave_id, None)
                continue
            slave_config = self._copier._get_slave_config_by_id(slave_id)
            if slave_config is not None:
                tasks.append(self._copier.build_terminal_state_task(slave_config))

        if not tasks:
            return
        self._algo_thread = _AlgoProbeThread(
            self._copier.executor, tasks, parent=self
        )
        self._algo_thread.result_ready.connect(self._on_algo_result)
        self._algo_thread.start()

    def _on_algo_result(self, states: dict):
        """Adopt probe results; forget anything that did not answer.

        Forgetting matters: a terminal that stops answering must fall back to
        "?" rather than keep displaying the last good value forever.
        """
        for slave_id in list(self._terminal_state):
            if slave_id != MASTER_ROW_ID and slave_id not in states:
                self._terminal_state.pop(slave_id, None)
        self._terminal_state.update(states)
        self._rebuild_table()

    # ---- Master terminal ----

    def _on_relaunch_master(self):
        """Relaunch the master terminal, off the GUI thread.

        `mt5.initialize` cold-starts a terminal in 11-45s (measured), so this
        cannot run inline — the window would be frozen for the whole of it.
        """
        path = self._master_path()
        if not path or self._master_relaunch is not None:
            return

        self._master_relaunch = _MasterRelaunchThread(path, parent=self)
        self._master_relaunch.done.connect(self._on_master_relaunched)
        self._master_relaunch.start()
        self._rebuild_table()          # greys the button while it runs
        self._info_label.setText("Relaunching master terminal…")

    def _on_master_relaunched(self, ok: bool, error: str):
        self._master_relaunch = None
        if not ok:
            QMessageBox.warning(
                self, "Master terminal",
                f"Could not relaunch the master terminal:\n{error}",
            )
        self._start_health_check()
        self._update_info()
        self._rebuild_table()

    def _auto_reconnect(self, slave_id: str):
        """Attempt to reconnect a disconnected slave (warmup in background)."""
        self._pending_reconnects.add(slave_id)
        if slave_id in self._warmup_threads:
            return  # Already reconnecting
        self._auto_reconnects.add(slave_id)
        self._start_slave_warmup(slave_id)

    # ---- Helpers ----

    def _update_info(self):
        """Summarise the fleet: how many terminals, and what is wrong."""
        state = get_slave_state()
        count = len(state.get_all()) + 1            # + the master
        parts = [f"{count} terminal(s)"]

        algo_off = sum(
            1 for rid in [MASTER_ROW_ID, *state.get_all()]
            if self._algo_for(rid) is False
        )
        if algo_off:
            parts.append(f"{algo_off} with Algo Trading OFF")
        if self._master_running() is False:
            parts.append("master down")
        if self._locked_out:
            parts.append("daily stop limit — no new copies today")

        self._info_label.setText(" · ".join(parts))
        self._info_label.setStyleSheet(
            "color: #C62828; font-weight: bold;" if len(parts) > 1
            else "color: #666;"
        )
