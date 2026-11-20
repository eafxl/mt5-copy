"""GUI dialog for approving initial position sync using PyQt."""
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor


@dataclass
class PositionInfo:
    """Position info for display in approval dialog."""
    ticket: int
    symbol: str
    side: str  # "BUY" or "SELL"
    volume: float
    entry_price: float
    current_price: float
    sl: float
    tp: float
    profit: float


class SyncApprovalDialog(QDialog):
    """Dialog window for approving positions to sync."""

    def __init__(self, positions: list[PositionInfo], parent=None):
        super().__init__(parent)
        self.positions = positions
        self.approved_tickets: set[int] = set()

        self.setWindowTitle("MetaCT — Initial Sync Approval")
        self.setMinimumSize(750, 450)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        self._create_widgets()
        self._center_on_screen()

    def _center_on_screen(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def _create_widgets(self):
        layout = QVBoxLayout(self)

        # Header
        header = QLabel("Existing Positions Found - Approve for Sync")
        header.setStyleSheet("font-size: 14px; font-weight: bold; margin: 10px;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        # Info
        info = QLabel("Select which positions to copy to slave accounts:")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Symbol", "Side", "Volume", "Entry", "Current", "Diff (pips)", "Profit"
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)

        # Populate table
        self.table.setRowCount(len(self.positions))
        for row, pos in enumerate(self.positions):
            diff_pips = abs(pos.current_price - pos.entry_price) * (100 if "JPY" in pos.symbol else 10000)
            profit_str = f"+{pos.profit:.2f}" if pos.profit >= 0 else f"{pos.profit:.2f}"
            color = QColor(0, 128, 0) if pos.profit >= 0 else QColor(200, 0, 0)

            items = [
                pos.symbol,
                pos.side,
                f"{pos.volume:.2f}",
                f"{pos.entry_price:.5f}",
                f"{pos.current_price:.5f}",
                f"{diff_pips:.1f}",
                profit_str
            ]

            for col, text in enumerate(items):
                item = QTableWidgetItem(str(text))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 6:  # Profit column
                    item.setForeground(color)
                self.table.setItem(row, col, item)

            # Store ticket in first column's data
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, pos.ticket)

        # Select all rows by default
        self.table.selectAll()

        layout.addWidget(self.table)

        # Selection buttons row
        sel_layout = QHBoxLayout()
        sel_layout.addStretch()

        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self.table.selectAll)
        sel_layout.addWidget(btn_select_all)

        btn_select_none = QPushButton("Select None")
        btn_select_none.clicked.connect(self.table.clearSelection)
        sel_layout.addWidget(btn_select_none)

        layout.addLayout(sel_layout)

        # Status label
        self.status_label = QLabel(f"{len(self.positions)} positions selected")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        # Update status on selection change
        self.table.itemSelectionChanged.connect(self._update_status)

        # Action buttons
        btn_layout = QHBoxLayout()

        btn_sync_selected = QPushButton("\u2713 Sync Selected")
        btn_sync_selected.clicked.connect(self._sync_selected)
        btn_layout.addWidget(btn_sync_selected)

        btn_sync_all = QPushButton("\u2713 Sync All")
        btn_sync_all.clicked.connect(self._sync_all)
        btn_layout.addWidget(btn_sync_all)

        btn_skip = QPushButton("\u2717 Skip All")
        btn_skip.clicked.connect(self._skip_all)
        btn_layout.addWidget(btn_skip)

        layout.addLayout(btn_layout)

    def _update_status(self):
        selected = len(self.table.selectedItems()) // self.table.columnCount()
        self.status_label.setText(f"{selected} of {len(self.positions)} positions selected")

    def _get_selected_tickets(self) -> set[int]:
        tickets = set()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).isSelected():
                ticket = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                tickets.add(ticket)
        return tickets

    def _sync_selected(self):
        self.approved_tickets = self._get_selected_tickets()
        self.accept()

    def _sync_all(self):
        self.approved_tickets = {pos.ticket for pos in self.positions}
        self.accept()

    def _skip_all(self):
        self.approved_tickets = set()
        self.reject()


def show_sync_approval(positions: list[PositionInfo]) -> set[int]:
    """
    Show approval dialog for positions.

    Args:
        positions: List of PositionInfo to display

    Returns:
        Set of approved ticket numbers
    """
    if not positions:
        return set()

    # Create QApplication if not exists
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        # Standalone (CLI) path: no run_gui to have set these already.
        from .gui.main_window import APP_NAME, app_icon

        app.setApplicationName(APP_NAME)
        app.setWindowIcon(app_icon())

    dialog = SyncApprovalDialog(positions)
    dialog.exec()

    return dialog.approved_tickets