"""Rules tab — per-slave account configuration (copy rules).

Split out of the Settings tab so global settings and per-account rules no
longer share one crowded page. This tab owns everything about *which* accounts
copy and *how* (risk mode, multiplier, symbol mappings, terminal), plus the
slave terminal template. Edits here persist immediately (add/edit/delete each
write config), so there is no unsaved-changes prompt.
"""
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..news_calendar import (
    IMPACTS as NEWS_IMPACTS,
    normalize_currencies as _normalize_news_currencies,
)

#: Offered in the currency filter. The feed's own set, plus "All" for the
#: global entries (bank holidays, Jackson Hole) — listed so it is visible that
#: they exist, though they match every filter regardless.
NEWS_CURRENCIES = (
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "All",
)
from ..utils.risk_ladder import default_tiers_as_dicts, normalize_tiers

from ..paths import icons_dir as _icons_dir

_ICONS = _icons_dir()

from ..template_manager import slaves_dir as _slaves_dir_fn, validate_template
from .config_manager import ConfigManager
from .seed_template_dialog import SeedTemplateDialog
from .symbol_mapping_dialog import SymbolMappingDialog, load_cached_symbols


def _fmt_num(value: float) -> str:
    """Format a risk number for the table without hiding small values.

    Two decimals so 0.04 stays visible, trailing zeros trimmed so 1.0 -> "1".
    """
    return f"{float(value):.2f}".rstrip("0").rstrip(".") or "0"


def _make_slider(spin: QDoubleSpinBox) -> QSlider:
    """Build a horizontal slider two-way bound to ``spin``.

    QSlider is integer-only, so the spinbox's range and step are scaled by
    10**decimals — a 0.01-precision field becomes a 1-unit slider step, so the
    slider can reach every value the spinbox accepts. Both directions block the
    peer's signals while writing, otherwise the round-trip would fight typing
    in the spinbox.

    The spinbox stays the precise input; the slider is for coarse dragging.
    """
    scale = 10 ** spin.decimals()

    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(round(spin.minimum() * scale), round(spin.maximum() * scale))
    slider.setSingleStep(max(1, round(spin.singleStep() * scale)))
    slider.setPageStep(max(1, round(spin.singleStep() * scale * 10)))
    slider.setValue(round(spin.value() * scale))
    slider.setMinimumWidth(120)

    def _spin_changed(value: float):
        slider.blockSignals(True)
        slider.setValue(round(value * scale))
        slider.blockSignals(False)

    def _slider_changed(value: int):
        spin.blockSignals(True)
        spin.setValue(value / scale)
        spin.blockSignals(False)

    spin.valueChanged.connect(_spin_changed)
    slider.valueChanged.connect(_slider_changed)
    return slider


def _make_cap(spin: QDoubleSpinBox, slider: QSlider, value: float) -> QDoubleSpinBox:
    """Max-risk box that sets the upper end of ``slider`` (and of ``spin``).

    Narrowing the range is the point: across the full 0.01–100 % span a slider
    moves in ~0.5 % steps per pixel, which is useless for picking 0.25 %. Capped
    at 1 % the same drag resolves to ~0.005 %.

    The cap is a real limit, not a visual one — ``spin``'s maximum follows it, so
    it bounds typed entry too. Qt clamps a now-out-of-range value DOWN to the new
    cap; toward less risk is the only direction safe to do silently.
    """
    hard_max = spin.maximum()
    scale = 10 ** spin.decimals()

    cap = QDoubleSpinBox()
    cap.setDecimals(spin.decimals())
    cap.setRange(spin.minimum(), hard_max)
    cap.setSingleStep(spin.singleStep())
    cap.setPrefix("≤ ")
    cap.setSuffix(" %")
    cap.setValue(value)
    cap.setFixedWidth(90)
    cap.setToolTip(
        "Max risk — upper limit of the slider and of the value you can type.\n"
        "Lower it for finer control; raise it before setting a bigger risk."
    )

    def _apply(cap_value: float):
        spin.setMaximum(cap_value)  # clamps the current value down if it exceeds
        slider.blockSignals(True)
        slider.setRange(round(spin.minimum() * scale), round(cap_value * scale))
        slider.setValue(round(spin.value() * scale))
        slider.blockSignals(False)

    cap.valueChanged.connect(_apply)
    _apply(cap.value())
    return cap


class _SideTabBar(QTabBar):
    """A tab strip down the side, with the labels still readable.

    Qt's own West/East tab positions rotate the text ninety degrees, which
    turns five short words into five vertical columns of letters. Everything
    here exists to keep the strip vertical while the words stay horizontal:
    the size hint is transposed (Qt measured it for a rotated label) and the
    text is painted separately from the tab shape, so the shape still comes
    from the active style and inherits the app's stylesheet.
    """

    #: Wide enough for the longest label plus the style's own padding, so the
    #: strip does not change width as the selection moves.
    _MIN_W = 104

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        size.transpose()
        size.setWidth(max(size.width(), self._MIN_W))
        return size

    def paintEvent(self, _event):
        painter = QStylePainter(self)
        option = QStyleOptionTab()
        for index in range(self.count()):
            self.initStyleOption(option, index)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)
            painter.drawText(
                self.tabRect(index),
                Qt.AlignmentFlag.AlignCenter,
                self.tabText(index),
            )


class SlaveDialog(QDialog):
    """Compact dialog for adding/editing a slave."""

    def __init__(
        self,
        config_manager: ConfigManager,
        slave_data: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.config_manager = config_manager
        self.editing = slave_data is not None
        self.result_data: dict[str, Any] | None = None
        self._symbol_mapping: dict[str, str] = dict(
            slave_data.get("symbol_mapping") or {} if slave_data else {}
        )

        self.setWindowTitle("Edit Slave" if self.editing else "Add Slave")
        # Wider than the old form: each risk row carries a slider, and the tab
        # strip down the side takes its own ~104px before the fields start.
        self.setMinimumWidth(680)
        self._setup_ui(slave_data)

    #: Tab order. Terminal first because a slave with no terminal cannot do
    #: anything else; symbols last because they are the fiddliest and the least
    #: often changed.
    _PAGES = ("Terminal", "Credentials", "Risk", "News", "Symbols")

    def _setup_ui(self, slave_data: dict[str, Any] | None):
        """Build the dialog as one tab per concern.

        It was a single grid of fourteen rows — every field for every subject
        at once, with the risk ladder and the news windows fighting for the
        same column. Split by what the operator came to change.
        """
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.tabs = QTabWidget()
        # A side strip rather than a top row: five sections is enough that a
        # vertical list reads as a menu of places to go, and the dialog is
        # taller than it is wide.
        self.tabs.setTabBar(_SideTabBar())
        self.tabs.setTabPosition(QTabWidget.TabPosition.West)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
        self._grids: dict[str, QGridLayout] = {}
        for title in self._PAGES:
            page = QWidget()
            grid = QGridLayout(page)
            grid.setSpacing(6)
            grid.setColumnStretch(1, 1)
            grid.setAlignment(Qt.AlignmentFlag.AlignTop)
            self._grids[title] = grid
            self.tabs.addTab(page, title)
        layout.addWidget(self.tabs)

        # Each section below binds `form`/`row` to its own page, so the field
        # code stays exactly as it reads: a grid and a row counter.
        form, row = self._grids["Credentials"], 0

        # Name
        form.addWidget(QLabel("Name:"), row, 0)
        self.name_edit = QLineEdit()
        if slave_data:
            self.name_edit.setText(slave_data.get("name", ""))
        else:
            next_num = self.config_manager.get_next_slave_number()
            self.name_edit.setText(f"Slave{next_num}")
        form.addWidget(self.name_edit, row, 1)
        row += 1

        # Login + Server on same row
        form.addWidget(QLabel("Login:"), row, 0)
        login_server = QHBoxLayout()
        self.login_edit = QSpinBox()
        self.login_edit.setRange(1, 999999999)
        self.login_edit.setValue(slave_data.get("login", 12345678) if slave_data else 12345678)
        login_server.addWidget(self.login_edit)
        login_server.addWidget(QLabel("Server:"))
        self.server_edit = QLineEdit()
        self.server_edit.setText(slave_data.get("server", "") if slave_data else "")
        self.server_edit.setPlaceholderText("Broker-Server")
        login_server.addWidget(self.server_edit, 1)
        form.addLayout(login_server, row, 1)
        row += 1

        # Password
        form.addWidget(QLabel("Password:"), row, 0)
        pw_layout = QHBoxLayout()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setText(slave_data.get("password", "") if slave_data else "")
        self.password_edit.setPlaceholderText("MT5 account password")
        pw_layout.addWidget(self.password_edit)
        self._eye_btn = QPushButton()
        self._eye_btn.setIcon(QIcon(str(_ICONS / "eye.svg")))
        self._eye_btn.setFixedWidth(28)
        self._eye_btn.setToolTip("Show/hide password")
        self._eye_btn.setCheckable(True)
        self._eye_btn.toggled.connect(self._toggle_password_visibility)
        pw_layout.addWidget(self._eye_btn)
        form.addLayout(pw_layout, row, 1)
        row += 1

        credentials_row = row           # resumed for "Enabled", further down
        form, row = self._grids["Risk"], 0

        # Risk Mode selector
        form.addWidget(QLabel("Risk Mode:"), row, 0)
        risk_mode_layout = QHBoxLayout()
        self.risk_mode_combo = QComboBox()
        self.risk_mode_combo.addItems(
            ["Multiplier", "Fixed Volume", "Fixed USD", "% of Deposit", "Dynamic % (ladder)"]
        )
        self.risk_mode_combo.setFixedWidth(140)
        self.risk_mode_combo.currentIndexChanged.connect(self._on_risk_mode_changed)
        risk_mode_layout.addWidget(self.risk_mode_combo)
        risk_mode_layout.addStretch()
        form.addLayout(risk_mode_layout, row, 1)
        row += 1

        # Multiplier settings (shown when risk_mode = multiplier)
        form.addWidget(QLabel("Multiplier:"), row, 0)
        self.multiplier_row = row
        mult_lev = QHBoxLayout()
        self.multiplier_spin = QDoubleSpinBox()
        self.multiplier_spin.setRange(0.01, 100.0)
        self.multiplier_spin.setDecimals(2)
        self.multiplier_spin.setSingleStep(0.1)
        self.multiplier_spin.setValue(slave_data.get("multiplier", 1.0) if slave_data else 1.0)
        self.multiplier_spin.setFixedWidth(80)
        mult_lev.addWidget(self.multiplier_spin)
        self.multiplier_slider = _make_slider(self.multiplier_spin)
        mult_lev.addWidget(self.multiplier_slider, 1)
        self.leverage_check = QCheckBox("Adjust by leverage")
        self.leverage_check.setChecked(
            slave_data.get("adjust_by_leverage", True) if slave_data else True
        )
        mult_lev.addWidget(self.leverage_check)
        self.multiplier_widget = QWidget()
        self.multiplier_widget.setLayout(mult_lev)
        form.addWidget(self.multiplier_widget, row, 1)
        row += 1

        # Fixed Volume settings (shown when risk_mode = fixed_volume)
        form.addWidget(QLabel("Volume:"), row, 0)
        self.fixed_vol_row = row
        fixed_vol_layout = QHBoxLayout()
        self.fixed_volume_spin = QDoubleSpinBox()
        self.fixed_volume_spin.setRange(0.01, 100.0)
        self.fixed_volume_spin.setDecimals(2)
        self.fixed_volume_spin.setSingleStep(0.1)
        self.fixed_volume_spin.setValue(slave_data.get("fixed_volume", 0.01) if slave_data else 0.01)
        self.fixed_volume_spin.setFixedWidth(80)
        self.fixed_volume_spin.setSuffix(" lots")
        fixed_vol_layout.addWidget(self.fixed_volume_spin)
        self.fixed_volume_slider = _make_slider(self.fixed_volume_spin)
        fixed_vol_layout.addWidget(self.fixed_volume_slider, 1)
        self.fixed_vol_widget = QWidget()
        self.fixed_vol_widget.setLayout(fixed_vol_layout)
        form.addWidget(self.fixed_vol_widget, row, 1)
        row += 1

        # Fixed USD Risk settings (shown when risk_mode = fixed_usd)
        form.addWidget(QLabel("Risk:"), row, 0)
        self.fixed_usd_row = row
        risk_layout = QHBoxLayout()
        self.fixed_risk_spin = QDoubleSpinBox()
        self.fixed_risk_spin.setRange(1.0, 100000.0)
        self.fixed_risk_spin.setDecimals(0)
        self.fixed_risk_spin.setSingleStep(10.0)
        self.fixed_risk_spin.setPrefix("$ ")
        self.fixed_risk_spin.setValue(slave_data.get("fixed_risk_usd", 100.0) if slave_data else 100.0)
        self.fixed_risk_spin.setFixedWidth(100)
        risk_layout.addWidget(self.fixed_risk_spin)
        self.fixed_risk_slider = _make_slider(self.fixed_risk_spin)
        risk_layout.addWidget(self.fixed_risk_slider, 1)
        risk_hint = QLabel("per trade (requires SL)")
        risk_hint.setStyleSheet("color: #999; font-size: 10px;")
        risk_layout.addWidget(risk_hint)
        self.fixed_usd_widget = QWidget()
        self.fixed_usd_widget.setLayout(risk_layout)
        form.addWidget(self.fixed_usd_widget, row, 1)
        row += 1

        # % of Deposit settings (shown when risk_mode = pct_deposit)
        form.addWidget(QLabel("Risk:"), row, 0)
        self.pct_deposit_row = row
        pct_layout = QHBoxLayout()
        self.risk_pct_spin = QDoubleSpinBox()
        self.risk_pct_spin.setRange(0.01, 100.0)
        self.risk_pct_spin.setDecimals(2)
        self.risk_pct_spin.setSingleStep(0.01)
        self.risk_pct_spin.setSuffix(" %")
        self.risk_pct_spin.setValue(slave_data.get("risk_pct", 1.0) if slave_data else 1.0)
        self.risk_pct_spin.setFixedWidth(80)
        pct_layout.addWidget(self.risk_pct_spin)
        self.risk_pct_slider = _make_slider(self.risk_pct_spin)
        pct_layout.addWidget(self.risk_pct_slider, 1)
        # Cap defaults to 1%, but never below the value already saved for this
        # slave — otherwise opening the dialog on a 2% slave would silently
        # clamp it to 1% just by looking at it.
        saved_pct = slave_data.get("risk_pct", 1.0) if slave_data else 1.0
        default_cap = slave_data.get("risk_pct_max", 0.0) if slave_data else 0.0
        self.risk_pct_cap = _make_cap(
            self.risk_pct_spin,
            self.risk_pct_slider,
            default_cap or max(1.0, saved_pct),
        )
        pct_layout.addWidget(self.risk_pct_cap)
        pct_layout.addWidget(QLabel("of"))
        self.risk_pct_base_combo = QComboBox()
        self.risk_pct_base_combo.addItems(["Balance", "Equity"])
        self.risk_pct_base_combo.setFixedWidth(90)
        if slave_data and slave_data.get("risk_pct_base", "balance") == "equity":
            self.risk_pct_base_combo.setCurrentIndex(1)
        pct_layout.addWidget(self.risk_pct_base_combo)
        pct_hint = QLabel("per trade (requires SL)")
        pct_hint.setStyleSheet("color: #999; font-size: 10px;")
        pct_layout.addWidget(pct_hint)
        pct_layout.addStretch()
        self.pct_deposit_widget = QWidget()
        self.pct_deposit_widget.setLayout(pct_layout)
        form.addWidget(self.pct_deposit_widget, row, 1)
        row += 1

        # Dynamic % ladder (shown when risk_mode = dynamic_pct)
        form.addWidget(QLabel("Ladder:"), row, 0)
        dyn_layout = QVBoxLayout()
        dyn_layout.setContentsMargins(0, 0, 0, 0)
        dyn_layout.setSpacing(4)

        dyn_top = QHBoxLayout()
        dyn_top.addWidget(QLabel("Initial balance:"))
        self.initial_balance_spin = QDoubleSpinBox()
        self.initial_balance_spin.setRange(0.0, 1_000_000_000.0)
        self.initial_balance_spin.setDecimals(2)
        self.initial_balance_spin.setSingleStep(1000.0)
        self.initial_balance_spin.setGroupSeparatorShown(True)
        self.initial_balance_spin.setFixedWidth(140)
        self.initial_balance_spin.setValue(
            slave_data.get("initial_balance", 0.0) if slave_data else 0.0
        )
        dyn_top.addWidget(self.initial_balance_spin)
        dyn_top.addWidget(QLabel("measured on"))
        self.dynamic_base_combo = QComboBox()
        self.dynamic_base_combo.addItems(["Balance", "Equity"])
        self.dynamic_base_combo.setFixedWidth(90)
        if slave_data and slave_data.get("dynamic_base", "balance") == "equity":
            self.dynamic_base_combo.setCurrentIndex(1)
        dyn_top.addWidget(self.dynamic_base_combo)
        # Typed by the operator, never resolved from the terminal: the value is
        # needed to configure a slave that has not been connected yet, and the
        # account figures only exist once MT5 is up — so there is no moment at
        # which the app could fill this in for a new account. Current balance
        # would be the wrong answer anyway, drifting with every trade.
        dyn_hint = QLabel("the account's starting balance; raise after a prop scale-up")
        dyn_hint.setStyleSheet("color: #999; font-size: 10px;")
        dyn_top.addWidget(dyn_hint)
        dyn_top.addStretch()
        dyn_layout.addLayout(dyn_top)

        # Table left, controls right. Two number columns don't need the dialog's
        # full width, and a ladder is only ever a handful of rows — stretched
        # across the page it reads as a wide empty strip.
        self.tier_table = QTableWidget(0, 2)
        self.tier_table.setHorizontalHeaderLabels(["Up to %", "Risk %"])
        header = self.tier_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tier_table.setColumnWidth(0, 90)
        self.tier_table.setColumnWidth(1, 80)
        self.tier_table.verticalHeader().setVisible(False)
        self.tier_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tier_table.setFixedWidth(90 + 80 + 2)
        self.tier_table.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        tier_side = QVBoxLayout()
        tier_side.setSpacing(4)
        add_tier_btn = QPushButton("Add Tier")
        add_tier_btn.setFixedWidth(90)
        add_tier_btn.clicked.connect(lambda: self._add_tier_row("", 0.25))
        tier_side.addWidget(add_tier_btn)
        del_tier_btn = QPushButton("Remove")
        del_tier_btn.setFixedWidth(90)
        del_tier_btn.clicked.connect(self._remove_tier_row)
        tier_side.addWidget(del_tier_btn)
        tier_side.addStretch()

        tier_note = QLabel(
            "Drift from initial balance, matched low → high;\n"
            "first tier that fits wins. Empty “Up to %” is the\n"
            "top tier — “and everything above”."
        )
        tier_note.setStyleSheet("color: #999; font-size: 10px;")
        tier_note.setAlignment(Qt.AlignmentFlag.AlignTop)

        tier_body = QHBoxLayout()
        tier_body.setSpacing(8)
        tier_body.addWidget(self.tier_table)
        tier_body.addLayout(tier_side)
        tier_body.addWidget(tier_note, 1)
        dyn_layout.addLayout(tier_body)

        self._load_tiers(
            (slave_data or {}).get("risk_tiers") or default_tiers_as_dicts()
        )

        self.dynamic_pct_widget = QWidget()
        self.dynamic_pct_widget.setLayout(dyn_layout)
        form.addWidget(self.dynamic_pct_widget, row, 1)
        row += 1

        # The risk guard — a ceiling on what any single trade may lose on THIS
        # account, whichever mode sized it. Always shown: it is not one of the
        # modes, it is the last check before the order goes out.
        #
        # Per-slave because one global number cannot fit two accounts. The cap
        # has to clear the loss of the broker's own MINIMUM lot or nothing can
        # ever be sent: on a $101 account whose min lot is 0.1, a 19-point stop
        # already risks 1.9%, so a 1% cap blocked every trade — silently, and
        # only in the block message at trade time.
        form.addWidget(QLabel("Max risk/trade:"), row, 0)
        guard_layout = QHBoxLayout()
        self.max_risk_pct_spin = QDoubleSpinBox()
        self.max_risk_pct_spin.setRange(0.0, 100.0)
        self.max_risk_pct_spin.setDecimals(2)
        self.max_risk_pct_spin.setSingleStep(0.01)
        self.max_risk_pct_spin.setSuffix(" %")
        self.max_risk_pct_spin.setSpecialValueText("off")   # rendered at 0
        self.max_risk_pct_spin.setFixedWidth(80)
        self.max_risk_pct_spin.setValue(
            slave_data.get("max_risk_pct_per_trade", 0.0) if slave_data else 0.0
        )
        self.max_risk_pct_spin.setToolTip(
            "Blocks any trade on this slave whose loss-if-SL-hit exceeds this % "
            "of its balance. Trades with no SL are blocked too (unbounded risk).\n"
            "Must exceed what the broker's MINIMUM lot risks, or every trade is "
            "blocked. 0 = off."
        )
        guard_layout.addWidget(self.max_risk_pct_spin)
        self.max_risk_pct_slider = _make_slider(self.max_risk_pct_spin)
        guard_layout.addWidget(self.max_risk_pct_slider, 1)
        guard_hint = QLabel("of this account's balance (off = no ceiling)")
        guard_hint.setStyleSheet("color: #999; font-size: 10px;")
        guard_layout.addWidget(guard_hint)
        guard_layout.addStretch()
        guard_widget = QWidget()
        guard_widget.setLayout(guard_layout)
        form.addWidget(guard_widget, row, 1)
        row += 1

        form, row = self._grids["News"], 0

        # News blocking — independent of risk mode, so always shown. Each box
        # is a half-window in SECONDS around a scheduled event, applied
        # literally: 124 blocks from 124s before the print to 124s after, and
        # nothing is added behind your back. 0 ("off") is the default, so a
        # slave is unrestricted until it is told otherwise.
        form.addWidget(QLabel("News block:"), row, 0)
        news_layout = QHBoxLayout()
        news_layout.setSpacing(6)
        saved = slave_data or {}
        # Seconds if the file has them; a legacy minutes-only config is shown
        # converted, so the dialog never displays a number that is not what
        # runs.
        if saved.get("news_windows_s"):
            saved_windows = saved["news_windows_s"]
        else:
            saved_windows = {
                k: v * 60.0 for k, v in (saved.get("news_windows") or {}).items()
            }
        self.news_spins: dict[str, QDoubleSpinBox] = {}
        for impact in NEWS_IMPACTS:
            news_layout.addWidget(QLabel(impact))
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 7200.0)      # up to two hours a side
            spin.setDecimals(0)
            spin.setSingleStep(1.0)
            spin.setSuffix(" s")
            spin.setSpecialValueText("off")  # rendered at 0 = no restriction
            spin.setFixedWidth(78)
            try:
                spin.setValue(float(saved_windows.get(impact, 0.0) or 0.0))
            except (TypeError, ValueError):
                spin.setValue(0.0)
            news_layout.addWidget(spin)
            self.news_spins[impact] = spin
        news_layout.addStretch()
        news_widget = QWidget()
        news_widget.setLayout(news_layout)
        form.addWidget(news_widget, row, 1)
        row += 1

        # The hint gets its own full-width row. Trailing it after four spin
        # boxes squeezed both off the edge of the dialog.
        news_hint = QLabel(
            "blocks NEW copies ±window exactly, in seconds; closes never blocked"
        )
        news_hint.setStyleSheet("color: #999; font-size: 10px;")
        form.addWidget(news_hint, row, 1)
        row += 1

        # Which currencies those windows apply to. A slave trading US indices
        # has no reason to stop for Australian retail sales, and a calendar
        # week is mostly events that cannot touch it.
        #
        # NOTHING TICKED MEANS EVERY CURRENCY, not none — an existing slave
        # must keep blocking exactly as it did, so narrowing is opt-in. The
        # label says so, because "no boxes ticked = maximum restriction" is
        # the opposite of what a row of checkboxes usually implies.
        #
        # "All" is the feed's own code for events belonging to no currency
        # (bank holidays, Jackson Hole). It is an ordinary box: tick it to
        # block on them, leave it to ignore them.
        form.addWidget(QLabel("News currencies:"), row, 0)
        # Wrapped into a grid rather than one long row: ten checkboxes in a
        # line ran off the dialog and clipped every label to two characters.
        cur_grid = QGridLayout()
        cur_grid.setSpacing(6)
        cur_grid.setContentsMargins(0, 0, 0, 0)
        saved_currencies = _normalize_news_currencies(
            (slave_data or {}).get("news_currencies")
        )
        self.news_currency_boxes: dict[str, QCheckBox] = {}
        per_row = 5
        for i, code in enumerate(NEWS_CURRENCIES):
            box = QCheckBox(code)
            if code == "All":
                box.setToolTip(
                    "Events the feed files under no currency — bank holidays, "
                    "Jackson Hole, OPEC.\nOnly blocked if you tick this."
                )
            box.setChecked(code in saved_currencies)
            cur_grid.addWidget(box, i // per_row, i % per_row)
            self.news_currency_boxes[code] = box
        cur_grid.setColumnStretch(per_row, 1)
        for box in self.news_currency_boxes.values():
            box.toggled.connect(self._update_news_currency_hint)
        cur_widget = QWidget()
        cur_widget.setLayout(cur_grid)
        form.addWidget(cur_widget, row, 1)
        row += 1

        self.news_currency_hint = QLabel("")
        self.news_currency_hint.setStyleSheet("color: #999; font-size: 10px;")
        self._update_news_currency_hint()
        form.addWidget(self.news_currency_hint, row, 1)
        row += 1

        # Set initial risk mode from data
        risk_mode = slave_data.get("risk_mode", "multiplier") if slave_data else "multiplier"
        if risk_mode == "fixed_volume":
            self.risk_mode_combo.setCurrentIndex(1)
        elif risk_mode == "fixed_usd":
            self.risk_mode_combo.setCurrentIndex(2)
        elif risk_mode == "pct_deposit":
            self.risk_mode_combo.setCurrentIndex(3)
        elif risk_mode == "dynamic_pct":
            self.risk_mode_combo.setCurrentIndex(4)
        else:
            self.risk_mode_combo.setCurrentIndex(0)
        self._on_risk_mode_changed(self.risk_mode_combo.currentIndex())

        form, row = self._grids["Terminal"], 0

        # Terminal path
        form.addWidget(QLabel("Terminal:"), row, 0)
        terminal_layout = QHBoxLayout()
        self.terminal_edit = QLineEdit()
        self.terminal_edit.setText(slave_data.get("terminal_path", "") if slave_data else "")
        self.terminal_edit.setPlaceholderText("Auto-created or browse...")
        terminal_layout.addWidget(self.terminal_edit)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._browse_terminal)
        terminal_layout.addWidget(browse_btn)
        form.addLayout(terminal_layout, row, 1)
        row += 1

        form, row = self._grids["Symbols"], 0

        # Symbol mappings
        form.addWidget(QLabel("Symbols:"), row, 0)
        mapping_layout = QHBoxLayout()
        self.mapping_btn = QPushButton(f"Mappings ({len(self._symbol_mapping)})...")
        self.mapping_btn.setFixedWidth(120)
        self.mapping_btn.clicked.connect(self._edit_mappings)
        mapping_layout.addWidget(self.mapping_btn)
        self.mapping_info = QLabel("")
        self.mapping_info.setStyleSheet("color: #666; font-size: 10px;")
        self._update_mapping_label()
        mapping_layout.addWidget(self.mapping_info, 1)
        form.addLayout(mapping_layout, row, 1)
        row += 1

        # Enabled: back on the Credentials page, where "is this account in
        # play" belongs.
        form, row = self._grids["Credentials"], credentials_row
        form.addWidget(QLabel(""), row, 0)
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(slave_data.get("enabled", False) if slave_data else False)
        form.addWidget(self.enabled_check, row, 1)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #ddd;")
        layout.addWidget(line)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Baseline for the unsaved-changes prompt. Taken AFTER every field is
        # populated, so it is what the operator was shown — not the defaults.
        self._baseline = self._collect()

    def _on_risk_mode_changed(self, index: int):
        """Enable/disable fields based on risk mode."""
        # 0 = Multiplier, 1 = Fixed Volume, 2 = Fixed USD, 3 = % of Deposit,
        # 4 = Dynamic % ladder
        self.multiplier_widget.setEnabled(index == 0)
        self.fixed_vol_widget.setEnabled(index == 1)
        self.fixed_usd_widget.setEnabled(index == 2)
        self.pct_deposit_widget.setEnabled(index == 3)
        self.dynamic_pct_widget.setEnabled(index == 4)

    def _fit_tier_table(self):
        """Size the table to its rows — no inner scrollbar, no empty strip.

        A ladder is 2-5 rows; a fixed height would either clip it or leave dead
        space under it, and the dialog has room to grow instead.
        """
        header = self.tier_table.horizontalHeader().height()
        rows = sum(self.tier_table.rowHeight(r) for r in range(self.tier_table.rowCount()))
        self.tier_table.setFixedHeight(header + rows + 2)

    def _add_tier_row(self, up_to, risk_pct: float):
        """Append one ladder row. ``up_to`` empty/None means the top tier."""
        r = self.tier_table.rowCount()
        self.tier_table.insertRow(r)
        up_text = "" if up_to is None or up_to == "" else _fmt_num(up_to)
        self.tier_table.setItem(r, 0, QTableWidgetItem(up_text))
        self.tier_table.setItem(r, 1, QTableWidgetItem(_fmt_num(risk_pct)))
        self.tier_table.setRowHeight(r, 22)
        self._fit_tier_table()

    def _remove_tier_row(self):
        r = self.tier_table.currentRow()
        if r >= 0:
            self.tier_table.removeRow(r)
            self._fit_tier_table()

    def _load_tiers(self, tiers):
        self.tier_table.setRowCount(0)
        for up_to, risk in normalize_tiers(tiers) or []:
            self._add_tier_row(up_to, risk)
        self._fit_tier_table()

    def _update_news_currency_hint(self):
        """Spell out what the current tick state means, since empty is not none."""
        picked = [c for c, b in self.news_currency_boxes.items() if b.isChecked()]
        if picked:
            self.news_currency_hint.setText(
                f"blocking only on {', '.join(picked)} — nothing else"
            )
        else:
            self.news_currency_hint.setText(
                "none ticked = EVERY currency (narrowing is opt-in)"
            )

    def _collect_tiers(self) -> list[dict]:
        """Read the table back into config shape.

        Unparsable rows are dropped here rather than saved and silently ignored
        later — normalize_tiers then sorts and guarantees a top tier.
        """
        raw = []
        for r in range(self.tier_table.rowCount()):
            up_item = self.tier_table.item(r, 0)
            risk_item = self.tier_table.item(r, 1)
            up_text = (up_item.text().strip() if up_item else "").replace("%", "")
            risk_text = (risk_item.text().strip() if risk_item else "").replace("%", "")
            try:
                risk = float(risk_text)
            except ValueError:
                continue
            up = None
            if up_text:
                try:
                    up = float(up_text)
                except ValueError:
                    continue
            raw.append({"up_to_pct": up, "risk_pct": risk})
        return [{"up_to_pct": up, "risk_pct": risk} for up, risk in normalize_tiers(raw)]

    def _edit_mappings(self):
        """Open symbol mapping editor."""
        slave_name = self.name_edit.text().strip()
        cached = load_cached_symbols(slave_name) if slave_name else None
        dlg = SymbolMappingDialog(self._symbol_mapping, cached, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._symbol_mapping = dlg.get_mappings()
            self.mapping_btn.setText(f"Mappings ({len(self._symbol_mapping)})...")
            self._update_mapping_label()

    def _update_mapping_label(self):
        """Update the mapping info label."""
        if self._symbol_mapping:
            pairs = [f"{k}→{v}" for k, v in self._symbol_mapping.items()]
            self.mapping_info.setText(", ".join(pairs))
        else:
            self.mapping_info.setText("none")

    def _toggle_password_visibility(self, visible: bool):
        """Toggle password field between plain text and masked."""
        if visible:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

    def _browse_terminal(self):
        """Browse for terminal executable."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select MT5 Terminal",
            "",
            "Executable (terminal64.exe terminal.exe);;All Files (*)",
        )
        if path:
            self.terminal_edit.setText(str(Path(path)))

    def _validate(self) -> tuple[bool, str]:
        """Validate form fields."""
        if not self.name_edit.text().strip():
            return False, "Name is required"
        if self.login_edit.value() <= 0:
            return False, "Login must be positive"
        if not self.password_edit.text():
            return False, "Password is required"
        if not self.server_edit.text().strip():
            return False, "Server is required"
        if self.risk_mode_combo.currentIndex() == 4:
            # Caught here rather than at trade time: without these the copy path
            # blocks every trade on this slave.
            if self.initial_balance_spin.value() <= 0:
                return False, "Dynamic % needs an initial balance to measure against"
            if not self._collect_tiers():
                return False, "Dynamic % needs at least one risk tier"
        return True, ""

    def _save(self):
        """Save and close dialog."""
        valid, msg = self._validate()
        if not valid:
            QMessageBox.warning(self, "Validation Error", msg)
            return

        self.result_data = self._collect()
        self.accept()

    def _collect(self) -> dict[str, Any]:
        """Every field, as it would be saved.

        One definition, used both to save and to decide whether anything
        changed. Two readings of "what is in this dialog" would drift, and the
        one that drifts is the one guarding against losing work.
        """
        # Determine risk mode from combo
        risk_mode_index = self.risk_mode_combo.currentIndex()
        if risk_mode_index == 1:
            risk_mode = "fixed_volume"
        elif risk_mode_index == 2:
            risk_mode = "fixed_usd"
        elif risk_mode_index == 3:
            risk_mode = "pct_deposit"
        elif risk_mode_index == 4:
            risk_mode = "dynamic_pct"
        else:
            risk_mode = "multiplier"

        return {
            "name": self.name_edit.text().strip(),
            "login": self.login_edit.value(),
            "password": self.password_edit.text(),
            "server": self.server_edit.text().strip(),
            "multiplier": self.multiplier_spin.value(),
            "adjust_by_leverage": self.leverage_check.isChecked(),
            "enabled": self.enabled_check.isChecked(),
            "terminal_path": self.terminal_edit.text(),
            "symbol_mapping": dict(self._symbol_mapping),
            "fixed_volume": self.fixed_volume_spin.value(),
            "fixed_risk_usd": self.fixed_risk_spin.value(),
            "risk_mode": risk_mode,
            "risk_pct": self.risk_pct_spin.value(),
            "risk_pct_max": self.risk_pct_cap.value(),
            "risk_pct_base": "equity" if self.risk_pct_base_combo.currentIndex() == 1 else "balance",
            "max_risk_pct_per_trade": self.max_risk_pct_spin.value(),
            "initial_balance": self.initial_balance_spin.value(),
            "dynamic_base": "equity" if self.dynamic_base_combo.currentIndex() == 1 else "balance",
            "risk_tiers": self._collect_tiers(),
            "news_windows_s": {
                impact: spin.value()
                for impact, spin in self.news_spins.items()
                if spin.value() > 0
            },
            "news_currencies": [
                code for code, box in self.news_currency_boxes.items()
                if box.isChecked()
            ],
        }

    # ------------------------------------------------------- unsaved changes
    def _is_dirty(self) -> bool:
        """Whether anything differs from what the dialog opened with.

        Compared against the values as SHOWN, not against the stored config:
        opening a legacy slave converts its news windows from minutes to
        seconds for display, and that conversion is not an edit the operator
        made — prompting for it would train them to dismiss the prompt.
        """
        try:
            return self._collect() != self._baseline
        except Exception:
            # A half-built dialog is not a reason to trap someone in it.
            return False

    def reject(self):
        """Cancel — but not silently, if there is work to lose."""
        if not self._is_dirty():
            super().reject()
            return

        choice = QMessageBox.warning(
            self, "Unsaved changes",
            "This slave has unsaved changes.\n\nSave them before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            self._save()            # validates; stays open if it cannot save
        elif choice == QMessageBox.StandardButton.Discard:
            super().reject()
        # Cancel: stay in the dialog.

    def closeEvent(self, event):
        """The window's X and Esc route through the same prompt as Cancel."""
        if self._is_dirty():
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)


class RulesTab(QWidget):
    """Per-slave copy rules: accounts, risk mode, mappings, terminal template."""

    config_changed = pyqtSignal()

    def __init__(self, config_manager: ConfigManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._data = config_manager.load()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # === Slaves Configuration ===
        slaves_group = QGroupBox("Slave Accounts")
        slaves_layout = QVBoxLayout(slaves_group)
        slaves_layout.setSpacing(6)

        # Slaves directory is locked. Show R/O for transparency.
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("Directory:"))
        self.slaves_dir_edit = QLineEdit(str(_slaves_dir_fn()))
        self.slaves_dir_edit.setReadOnly(True)
        self.slaves_dir_edit.setToolTip(
            "Slaves directory is fixed for stability — see template_manager.slaves_dir()"
        )
        dir_layout.addWidget(self.slaves_dir_edit)
        slaves_layout.addLayout(dir_layout)

        # Slave Template (R/O fields + Seed/Re-seed button)
        tpl_layout = QGridLayout()
        tpl_layout.setSpacing(4)
        tpl_layout.setColumnStretch(1, 1)
        tpl_layout.addWidget(QLabel("Template:"), 0, 0)
        self.template_path_edit = QLineEdit()
        self.template_path_edit.setReadOnly(True)
        self.template_path_edit.setPlaceholderText("Not seeded — click Seed Template")
        tpl_layout.addWidget(self.template_path_edit, 0, 1)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(4)
        self.seed_template_btn = QPushButton("Seed Template...")
        self.seed_template_btn.setFixedWidth(140)
        self.seed_template_btn.clicked.connect(self._seed_template)
        btn_row.addWidget(self.seed_template_btn)
        self.verify_template_btn = QPushButton("Verify")
        self.verify_template_btn.setFixedWidth(70)
        self.verify_template_btn.clicked.connect(self._verify_template_clicked)
        btn_row.addWidget(self.verify_template_btn)
        tpl_layout.addLayout(btn_row, 0, 2)

        self.template_status_label = QLabel("")
        self.template_status_label.setStyleSheet("font-size: 11px;")
        tpl_layout.addWidget(self.template_status_label, 3, 1, 1, 2)

        tpl_layout.addWidget(QLabel("Source:"), 1, 0)
        self.template_source_edit = QLineEdit()
        self.template_source_edit.setReadOnly(True)
        tpl_layout.addWidget(self.template_source_edit, 1, 1, 1, 2)

        tpl_layout.addWidget(QLabel("Created:"), 2, 0)
        self.template_created_edit = QLineEdit()
        self.template_created_edit.setReadOnly(True)
        tpl_layout.addWidget(self.template_created_edit, 2, 1, 1, 2)

        slaves_layout.addLayout(tpl_layout)

        # Compact table with inline Edit/Delete per row and "+" row at bottom
        self.add_slave_btn = None  # Created in _refresh_slave_table
        self.slave_table = QTableWidget()
        self.slave_table.setColumnCount(7)
        self.slave_table.setHorizontalHeaderLabels(["Name", "Login", "Server", "Risk Mode", "Value", "On", ""])
        self.slave_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.slave_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.slave_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.slave_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.slave_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.slave_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.slave_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.slave_table.setColumnWidth(6, 100)
        self.slave_table.verticalHeader().setVisible(False)
        self.slave_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.slave_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.slave_table.setMinimumHeight(240)
        self.slave_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.slave_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.slave_table.itemDoubleClicked.connect(self._edit_slave_dblclick)
        slaves_layout.addWidget(self.slave_table)

        main_layout.addWidget(slaves_group, 1)

    def reload(self):
        """Re-read config from disk and refresh the table + template fields."""
        self._data = self.config_manager.load()
        self._load()

    def _load(self):
        self._refresh_template_fields()
        self._refresh_slave_table()

    # ---- template ----
    def _refresh_template_fields(self):
        """Populate template R/O fields and update Seed/Re-seed button label."""
        st = self._data.get("slave_terminal", {})
        tpl_path = st.get("template_path", "")
        self.template_path_edit.setText(tpl_path)
        self.template_source_edit.setText(st.get("template_source", ""))
        self.template_created_edit.setText(st.get("template_created_at", ""))
        self.seed_template_btn.setText("Re-seed Template..." if tpl_path else "Seed Template...")
        # Auto-verify so the user sees stale-template warnings without clicking.
        ok, msg = validate_template(tpl_path)
        if not tpl_path:
            self.template_status_label.setText("")
        elif ok:
            self.template_status_label.setText(f"✓ {msg}")
            self.template_status_label.setStyleSheet("color: green; font-size: 11px;")
        else:
            self.template_status_label.setText(f"✗ {msg}")
            self.template_status_label.setStyleSheet("color: red; font-size: 11px;")

    def _verify_template_clicked(self):
        """Re-run validate_template; the inline status_label shows the result."""
        self._refresh_template_fields()

    def _seed_template(self):
        """Run the Seed Template wizard. Persist result on success."""
        slaves_dir = _slaves_dir_fn()
        try:
            slaves_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Slaves directory", f"Cannot create {slaves_dir}: {e}")
            return

        dlg = SeedTemplateDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return

        # Merge into in-memory data and persist to disk.
        if "slave_terminal" not in self._data:
            self._data["slave_terminal"] = {}
        self._data["slave_terminal"].update(dlg.result_data)
        try:
            on_disk = self.config_manager.load()
            if "slave_terminal" not in on_disk:
                on_disk["slave_terminal"] = {}
            on_disk["slave_terminal"].update(dlg.result_data)
            self.config_manager.save(on_disk)
        except Exception as e:
            import logging
            logging.getLogger("metact").exception("Save failed in _seed_template: %s", e)
            QMessageBox.critical(self, "Save failed", f"Could not save template settings: {e}")
            return
        self._refresh_template_fields()
        self.config_changed.emit()

    # ---- slave table ----
    def _refresh_slave_table(self):
        """Refresh slave table with inline Edit/Delete buttons and a '+' add row."""
        slaves = self._data.get("slaves", [])
        # Full clear: remove all spans, widgets, and rows to avoid stale artifacts
        for r in range(self.slave_table.rowCount()):
            self.slave_table.setSpan(r, 0, 1, 1)
            for c in range(self.slave_table.columnCount()):
                self.slave_table.removeCellWidget(r, c)
        self.slave_table.setRowCount(0)
        # Extra row at the bottom for the "+" add button
        self.slave_table.setRowCount(len(slaves) + 1)

        for row, slave in enumerate(slaves):
            self.slave_table.setItem(row, 0, QTableWidgetItem(slave.get("name", "")))
            self.slave_table.setItem(row, 1, QTableWidgetItem(str(slave.get("login", ""))))
            self.slave_table.setItem(row, 2, QTableWidgetItem(slave.get("server", "")))

            # Risk mode column
            risk_mode = slave.get("risk_mode", "multiplier")
            if risk_mode == "fixed_volume":
                mode_text = "Fixed Vol"
                value_text = f"{slave.get('fixed_volume', 0.0):.2f}"
            elif risk_mode == "fixed_usd":
                mode_text = "Fixed $"
                value_text = f"${slave.get('fixed_risk_usd', 0.0):.0f}"
            elif risk_mode == "pct_deposit":
                base = slave.get("risk_pct_base", "balance")
                mode_text = f"% {'Eq' if base == 'equity' else 'Bal'}"
                value_text = f"{_fmt_num(slave.get('risk_pct', 1.0))}%"
            elif risk_mode == "dynamic_pct":
                base = slave.get("dynamic_base", "balance")
                mode_text = f"Dyn {'Eq' if base == 'equity' else 'Bal'}"
                tiers = normalize_tiers(slave.get("risk_tiers"))
                ladder = "/".join(_fmt_num(t[1]) for t in tiers) if tiers else "-"
                value_text = f"{ladder}% @ {_fmt_num(slave.get('initial_balance', 0.0))}"
            else:
                mode_text = "Multiplier"
                value_text = f"×{_fmt_num(slave.get('multiplier', 1.0))}"

            self.slave_table.setItem(row, 3, QTableWidgetItem(mode_text))
            self.slave_table.setItem(row, 4, QTableWidgetItem(value_text))

            enabled = slave.get("enabled", False)
            item = QTableWidgetItem("●" if enabled else "○")
            item.setForeground(QColor("#4CAF50") if enabled else QColor("#ccc"))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.slave_table.setItem(row, 5, item)

            # Inline Edit / Delete buttons
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(2, 0, 2, 0)
            btn_layout.setSpacing(2)

            edit_btn = QPushButton()
            edit_btn.setIcon(QIcon(str(_ICONS / "text-editor.svg")))
            edit_btn.setFixedHeight(22)
            edit_btn.clicked.connect(lambda _, r=row: self._edit_slave(r))
            btn_layout.addWidget(edit_btn)

            del_btn = QPushButton()
            del_btn.setIcon(QIcon(str(_ICONS / "close.svg")))
            del_btn.setFixedHeight(22)
            del_btn.clicked.connect(lambda _, r=row: self._delete_slave(r))
            btn_layout.addWidget(del_btn)

            self.slave_table.setCellWidget(row, 6, btn_widget)

        # "+" add row at the bottom
        add_row = len(slaves)
        self.slave_table.setSpan(add_row, 0, 1, 7)
        self.add_slave_btn = QPushButton()
        self.add_slave_btn.setIcon(QIcon(str(_ICONS / "plus.svg")))
        self.add_slave_btn.setFixedHeight(24)
        self.add_slave_btn.clicked.connect(self._add_slave)
        self.slave_table.setCellWidget(add_row, 0, self.add_slave_btn)

    def _add_slave(self):
        """Add new slave."""
        dlg = SlaveDialog(self.config_manager, None, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            self._process_new_slave(dlg.result_data)

    def _save_slaves(self):
        """Persist current slaves list to config file and notify listeners."""
        try:
            on_disk = self.config_manager.load()
            on_disk["slaves"] = self._data.get("slaves", [])
            # Keep slave_terminal in sync. Merge into existing dict so template
            # fields (template_path / template_source / template_created_at)
            # survive any save path.
            first_term = next(
                (s["terminal_path"] for s in on_disk["slaves"] if s.get("terminal_path")),
                "",
            )
            existing_st = on_disk.get("slave_terminal", {}) or {}
            existing_term = existing_st.get("terminal_path", "")
            existing_st["terminal_path"] = first_term or existing_term
            on_disk["slave_terminal"] = existing_st
            self.config_manager.save(on_disk)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Save failed: {e}")
            return

        # Update LiveSettings immediately
        try:
            from ..live_settings import get_live_settings
            get_live_settings().update_from_dict(on_disk, issuer="user")
        except Exception:
            pass

        self.config_changed.emit()

    def _ensure_slave_terminal(self, data: dict[str, Any]) -> bool:
        """Offer to create MT5 terminal folder if missing.

        Returns True if terminal exists or was created, False otherwise.
        """
        terminal_path = data.get("terminal_path", "")
        if terminal_path and Path(terminal_path).exists():
            return True

        reply = QMessageBox.question(
            self, "Create Terminal",
            f"Create MT5 terminal for '{data['name']}'?\n\nThis copies the master folder.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        ok, msg, path = self.config_manager.create_slave_terminal(data["name"])
        if ok:
            data["terminal_path"] = path
            QMessageBox.information(self, "Created", msg)
            return True
        else:
            QMessageBox.warning(self, "Error", msg)
            return False

    def _process_new_slave(self, data: dict[str, Any]):
        """Process newly added slave - create terminal if needed."""
        if not self._ensure_slave_terminal(data):
            return

        if "slaves" not in self._data:
            self._data["slaves"] = []
        self._data["slaves"].append(data)
        self._save_slaves()
        self._refresh_slave_table()

    def _edit_slave_dblclick(self, item: QTableWidgetItem):
        """Edit on double-click."""
        self._edit_slave(item.row())

    def _edit_slave(self, row=None):
        """Edit selected slave."""
        if row is None:
            row = self.slave_table.currentRow()
        if row < 0:
            return
        slaves = self._data.get("slaves", [])
        if row >= len(slaves):
            return

        slave = slaves[row]
        dlg = SlaveDialog(self.config_manager, slave, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            if not self._ensure_slave_terminal(dlg.result_data):
                return
            slaves[row] = dlg.result_data
            self._save_slaves()
            self._refresh_slave_table()

    def _delete_slave(self, row=None):
        """Delete selected slave (blocked if open trades exist)."""
        if row is None:
            row = self.slave_table.currentRow()
        if row < 0:
            return
        slaves = self._data.get("slaves", [])
        if row >= len(slaves):
            return

        slave = slaves[row]
        name = slave.get("name", "")

        # Block delete if slave is running/paused in SlaveStateMachine
        from ..slave_state import get_slave_state
        state = get_slave_state()
        if not state.can_delete(name):
            QMessageBox.warning(
                self, "Cannot Delete",
                f"Stop '{name}' in the Slaves tab first.",
            )
            return
        terminal = slave.get("terminal_path", "")

        reply = QMessageBox.question(
            self, "Delete Slave",
            f"Delete '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Ask about folder
        if terminal:
            folder_reply = QMessageBox.question(
                self, "Delete Folder",
                f"Also delete MT5 folder?\n{Path(terminal).parent}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if folder_reply == QMessageBox.StandardButton.Yes:
                ok, msg = self.config_manager.delete_slave_terminal(terminal)
                if not ok:
                    QMessageBox.warning(self, "Error", msg)
                    return

        slaves.pop(row)
        self._save_slaves()
        self._refresh_slave_table()