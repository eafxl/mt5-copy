"""Symbol mapping editor dialog for per-slave symbol name mappings."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def load_cached_symbols(slave_name: str) -> list[str] | None:
    """Load cached broker symbols from LiveSettings (in-memory, populated at warmup)."""
    try:
        from ..live_settings import get_live_settings
        return get_live_settings().get_slave_symbols(slave_name)
    except Exception:
        return None


class SymbolMappingDialog(QDialog):
    """Dialog for editing per-slave symbol mappings.

    Two-column table: Master Symbol → Slave Symbol.
    If cached broker symbols are available, provides autocomplete
    on the Slave Symbol column and warns on invalid symbols.
    """

    def __init__(
        self,
        mappings: dict[str, str],
        cached_symbols: list[str] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.cached_symbols = cached_symbols
        self.setWindowTitle("Symbol Mappings")
        self.setMinimumSize(420, 300)
        self._setup_ui(mappings)

    def _setup_ui(self, mappings: dict[str, str]):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Help text
        help_text = QLabel(
            "Map master symbol names to this slave's broker symbols.\n"
            "Leave empty rows will be ignored."
        )
        help_text.setStyleSheet("color: #666; font-size: 11px;")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        if self.cached_symbols:
            cached_label = QLabel(
                f"Broker symbols loaded ({len(self.cached_symbols)} available) — autocomplete active"
            )
            cached_label.setStyleSheet("color: #4CAF50; font-size: 10px;")
            layout.addWidget(cached_label)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Master Symbol", "Slave Symbol"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.table, 1)

        # Populate existing mappings
        for master, slave in mappings.items():
            self._add_row(master, slave)

        # Always add one empty row for easy entry
        if not mappings:
            self._add_row("", "")

        # Buttons row
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("+ Add Row")
        add_btn.setFixedWidth(80)
        add_btn.clicked.connect(lambda: self._add_row("", ""))
        btn_layout.addWidget(add_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.setFixedWidth(70)
        remove_btn.clicked.connect(self._remove_row)
        btn_layout.addWidget(remove_btn)

        btn_layout.addStretch()

        self._warning_label = QLabel("")
        self._warning_label.setStyleSheet("color: #f44336; font-size: 10px;")
        btn_layout.addWidget(self._warning_label)

        layout.addLayout(btn_layout)

        # OK/Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, master: str = "", slave: str = ""):
        """Add a row to the mapping table."""
        row = self.table.rowCount()
        self.table.insertRow(row)

        master_item = QTableWidgetItem(master)
        self.table.setItem(row, 0, master_item)

        # For slave column, use a QLineEdit with completer if symbols available
        if self.cached_symbols:
            editor = QLineEdit(slave)
            completer = QCompleter(self.cached_symbols, editor)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            editor.setCompleter(completer)
            editor.setStyleSheet("border: none; padding: 2px;")
            self.table.setCellWidget(row, 1, editor)
        else:
            slave_item = QTableWidgetItem(slave)
            self.table.setItem(row, 1, slave_item)

    def _remove_row(self):
        """Remove selected row."""
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _get_slave_text(self, row: int) -> str:
        """Get slave symbol text from row (handles both QLineEdit and QTableWidgetItem)."""
        widget = self.table.cellWidget(row, 1)
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        item = self.table.item(row, 1)
        return item.text().strip() if item else ""

    def get_mappings(self) -> dict[str, str]:
        """Read table into a dict. Skips empty rows."""
        mappings = {}
        for row in range(self.table.rowCount()):
            master_item = self.table.item(row, 0)
            master = master_item.text().strip() if master_item else ""
            slave = self._get_slave_text(row)

            if master and slave:
                mappings[master] = slave
        return mappings

    def _validate(self) -> tuple[bool, str]:
        """Validate mappings."""
        mappings = self.get_mappings()
        masters_seen = set()
        warnings = []

        for row in range(self.table.rowCount()):
            master_item = self.table.item(row, 0)
            master = master_item.text().strip() if master_item else ""
            slave = self._get_slave_text(row)

            # Skip fully empty rows
            if not master and not slave:
                continue

            if master and not slave:
                return False, f"Row {row + 1}: slave symbol is empty"
            if not master and slave:
                return False, f"Row {row + 1}: master symbol is empty"

            if master in masters_seen:
                return False, f"Duplicate master symbol: {master}"
            masters_seen.add(master)

            # Warn if slave symbol not in broker cache
            if self.cached_symbols and slave and slave not in self.cached_symbols:
                warnings.append(f"'{slave}' not found in broker symbols")

        if warnings:
            self._warning_label.setText("; ".join(warnings))
        else:
            self._warning_label.setText("")

        return True, ""

    def _accept(self):
        """Validate and accept."""
        valid, msg = self._validate()
        if not valid:
            self._warning_label.setText(msg)
            return
        self.accept()
