"""The news gate in the copy path: opens are blocked, exits never are."""
import time
from pathlib import Path

import pytest

from src.events import EventType
from src.master import PositionChange
from src.news_calendar import NewsCalendar, NewsEvent


def _now_event(impact: str = "High") -> NewsEvent:
    """An event printing right now, so any window is open."""
    return NewsEvent(title="CPI m/m", currency="USD", impact=impact, ts=time.time())


def _calendar(events, tmp_path) -> NewsCalendar:
    """A calendar with a fixed event list and no network or shared cache."""
    cal = NewsCalendar(
        fetch=lambda: (events, "", 0.0), cache_path=tmp_path / "ff.json")
    cal.events = events
    return cal


def _armed_config(make_config, windows_per_slave):
    return make_config(
        num_slaves=len(windows_per_slave),
        slave_overrides=[{"news_windows_s": w} for w in windows_per_slave],
    )


class TestNewsBlock:

    @pytest.mark.asyncio
    async def test_armed_slave_is_skipped_while_an_unarmed_one_copies(
        self, copier_factory, make_config, make_position, event_collector, tmp_path
    ):
        """Blocking is per slave — one slave's window must not gate the others."""
        config = _armed_config(make_config, [{"High": 120.0}, {}])
        copier = copier_factory(config)
        copier.news = _calendar([_now_event()], tmp_path)

        pos = make_position(ticket=300)
        await copier.process_changes(PositionChange(
            new_positions=[pos], closed_tickets=[], modified_positions=[],
            source="Polling",
        ))

        opened = [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]
        assert [e.slave_name for e in opened] == ["Slave2"]

        blocked = [e for e in event_collector if e.event_type == EventType.NEWS_BLOCK]
        assert len(blocked) == 1
        assert blocked[0].slave_name == "Slave1"
        assert blocked[0].symbol == "EURUSD"
        assert "CPI m/m" in blocked[0].error
        assert blocked[0].news_window_s == 120.0     # seconds, as configured

    @pytest.mark.asyncio
    async def test_close_is_never_blocked(
        self, copier_factory, make_config, event_collector, tmp_path
    ):
        """Refusing to exit during news is worse than exiting. Closes run."""
        config = _armed_config(make_config, [{"High": 120.0}])
        copier = copier_factory(config)
        copier.news = _calendar([_now_event()], tmp_path)

        copier.tracker.add_position(
            master_ticket=301, symbol="EURUSD", order_type=0, master_volume=0.1,
        )
        copier.tracker.add_slave_position(
            master_ticket=301, slave_id="200000@TestServer",
            slave_ticket=900001, volume=0.1,
        )

        await copier.process_changes(PositionChange(
            new_positions=[], closed_tickets=[301], modified_positions=[],
            source="Polling",
        ))

        assert not copier.tracker.is_tracked(301)
        closed = [e for e in event_collector if e.event_type == EventType.TRADE_CLOSED]
        assert len(closed) == 1
        assert not [e for e in event_collector if e.event_type == EventType.NEWS_BLOCK]

    @pytest.mark.asyncio
    async def test_no_windows_configured_blocks_nothing(
        self, copier_factory, make_config, make_position, event_collector, tmp_path
    ):
        """Feature is inert until a slave is given a window."""
        config = _armed_config(make_config, [{}])
        copier = copier_factory(config)
        copier.news = _calendar([_now_event()], tmp_path)

        await copier.process_changes(PositionChange(
            new_positions=[make_position(ticket=302)], closed_tickets=[],
            modified_positions=[], source="Polling",
        ))

        assert [e.event_type for e in event_collector].count(EventType.TRADE_OPENED) == 1
        assert not [e for e in event_collector if e.event_type == EventType.NEWS_BLOCK]

    @pytest.mark.asyncio
    async def test_event_outside_the_window_does_not_block(
        self, copier_factory, make_config, make_position, event_collector, tmp_path
    ):
        config = _armed_config(make_config, [{"High": 120.0}])
        copier = copier_factory(config)
        far = NewsEvent(title="CPI m/m", currency="USD", impact="High",
                        ts=time.time() + 3600)
        copier.news = _calendar([far], tmp_path)

        await copier.process_changes(PositionChange(
            new_positions=[make_position(ticket=303)], closed_tickets=[],
            modified_positions=[], source="Polling",
        ))

        assert [e.event_type for e in event_collector].count(EventType.TRADE_OPENED) == 1
        assert not [e for e in event_collector if e.event_type == EventType.NEWS_BLOCK]

    @pytest.mark.asyncio
    async def test_impact_the_slave_ignores_does_not_block(
        self, copier_factory, make_config, make_position, event_collector, tmp_path
    ):
        """A slave armed only against High trades straight through a Low print."""
        config = _armed_config(make_config, [{"High": 120.0}])
        copier = copier_factory(config)
        copier.news = _calendar([_now_event(impact="Low")], tmp_path)

        await copier.process_changes(PositionChange(
            new_positions=[make_position(ticket=304)], closed_tickets=[],
            modified_positions=[], source="Polling",
        ))

        assert [e.event_type for e in event_collector].count(EventType.TRADE_OPENED) == 1

    @pytest.mark.asyncio
    async def test_no_calendar_means_no_gate(
        self, copier_factory, make_config, make_position, event_collector
    ):
        """News off (copier.news is None) must not change copy behaviour."""
        config = _armed_config(make_config, [{"High": 120.0}])
        copier = copier_factory(config)
        assert copier.news is None

        await copier.process_changes(PositionChange(
            new_positions=[make_position(ticket=305)], closed_tickets=[],
            modified_positions=[], source="Polling",
        ))

        assert [e.event_type for e in event_collector].count(EventType.TRADE_OPENED) == 1

    @pytest.mark.asyncio
    async def test_blocked_trade_is_not_retried_later(
        self, copier_factory, make_config, make_position, event_collector, tmp_path
    ):
        """Skipped means skipped: by the time the window closes the master's
        entry is stale, so a second poll of the same position must not copy it."""
        config = _armed_config(make_config, [{"High": 120.0}])
        copier = copier_factory(config)
        copier.news = _calendar([_now_event()], tmp_path)

        pos = make_position(ticket=306)
        change = PositionChange(new_positions=[pos], closed_tickets=[],
                                modified_positions=[], source="Polling")
        await copier.process_changes(change)

        # Window over, same position seen again by the poller.
        copier.news.events = []
        await copier.process_changes(change)

        assert not [e for e in event_collector if e.event_type == EventType.TRADE_OPENED]


class TestNewsCurrencyFilter:
    """The per-slave currency filter, through the copier's own gate.

    A slave trading US indices should not stop for Australian retail sales; the
    same calendar has to block one slave and not another.
    """

    def test_a_slave_ignores_an_event_in_a_currency_it_does_not_watch(self):
        from src.news_calendar import NewsCalendar, NewsEvent

        cal = NewsCalendar(cache_path=Path("/nonexistent/never"))
        now = time.time()
        cal.events = [NewsEvent("AU Jobs", "AUD", "High", now)]

        assert cal.blocked_for({"High": 300}, now=now, currencies=("USD",)) is None
        assert cal.blocked_for({"High": 300}, now=now, currencies=("AUD",)) is not None

    def test_one_calendar_blocks_one_slave_and_not_another(self):
        from src.news_calendar import NewsCalendar, NewsEvent

        cal = NewsCalendar(cache_path=Path("/nonexistent/never"))
        now = time.time()
        cal.events = [NewsEvent("CPI m/m", "USD", "High", now)]

        usd_slave = cal.blocked_for({"High": 300}, now=now, currencies=("USD",))
        jpy_slave = cal.blocked_for({"High": 300}, now=now, currencies=("JPY",))

        assert usd_slave is not None and usd_slave[0].title == "CPI m/m"
        assert jpy_slave is None

    def test_an_existing_slave_with_no_filter_is_unaffected(self):
        """Adding the field must not change behaviour for anyone who has not
        set it — empty means every currency."""
        from src.news_calendar import NewsCalendar, NewsEvent

        cal = NewsCalendar(cache_path=Path("/nonexistent/never"))
        now = time.time()
        cal.events = [NewsEvent("AU Jobs", "AUD", "High", now)]

        assert cal.blocked_for({"High": 300}, now=now) is not None


class TestNewsCurrencyConfig:
    """Config plumbing for the filter."""

    def test_slave_config_normalizes_currencies(self):
        from src.config import SlaveConfig

        s = SlaveConfig(name="S", login=1, password="p", server="X")
        assert s.news_currencies == []

        s2 = SlaveConfig(name="S", login=1, password="p", server="X",
                         news_currencies=["usd", " eur ", "usd", ""])
        assert s2.news_currencies == ["USD", "EUR"]

    def test_live_settings_carry_the_filter(self):
        from src.config import (
            Config, MasterConfig, SlaveConfig, SlaveTerminalConfig,
        )
        from src.live_settings import LiveSettings

        cfg = Config(
            master=MasterConfig(terminal_path="x"),
            slave_terminal=SlaveTerminalConfig(terminal_path="y"),
            symbol_mapping={},
            slaves=[SlaveConfig(name="S", login=1, password="p", server="X",
                                news_windows_s={"High": 124},
                                news_currencies=["usd"])],
        )
        LiveSettings.reset()
        live = LiveSettings()
        live.update_from_config(cfg)

        slave = live.get_slave("1@X")          # slave_id is login@server
        assert slave.news_currencies == ("USD",)
        assert slave.news_windows_s == {"High": 124.0}
        assert live.snapshot()["slaves"]["1@X"]["news_currencies"] == ["USD"]
