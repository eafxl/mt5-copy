"""Main window for MetaCT application.

Architecture: Qt GUI runs in main thread, copy service runs in a background
QThread (ServiceThread). Communication via LiveSettings, UIEventQueue, and
Qt signals.
"""
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, Qt, QThread, QUrl, QTimer
from PyQt6.QtGui import QAction, QDesktopServices, QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QPlainTextEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config_manager import ConfigManager
from .settings_tab import SettingsTab
from .rules_tab import RulesTab
from .terminals_tab import TerminalsTab
from .toast import ToastManager
from .trades_tab import TradesTab
from ..service_thread import ServiceThread, WarmupThread

from ..paths import (
    APP_NAME,
    app_home as _app_home,
    icons_dir as _icons_dir,
)

_ICONS = _icons_dir()


def app_icon() -> QIcon:
    """The MetaCT app icon.

    Prefers the multi-size .ico so Windows can pick the right bitmap per
    context (16px tray, 32px taskbar, 256px alt-tab); falls back to the SVG,
    then the 256px PNG. Returns an empty icon if none are on disk — a missing
    icon file must never stop the app from starting.
    """
    for name in ("app_icon.ico", "app_icon.svg", "app_icon.png"):
        path = _ICONS / name
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


class _StatsCollector(QThread):
    """Background thread that periodically collects debug stats and pushes to GUI.

    All stats collection happens here — the main thread only receives
    the final dict via Qt signal (never blocked).
    Reads trace/dump files incrementally (tail behaviour).
    """

    stats_updated = pyqtSignal(dict)

    #: Ceilings on one tick's worth of tail. The pane is a live view, not an
    #: archive: past these, the file itself is the answer and handing more to
    #: the GUI thread only costs it latency.
    MAX_TAIL_BYTES = 256 * 1024
    MAX_TAIL_LINES = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self._start_time = time.time()
        self._base_dir = _app_home()
        # None means "not seen yet" — the first read seeks to the end rather
        # than replaying the file from its beginning.
        self._trace_pos: int | None = None
        self._dump_pos: int | None = None
        #: Which file each offset belongs to, so a change of file resets it.
        self._tailing: dict[str, str] = {}

    def run(self):
        while self._running:
            try:
                stats = self._collect()
                self.stats_updated.emit(stats)
            except Exception:
                pass
            # Sleep in small increments so stop() is responsive
            for _ in range(20):
                if not self._running:
                    break
                time.sleep(0.1)

    def stop(self):
        self._running = False

    def _collect(self) -> dict:
        runtime_s = time.time() - self._start_time

        # Threads
        thread_list = [(t.name, t.is_alive(), t.daemon) for t in threading.enumerate()]

        # Read new trace/dump lines (incremental)
        trace_lines, trace_path = self._read_new_lines(
            "traces", "", "_traces.jsonl", "_trace_pos",
        )
        dump_lines, dump_path = self._read_new_lines(
            "dumps", "", "_debug.jsonl", "_dump_pos",
        )

        # Log file path (for hyperlink only, not content)
        log_path = self._find_session_log()

        return {
            "runtime_s": runtime_s,
            "threads": thread_list,
            "thread_count": len(thread_list),
            "trace_lines": trace_lines,
            "dump_lines": dump_lines,
            "trace_path": trace_path,
            "dump_path": dump_path,
            "log_path": log_path,
        }

    def _find_session_log(self) -> str:
        """Relative path of this session's log, or "" if there is none."""
        return self._session_file("logs", "copier_", ".log")

    def _session_file(self, subdir: str, prefix: str, suffix: str) -> str:
        """This run's file in ``subdir``, relative to the data root.

        Everything the app writes is now named for the session — so this asks
        for THIS session's file and never falls back to another run's. If the
        stamp cannot be resolved (it is computed by the logging setup), the
        newest matching file is the honest second-best.
        """
        directory = self._base_dir / subdir
        try:
            from ..utils.logging import session_stamp

            target = directory / f"{prefix}{session_stamp()}{suffix}"
            if target.exists():
                return str(target.relative_to(self._base_dir))
            files = sorted(directory.glob(f"{prefix}*{suffix}"))
        except OSError:
            return ""
        except Exception:
            return ""
        return str(files[-1].relative_to(self._base_dir)) if files else ""

    def _read_new_lines(
        self, subdir: str, prefix: str, suffix: str, pos_attr: str,
    ) -> tuple[list[str], str]:
        """Tail this session's file — bounded in every direction.

        WHAT THIS USED TO DO, AND WHY THE APP FROZE
            It started at offset 0 of a per-DAY file and handed every line it
            found to the GUI thread, which appended them to an unbounded
            QTextEdit. Opening the app late in a day that had already written
            28 MB of traces meant reading all of it, splitting it into ~80,000
            strings, and laying them out as a rich-text document on the main
            thread. That is the "Not Responding" — the copy engine was fine,
            the window was rendering a day of history nobody asked for.

            Three bounds fix it, all of them necessary:
              * start at the END of an existing file (this pane shows what
                happens FROM NOW; the file itself is there for history);
              * read at most MAX_TAIL_BYTES per tick, so a burst cannot hand
                over an unbounded string;
              * the widgets cap their own line count (see _make_output).
        """
        rel_path = self._session_file(subdir, prefix, suffix)
        if not rel_path:
            return [], ""
        target = self._base_dir / rel_path
        pos = getattr(self, pos_attr)

        # The file can change identity underneath us: before the service
        # starts there is no file for this session, so the newest older one is
        # shown, and it is replaced the moment the service writes its first
        # record. An offset carried across that switch points into the middle
        # of a different file.
        if self._tailing.get(pos_attr) != rel_path:
            self._tailing[pos_attr] = rel_path
            pos = None

        try:
            size = target.stat().st_size
            if pos is None:
                # First sight of this file: skip whatever is already in it.
                setattr(self, pos_attr, size)
                return [], rel_path
            if size < pos:
                pos = 0            # truncated or replaced — start over
            if size <= pos:
                return [], rel_path

            with open(target, "r", encoding="utf-8") as f:
                f.seek(pos)
                new_data = f.read(self.MAX_TAIL_BYTES)
                setattr(self, pos_attr, f.tell())

            lines = [line for line in new_data.splitlines() if line.strip()]
            if len(lines) > self.MAX_TAIL_LINES:
                dropped = len(lines) - self.MAX_TAIL_LINES
                lines = ([f"... {dropped} line(s) not shown — see {rel_path}"]
                         + lines[-self.MAX_TAIL_LINES:])
            return lines, rel_path
        except Exception:
            return [], rel_path


class DashboardTab(QWidget):
    """Debug tab for monitoring service internals.

    Manages the ServiceThread lifecycle and displays system stats,
    observability info, session files, and service log output.
    Service controls (Start/Stop/Pause) live in TradesTab.
    """

    service_started = pyqtSignal()
    service_stopped = pyqtSignal()
    status_changed_external = pyqtSignal(str)  # Forwarded to TradesTab

    def __init__(self, config_manager: ConfigManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._service_thread = None  # ServiceThread | None
        self._warmup_thread = None   # WarmupThread | None
        self._warmed_service = None  # CopyTradingService | None
        self._base_dir = _app_home()
        self._setup_ui()

        # Start background stats collector
        self._stats_collector = _StatsCollector(self)
        self._stats_collector.stats_updated.connect(self._on_stats_updated)
        self._stats_collector.start()

    # Both types are named: the live panes are QPlainTextEdit (bounded, plain
    # text) while the report dialog still uses QTextEdit, and a selector that
    # named only one of them would leave the other unstyled.
    _DARK_TEXT_STYLE = (
        "QTextEdit, QPlainTextEdit {"
        "  background-color: #1e1e1e; color: #d4d4d4;"
        "  font-family: Consolas, monospace; font-size: 11px;"
        "  border: 1px solid #333; border-radius: 4px;"
        "}"
    )

    def _setup_ui(self):
        """Setup the debug page UI with 2x2 grid layout."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # === 2x2 Grid (inside a container for hide/show) ===
        self._grid_widget = QWidget()
        grid = QGridLayout(self._grid_widget)
        grid.setSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        # --- Top-left: Stats panel ---
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(4)
        stats_widget.setStyleSheet(
            "background-color: #f5f5f5; border-radius: 4px;"
        )

        self._runtime_label = QLabel("Runtime: --")
        stats_layout.addWidget(self._runtime_label)

        self._threads_label = QLabel("Threads: --")
        self._threads_label.setToolTip("Hover for thread list")
        stats_layout.addWidget(self._threads_label)

        # File hyperlinks
        stats_layout.addSpacing(8)
        files_header = QLabel("Files:")
        files_header.setStyleSheet("font-weight: bold; color: #555;")
        stats_layout.addWidget(files_header)

        self._file_links = QLabel("(none yet)")
        self._file_links.setStyleSheet("font-size: 11px;")
        self._file_links.setTextFormat(Qt.TextFormat.RichText)
        self._file_links.linkActivated.connect(self._open_file_location)
        self._file_links.setWordWrap(True)
        stats_layout.addWidget(self._file_links)
        self._file_links_set = False

        stats_layout.addStretch()
        grid.addWidget(stats_widget, 0, 0)

        # --- Top-right: Service Output ---
        svc_widget = QWidget()
        svc_layout = QVBoxLayout(svc_widget)
        svc_layout.setContentsMargins(0, 0, 0, 0)
        svc_layout.setSpacing(2)

        svc_label = QLabel("Service Output")
        svc_label.setStyleSheet("color: #666; font-size: 11px;")
        svc_layout.addWidget(svc_label)

        self.log_output = self._make_output(self._DARK_TEXT_STYLE)
        svc_layout.addWidget(self.log_output)

        grid.addWidget(svc_widget, 0, 1)

        # --- Bottom-left: Traces ---
        trace_widget = QWidget()
        trace_layout = QVBoxLayout(trace_widget)
        trace_layout.setContentsMargins(0, 0, 0, 0)
        trace_layout.setSpacing(2)

        trace_label = QLabel("Traces")
        trace_label.setStyleSheet("color: #666; font-size: 11px;")
        trace_layout.addWidget(trace_label)

        self._trace_output = self._make_output(self._DARK_TEXT_STYLE)
        trace_layout.addWidget(self._trace_output)

        grid.addWidget(trace_widget, 1, 0)

        # --- Bottom-right: Dumps ---
        dump_widget = QWidget()
        dump_layout = QVBoxLayout(dump_widget)
        dump_layout.setContentsMargins(0, 0, 0, 0)
        dump_layout.setSpacing(2)

        dump_label = QLabel("Dumps")
        dump_label.setStyleSheet("color: #666; font-size: 11px;")
        dump_layout.addWidget(dump_label)

        self._dump_output = self._make_output(self._DARK_TEXT_STYLE)
        dump_layout.addWidget(self._dump_output)

        grid.addWidget(dump_widget, 1, 1)

        outer.addWidget(self._grid_widget, 1)

        # Quick start guide (shown when not configured)
        self.guide_widget = QWidget()
        guide_layout = QVBoxLayout(self.guide_widget)
        guide_layout.setContentsMargins(20, 20, 20, 20)

        guide_title = QLabel("Quick Start")
        guide_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        guide_layout.addWidget(guide_title)

        steps = [
            "1. Go to Settings tab",
            "2. Set your Master MT5 terminal path",
            "3. Click '+ Add' to add slave accounts",
            "4. Fill in credentials and enable the slave",
            "5. Save configuration",
            "6. Click Start on the Trading tab",
        ]
        for step in steps:
            lbl = QLabel(step)
            lbl.setStyleSheet("margin: 2px 0; color: #555;")
            guide_layout.addWidget(lbl)

        guide_layout.addStretch()
        self.guide_widget.setStyleSheet(
            "background-color: #f5f5f5; border-radius: 8px;"
        )

        # Initially show guide, hide grid
        self._grid_widget.hide()
        outer.addWidget(self.guide_widget)

    # -------------------------------------------------------- stats updates
    #: Lines kept in each live output pane. A QTextEdit with a day of trace
    #: lines in it is slow to lay out and impossible to read; QPlainTextEdit
    #: with a block cap discards the oldest line in O(1) as new ones arrive.
    #: The files keep everything — these panes are a window, not the record.
    OUTPUT_MAX_LINES = 2000

    @classmethod
    def _make_output(cls, style: str) -> QPlainTextEdit:
        """A read-only, bounded, plain-text pane for live output."""
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet(style)
        view.setMaximumBlockCount(cls.OUTPUT_MAX_LINES)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        return view

    def _on_stats_updated(self, stats: dict):
        """Slot: receive stats from _StatsCollector (runs in main thread)."""
        # Runtime
        secs = int(stats.get("runtime_s", 0))
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        self._runtime_label.setText(f"Runtime: {h}h {m:02d}m {s:02d}s")

        # Threads
        thread_count = stats.get("thread_count", 0)
        self._threads_label.setText(f"Threads: {thread_count}")
        thread_list = stats.get("threads", [])
        tooltip_lines = [f"{name} ({'daemon' if daemon else 'non-daemon'})"
                         for name, alive, daemon in thread_list if alive]
        self._threads_label.setToolTip("\n".join(tooltip_lines) if tooltip_lines else "No threads")

        # File hyperlinks (set once when paths become available)
        if not self._file_links_set:
            links = []
            for key in ("trace_path", "dump_path", "log_path"):
                rel = stats.get(key, "")
                if rel:
                    abs_path = str(self._base_dir / rel)
                    links.append(f'<a href="{abs_path}">{rel}</a>')
            if links:
                self._file_links.setText("<br>".join(links))
                self._file_links_set = True

        # Append new trace lines (batched to avoid per-line repaints)
        trace_lines = stats.get("trace_lines", [])
        if trace_lines:
            self._trace_output.setUpdatesEnabled(False)
            self._trace_output.appendPlainText("\n".join(trace_lines))
            self._trace_output.setUpdatesEnabled(True)

        # Append new dump lines (batched)
        dump_lines = stats.get("dump_lines", [])
        if dump_lines:
            self._dump_output.setUpdatesEnabled(False)
            self._dump_output.appendPlainText("\n".join(dump_lines))
            self._dump_output.setUpdatesEnabled(True)

    def _open_file_location(self, link: str):
        """Open the containing folder of a session file."""
        path = Path(link)
        folder = path.parent if path.is_file() else path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -------------------------------------------------------- public API
    @property
    def is_running(self) -> bool:
        """Whether the service or warmup thread is currently running."""
        return (
            (self._service_thread is not None and self._service_thread.isRunning())
            or (self._warmup_thread is not None and self._warmup_thread.isRunning())
        )

    def set_config_ready(self, ready: bool, slave_count: int = 0):
        """Update UI based on config state."""
        self._config_ready = ready

        if ready:
            self.guide_widget.hide()
            self._grid_widget.show()
            self.log_output.appendPlainText(f"[Ready] {slave_count} slave(s) configured")
        else:
            if not self.is_running:
                self.guide_widget.show()
                self._grid_widget.hide()

    def _get_terminal_paths(self) -> set[str]:
        """Collect all configured MT5 terminal paths for killing.

        Union of: master path, every slave's terminal_path, default slave
        terminal_path, AND a live disk scan of slaves_dir so orphan slave
        folders (deleted from config but still on disk with running terminals)
        are also caught.
        """
        from ..template_manager import collect_slave_terminals_on_disk
        paths = set()
        try:
            data = self.config_manager.load()
            tp = data.get("master", {}).get("terminal_path", "")
            if tp:
                paths.add(tp)
            for s in data.get("slaves", []):
                tp = s.get("terminal_path", "")
                if tp:
                    paths.add(tp)
            tp = data.get("slave_terminal", {}).get("terminal_path", "")
            if tp:
                paths.add(tp)
        except Exception:
            pass
        paths |= collect_slave_terminals_on_disk()
        return paths

    def _close_terminals_on_stop(self) -> bool:
        """Whether stopping the service should also close the terminals.

        Off by default — see TerminalLifecycleConfig. Read from the file each
        time rather than cached, so flipping it takes effect without a restart.
        """
        try:
            return bool(
                (self.config_manager.load().get("terminals", {}) or {})
                .get("close_on_stop", False)
            )
        except Exception:
            return False

    def _kill_terminals_on_stop(self):
        """Close the terminals when stopping — unless we mean to reuse them.

        Leaving them running is what makes the next warmup instant: measured,
        100% of an 11-45s warmup was `mt5.initialize` cold-launching terminals
        the previous Stop had just closed, while the warmup work itself took
        38-187ms. Attaching to a live terminal returns immediately.
        """
        if self._close_terminals_on_stop():
            self.log_output.appendPlainText("[Closing terminals...]")
            self._kill_all_terminals()
        else:
            self.log_output.appendPlainText(
                "[Terminals left running — next warmup will reuse them]"
            )

    def _kill_all_terminals(self, grace_s: float | None = None):
        """Close all configured MT5 terminals, force-killing only stragglers.

        Blocks the GUI thread while the terminals save and exit (seconds), so
        Qt's event loop is pumped from the wait to keep the window painted.
        """
        import logging

        log = logging.getLogger("metact.gui")
        paths = self._get_terminal_paths()
        log.info("_kill_all_terminals: %d path(s): %s", len(paths), sorted(paths))
        if not paths:
            return

        from ..parallel_worker import _kill_mt5_terminals
        _kill_mt5_terminals(
            paths, grace_s=grace_s, on_wait=QApplication.processEvents,
        )

    def _warmup_service(self):
        """Start warmup in a background thread (setup + warmup_slaves)."""
        if self._warmup_thread is not None and self._warmup_thread.isRunning():
            self.log_output.appendPlainText("[Warmup already in progress]")
            return

        self._warmup_thread = WarmupThread(self)
        self._warmup_thread.log_message.connect(self._on_log_message)
        self._warmup_thread.status_changed.connect(self._on_status_changed)
        self._warmup_thread.finished.connect(self._on_warmup_finished)
        self._warmup_thread.start()

        self.log_output.appendPlainText("[Warming up...]")
        self.status_changed_external.emit("warming_up")

    def _on_warmup_finished(self):
        """Handle warmup thread finish — read success/service from thread."""
        thread = self._warmup_thread
        self._warmup_thread = None

        if thread and thread.success:
            self._warmed_service = thread.service
            ok = thread.warmup_ok
            failed = thread.warmup_failed
            if failed == 0:
                self.log_output.appendPlainText("[Warmup complete — ready to start]")
            elif ok > 0:
                self.log_output.appendPlainText(
                    f"[Warmup partial — {ok} OK, {failed} failed. "
                    f"Check slave terminal paths and permissions.]"
                )
            else:
                self.log_output.appendPlainText(
                    f"[Warmup failed — all {failed} slave(s) failed to connect. "
                    f"Check terminal paths and relaunch as Administrator if needed.]"
                )
            self.status_changed_external.emit("warm")

            # Auto-enable copy trading once warmup is done (same path as the
            # Start button). Only when at least one slave connected — no point
            # starting the service if every slave failed to warm up.
            if ok > 0:
                self.log_output.appendPlainText("[Auto-starting copy trading after warmup...]")
                self._start_service()

    def _start_service(self):
        """Start the copy trading service using the warmed service instance."""
        if self._warmed_service is None:
            self.log_output.appendPlainText("[Error: No warmed service — run Warm Up first]")
            return

        self._service_thread = ServiceThread(self._warmed_service, self)
        self._service_thread.log_message.connect(self._on_log_message)
        self._service_thread.status_changed.connect(self._on_status_changed)
        self._service_thread.finished.connect(self._on_finished)
        self._service_thread.start()

        self.log_output.appendPlainText("[Starting service...]")
        self.status_changed_external.emit("starting")
        self.service_started.emit()

    def _stop_service(self):
        """Stop the running/warmed service and kill all terminals."""
        if self._service_thread:
            self.log_output.appendPlainText("[Stopping service...]")
            self._service_thread.request_stop()
            # _on_finished handles cleanup when thread exits
        elif self._warmed_service:
            # Warmed but not running — shutdown the warmed service directly
            self.log_output.appendPlainText("[Stopping warmed service...]")
            self._warmed_service.shutdown()
            self._warmed_service = None
            self._kill_terminals_on_stop()
            self.log_output.appendPlainText("[Service stopped]")
            self.status_changed_external.emit("stopped")
            self.service_stopped.emit()
        elif self._warmup_thread:
            # Warmup in progress — can't interrupt, but kill terminals
            self.log_output.appendPlainText("[Killing terminals...]")
            self._kill_all_terminals()
            self.status_changed_external.emit("stopped")
            self.service_stopped.emit()

    def _on_log_message(self, msg: str):
        """Handle log output from service thread."""
        self.log_output.appendPlainText(msg)

    def _on_status_changed(self, status: str):
        """Handle service status changes."""
        self.status_changed_external.emit(status)

    def _on_finished(self):
        """Handle service thread finish — do full shutdown here."""
        self._service_thread = None
        if self._warmed_service:
            self._warmed_service.shutdown()
            self._warmed_service = None
        self._kill_terminals_on_stop()
        self.log_output.appendPlainText("[Service stopped]")
        self.status_changed_external.emit("stopped")
        self.service_stopped.emit()

    def _toggle_pause(self, paused: bool):
        """Handle pause toggle from TradesTab."""
        from ..live_settings import get_live_settings

        settings = get_live_settings()
        settings.paused = paused

        state = "PAUSED" if paused else "RESUMED"
        self.log_output.appendPlainText(f"[Copy trading {state}]")


class MainWindow(QMainWindow):
    """Main application window."""

    # Signal to bridge UIEventQueue callback (service thread) → main thread
    _trade_event_signal = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.config_manager = ConfigManager()
        self._setup_ui()
        self._setup_menu()
        self._check_config()

        # The daily stop wall, once it exists, and the flag that lets its
        # button close the app without being asked to confirm.
        self._stop_wall = None
        self._force_quit = False

        # Toast notifications
        self.toast_manager = ToastManager()
        self._trade_event_signal.connect(self.toast_manager.on_trade_event)
        # Also handle service-level prompts (e.g. EA port unavailable) on the
        # GUI thread.
        self._trade_event_signal.connect(self._on_service_prompt)

        # Apply saved toast position + real-price-calculator strip
        try:
            data = self.config_manager.load()
            pos = data.get("settings", {}).get("toast_position", "Bottom-Right")
            self.toast_manager.set_position(pos)
            self.trades_tab.apply_config(data)
            self.trades_tab.price_strip.apply_config(data)
            self.trades_tab.news_strip.apply_config(data)
        except Exception:
            pass

        # Register callback on UIEventQueue for toast notifications
        from ..events import get_ui_queue
        self._ui_queue = get_ui_queue()
        self._ui_queue.register_callback(self._on_trade_event_from_service)

        # EA-down alert debounce: only warn if the EA link stays down ≥5s.
        # Flapping reconnects cancel the timer, so 10 quick reconnects = 0 popups.
        self._ea_down_timer = QTimer(self)
        self._ea_down_timer.setSingleShot(True)
        self._ea_down_timer.setInterval(5000)
        self._ea_down_timer.timeout.connect(self._ea_down_alert)

    def _setup_ui(self):
        """Setup the main window UI."""
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(975, 720)
        self.resize(1125, 825)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # Tab widget
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(
            """
            QTabWidget::pane {
                border: none;
                background: white;
            }
            QTabBar::tab {
                padding: 8px 20px;
                margin: 0 1px;
                background: #f0f0f0;
            }
            QTabBar::tab:selected {
                background: white;
                border-bottom: 2px solid #4CAF50;
            }
            QTabBar::tab:hover {
                background: #e8e8e8;
            }
            """
        )

        # Trades tab (first tab)
        self.trades_tab = TradesTab()
        self.tabs.addTab(self.trades_tab, "Trading")

        # Slaves tab (per-slave runtime control)
        self.terminals_tab = TerminalsTab()
        self.tabs.addTab(self.terminals_tab, "Terminals")

        # Dashboard tab (debug page)
        self.dashboard_tab = DashboardTab(self.config_manager)
        self.dashboard_tab.service_started.connect(self.trades_tab.start_polling)
        self.dashboard_tab.service_stopped.connect(self.trades_tab.stop_polling)
        self.tabs.addTab(self.dashboard_tab, "Debug")

        # Wire Trading tab controls → DashboardTab service logic
        self.trades_tab.warmup_requested.connect(self.dashboard_tab._warmup_service)
        self.trades_tab.start_requested.connect(self.dashboard_tab._start_service)
        self.trades_tab.stop_requested.connect(self.dashboard_tab._stop_service)
        self.trades_tab.pause_toggled.connect(self.dashboard_tab._toggle_pause)

        # Forward status changes to Trading tab
        self.dashboard_tab.status_changed_external.connect(self.trades_tab.update_status)

        # Wire warmup done → populate slaves tab
        self.dashboard_tab.status_changed_external.connect(self._on_status_for_slaves)

        # Wire service lifecycle → slaves tab
        self.dashboard_tab.service_stopped.connect(self.terminals_tab.on_service_stopped)
        self.dashboard_tab.service_started.connect(self.terminals_tab.on_service_started)

        # Wire slaves tab delete → save config + refresh settings
        self.terminals_tab.settings_save_requested.connect(self._on_slaves_config_changed)

        # Rules tab (per-slave account config: risk mode, mappings, template)
        self.rules_tab = RulesTab(self.config_manager)
        self.rules_tab.config_changed.connect(self._on_config_changed)
        self.tabs.addTab(self.rules_tab, "Rules")

        # Settings tab
        self.settings_tab = SettingsTab(self.config_manager)
        self.settings_tab.config_changed.connect(self._on_config_changed)
        self.tabs.addTab(self.settings_tab, "Settings")

        # Pre-populate slaves tab from config (controls disabled until warmup)
        try:
            config_data = self.config_manager.load()
            self.terminals_tab.on_config_changed(config_data)
        except Exception:
            pass

        layout.addWidget(self.tabs)

        # Prompt to save unsaved settings when switching away from the Settings tab.
        self._settings_tab_index = self.tabs.indexOf(self.settings_tab)
        self._prev_tab_index = self.tabs.currentIndex()
        self._reverting_tab = False
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Status bar
        self.status_bar = QStatusBar()
        self.status_bar.setStyleSheet("background: #f5f5f5; border-top: 1px solid #ddd;")
        self.setStatusBar(self.status_bar)

    def _setup_menu(self):
        """Setup the menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")

        reload_action = QAction("Reload Config", self)
        reload_action.setShortcut("Ctrl+R")
        reload_action.triggered.connect(self._reload_config)
        file_menu.addAction(reload_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Tools menu
        tools_menu = menubar.addMenu("Tools")

        diag_action = QAction("Diagnostics", self)
        diag_action.setShortcut("Ctrl+D")
        diag_action.triggered.connect(self._show_diagnostics)
        tools_menu.addAction(diag_action)

        # About lives under Tools. A whole menu holding one static dialog was
        # a row of pixels earning nothing.
        tools_menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        tools_menu.addAction(about_action)

    def _check_config(self):
        """Check if configuration is valid."""
        try:
            data = self.config_manager.load()
            master_path = data.get("master", {}).get("terminal_path", "")
            slaves = data.get("slaves", [])

            valid, _ = self.config_manager.validate_master_path(master_path)
            enabled_slaves = [s for s in slaves if s.get("enabled", False)]
            config_ready = valid and len(enabled_slaves) > 0

            self.dashboard_tab.set_config_ready(config_ready, len(enabled_slaves))
            self.trades_tab.warmup_btn.setEnabled(
                config_ready and not self.dashboard_tab.is_running
            )
            self.trades_tab.play_pause_btn.setEnabled(False)

            if config_ready:
                self.status_bar.showMessage(f"Ready - {len(enabled_slaves)} slave(s) enabled")
            else:
                self.status_bar.showMessage("Configure accounts in Settings tab")

        except Exception as e:
            self.status_bar.showMessage(f"Config error: {e}")
            self.dashboard_tab.set_config_ready(False)

    def _on_status_for_slaves(self, status: str):
        """Populate slaves tab when warmup completes."""
        if status == "warm" and self.dashboard_tab._warmed_service:
            service = self.dashboard_tab._warmed_service
            try:
                config_data = self.config_manager.load()
                self.terminals_tab.populate(config_data, service.copier)
            except Exception:
                pass

    def _on_slaves_config_changed(self):
        """Handle config save from slaves tab (disable slave)."""
        try:
            config_data = self.terminals_tab._config_data
            if config_data:
                self.config_manager.save(config_data)
                # Refresh the Rules tab (owns the slave table)
                self.rules_tab.reload()
                # Update LiveSettings
                from ..live_settings import get_live_settings
                get_live_settings().update_from_dict(config_data)
        except Exception:
            pass

    def _on_tab_changed(self, index: int):
        """Prompt to save when leaving the Settings tab with unsaved edits.

        currentChanged fires after the switch, so if the user cancels we snap
        back to the Settings tab (guarded against re-entry by _reverting_tab).
        """
        if self._reverting_tab:
            return
        leaving_settings = (
            self._prev_tab_index == self._settings_tab_index
            and index != self._settings_tab_index
        )
        if leaving_settings and not self.settings_tab.maybe_prompt_save():
            # User cancelled (or save failed) — stay on Settings.
            self._reverting_tab = True
            self.tabs.setCurrentIndex(self._settings_tab_index)
            self._reverting_tab = False
            self._prev_tab_index = self._settings_tab_index
            return
        self._prev_tab_index = index

    def _on_config_changed(self):
        """Handle config changes from settings tab."""
        self._check_config()
        self.status_bar.showMessage("Configuration saved", 3000)

        # Sync slaves tab with updated config (re-enabled slaves, etc.)
        try:
            data = self.config_manager.load()
            self.terminals_tab.on_config_changed(data)
        except Exception:
            pass

        # Update toast position + price-calculator strip from saved config
        try:
            data = self.config_manager.load()
            pos = data.get("settings", {}).get("toast_position", "Bottom-Right")
            self.toast_manager.set_position(pos)
            self.trades_tab.apply_config(data)
            self.trades_tab.price_strip.apply_config(data)
            self.trades_tab.news_strip.apply_config(data)
        except Exception:
            pass

    def _reload_config(self):
        """Reload configuration from file."""
        self.config_manager.load()
        self.settings_tab._data = self.config_manager._data
        self.settings_tab._load_config()
        self.rules_tab.reload()
        self.trades_tab.apply_config(self.config_manager._data)
        self.trades_tab.price_strip.apply_config(self.config_manager._data)
        self.trades_tab.news_strip.apply_config(self.config_manager._data)
        self._check_config()
        self.status_bar.showMessage("Configuration reloaded", 3000)

    def _show_about(self):
        """Show about dialog."""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            "<p>Automatically copy trades from a master MT5 account "
            "to multiple slave accounts.</p>"
            "<p><b>Features:</b></p>"
            "<ul>"
            "<li>Multi-terminal parallel execution</li>"
            "<li>Per-slave symbol mapping</li>"
            "<li>Volume adjustment by leverage</li>"
            "<li>SL/TP copying</li>"
            "</ul>",
        )

    def _show_diagnostics(self):
        """Show diagnostics dialog with system and debug info."""
        import json
        import platform
        import sys
        import threading

        from ..events import get_ui_queue

        lines = []
        lines.append("<h3>Diagnostics</h3>")

        # System info
        lines.append("<p><b>System:</b></p>")
        lines.append(f"<pre>Python: {sys.version.split()[0]}")
        lines.append(f"Platform: {platform.platform()}")
        lines.append(f"Processor: {platform.processor()}")
        lines.append(f"Threads: {threading.active_count()}</pre>")

        # UI Event Queue
        queue = get_ui_queue()
        lines.append("<p><b>UI Event Queue:</b></p>")
        lines.append(f"<pre>Dropped: {queue.dropped_count}")
        lines.append(f"Total emitted: {queue.total_emitted}</pre>")

        # EA push link. First thing to look at when copies stop being instant:
        # `connected` says the socket is up, `silent_for_s` says whether the EA
        # on the other end is still talking, and those two disagree exactly
        # when the terminal has wedged.
        bridge = getattr(
            self.dashboard_tab._warmed_service, "_ea_bridge", None
        ) if self.dashboard_tab._warmed_service else None
        lines.append("<p><b>EA Push Link (TCP):</b></p>")
        if bridge is None:
            lines.append("<pre>not started (service is not warm)</pre>")
        else:
            try:
                lines.append(
                    f"<pre>{json.dumps(bridge.stats(), indent=2, default=str)}</pre>"
                )
            except Exception as e:
                lines.append(f"<pre>unavailable: {e}</pre>")

        # Try to get debug stats
        try:
            from ..utils.debug import get_debug_stats
            stats = get_debug_stats()
            lines.append("<p><b>Debug Stats:</b></p>")
            lines.append(f"<pre>{json.dumps(stats, indent=2, default=str)}</pre>")
        except Exception as e:
            lines.append(f"<p>Debug stats unavailable: {e}</p>")

        # Thread list
        lines.append("<p><b>Active Threads:</b></p>")
        lines.append("<pre>")
        for t in threading.enumerate():
            lines.append(f"  {t.name} (daemon={t.daemon})")
        lines.append("</pre>")

        self._show_report("Diagnostics", "\n".join(lines))

    def _show_report(self, title: str, html: str):
        """A resizable, scrollable, selectable report window.

        QMessageBox sizes itself to its text and then stops: the diagnostics
        run to a few hundred lines of thread and debug state, and everything
        past the first screenful was simply unreachable — no scrollbar, no
        resize, and no way to select the part you wanted to paste into a bug
        report. A plain dialog around a read-only QTextEdit gives all three.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(760, 560)

        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setHtml(html)
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)   # keep tables aligned
        layout.addWidget(view)

        buttons = QHBoxLayout()
        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(view.toPlainText())
        )
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(dialog.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        dialog.exec()

    def _on_trade_event_from_service(self, event):
        """UIEventQueue callback (called from service thread).

        Emits Qt signal to safely deliver event to main thread for toast display.
        """
        self._trade_event_signal.emit(event)

    def _on_service_prompt(self, event):
        """Handle service-level prompts on the GUI thread (main thread)."""
        from ..events import EventType
        et = getattr(event, "event_type", None)

        if et == EventType.EA_STATUS:
            self._on_ea_status(bool(event.ea_connected))
            return

        if et == EventType.DAILY_STOP_LIMIT:
            self._on_daily_stop_limit(event)
            return

        if et != EventType.EA_UNAVAILABLE:
            return
        reply = QMessageBox.warning(
            self,
            "EA Fast-Copy Unavailable",
            f"{event.error}\n\n"
            "Continue on Polling (slower), or Close the service?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Close,
            QMessageBox.StandardButton.Ok,
        )
        if reply == QMessageBox.StandardButton.Close:
            # User chose to stop rather than run degraded.
            self.dashboard_tab._stop_service()

    def _on_daily_stop_limit(self, event):
        """The day's loss limit is reached: freeze the app, then end it.

        ORDER MATTERS
            The lock-out is applied BEFORE the wall goes up, so the screen
            underneath is already frozen when it appears — nothing behind the
            overlay is live for even the moment it takes to read it, and
            nothing is live afterwards either, because the only button closes
            the app.

        WHY THE APP CLOSES
            A limit you can sit and look at is one you can talk yourself out
            of. The wall is modal and undismissable and its button quits, so
            the day ends with the process. What is already open on the slaves
            keeps the SL and TP it was given at the broker; what stops is this
            app mirroring anything further, which is stated on the wall itself
            rather than left to be discovered.

        Shown once per trip — on a trip that happened while running, and again
        on the next launch, where the service re-reads the same history and
        announces the same limit before it will start.
        """
        banner = (
            f"DAILY STOP LIMIT — {event.stop_count} losing trade(s) today "
            f"(limit {event.stop_limit}). Trading is finished for today."
        )
        self.trades_tab.lock_out(banner)
        self.terminals_tab.lock_out()
        self.statusBar().showMessage(banner)

        self._show_stop_wall(event)

    def _show_stop_wall(self, event):
        """Put the full-screen limit wall up, then close the app behind it.

        Guarded against a second showing: the trip is announced once by the
        service, but a queued duplicate must not stack two full-screen modals
        with one quit between them.
        """
        if getattr(self, "_stop_wall", None) is not None:
            return

        from .stop_overlay import DailyStopOverlay

        # "On launch" is the case where the service refused to start at all —
        # it re-read the day's history and never reached its poll loop, so
        # nothing was being copied when this arrived.
        on_launch = not self.dashboard_tab.is_running

        wall = DailyStopOverlay(
            count=event.stop_count,
            limit=event.stop_limit,
            details=tuple(event.stop_details or ()),
            open_positions=event.open_positions,
            on_launch=on_launch,
            parent=self,
        )
        self._stop_wall = wall
        wall.show_over_everything()
        wall.exec()

        # The wall's only button returns here. Skip the "service is running,
        # stop and exit?" question — it was just answered, by a rule that does
        # not take questions.
        self._force_quit = True
        self.close()

    def _on_ea_status(self, connected: bool):
        """Update the green/red EA dot; arm a debounced down-alert."""
        self.trades_tab.set_ea_status(connected)
        if connected:
            self._ea_down_timer.stop()  # recovered (or flap) → cancel pending alert
        elif not self._ea_down_timer.isActive():
            self._ea_down_timer.start()  # alert only if still down after 5s

    def _ea_down_alert(self):
        """Fires only if the EA link stayed down ≥5s (flaps cancelled it)."""
        if getattr(self.trades_tab, "_ea_connected", False):
            return  # recovered between timer fire and now
        QMessageBox.warning(
            self,
            "EA Push Down",
            "EA (TCP) link to the master CopierBridge is down.\n"
            "Copies are running on the polling fallback (slower).\n\n"
            "Check: CopierBridge EA attached to a master chart + Algo Trading enabled.",
        )

    def closeEvent(self, event):
        """Handle window close."""
        # The daily stop wall closes the app itself; its button IS the answer
        # to the confirmation below, so asking again would let Enter-on-No
        # reopen a session the limit just ended.
        if getattr(self, "_force_quit", False):
            pass
        elif self.dashboard_tab.is_running or self.dashboard_tab._warmed_service:
            reply = QMessageBox.question(
                self, "Confirm Exit",
                "Service is running. Stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        # --- Point of no return: tear everything down immediately ---

        # Update slaves tab state before killing terminals
        self.terminals_tab.on_service_stopped()

        # Unregister event callback
        self._ui_queue.unregister_callback(self._on_trade_event_from_service)

        # Stop stats collector
        self.dashboard_tab._stats_collector.stop()

        # Stop trades polling
        self.trades_tab.stop_polling()

        # Kill worker processes first: a worker mid-request would otherwise be
        # talking to a terminal that is in the middle of shutting down.
        import multiprocessing
        for child in multiprocessing.active_children():
            child.kill()

        # Then ask the terminals to close and give them time to save. This
        # blocks for seconds by design — a force-kill here is what loses EA
        # inputs and other terminal settings changed this session.
        from PyQt6.QtGui import QCursor

        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            self.dashboard_tab._kill_all_terminals()
        finally:
            QApplication.restoreOverrideCursor()

        # Clean up warmed service if present
        if self.dashboard_tab._warmed_service:
            self.dashboard_tab._warmed_service.shutdown()
            self.dashboard_tab._warmed_service = None

        # Fire-and-forget service stop (don't block GUI thread)
        if self.dashboard_tab._service_thread and self.dashboard_tab._service_thread.isRunning():
            self.dashboard_tab._service_thread.request_stop()

        event.accept()


def run_gui():
    """Run the GUI application."""
    import logging
    log = logging.getLogger("metact").getChild("gui")
    log.info("run_gui: building QApplication (argv=%s, platform=%s)", sys.argv, sys.platform)

    # Windows groups taskbar buttons by AppUserModelID. Without an explicit one
    # a PyQt app inherits python.exe's identity, so the taskbar shows Python's
    # icon and title no matter what setWindowIcon says. Must be set before any
    # window is created.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_NAME}.Desktop"
            )
        except Exception as e:  # pragma: no cover - Windows-only path
            log.debug("Could not set AppUserModelID: %s", e)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    # Applies to every window the process opens — dialogs, toasts, message
    # boxes — so each one doesn't need its own setWindowIcon call.
    app.setWindowIcon(app_icon())

    # Set application font
    font = QFont("Segoe UI", 9)
    app.setFont(font)

    # Admin gate: required on Windows to install MT5, manage terminals, kill
    # terminal processes on shutdown. If not elevated, ask Windows to UAC-
    # elevate us automatically (relaunches self with verb=runas). User can
    # accept (we exit silently, elevated child takes over) or refuse (we
    # show a popup and exit).
    from ..utils.admin import is_admin, try_elevate
    admin = is_admin()
    log.info("run_gui: admin check result=%s", admin)
    if not admin:
        log.warning("run_gui: not admin — attempting UAC self-elevation")
        ok, reason = try_elevate()
        if ok:
            log.info("run_gui: elevated child spawned, exiting non-elevated process")
            sys.exit(0)
        log.error("run_gui: elevation failed/refused (reason=%s) — closing", reason)
        if reason == "cancelled":
            QMessageBox.critical(
                None,
                "Administrator required",
                "Administrator rights are required to run MetaCT.\n\n"
                "You clicked No on the UAC prompt. The app will now close.\n\n"
                "Restart the app and click Yes when Windows asks for "
                "administrator permission.",
            )
        else:
            QMessageBox.critical(
                None,
                "Could not elevate",
                f"Failed to request administrator rights ({reason}).\n\n"
                "Right-click the app shortcut (or gui.bat) and choose "
                "\"Run as administrator\", then start it again.",
            )
        log.info("run_gui: admin gate dismissed — sys.exit(1)")
        sys.exit(1)

    log.info("run_gui: constructing MainWindow")
    window = MainWindow()
    window.showMaximized()
    log.info("run_gui: entering Qt event loop")

    sys.exit(app.exec())


if __name__ == "__main__":
    run_gui()
