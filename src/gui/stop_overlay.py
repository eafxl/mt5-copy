"""The full-screen wall that ends the trading day.

WHY A WALL AND NOT A MESSAGE BOX
    The limit was announced with a QMessageBox. A message box is a small grey
    rectangle inside one app's window, and this app spends its life behind five
    maximized MT5 terminals — so the one moment the rule exists for was the one
    moment it could be missed entirely, dismissed by a stray Enter while the
    person was looking at a chart.

    This covers the whole screen, sits above every other application, and has
    exactly one button. It is not something you click past on the way to
    something else; it IS the screen until the app closes.

WHY IT CANNOT BE DISMISSED
    Esc, Alt+F4 and the window's own close all do nothing here: every one of
    them is a reflex, and a reflex is precisely what must not be able to clear
    a loss limit. The only way out is the button, and the button closes the
    app.

WHY IT COMES BACK ON LAUNCH
    The count is read from the broker's own history, so reopening the app finds
    the same four losses and shows this same screen again. That is the point —
    the limit outlives the process, and being told so on launch is what stops
    the restart from feeling like a way around it.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: Deep red-black. Not the app's palette: this screen is not part of the app's
#: normal life and should not look like it is.
_BG = "#180D0D"
_ACCENT = "#FF5252"
_TEXT = "#F5E9E9"
_MUTED = "#B08585"


class DailyStopOverlay(QDialog):
    """Full-screen, always-on-top, undismissable. Its button closes the app.

    Modal to the application, so everything underneath is frozen for as long as
    it is up — the lock-out applied to the tabs behind it is what the person
    sees for the half second between this closing and the app going with it.
    """

    def __init__(
        self,
        count: int,
        limit: int,
        details: tuple[str, ...] = (),
        open_positions: int = 0,
        on_launch: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._closing = False

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setStyleSheet(f"background-color: {_BG};")

        self._build(count, limit, details, open_positions, on_launch)

    # ------------------------------------------------------------------ build
    def _build(self, count, limit, details, open_positions, on_launch):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        # One centered column, capped in width. Full-screen text set flush to
        # the edges is unreadable on a wide monitor.
        card = QWidget()
        card.setMaximumWidth(760)
        col = QVBoxLayout(card)
        col.setSpacing(14)
        col.setContentsMargins(40, 40, 40, 40)

        headline = QLabel("DAILY STOP LIMIT")
        headline.setStyleSheet(
            f"color: {_ACCENT}; font-size: 46px; font-weight: 700; "
            "letter-spacing: 3px;"
        )
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(headline)

        # The one sentence someone reads before deciding whether to read the
        # rest. Different on launch: there, nothing just happened — the day was
        # already over before the app opened.
        lede = QLabel(
            "That is it for today." if not on_launch
            else "That was it for today — before you opened this."
        )
        lede.setStyleSheet(f"color: {_TEXT}; font-size: 22px;")
        lede.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(lede)

        count_line = QLabel(f"{count} losing trade(s) today. The limit is {limit}.")
        count_line.setStyleSheet(f"color: {_TEXT}; font-size: 17px;")
        count_line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(count_line)

        col.addWidget(self._rule())

        if details:
            losses_title = QLabel("Today's losses")
            losses_title.setStyleSheet(
                f"color: {_MUTED}; font-size: 12px; letter-spacing: 2px;"
            )
            losses_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(losses_title)

            # Monospaced and broker-timed, so the list can be checked line for
            # line against the terminal's own History tab.
            losses = QLabel("\n".join(details))
            losses.setStyleSheet(
                f"color: {_TEXT}; font-size: 15px; font-family: Consolas, monospace;"
            )
            losses.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(losses)
            col.addWidget(self._rule())

        notes = [
            "The count is read from the master's own trade history, so "
            "restarting does not reset it.",
        ]
        if open_positions:
            notes.append(
                f"{open_positions} slave position(s) are still open, with their "
                "SL and TP set at the broker. Closing this app stops the copier "
                "mirroring any further change the master makes to them."
            )
        for text in notes:
            note = QLabel(text)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {_MUTED}; font-size: 13px;")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(note)

        col.addSpacing(10)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.close_btn = QPushButton("Close MetaCT")
        self.close_btn.setFixedSize(220, 46)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet(
            f"QPushButton {{ background-color: {_ACCENT}; color: #2A0A0A; "
            "font-size: 16px; font-weight: 600; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #FF7676; }"
        )
        self.close_btn.clicked.connect(self._confirm)
        buttons.addWidget(self.close_btn)
        buttons.addStretch()
        col.addLayout(buttons)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    @staticmethod
    def _rule() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #3A2020; border: none; max-height: 1px;")
        return line

    # ------------------------------------------------------------- appearance
    def show_over_everything(self):
        """Cover the screen the app is on, above every other application."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        # Flash the taskbar too: on Windows a background process cannot always
        # take focus, and a full-screen window nobody looked at is a wallpaper.
        app = QApplication.instance()
        if app is not None:
            app.alert(self, 0)
        self.close_btn.setFocus()

    # ---------------------------------------------------------- no way around
    def _confirm(self):
        self._closing = True
        self.accept()

    def keyPressEvent(self, event):
        """Swallow Esc. Everything else behaves normally (Tab, Space, Enter)."""
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def reject(self):
        """Esc and the window manager's close both land here — refuse both."""
        if self._closing:
            super().reject()

    def closeEvent(self, event):
        if self._closing:
            event.accept()
        else:
            event.ignore()
