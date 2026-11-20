"""What the daily stop limit does to the copier once it trips.

The counting itself is pinned in test_daily_stops.py. This is the other half:
the limit has to STOP new copies, has to keep letting positions OUT, and has to
ask for the shutdown exactly once.

The exits matter most. A limit that also blocked closes would leave open risk
on the slaves with nothing left running to manage it — turning a bad day into
an unhedged one, which is the opposite of what a loss limit is for.
"""
import pytest

from src.events import EventType
from src.mt5_client import ClosedTrade


def _losses(n):
    """``n`` losing trades, dated today on the BROKER's calendar."""
    from src.daily_stops import host_today

    return [
        ClosedTrade(position_id=1000 + i, symbol="NAS100", net=-50.0,
                    closed_at=1_700_000_000.0 + i, server_day=host_today())
        for i in range(n)
    ]


@pytest.fixture
def copier(copier_factory, make_config):
    return copier_factory(make_config(num_slaves=2))


def trip(copier, mock_mt5, count=4):
    """Put ``count`` losing closes in the master's history and read them."""
    mock_mt5.closed_trades.return_value = _losses(count)
    copier.stops.refresh()
    return copier


# ==================== new copies stop ====================

@pytest.mark.asyncio
async def test_a_new_position_is_not_copied_once_tripped(
    copier, mock_mt5, make_position
):
    trip(copier, mock_mt5)

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    assert copier.executor.submitted_tasks == []


@pytest.mark.asyncio
async def test_the_ticket_is_not_claimed_when_blocked(
    copier, mock_mt5, make_position
):
    """Blocking before the claim keeps the tracker clean — otherwise the
    position looks copied and its later close would be mirrored to nothing."""
    trip(copier, mock_mt5)

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    assert copier.tracker.get_position(555) is None


@pytest.mark.asyncio
async def test_three_losses_still_copy(copier, mock_mt5, make_position):
    """One short of the limit is a normal trading day."""
    trip(copier, mock_mt5, count=3)

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    assert copier.executor.submitted_tasks, "the copy should have gone out"


@pytest.mark.asyncio
async def test_an_unreadable_history_does_not_block_trading(
    copier, mock_mt5, make_position
):
    """A terminal that blinked must not become an outage of our own making."""
    mock_mt5.closed_trades.return_value = None
    copier.stops.refresh()

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    assert copier.executor.submitted_tasks


# ==================== exits keep working ====================

@pytest.mark.asyncio
async def test_a_close_is_still_mirrored_once_tripped(
    copier, mock_mt5, make_position
):
    """The whole point: stop opening, never stop closing."""
    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")
    trip(copier, mock_mt5)
    copier.executor.submitted_tasks.clear()

    await copier._close_copied_positions(555, "TCP")

    assert any(
        call.action.value == "close" for call in copier.executor.submitted_tasks
    ), "the exit must still be dispatched"


@pytest.mark.asyncio
async def test_sl_tp_updates_still_go_out_once_tripped(
    copier, mock_mt5, make_position
):
    pos = make_position(ticket=555)
    await copier._copy_new_position(pos, 100, "TCP")
    trip(copier, mock_mt5)
    copier.executor.submitted_tasks.clear()

    pos.sl = 1.0900
    await copier._update_sl_tp(pos, "TCP")

    assert any(call.action.value == "modify" for call in copier.executor.submitted_tasks)


# ==================== announcing, once ====================

@pytest.mark.asyncio
async def test_the_limit_asks_the_app_to_close(
    copier, mock_mt5, make_position, event_collector
):
    trip(copier, mock_mt5)

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    events = [e for e in event_collector
              if e.event_type == EventType.DAILY_STOP_LIMIT]
    assert len(events) == 1
    assert events[0].stop_count == 4
    assert events[0].stop_limit == 4


@pytest.mark.asyncio
async def test_it_is_announced_once_however_many_trades_are_blocked(
    copier, mock_mt5, make_position, event_collector
):
    """A modal dialog per blocked signal would be unusable."""
    trip(copier, mock_mt5)

    for ticket in (555, 556, 557):
        await copier._copy_new_position(make_position(ticket=ticket), 100, "TCP")

    assert len([e for e in event_collector
                if e.event_type == EventType.DAILY_STOP_LIMIT]) == 1


@pytest.mark.asyncio
async def test_the_warning_carries_the_still_open_positions(
    copier, mock_mt5, make_position, event_collector
):
    """Closing the app orphans them — that cannot be discovered later."""
    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")
    trip(copier, mock_mt5)

    await copier._copy_new_position(make_position(ticket=556), 100, "TCP")

    event = [e for e in event_collector
             if e.event_type == EventType.DAILY_STOP_LIMIT][0]
    assert event.open_positions == 2          # one master ticket, two slaves


@pytest.mark.asyncio
async def test_the_losing_trades_are_named_in_the_message(
    copier, mock_mt5, make_position, event_collector
):
    trip(copier, mock_mt5)

    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")

    event = [e for e in event_collector
             if e.event_type == EventType.DAILY_STOP_LIMIT][0]
    assert len(event.stop_details) == 4
    assert "NAS100" in event.stop_details[0]


# ==================== the count is refreshed by closes ====================

@pytest.mark.asyncio
async def test_a_close_re_reads_the_history(copier, mock_mt5, make_position):
    """The count moves when trades close, so that is when it is re-read —
    and from the master's own record, not from the slaves' realized PnL."""
    await copier._copy_new_position(make_position(ticket=555), 100, "TCP")
    mock_mt5.closed_trades.return_value = _losses(4)

    await copier._close_copied_positions(555, "TCP")

    assert copier.stops.count == 4
    assert copier.stops.tripped


@pytest.mark.asyncio
async def test_a_master_loss_nobody_copied_still_counts_at_that_close(
    copier, mock_mt5, event_collector
):
    """The gap this closes: the count was only re-read where slave positions
    were being closed, so a master position nothing mirrored — every slave
    skipped it, or it predates this session — went uncounted until the next
    copied close or the next restart. The limit has to land on the loss that
    reached it, not an hour later.
    """
    mock_mt5.closed_trades.return_value = _losses(4)

    await copier._close_copied_positions(4242, "Polling")   # never tracked

    assert copier.stops.count == 4
    assert [e for e in event_collector
            if e.event_type == EventType.DAILY_STOP_LIMIT]


@pytest.mark.asyncio
async def test_both_transports_reporting_one_close_re_read_it_once(
    copier, mock_mt5
):
    """EA push and polling both report every close. Reading the master's
    history twice for the same ticket buys nothing and costs a round trip on
    the path that has just been told a trade closed."""
    mock_mt5.closed_trades.return_value = _losses(1)

    await copier._close_copied_positions(4242, "TCP")
    await copier._close_copied_positions(4242, "Polling")

    assert mock_mt5.closed_trades.call_count == 1


@pytest.mark.asyncio
async def test_a_fresh_copier_starts_from_history_not_from_zero(
    copier_factory, make_config, mock_mt5
):
    """The bug this replaces: restarting reset the session counter."""
    mock_mt5.closed_trades.return_value = _losses(4)

    fresh = copier_factory(make_config(num_slaves=1))
    fresh.stops.refresh()

    assert fresh.stops.tripped


# ============= the UI freezes, then the app closes behind the wall ==========
#
# A trip raises a full-screen wall (see test_stop_wall.py) whose only button
# closes the app. The controls are frozen BEFORE it goes up, so nothing behind
# it is live while it is read — and the freeze is what these pin.
#
# These drive the lock against button stubs rather than real widgets:
# constructing a QWidget inside this suite (Qt is stubbed for everyone else)
# aborts the interpreter, and what needs pinning here is the LOGIC — which
# controls die, which survive, and that a later status change cannot undo it.


@pytest.fixture
def gui(monkeypatch):
    """The GUI modules against the REAL Qt.

    conftest stubs PyQt6 with MagicMock for the rest of the suite, and a class
    whose base is a mock cannot even be reached through __new__. Nothing here
    CONSTRUCTS a widget — building a real QWidget inside this process aborts
    the interpreter — the classes are only used unbound.
    """
    import importlib
    import sys

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in [m for m in list(sys.modules) if m.startswith("PyQt6")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    pytest.importorskip("PyQt6.QtWidgets")
    yield importlib.import_module("src.gui.trades_tab")
    for name in [m for m in list(sys.modules) if m.startswith("src.gui")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


class _Button:
    def __init__(self):
        self.enabled = True
        self.tooltip = ""

    def setEnabled(self, value):
        self.enabled = bool(value)

    def setToolTip(self, text):
        self.tooltip = text

    def isEnabled(self):
        return self.enabled


class _Banner:
    def __init__(self):
        self.text = ""
        self.visible = False

    def setText(self, text):
        self.text = text

    def setVisible(self, value):
        self.visible = bool(value)


def _tab(gui):
    """A TradesTab with only the pieces the lock touches (never constructed)."""
    tab = gui.TradesTab.__new__(gui.TradesTab)
    tab._locked_out = False
    tab.warmup_btn = _Button()
    tab.play_pause_btn = _Button()
    tab.stop_btn = _Button()
    tab.lockout_banner = _Banner()
    return tab


def test_the_start_controls_are_dead_after_a_trip(gui):
    tab = _tab(gui)
    tab.lock_out("limit reached")

    assert not tab.warmup_btn.isEnabled()
    assert not tab.play_pause_btn.isEnabled()


def test_stop_still_works_after_a_trip(gui):
    """Ending the session must always be possible."""
    tab = _tab(gui)
    tab.lock_out("limit reached")

    assert tab.stop_btn.isEnabled()


def test_the_frozen_buttons_say_why(gui):
    tab = _tab(gui)
    tab.lock_out("limit reached")

    assert "Daily stop limit" in tab.play_pause_btn.tooltip


def test_a_re_enable_is_undone_by_the_lock(gui):
    """update_status re-enables buttons per state and then calls _apply_lockout.

    This is the regression that would quietly undo the whole feature: stop the
    service, warm it again, and Start would come back.
    """
    tab = _tab(gui)
    tab.lock_out("limit reached")

    tab.warmup_btn.setEnabled(True)          # what a status change does
    tab.play_pause_btn.setEnabled(True)
    tab._apply_lockout()

    assert not tab.warmup_btn.isEnabled()
    assert not tab.play_pause_btn.isEnabled()


def test_update_status_applies_the_lock_last(gui):
    """The ordering the test above relies on, pinned in the source itself."""
    import inspect

    body = inspect.getsource(gui.TradesTab.update_status).rstrip().splitlines()

    assert body[-1].strip() == "self._apply_lockout()"


def test_the_banner_carries_the_state_after_the_dialog_is_gone(gui):
    tab = _tab(gui)
    tab.lock_out("DAILY STOP LIMIT — 4 losing trade(s) today")

    assert tab.lockout_banner.visible
    assert "DAILY STOP LIMIT" in tab.lockout_banner.text


def test_nothing_is_frozen_before_the_limit(gui):
    tab = _tab(gui)
    tab._apply_lockout()

    assert tab.warmup_btn.isEnabled()
    assert not tab.lockout_banner.visible


def test_the_trip_freezes_the_ui_and_then_ends_the_app(gui):
    """A trip locks the controls FIRST, then raises the wall that closes the app.

    The order is the assertion: the wall is modal and full-screen, so anything
    left live behind it would be live again for the instant between the wall
    closing and the process going — and the controls left live would be the
    ones that start copying.
    """
    import importlib
    import inspect

    main_window = importlib.import_module("src.gui.main_window")
    handler = inspect.getsource(main_window.MainWindow._on_daily_stop_limit)
    wall = inspect.getsource(main_window.MainWindow._show_stop_wall)

    assert handler.index("lock_out") < handler.index("_show_stop_wall")
    assert "self.close()" in wall
    assert "_force_quit = True" in wall


def test_the_exit_confirmation_is_skipped_when_the_limit_closes_the_app(gui):
    """Otherwise the wall's button raises "Stop and exit?" and No cancels it —
    the limit would be undone by the reflex it exists to outlast."""
    import importlib
    import inspect

    main_window = importlib.import_module("src.gui.main_window")
    source = inspect.getsource(main_window.MainWindow.closeEvent)

    assert "_force_quit" in source
    assert source.index("_force_quit") < source.index("Confirm Exit")
