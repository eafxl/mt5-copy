"""Settings tab — global copier settings (master terminal + engine options).

Per-slave account rules (accounts, risk mode, symbol mappings, terminal
template) live in the separate Rules tab (gui/rules_tab.py) so global settings
and per-account config no longer share one crowded page.
"""
from pathlib import Path

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config_manager import ConfigManager


class SettingsTab(QWidget):
    """Global settings: master terminal path/poll and engine options."""

    config_changed = pyqtSignal()

    def __init__(self, config_manager: ConfigManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._data = config_manager.load()
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        """Setup compact settings UI."""
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # === Master Configuration ===
        master_group = QGroupBox("Master Terminal")
        master_layout = QGridLayout(master_group)
        master_layout.setSpacing(6)

        # Path row
        master_layout.addWidget(QLabel("Path:"), 0, 0)
        path_layout = QHBoxLayout()
        self.master_path_edit = QLineEdit()
        self.master_path_edit.setPlaceholderText("C:\\Program Files\\MetaTrader 5\\terminal64.exe")
        self.master_path_edit.setToolTip(
            "Full path to the master MT5 terminal64.exe the copier watches for trades."
        )
        self.master_path_edit.textChanged.connect(self._validate_master_path)
        path_layout.addWidget(self.master_path_edit)
        browse_btn = QPushButton("Browse")
        browse_btn.setFixedWidth(60)
        browse_btn.clicked.connect(self._browse_master)
        path_layout.addWidget(browse_btn)
        master_layout.addLayout(path_layout, 0, 1)

        # Status + Poll on same row
        self.master_status = QLabel()
        self.master_status.setStyleSheet("font-size: 11px;")
        master_layout.addWidget(self.master_status, 1, 1)

        master_layout.addWidget(QLabel("Poll:"), 2, 0)
        poll_layout = QHBoxLayout()
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(10, 5000)
        self.poll_spin.setSingleStep(100)
        self.poll_spin.setSuffix(" ms")
        self.poll_spin.setFixedWidth(100)
        self.poll_spin.setToolTip(
            "How often to poll the master for position changes (fallback when EA push is unavailable)."
        )
        poll_layout.addWidget(self.poll_spin)
        poll_layout.addStretch()
        master_layout.addLayout(poll_layout, 2, 1)

        layout.addWidget(master_group)

        # === General Settings ===
        settings_group = QGroupBox("Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.setSpacing(6)

        # Row 1: Sync + Maximize Master
        self.sync_check = QCheckBox("Sync on start")
        self.sync_check.setToolTip(
            "On start, copy the master's already-open positions to slaves (not just new trades)."
        )
        settings_layout.addWidget(self.sync_check, 0, 0)

        self.maximize_master_check = QCheckBox("Maximize Master on start")
        self.maximize_master_check.setToolTip(
            "Bring the master terminal window to front / maximized when the service starts."
        )
        settings_layout.addWidget(self.maximize_master_check, 0, 1)

        # Row 2: Slippage
        slip_layout = QHBoxLayout()
        slip_layout.addWidget(QLabel("Slippage:"))
        self.slippage_spin = QSpinBox()
        self.slippage_spin.setRange(0, 100)
        self.slippage_spin.setSuffix(" pts")
        self.slippage_spin.setFixedWidth(80)
        self.slippage_spin.setToolTip(
            "Max price slippage allowed when sending slave orders, in points."
        )
        slip_layout.addWidget(self.slippage_spin)
        slip_layout.addStretch()
        settings_layout.addLayout(slip_layout, 1, 0)

        # Row 2: Retry + Delay
        retry_layout = QHBoxLayout()
        retry_layout.addWidget(QLabel("Retries:"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(1, 10)
        self.retry_spin.setFixedWidth(50)
        self.retry_spin.setToolTip(
            "Number of times to retry a failed slave order before giving up."
        )
        retry_layout.addWidget(self.retry_spin)
        retry_layout.addStretch()
        settings_layout.addLayout(retry_layout, 1, 1)

        delay_layout = QHBoxLayout()
        delay_layout.addWidget(QLabel("Retry Delay:"))
        self.retry_delay_spin = QSpinBox()
        self.retry_delay_spin.setRange(100, 10000)
        self.retry_delay_spin.setSingleStep(100)
        self.retry_delay_spin.setSuffix(" ms")
        self.retry_delay_spin.setFixedWidth(90)
        self.retry_delay_spin.setToolTip(
            "Wait between retry attempts for a failed slave order."
        )
        delay_layout.addWidget(self.retry_delay_spin)
        delay_layout.addStretch()
        settings_layout.addLayout(delay_layout, 2, 0)

        # Row 3: Deviation
        dev_layout = QHBoxLayout()
        dev_layout.addWidget(QLabel("Max entry deviation:"))
        self.deviation_spin = QDoubleSpinBox()
        self.deviation_spin.setRange(0, 1.0)
        self.deviation_spin.setDecimals(2)
        self.deviation_spin.setSingleStep(0.05)
        self.deviation_spin.setFixedWidth(70)
        self.deviation_spin.setToolTip(
            "Skip a copy if the master moved from entry by more than this fraction "
            "of the SL distance (0 = never skip)."
        )
        dev_layout.addWidget(self.deviation_spin)
        dev_layout.addWidget(QLabel("(ratio of SL)"))
        dev_layout.addStretch()
        settings_layout.addLayout(dev_layout, 2, 1)

        # Row 4: Toast position + Test button
        toast_layout = QHBoxLayout()
        toast_layout.addWidget(QLabel("Toasts:"))
        self.toast_position_combo = QComboBox()
        self.toast_position_combo.addItems(
            ["Off", "Bottom-Left", "Bottom-Right", "Top-Left", "Top-Right"]
        )
        self.toast_position_combo.setFixedWidth(120)
        self.toast_position_combo.setToolTip(
            "Where trade notification toasts appear on screen (Off to disable)."
        )
        toast_layout.addWidget(self.toast_position_combo)
        self.toast_test_btn = QPushButton("Test")
        self.toast_test_btn.setFixedWidth(50)
        self.toast_test_btn.clicked.connect(self._test_toasts)
        toast_layout.addWidget(self.toast_test_btn)
        toast_layout.addStretch()
        settings_layout.addLayout(toast_layout, 3, 0, 1, 2)

        # Row 5 used to hold ONE max-risk cap for every slave. It now lives on
        # each slave (Rules tab → Risk), because the cap only means anything
        # next to the account it guards: the same 1% that is right for a 100k
        # account blocks every trade a $100 account could ever place, since its
        # broker's minimum lot already risks more than that.

        # Row 6: Debug / idle mode
        self.debug_skip_check = QCheckBox("Debug mode (simulate — skip execution)")
        self.debug_skip_check.setToolTip(
            "Trades are computed and logged as usual but NOT sent to the broker. "
            "Use to test copy behavior with zero risk. Applies immediately — no Save needed."
        )
        # Apply IMMEDIATELY on toggle (safety switch — must not wait for Save,
        # and must not depend on the config-reload path which never touches it).
        self.debug_skip_check.toggled.connect(self._apply_debug_skip)
        settings_layout.addWidget(self.debug_skip_check, 5, 0, 1, 2)

        # Row 7: how long traces / logs / dumps are kept. These roll by day and
        # are never otherwise removed, so this is the only limit on how much
        # disk the install eventually takes.
        keep_layout = QHBoxLayout()
        keep_layout.addWidget(QLabel("Keep logs & traces:"))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 365)
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setSpecialValueText("forever")
        self.retention_spin.setFixedWidth(90)
        self.retention_spin.setToolTip(
            "Delete traces, logs and debug dumps older than this. Swept at "
            "startup and once a day while running. Only files this app wrote "
            "are ever removed — anything else in those folders is left alone.\n"
            "0 = keep forever."
        )
        keep_layout.addWidget(self.retention_spin)
        keep_layout.addWidget(QLabel("(0 = forever)"))
        keep_layout.addStretch()
        settings_layout.addLayout(keep_layout, 6, 0, 1, 2)

        layout.addWidget(settings_group)

        layout.addWidget(self._build_price_calc_group())
        layout.addWidget(self._build_news_group())
        layout.addStretch()

        scroll.setWidget(content)
        main_layout.addWidget(scroll)

        # Save button
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.save_btn = QPushButton("Save Configuration")
        self.save_btn.setMinimumWidth(140)
        self.save_btn.clicked.connect(self._save_config)
        bottom.addWidget(self.save_btn)
        main_layout.addLayout(bottom)

    # ==================== Real price calculator ====================

    def _build_price_calc_group(self) -> QGroupBox:
        """TradingView ↔ master ticker mappings for the Trading tab's strip."""
        group = QGroupBox("Real Price Calculator")
        outer = QVBoxLayout(group)
        outer.setSpacing(6)

        top = QHBoxLayout()
        self.price_calc_check = QCheckBox("Enabled")
        self.price_calc_check.setToolTip(
            "Show the TradingView ↔ master price converter on the Trading tab.\n"
            "Prices come from TradingView's public scanner; delayed (unpaid) "
            "tickers are handled by comparing against the master's price at the "
            "same past moment."
        )
        self.price_calc_check.toggled.connect(self._on_price_calc_toggled)
        top.addWidget(self.price_calc_check)

        top.addSpacing(12)
        top.addWidget(QLabel("Refresh:"))
        self.price_calc_refresh_spin = QDoubleSpinBox()
        self.price_calc_refresh_spin.setRange(1.0, 300.0)
        self.price_calc_refresh_spin.setDecimals(0)
        self.price_calc_refresh_spin.setSingleStep(1.0)
        self.price_calc_refresh_spin.setSuffix(" s")
        self.price_calc_refresh_spin.setFixedWidth(80)
        self.price_calc_refresh_spin.setToolTip(
            "How often to poll TradingView. The offset barely moves intraday, so "
            "there is nothing to gain from hammering the endpoint."
        )
        top.addWidget(self.price_calc_refresh_spin)
        top.addStretch()
        outer.addLayout(top)

        self.price_calc_table = QTableWidget(0, 2)
        self.price_calc_table.setHorizontalHeaderLabels(
            ["TradingView Ticker", "Master Symbol"]
        )
        self.price_calc_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.price_calc_table.verticalHeader().setVisible(False)
        self.price_calc_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.price_calc_table.setMaximumHeight(130)
        self.price_calc_table.setToolTip(
            "TradingView tickers are EXCHANGE:SYMBOL, exactly as the site spells "
            "them — e.g. COMEX:GC1!, OANDA:XAUUSD, FX:EURUSD, NASDAQ:AAPL."
        )
        outer.addWidget(self.price_calc_table)

        btns = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(60)
        add_btn.clicked.connect(self._add_price_calc_row)
        btns.addWidget(add_btn)

        del_btn = QPushButton("Remove")
        del_btn.setFixedWidth(70)
        del_btn.clicked.connect(self._remove_price_calc_row)
        btns.addWidget(del_btn)

        self.price_calc_test_btn = QPushButton("Test")
        self.price_calc_test_btn.setFixedWidth(60)
        self.price_calc_test_btn.setToolTip(
            "Resolve every ticker against TradingView right now (no MT5 needed)."
        )
        self.price_calc_test_btn.clicked.connect(self._test_price_calc)
        btns.addWidget(self.price_calc_test_btn)

        self.price_calc_status = QLabel("")
        self.price_calc_status.setStyleSheet("color: #666; font-size: 11px;")
        self.price_calc_status.setWordWrap(True)
        btns.addWidget(self.price_calc_status, 1)
        outer.addLayout(btns)

        return group

    def _build_news_group(self) -> QGroupBox:
        """ForexFactory calendar — per-slave block windows live in the Rules tab."""
        group = QGroupBox("News Blocking (ForexFactory)")
        outer = QVBoxLayout(group)
        outer.setSpacing(6)

        top = QHBoxLayout()
        self.news_check = QCheckBox("Enabled")
        self.news_check.setToolTip(
            "Skip NEW copies around scheduled economic events.\n"
            "Windows are set per slave in the Rules tab — with none set, "
            "nothing is ever blocked.\n"
            "Closes and SL/TP updates are never blocked."
        )
        self.news_check.toggled.connect(self._on_news_toggled)
        top.addWidget(self.news_check)

        top.addSpacing(12)
        top.addWidget(QLabel("Refresh:"))
        self.news_refresh_spin = QDoubleSpinBox()
        self.news_refresh_spin.setRange(1.0, 240.0)
        self.news_refresh_spin.setDecimals(0)
        self.news_refresh_spin.setSingleStep(1.0)
        self.news_refresh_spin.setSuffix(" min")
        self.news_refresh_spin.setFixedWidth(90)
        self.news_refresh_spin.setToolTip(
            "How often to re-download the weekly calendar. It changes rarely, "
            "and the last good copy is cached on disk."
        )
        top.addWidget(self.news_refresh_spin)

        self.news_test_btn = QPushButton("Test")
        self.news_test_btn.setFixedWidth(60)
        self.news_test_btn.setToolTip("Download the calendar right now (no MT5 needed).")
        self.news_test_btn.clicked.connect(self._test_news)
        top.addWidget(self.news_test_btn)

        self.news_status = QLabel("")
        self.news_status.setStyleSheet("color: #666; font-size: 11px;")
        self.news_status.setWordWrap(True)
        top.addWidget(self.news_status, 1)
        outer.addLayout(top)

        return group

    def _on_news_toggled(self, checked: bool):
        self.news_refresh_spin.setEnabled(checked)
        self.news_test_btn.setEnabled(checked)

    def _test_news(self):
        """Report what the calendar holds, downloading only if that is allowed.

        Goes through NewsCalendar rather than calling the fetcher directly. The
        direct call was a hole straight through every rate-limit rule in the
        app: pressing Test a few times while setting news up burned the
        allowance, and the service — whose own cooldown was working — then met
        a 429 it had done nothing to earn.
        """
        from ..news_calendar import NewsCalendar, events_today

        self.news_status.setText("Checking…")
        self.news_test_btn.setEnabled(False)
        try:
            events, error, note = NewsCalendar().probe()
        finally:
            self.news_test_btn.setEnabled(True)

        if error:
            self.news_status.setText(f"✖ {error}")
            self.news_status.setStyleSheet("color: #E53935; font-size: 11px;")
            return

        import time as _time
        today = events_today(events, _time.time())
        high = sum(1 for e in today if e.impact == "High")
        summary = (
            f"✓ {len(events)} events this week · "
            f"{len(today)} today ({high} high impact)"
        )
        self.news_status.setText(f"{summary} · {note}" if note else summary)
        self.news_status.setStyleSheet("color: #2E7D32; font-size: 11px;")

    def _on_price_calc_toggled(self, checked: bool):
        self.price_calc_table.setEnabled(checked)
        self.price_calc_refresh_spin.setEnabled(checked)

    def _add_price_calc_row(self, tv: str = "", master: str = ""):
        row = self.price_calc_table.rowCount()
        self.price_calc_table.insertRow(row)
        self.price_calc_table.setItem(row, 0, QTableWidgetItem(tv))
        self.price_calc_table.setItem(row, 1, QTableWidgetItem(master))

    def _remove_price_calc_row(self):
        row = self.price_calc_table.currentRow()
        if row >= 0:
            self.price_calc_table.removeRow(row)

    def _price_calc_pairs(self) -> list[dict]:
        """Non-empty rows of the mapping table."""
        pairs = []
        for row in range(self.price_calc_table.rowCount()):
            tv_item = self.price_calc_table.item(row, 0)
            master_item = self.price_calc_table.item(row, 1)
            tv = tv_item.text().strip().upper() if tv_item else ""
            master = master_item.text().strip() if master_item else ""
            if tv and master:
                pairs.append({"tv": tv, "master": master})
        return pairs

    def _test_price_calc(self):
        """One scanner call for the configured tickers; report what resolved.

        Deliberately does NOT touch MT5 — this answers "does TradingView know
        these tickers, and how delayed are they", which is the half that fails.
        """
        from ..price_calc import fetch_tv_quotes, resolve_ticker

        pairs = self._price_calc_pairs()
        if not pairs:
            self.price_calc_status.setText("Add at least one ticker mapping first.")
            self.price_calc_status.setStyleSheet("color: #E65100; font-size: 11px;")
            return

        self.price_calc_status.setText("Querying TradingView…")
        self.price_calc_status.setStyleSheet("color: #666; font-size: 11px;")
        self.price_calc_test_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            # The scanner only answers on EXCHANGE:TICKER, so expand anything
            # bare ("NQ1!" -> "CME_MINI:NQ1!") and write the qualified form back
            # into the table — Test doubles as the way to fix a ticker.
            resolve_errors = []
            for row in range(self.price_calc_table.rowCount()):
                item = self.price_calc_table.item(row, 0)
                text = item.text().strip() if item else ""
                if not text or ":" in text:
                    continue
                qualified, _desc, err = resolve_ticker(text)
                if err:
                    resolve_errors.append(err)
                elif qualified != text:
                    item.setText(qualified)
                QApplication.processEvents()

            pairs = self._price_calc_pairs()
            quotes, error = fetch_tv_quotes([p["tv"] for p in pairs])
        finally:
            self.price_calc_test_btn.setEnabled(True)

        bits = []
        for pair in pairs:
            q = quotes.get(pair["tv"])
            if q is None:
                bits.append(f"✗ {pair['tv']}")
                continue
            delay = "live" if q.delay_s <= 0 else f"delayed {q.delay_s / 60:.0f}m"
            bits.append(f"✓ {pair['tv']} {q.price:g} ({delay})")

        warnings = "   ".join(f"⚠ {w}" for w in resolve_errors + ([error] if error else []))
        self.price_calc_status.setText("   ".join(bits) + ("   " + warnings if warnings else ""))
        ok = bool(quotes) and not warnings
        self.price_calc_status.setStyleSheet(
            "color: %s; font-size: 11px;" % ("green" if ok else "#E65100")
        )

    def _browse_master(self):
        """Browse for master terminal."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Master MT5", "C:\\Program Files",
            "Executable (terminal64.exe terminal.exe);;All Files (*)",
        )
        if path:
            self.master_path_edit.setText(str(Path(path)))

    def _validate_master_path(self):
        """Validate master path and update UI."""
        path = self.master_path_edit.text()
        valid, msg = self.config_manager.validate_master_path(path)

        if valid:
            self.master_status.setText(f"✓ {msg}")
            self.master_status.setStyleSheet("color: green; font-size: 11px;")
            self.master_path_edit.setStyleSheet("")
        else:
            self.master_status.setText(f"✗ {msg}")
            self.master_status.setStyleSheet("color: red; font-size: 11px;")
            self.master_path_edit.setStyleSheet("background-color: #ffe0e0;")

        if "master" not in self._data:
            self._data["master"] = {}
        self._data["master"]["terminal_path"] = path

    def _load_config(self):
        """Load config into UI."""
        master = self._data.get("master", {})
        self.master_path_edit.setText(master.get("terminal_path", ""))
        self.poll_spin.setValue(master.get("poll_interval_ms", 200))

        settings = self._data.get("settings", {})
        self.sync_check.setChecked(settings.get("sync_existing_on_start", True))
        self.maximize_master_check.setChecked(settings.get("maximize_master_on_start", True))
        self.slippage_spin.setValue(settings.get("max_slippage_points", 20))
        self.retry_spin.setValue(settings.get("retry_attempts", 5))
        self.retry_delay_spin.setValue(settings.get("retry_delay_ms", 2000))
        self.deviation_spin.setValue(settings.get("max_entry_deviation_ratio", 0.2))
        self.debug_skip_check.setChecked(settings.get("debug_skip_execution", False))
        # Sync the live engine to the displayed checkbox so they can never
        # silently disagree (checkbox says "debug" while engine trades live).
        self._apply_debug_skip(self.debug_skip_check.isChecked())
        self.toast_position_combo.setCurrentText(
            settings.get("toast_position", "Bottom-Right")
        )

        price_calc = self._data.get("price_calc", {}) or {}
        self.price_calc_check.setChecked(bool(price_calc.get("enabled", False)))
        self.price_calc_refresh_spin.setValue(float(price_calc.get("refresh_sec", 5.0)))
        self.price_calc_table.setRowCount(0)
        for pair in price_calc.get("pairs", []) or []:
            self._add_price_calc_row(pair.get("tv", ""), pair.get("master", ""))
        self._on_price_calc_toggled(self.price_calc_check.isChecked())

        # Disabled retention and "0 days" mean the same thing to the user —
        # nothing is deleted — so the checkbox collapses into the spinbox's
        # special value rather than being a second control that can disagree
        # with it.
        retention = self._data.get("retention", {}) or {}
        self.retention_spin.setValue(
            int(retention.get("days", 7)) if retention.get("enabled", True) else 0
        )

        news = self._data.get("news", {}) or {}
        self.news_check.setChecked(bool(news.get("enabled", False)))
        self.news_refresh_spin.setValue(float(news.get("refresh_sec", 900.0)) / 60.0)
        self._on_news_toggled(self.news_check.isChecked())

        # Snapshot the saved state so we can detect unsaved edits on tab switch.
        self._clean_snapshot = self._settings_snapshot()

    def _settings_snapshot(self) -> dict:
        """Values of the settings widgets, compared to detect a dirty state."""
        return {
            "master_path": self.master_path_edit.text(),
            "poll": self.poll_spin.value(),
            "sync": self.sync_check.isChecked(),
            "maximize": self.maximize_master_check.isChecked(),
            "slippage": self.slippage_spin.value(),
            "retries": self.retry_spin.value(),
            "retry_delay": self.retry_delay_spin.value(),
            "deviation": self.deviation_spin.value(),
            "toast": self.toast_position_combo.currentText(),
            "debug": self.debug_skip_check.isChecked(),
            "price_calc_on": self.price_calc_check.isChecked(),
            "price_calc_refresh": self.price_calc_refresh_spin.value(),
            "price_calc_pairs": self._price_calc_pairs(),
            "news_on": self.news_check.isChecked(),
            "news_refresh": self.news_refresh_spin.value(),
            "retention_days": self.retention_spin.value(),
        }

    def has_unsaved_changes(self) -> bool:
        """True if a settings widget differs from the last saved/loaded snapshot."""
        return self._settings_snapshot() != getattr(self, "_clean_snapshot", None)

    def maybe_prompt_save(self) -> bool:
        """Prompt to save unsaved settings before leaving the tab.

        Returns True if it's OK to switch away (saved, or user discarded),
        False to stay on the Settings tab (Cancel, or a failed save).
        """
        if not self.has_unsaved_changes():
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            "You have unsaved settings changes. Save them?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Save:
            return self._save_config()  # False if validation fails -> stay
        if reply == QMessageBox.StandardButton.Discard:
            # Revert widgets to the saved state on disk.
            self._data = self.config_manager.load()
            self._load_config()
            return True
        return False  # Cancel

    def _apply_debug_skip(self, checked: bool):
        """Apply debug/simulate mode to the live engine IMMEDIATELY.

        Debug is a runtime safety switch: it is set directly on the LiveSettings
        singleton (shared with the service thread) rather than via the bulk
        config/dict paths, so a config-file reload can never silently turn it
        off and let real orders through. Applies without pressing Save.
        """
        try:
            from ..live_settings import get_live_settings
            get_live_settings().debug_skip_execution = bool(checked)
        except Exception:
            pass  # Service may not be running yet

    def _test_toasts(self):
        """Fire 3 sample toasts to preview current position setting."""
        from .toast import ToastType

        main_win = self.window()
        tm = getattr(main_win, "toast_manager", None)
        if not tm:
            return

        # Apply current combo selection immediately
        tm.set_position(self.toast_position_combo.currentText())

        tm.show_toast("BUY  EURUSD", "0.10 lots  \u00b7  Slave1", ToastType.OPEN, 4000, "\u25b2")
        QTimer.singleShot(300, lambda: tm.show_toast(
            "CLOSED SELL  GBPUSD", "0.50 lots  \u00b7  Slave2",
            ToastType.CLOSE, 4000, "\u2713", pnl=42.50,
        ))
        QTimer.singleShot(600, lambda: tm.show_toast(
            "ERROR  \u00b7  Slave3", "Connection timeout",
            ToastType.ERROR, 4000, "\u26a0",
        ))

    def _save_config(self) -> bool:
        """Save global settings, warn if a restart is needed.

        Merges only the master + settings sections into the on-disk config so
        per-slave rules (owned by the Rules tab) are never clobbered. Returns
        True on a successful save, False if validation blocked it.
        """
        path = self.master_path_edit.text()
        valid, msg = self.config_manager.validate_master_path(path)
        if not valid:
            QMessageBox.warning(self, "Error", f"Master: {msg}")
            return False

        # Reload the on-disk config and update ONLY our sections, so slaves /
        # slave_terminal edited on the Rules tab survive this save.
        on_disk = self.config_manager.load()
        old_master_path = on_disk.get("master", {}).get("terminal_path", "")

        on_disk["master"] = {
            "terminal_path": path,
            "poll_interval_ms": self.poll_spin.value(),
        }
        on_disk["settings"] = {
            "sync_existing_on_start": self.sync_check.isChecked(),
            "maximize_master_on_start": self.maximize_master_check.isChecked(),
            "max_slippage_points": self.slippage_spin.value(),
            "retry_attempts": self.retry_spin.value(),
            "retry_delay_ms": self.retry_delay_spin.value(),
            "log_level": "INFO",
            "max_entry_deviation_ratio": self.deviation_spin.value(),
            # ``max_risk_pct_per_trade`` is deliberately absent: it is a slave
            # field now, and writing the old global key back would resurrect it
            # on the next load. ConfigManager.load has already moved any legacy
            # value onto the slaves.
            "debug_skip_execution": self.debug_skip_check.isChecked(),
            "toast_position": self.toast_position_combo.currentText(),
        }
        # Price calculator. Carry each pair's cached offset across the save so
        # editing an unrelated row doesn't throw away a measured offset.
        cached = {
            p.get("tv"): p
            for p in (on_disk.get("price_calc", {}) or {}).get("pairs", []) or []
        }
        pairs = []
        for pair in self._price_calc_pairs():
            old = cached.get(pair["tv"], {})
            if old.get("master") == pair["master"] and old.get("last_diff_at"):
                pair["last_diff"] = old.get("last_diff", 0.0)
                pair["last_diff_at"] = old.get("last_diff_at", 0.0)
            pairs.append(pair)
        on_disk["price_calc"] = {
            "enabled": self.price_calc_check.isChecked(),
            "refresh_sec": self.price_calc_refresh_spin.value(),
            "pairs": pairs,
        }

        on_disk["retention"] = {
            "enabled": self.retention_spin.value() > 0,
            # Keep the last non-zero value out of the file: "forever" is stored
            # as days=0 so a hand-edited config reads the same way the UI does.
            "days": self.retention_spin.value(),
        }

        on_disk["news"] = {
            "enabled": self.news_check.isChecked(),
            # Stored in seconds (config.NewsConfig), edited in minutes.
            "refresh_sec": self.news_refresh_spin.value() * 60.0,
        }

        on_disk.setdefault("symbol_mapping", {})

        try:
            self.config_manager.save(on_disk)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save failed: {e}")
            return False
        self._data = on_disk

        # Immediately update LiveSettings (don't wait for ConfigWatcher 2s poll)
        try:
            from ..live_settings import get_live_settings
            get_live_settings().update_from_dict(self._data, issuer="user")
            # debug_skip_execution is excluded from update_from_dict (runtime
            # safety switch) — apply it directly so Save also takes effect.
            self._apply_debug_skip(self.debug_skip_check.isChecked())
        except Exception:
            pass  # Service may not be running yet

        if old_master_path and old_master_path != path:
            QMessageBox.information(
                self, "Restart Required",
                "Configuration saved.\n\n"
                "The master terminal path changed — restart the service for it "
                "to take effect.\n\nLive-updatable settings (slippage, deviation, "
                "etc.) take effect immediately.",
            )
        else:
            QMessageBox.information(self, "Saved", "Configuration saved.\nLive settings updated immediately.")

        # Refresh the clean snapshot so we no longer report unsaved changes.
        self._clean_snapshot = self._settings_snapshot()
        self.config_changed.emit()
        return True