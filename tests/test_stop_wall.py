"""The full-screen wall that ends the trading day.

What is pinned here is everything that makes it a WALL rather than a dialog:
it covers the screen, it sits above other applications, the app underneath is
frozen while it is up, and none of the reflexes that dismiss a dialog — Esc,
the window's close, the window manager — can get past it. The only way out is
its button, and the button closes the app.

These run against real Qt on the offscreen platform; PyQt6 is stubbed out in
conftest for the non-GUI suite, so the stub is removed first.
"""
import importlib
import sys

import pytest


@pytest.fixture
def qt(monkeypatch):
    """Real PyQt6 + one offscreen QApplication, with src.gui reimported."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in [m for m in list(sys.modules) if m.startswith("PyQt6")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    pytest.importorskip("PyQt6.QtWidgets")

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    overlay = importlib.import_module("src.gui.stop_overlay")
    yield app, overlay
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def _wall(overlay, **kwargs):
    return overlay.DailyStopOverlay(
        count=kwargs.pop("count", 4),
        limit=kwargs.pop("limit", 4),
        details=kwargs.pop("details", ("14:03 NAS100 -122.50",)),
        open_positions=kwargs.pop("open_positions", 0),
        **kwargs,
    )


# ==================== it is a wall ====================

def test_it_covers_the_whole_screen(qt):
    _, overlay = qt
    wall = _wall(overlay)

    wall.show_over_everything()

    screen = wall.screen().geometry()
    assert wall.geometry().width() >= screen.width()
    assert wall.geometry().height() >= screen.height()
    wall._closing = True
    wall.close()


def test_it_sits_above_other_applications(qt):
    from PyQt6.QtCore import Qt

    _, overlay = qt
    flags = _wall(overlay).windowFlags()

    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.FramelessWindowHint


def test_the_app_underneath_is_frozen_while_it_is_up(qt):
    """Application-modal: no window of this app takes input behind the wall."""
    from PyQt6.QtCore import Qt

    _, overlay = qt

    assert _wall(overlay).windowModality() == Qt.WindowModality.ApplicationModal


# ==================== nothing dismisses it but the button ====================

def test_escape_does_nothing(qt):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent

    _, overlay = qt
    wall = _wall(overlay)

    wall.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )

    assert wall.isVisible() is False  # never shown, but crucially not accepted
    assert wall.result() == 0


def test_the_window_close_is_refused(qt):
    _, overlay = qt
    wall = _wall(overlay)
    wall.show_over_everything()

    wall.close()          # Alt+F4 / window manager

    assert wall.isVisible()
    wall._closing = True
    wall.close()


def test_reject_is_refused(qt):
    """QDialog.reject is what Esc and a window close route into."""
    _, overlay = qt
    wall = _wall(overlay)
    wall.show_over_everything()

    wall.reject()

    assert wall.isVisible()
    wall._closing = True
    wall.close()


def test_the_button_is_the_only_way_out(qt):
    from PyQt6.QtWidgets import QDialog

    _, overlay = qt
    wall = _wall(overlay)
    wall.show_over_everything()

    wall.close_btn.click()

    assert not wall.isVisible()
    assert wall.result() == QDialog.DialogCode.Accepted


# ==================== what it says ====================

def test_it_lists_the_days_losses(qt):
    """Broker-timed and checkable line for line against the History tab."""
    _, overlay = qt
    wall = _wall(overlay, details=("14:03 NAS100 -122.50", "15:20 NAS100 -88.10"))

    assert "15:20 NAS100 -88.10" in _text(wall)


def test_it_says_the_count_and_the_limit(qt):
    _, overlay = qt

    assert "4 losing trade(s) today. The limit is 4." in _text(_wall(overlay))


def test_it_says_a_restart_will_not_clear_it(qt):
    _, overlay = qt

    assert "restarting does not reset it" in _text(_wall(overlay))


def test_on_launch_it_says_the_day_was_already_over(qt):
    """Opened onto this screen, nothing just happened — the wording has to say
    so, or it reads as a loss the person has not seen yet."""
    _, overlay = qt

    text = _text(_wall(overlay, on_launch=True))
    assert "That was it for today" in text


def test_during_a_session_it_says_the_day_ends_now(qt):
    _, overlay = qt

    assert "That is it for today." in _text(_wall(overlay))


def test_open_positions_are_spelled_out_with_what_closing_costs(qt):
    """The app is about to close: what that means for positions still open is
    the one thing the person cannot find out afterwards."""
    _, overlay = qt

    text = _text(_wall(overlay, open_positions=2))
    assert "2 slave position(s) are still open" in text
    assert "stops the copier mirroring" in text


def _text(wall) -> str:
    from PyQt6.QtWidgets import QLabel

    return "\n".join(label.text() for label in wall.findChildren(QLabel))
