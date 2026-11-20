"""Tests for the ForexFactory news calendar and its blocking windows."""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import news_calendar as nc  # noqa: E402
from src.news_calendar import (  # noqa: E402
    IMPACTS,
    NewsCalendar,
    NewsEvent,
    active_block,
    events_today,
    fetch_calendar,
    load_cache,
    normalize_impact,
    normalize_windows,
    parse_events,
    save_cache,
)


def _blocked(events, windows, now):
    """active_block returns (event, minutes) — this keeps the event assertions terse."""
    got = active_block(events, windows, now)
    return got[0] if got else None


# 19:30 local on an arbitrary day, as the example in the spec.
def _at(hh: int, mm: int, day_offset: int = 0) -> float:
    base = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (base + timedelta(days=day_offset)).timestamp()


def _event(ts: float, impact: str = "High", currency: str = "USD",
           title: str = "CPI m/m") -> NewsEvent:
    return NewsEvent(title=title, currency=currency, impact=impact, ts=ts)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(body):
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    return lambda *a, **k: _FakeResponse(payload)


# ------------------------------------------------------------------ parsing

def test_offset_aware_date_becomes_correct_epoch():
    """The feed's -04:00 offset must be honoured, not read as host-local."""
    events = parse_events([{
        "title": "CPI m/m", "country": "AUD", "impact": "High",
        "date": "2026-08-25T21:30:00-04:00",
    }])
    assert len(events) == 1
    expected = datetime(2026, 8, 26, 1, 30, tzinfo=timezone.utc).timestamp()
    assert events[0].ts == expected
    assert events[0].currency == "AUD"


def test_rows_without_a_usable_date_or_impact_are_dropped():
    """A mis-dated event would block at the wrong minute — better to skip it."""
    events = parse_events([
        {"title": "A", "impact": "High", "date": "not-a-date"},
        {"title": "B", "impact": "High"},                      # no date
        {"title": "C", "impact": "", "date": "2026-08-25T21:30:00-04:00"},
        {"title": "D", "impact": "Bogus", "date": "2026-08-25T21:30:00-04:00"},
        "not-a-dict",
    ])
    assert events == []


def test_holiday_and_all_currency_survive():
    events = parse_events([{
        "title": "Bank Holiday", "country": "All", "impact": "Holiday",
        "date": "2026-08-25T00:00:00-04:00",
    }])
    assert events[0].impact == "Holiday"
    assert events[0].currency == "All"


def test_events_come_back_sorted():
    events = parse_events([
        {"title": "late", "impact": "Low", "date": "2026-08-25T23:00:00-04:00"},
        {"title": "early", "impact": "Low", "date": "2026-08-25T01:00:00-04:00"},
    ])
    assert [e.title for e in events] == ["early", "late"]


@pytest.mark.parametrize("raw,expected", [
    ("High", "High"), ("high", "High"), ("  MEDIUM ", "Medium"),
    ("Holiday", "Holiday"), ("", ""), (None, ""), ("critical", ""),
])
def test_impact_normalization(raw, expected):
    assert normalize_impact(raw) == expected


# ------------------------------------------------------------------- fetch

def test_fetch_parses_a_live_shaped_payload():
    body = [{"title": "NFP", "country": "USD", "impact": "High",
             "date": "2026-08-25T08:30:00-04:00"}]
    with patch("urllib.request.urlopen", _fake_urlopen(body)):
        events, error, retry = fetch_calendar()
    assert error == ""
    assert [e.title for e in events] == ["NFP"]
    assert retry == 0.0


def test_fetch_never_raises_on_network_failure():
    def boom(*a, **k):
        raise OSError("no route to host")

    with patch("urllib.request.urlopen", boom):
        events, error, _retry = fetch_calendar()
    assert events == []
    assert "feed error" in error or "unreachable" in error


def test_fetch_reports_an_empty_calendar_as_an_error():
    with patch("urllib.request.urlopen", _fake_urlopen([])):
        events, error, _retry = fetch_calendar()
    assert events == []
    assert "no usable events" in error


# ------------------------------------------------------------------ windows

def test_the_window_is_exactly_what_was_configured():
    """124s on 19:30 blocks 19:27:56 -> 19:32:04, and not a tick more.

    No padding is added anywhere. The old version widened every window by 5s a
    side "for feed rounding", which meant the guard on screen was never the
    guard that ran — a "2m" setting actually blocked for 4m10s. If 4 extra
    seconds are wanted, 124 is how you ask for them.
    """
    ts = _at(19, 30)
    events = [_event(ts)]
    windows = {"High": 124}

    assert _blocked(events, windows, ts) is not None            # the print
    assert _blocked(events, windows, ts - 124.0) is not None    # 19:27:56
    assert _blocked(events, windows, ts - 124.1) is None        # a tick before
    assert _blocked(events, windows, ts + 124.0) is not None    # 19:32:04
    assert _blocked(events, windows, ts + 124.1) is None        # a tick after


def test_windows_are_seconds_not_minutes():
    ts = _at(19, 30)
    events = [_event(ts)]

    assert _blocked(events, {"High": 120}, ts + 119) is not None
    assert _blocked(events, {"High": 120}, ts + 121) is None


def test_no_windows_means_no_restrictions():
    ts = _at(19, 30)
    events = [_event(ts)]
    assert _blocked(events, {}, ts) is None
    assert _blocked(events, {"High": 0}, ts) is None
    assert _blocked(events, {"Medium": 300}, ts) is None  # wrong impact


def test_unknown_impact_key_never_blocks():
    ts = _at(19, 30)
    assert _blocked([_event(ts)], {"Critical": 600}, ts) is None


def test_earliest_matching_event_wins():
    """The toast must name the event that actually opened the block."""
    first = _event(_at(19, 30), title="CPI")
    second = _event(_at(19, 31), title="Retail Sales")
    got = _blocked([second, first], {"High": 120}, _at(19, 31))
    assert got.title == "CPI"


def test_different_impacts_get_different_windows():
    high = _event(_at(19, 30), impact="High")
    low = _event(_at(20, 30), impact="Low")
    windows = {"High": 120, "Low": 600}
    assert _blocked([high, low], windows, _at(19, 31)) is high
    assert _blocked([high, low], windows, _at(20, 35)) is low
    assert _blocked([high, low], windows, _at(20, 45)) is None


@pytest.mark.parametrize("raw,expected", [
    ({"High": 124}, {"High": 124.0}),
    ({"high": "2.5"}, {"High": 2.5}),
    ({"High": 0, "Low": -1}, {}),
    ({"Bogus": 5}, {}),
    ({"High": "abc"}, {}),
    (None, {}),
])
def test_window_normalization(raw, expected):
    assert normalize_windows(raw) == expected


def test_legacy_minutes_are_converted_not_reinterpreted():
    """A file written before the switch holds minutes.

    Reading those as seconds would turn a 2-minute guard into a 2-second one —
    protection all but removed, and silently.
    """
    assert nc.minutes_to_seconds({"High": 2, "Low": 0.5}) == {
        "High": 120.0, "Low": 30.0,
    }


def test_events_today_filters_to_the_local_day():
    today = _event(_at(12, 0))
    tomorrow = _event(_at(12, 0, day_offset=1))
    yesterday = _event(_at(12, 0, day_offset=-1))
    got = events_today([yesterday, today, tomorrow], time.time())
    assert got == [today]


# -------------------------------------------------------------------- cache

def test_cache_round_trip(tmp_path):
    path = tmp_path / "ff.json"
    events = [_event(_at(19, 30)), _event(_at(20, 0), impact="Low")]
    save_cache(events, path)
    loaded, fetched_at = load_cache(path)
    assert loaded == events
    assert fetched_at > 0


def test_missing_cache_is_not_an_error(tmp_path):
    assert load_cache(tmp_path / "nope.json") == ([], 0.0)


def test_calendar_seeds_itself_from_cache_and_marks_it_stale(tmp_path):
    path = tmp_path / "ff.json"
    save_cache([_event(_at(19, 30))], path)
    cal = NewsCalendar(fetch=lambda: ([], "down", 0.0), cache_path=path)
    assert len(cal.events) == 1
    assert cal.stale is True
    # A stale calendar still blocks — the events didn't expire, the site did.
    assert cal.blocked_for({"High": 2}, _at(19, 30)) is not None


def test_failed_refresh_keeps_the_previous_calendar(tmp_path):
    path = tmp_path / "ff.json"
    events = [_event(_at(19, 30))]
    cal = NewsCalendar(fetch=lambda: (events, "", 0.0), cache_path=path)
    assert cal.refresh() == ""
    assert cal.stale is False

    cal._fetch = lambda: ([], "ForexFactory feed HTTP 503", 0.0)
    cal._last_attempt = 0.0
    error = cal.refresh()
    assert "503" in error
    assert cal.events == events      # still enforced
    assert cal.stale is True


def test_no_cache_and_no_feed_blocks_nothing(tmp_path):
    cal = NewsCalendar(fetch=lambda: ([], "down", 0.0), cache_path=tmp_path / "ff.json")
    assert cal.refresh() == "down"
    assert cal.events == []
    assert cal.blocked_for({"High": 2}, _at(19, 30)) is None


def test_refresh_writes_the_cache(tmp_path):
    path = tmp_path / "ff.json"
    cal = NewsCalendar(
        fetch=lambda: ([_event(_at(19, 30))], "", 0.0), cache_path=path)
    cal.refresh()
    assert path.exists()
    assert len(load_cache(path)[0]) == 1


# ------------------------------------------------------------ service shape

def test_refresh_is_throttled(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return [_event(_at(19, 30))], "", 0.0

    cal = NewsCalendar(refresh_sec=900, fetch=fetch, cache_path=tmp_path / "ff.json")
    assert cal.due() is True
    cal.refresh()
    assert cal.due() is False
    assert len(calls) == 1


def test_refresh_floor_protects_the_feed(tmp_path):
    """The floor is what the DATA justifies, not what a poll loop can manage.

    The file is this week's calendar. Asking for it every minute earns HTTP 429
    from faireconomy.media — observed — so no config value can go below the
    interval the data changes on.
    """
    cal = NewsCalendar(refresh_sec=1, cache_path=tmp_path / "ff.json")
    assert cal.refresh_sec == nc.MIN_REFRESH_S == 900.0

    cal.set_refresh(30)
    assert cal.refresh_sec == 900.0


def test_a_fresh_cache_defers_the_first_fetch(tmp_path):
    """A restart is not a reason to re-fetch a file that changes a few times
    a day. A morning of restarts was a morning of requests."""
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)

    cal = NewsCalendar(cache_path=path)

    assert cal.events
    assert cal.due() is False


def test_a_stale_cache_does_not_defer_it(tmp_path):
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)
    # Backdate the cache past the refresh interval.
    payload = json.loads(path.read_text())
    old = time.time() - nc.MIN_REFRESH_S - 1
    payload["fetched_at"] = payload["last_attempt"] = old
    path.write_text(json.dumps(payload))

    assert NewsCalendar(cache_path=path).due() is True


def test_the_cooldown_outlives_the_process(tmp_path):
    """A restart must inherit the backoff, not start the cycle over.

    This is what kept the 429 coming back after the first fix: the backoff lived
    in memory, so every restart asked again immediately — and a development
    session is a lot of restarts.
    """
    path = tmp_path / "ff.json"
    first = NewsCalendar(fetch=lambda: ([], "HTTP 429", 900.0), cache_path=path)
    now = time.time()
    first.refresh(now=now)
    inherited = first._backoff_s
    assert inherited > 0

    # A brand-new process, same machine.
    second = NewsCalendar(fetch=lambda: ([], "HTTP 429", 900.0), cache_path=path)

    assert second._backoff_s == inherited
    assert second.due(now=now + 60) is False
    assert second.due(now=now + inherited + 1) is True


def test_a_refusal_is_remembered_even_with_no_events_to_cache(tmp_path):
    """A cold start that gets refused has nothing to cache but everything to
    remember — that is precisely the case the file has to cover."""
    path = tmp_path / "ff.json"
    cal = NewsCalendar(fetch=lambda: ([], "HTTP 429", 0.0), cache_path=path)
    cal.refresh()

    assert path.exists()
    last_attempt, backoff = nc.load_state(path)
    assert last_attempt > 0
    assert backoff == cal._backoff_s > 0


def test_a_successful_fetch_clears_the_cooldown_on_disk(tmp_path):
    path = tmp_path / "ff.json"
    answers = [([], "HTTP 429", 0.0), ([nc.NewsEvent("CPI", "USD", "High", 1.0)], "", 0.0)]
    cal = NewsCalendar(fetch=lambda: answers.pop(0), cache_path=path)
    cal.refresh()
    cal.refresh()

    assert nc.load_state(path)[1] == 0.0
    assert NewsCalendar(fetch=lambda: ([], "", 0.0), cache_path=path)._backoff_s == 0.0


def test_a_refusal_backs_off_instead_of_retrying_on_schedule(tmp_path):
    """Retrying a 429 on the normal cadence keeps earning 429s."""
    calls = []

    def _fetch():
        calls.append(1)
        return [], "ForexFactory feed HTTP 429", 0.0


    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)
    cal = NewsCalendar(fetch=_fetch, cache_path=path)
    cal._last_attempt = 0.0
    now = time.time()
    assert cal.refresh(now=now)
    assert cal._backoff_s == nc.BACKOFF_START_S

    # Due again by the normal interval, but not by the backoff.
    assert cal.due(now=now + nc.MIN_REFRESH_S + 1) is False
    assert cal.due(now=now + nc.BACKOFF_START_S + 1) is True

    cal.refresh(now=now + nc.BACKOFF_START_S + 1)
    assert cal._backoff_s == nc.BACKOFF_START_S * 2     # doubling


def test_backoff_is_capped():
    cal = NewsCalendar(fetch=lambda: ([], "down", 0.0), cache_path=Path("/nonexistent/x"))
    now = time.time()
    for i in range(20):
        cal.refresh(now=now + i * nc.BACKOFF_MAX_S * 2)
    assert cal._backoff_s == nc.BACKOFF_MAX_S


def test_a_good_fetch_clears_the_backoff(tmp_path):
    answers = [([], "down", 0.0), ([nc.NewsEvent("CPI", "USD", "High", 1.0)], "", 0.0)]
    cal = NewsCalendar(
        fetch=lambda: answers.pop(0), cache_path=tmp_path / "ff.json"
    )
    cal.refresh()
    assert cal._backoff_s > 0
    cal.refresh()
    assert cal._backoff_s == 0.0


def test_rate_limiting_is_named_in_the_error(monkeypatch):
    """"HTTP 429" alone reads as the server's problem. It is ours."""
    import urllib.error

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            nc.FEED_URL, 429, "Too Many Requests", {"Retry-After": "600"}, None
        )

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    _events, error, retry = nc.fetch_calendar()

    assert "429" in error and "rate limited" in error
    assert "too soon" in error
    assert retry == 600.0


def test_error_edge_reports_once(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    assert cal.take_error_edge("down") == "down"
    assert cal.take_error_edge("down") == ""
    assert cal.take_error_edge("") == ""      # recovery clears it
    assert cal.take_error_edge("down") == "down"


def test_armed_today_lists_only_events_some_slave_blocks(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        _event(_at(19, 30), impact="High", title="CPI"),
        _event(_at(20, 30), impact="Low", title="Sentiment"),
        _event(_at(12, 0, day_offset=1), impact="High", title="Tomorrow"),
    ]
    rows = cal.armed_today([("A", {"High": 120}, ()), ("B", {}, ())])
    assert [r[3] for r in rows] == ["CPI"]
    assert rows[0][4] == 120.0
    assert rows[0][5] == (("A", 120.0),)


def test_armed_today_uses_the_widest_window_across_slaves(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [_event(_at(19, 30), impact="High")]
    rows = cal.armed_today([("A", {"High": 120}, ()), ("B", {"High": 300}, ())])
    assert rows[0][4] == 300.0                      # widest, for the summary
    # ...but every slave's own window is carried, because two slaves guarding
    # the same event differently is the normal case and one merged number is
    # wrong for all but one of them.
    assert rows[0][5] == (("A", 120.0), ("B", 300.0))


def test_armed_today_is_empty_when_no_slave_is_armed(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [_event(_at(19, 30))]
    assert cal.armed_today([("A", {}, ()), ("B", {}, ())]) == []


# ----------------------------------------------------------- config plumbing

def test_slave_config_normalizes_news_windows():
    from src.config import SlaveConfig

    s = SlaveConfig(name="S", login=1, password="p", server="X")
    assert s.news_windows_s == {}

    s2 = SlaveConfig(name="S", login=1, password="p", server="X",
                     news_windows_s={"high": 124, "Low": 0, "bogus": 9})
    assert s2.news_windows_s == {"High": 124.0}


def test_a_legacy_minutes_config_is_migrated_on_load():
    """An old config.yaml holds minutes. Read as seconds it would leave a
    2-minute guard blocking for two seconds."""
    from src.config import SlaveConfig

    s = SlaveConfig(name="S", login=1, password="p", server="X",
                    news_windows={"High": 2, "Low": 0.5})

    assert s.news_windows_s == {"High": 120.0, "Low": 30.0}
    assert s.news_windows == {}          # migration is one-way


def test_the_new_field_wins_when_both_are_present():
    """Once seconds exist they are the truth; the legacy key is ignored."""
    from src.config import SlaveConfig

    s = SlaveConfig(name="S", login=1, password="p", server="X",
                    news_windows={"High": 99}, news_windows_s={"High": 124})

    assert s.news_windows_s == {"High": 124.0}


def test_live_settings_carries_news_windows():
    from src.live_settings import LiveSettings, get_live_settings

    LiveSettings.reset()
    settings = get_live_settings()
    settings.update_from_dict({
        "news": {"enabled": True, "refresh_sec": 600},
        "slaves": [{"name": "S", "login": 7, "server": "B",
                    "news_windows_s": {"high": 124, "bogus": 3}}],
    })
    assert settings.news == {"enabled": True, "refresh_sec": 600}
    assert settings.get_slave("7@B").news_windows_s == {"High": 124.0}
    LiveSettings.reset()


def test_impacts_are_the_four_the_feed_uses():
    assert IMPACTS == ("High", "Medium", "Low", "Holiday")


# ==================== the status the UI reports ====================

def test_news_update_carries_whether_blocking_is_on(monkeypatch):
    """An empty table is the normal state on a quiet day.

    Reporting "armed?" from the row count called an enabled, armed calendar
    switched off for most of the week.
    """
    from src.events import UIEventQueue, emit_news_update

    UIEventQueue.reset()
    emit_news_update((), stale=False, enabled=True, armed=True)
    (ev,) = UIEventQueue().poll_all()

    assert ev.news_rows == ()
    assert ev.news_enabled is True
    assert ev.news_armed is True


def test_news_update_can_say_blocking_is_off():
    from src.events import UIEventQueue, emit_news_update

    UIEventQueue.reset()
    emit_news_update((), enabled=False, armed=False)
    (ev,) = UIEventQueue().poll_all()

    assert ev.news_enabled is False
    assert ev.news_armed is False


# ==================== the manual Test button ====================
#
# It called `fetch_calendar` directly, so it honoured no interval, no
# cross-restart cooldown and no backoff. Pressing it a few times while setting
# news up burned the rate-limit allowance, and the service — whose own cooldown
# was working — then met a 429 it had not earned. This is why the 429 outlived
# three fixes aimed at the service loop.

def test_probe_does_not_fetch_when_a_fetch_is_not_due(tmp_path):
    calls = []
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)

    cal = NewsCalendar(fetch=lambda: (calls.append(1), ([], "boom", 0.0))[1],
                       cache_path=path)
    events, error, note = cal.probe()

    assert calls == []                       # the network was never touched
    assert [e.title for e in events] == ["CPI"]
    assert error == ""
    assert "next download allowed in" in note


def test_probe_fetches_when_it_is_allowed(tmp_path):
    fresh = [nc.NewsEvent("NFP", "USD", "High", time.time() + 600)]
    cal = NewsCalendar(fetch=lambda: (fresh, "", 0.0), cache_path=tmp_path / "ff.json")

    events, error, note = cal.probe()

    assert [e.title for e in events] == ["NFP"]
    assert error == "" and note == ""


def test_probe_says_so_when_it_has_nothing_and_may_not_ask(tmp_path):
    """A refused cold start: no events to show, and asking again is not allowed."""
    path = tmp_path / "ff.json"
    NewsCalendar(fetch=lambda: ([], "HTTP 429", 0.0), cache_path=path).refresh()

    events, error, note = NewsCalendar(
        fetch=lambda: ([], "HTTP 429", 0.0), cache_path=path
    ).probe()

    assert events == []
    assert error == ""
    assert "next download allowed in" in note


def test_probe_counts_as_an_attempt(tmp_path):
    """Otherwise Test and the service each keep their own allowance."""
    path = tmp_path / "ff.json"
    cal = NewsCalendar(
        fetch=lambda: ([nc.NewsEvent("CPI", "USD", "High", 1.0)], "", 0.0),
        cache_path=path,
    )
    cal.probe()

    assert NewsCalendar(cache_path=path).due() is False


def test_next_fetch_in_counts_down(tmp_path):
    cal = NewsCalendar(
        fetch=lambda: ([nc.NewsEvent("CPI", "USD", "High", 1.0)], "", 0.0),
        cache_path=tmp_path / "ff.json",
    )
    now = time.time()
    cal.refresh(now=now)

    assert cal.next_fetch_in(now=now) == pytest.approx(nc.MIN_REFRESH_S)
    assert cal.next_fetch_in(now=now + nc.MIN_REFRESH_S) == 0.0


# ============ the server's own interval beats our guess ============
#
# Measured live (2026-08-27): the feed answers one request, then returns 429 to
# the next one 0.1s later with Retry-After 300 — Cloudflare enforcing roughly
# one request per five minutes per IP. So the wait that matters is the one the
# server states. Sitting out the escalating 30-minute guess instead left the app
# with no calendar at all for half an hour, for no reason.

def test_with_no_calendar_the_servers_own_time_is_used(tmp_path):
    """Nothing cached means blocking is not running at all.

    Every extra minute of waiting is a minute of unprotected copying, so the
    interval floor — which exists to stop pointless re-fetching of data we
    already hold — must not apply. Observed live: the server asked for 119s and
    the app sat out 15 minutes with no calendar.
    """
    cal = NewsCalendar(
        fetch=lambda: ([], "HTTP 429", 119.0), cache_path=tmp_path / "ff.json"
    )
    now = time.time()
    cal.refresh(now=now)

    assert cal._backoff_s == pytest.approx(124.0)      # 119 + a small margin
    assert cal.due(now=now + 130) is True


def test_with_a_calendar_in_hand_the_full_interval_still_applies(tmp_path):
    """Refreshing is optional then, and asking sooner is what got us refused."""
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)
    cal = NewsCalendar(fetch=lambda: ([], "HTTP 429", 119.0), cache_path=path)
    cal._last_attempt = 0.0                            # let it try
    now = time.time()
    cal.refresh(now=now)

    assert cal._backoff_s == nc.MIN_REFRESH_S
    assert cal.due(now=now + 130) is False


def test_a_long_retry_after_is_obeyed_in_full(tmp_path):
    cal = NewsCalendar(
        fetch=lambda: ([], "HTTP 429", 5400.0), cache_path=tmp_path / "ff.json"
    )
    now = time.time()
    cal.refresh(now=now)

    assert cal._backoff_s == pytest.approx(5405.0)
    assert cal.due(now=now + 5400) is False
    assert cal.due(now=now + 5410) is True


def test_retry_after_never_shortens_the_interval_when_we_hold_a_calendar(tmp_path):
    """Honouring it must not become a licence to poll faster than we would."""
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)
    cal = NewsCalendar(fetch=lambda: ([], "HTTP 429", 5.0), cache_path=path)
    cal._last_attempt = 0.0
    now = time.time()
    cal.refresh(now=now)

    assert cal._backoff_s == nc.MIN_REFRESH_S
    assert cal.due(now=now + 10) is False


def test_without_a_hint_the_backoff_still_escalates(tmp_path):
    path = tmp_path / "ff.json"
    nc.save_cache([nc.NewsEvent("CPI", "USD", "High", time.time() + 600)], path)
    cal = NewsCalendar(fetch=lambda: ([], "unreachable", 0.0), cache_path=path)
    cal._last_attempt = 0.0
    cal.refresh()
    assert cal._backoff_s == nc.BACKOFF_START_S
    cal.refresh()
    assert cal._backoff_s == nc.BACKOFF_START_S * 2


def test_without_a_hint_and_with_nothing_cached_it_retries_sooner(tmp_path):
    """An outage with no calendar must not start at half an hour."""
    cal = NewsCalendar(
        fetch=lambda: ([], "unreachable", 0.0), cache_path=tmp_path / "ff.json"
    )
    cal.refresh()
    assert cal._backoff_s == nc.MIN_REFRESH_S


@pytest.mark.parametrize("raw,expected", [
    ("300", 300.0),
    ("0", 0.0),
    ("", 0.0),
    ("not-a-number", 0.0),
    (None, 0.0),
])
def test_retry_after_parsing(raw, expected):
    headers = {} if raw is None else {"Retry-After": raw}
    assert nc._retry_after_seconds(headers) == expected


def test_retry_after_accepts_an_http_date():
    """The header may be a date; an unparseable one must degrade, not raise."""
    from email.utils import formatdate

    when = formatdate(time.time() + 120, usegmt=True)
    assert nc._retry_after_seconds({"Retry-After": when}) == pytest.approx(120, abs=5)
    assert nc._retry_after_seconds({"Retry-After": "Mon, 99 Xxx"}) == 0.0


# ==================== per-currency filtering ====================
#
# A slave trading NAS100 has no reason to stop for Australian retail sales, and
# a calendar week is mostly events that cannot touch a given account. The filter
# is per slave and is checked TOGETHER with the impact window.

@pytest.mark.parametrize("raw,expected", [
    (["usd", "EUR"], ("USD", "EUR")),
    ("usd", ("USD",)),
    (["USD", "usd", " usd "], ("USD",)),      # deduped, order kept
    ([" ", "", None], ()),
    (None, ()),
    ([], ()),
])
def test_currency_normalization(raw, expected):
    assert nc.normalize_currencies(raw) == expected


def test_no_filter_means_every_currency():
    """Empty is ALL, not none — adding this field must not silently switch
    protection off for every config that predates it."""
    assert nc.currency_matches("USD", ()) is True
    assert nc.currency_matches("AUD", ()) is True


def test_a_filter_admits_only_what_it_lists():
    assert nc.currency_matches("USD", ("USD",)) is True
    assert nc.currency_matches("aud", ("USD",)) is False


def test_global_events_are_opt_in_like_any_other_code():
    """"All" is the feed's code for events filed under no currency.

    It used to match every filter automatically, on the reasoning that these
    move everything — but that made a USD-only slave stop for a Japanese bank
    holiday with no way to say no. A filter that admits what you did not ask
    for is not a filter.
    """
    usd = nc.normalize_currencies(["USD"])
    usd_and_global = nc.normalize_currencies(["USD", "All"])

    assert nc.currency_matches("All", usd) is False
    assert nc.currency_matches("All", usd_and_global) is True
    assert nc.currency_matches("All", ()) is True        # no filter = everything


def test_the_filter_must_arrive_normalized():
    """Hoisted to the caller: this runs per event, per slave, per cycle.

    Stated as a test because the failure is silent — a raw config value would
    simply never match.
    """
    assert nc.currency_matches("USD", nc.normalize_currencies(["usd"])) is True
    assert nc.currency_matches("USD", ("usd",)) is False     # not normalized


def test_block_ignores_an_event_in_another_currency():
    ts = _at(19, 30)
    events = [_event(ts, impact="High", currency="AUD")]

    assert active_block(events, {"High": 300}, ts, currencies=("USD",)) is None
    assert active_block(events, {"High": 300}, ts, currencies=()) is not None
    assert active_block(events, {"High": 300}, ts, currencies=("AUD",)) is not None


def test_a_global_event_is_ignored_unless_all_is_ticked():
    ts = _at(19, 30)
    events = [_event(ts, impact="Holiday", currency="All", title="Bank Holiday")]

    assert active_block(events, {"Holiday": 1800}, ts, currencies=("USD",)) is None

    got = active_block(events, {"Holiday": 1800}, ts, currencies=("USD", "All"))
    assert got is not None and got[0].title == "Bank Holiday"


def test_the_earliest_MATCHING_event_wins_not_the_earliest_event():
    """The toast must name the event that actually opened the block."""
    ignored = _event(_at(19, 30), currency="AUD", title="AUD first")
    blocking = _event(_at(19, 31), currency="USD", title="USD second")

    got = active_block([ignored, blocking], {"High": 300}, _at(19, 31),
                       currencies=("USD",))
    assert got[0].title == "USD second"


def test_blocked_for_passes_the_currency_filter_through(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    ts = _at(19, 30)
    cal.events = [_event(ts, impact="High", currency="AUD")]

    assert cal.blocked_for({"High": 300}, now=ts, currencies=("USD",)) is None
    assert cal.blocked_for({"High": 300}, now=ts) is not None      # default: all


# ============ the main-tab table lists only what will actually block ============

def test_armed_today_skips_currencies_no_slave_watches(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        _event(_at(19, 30), impact="High", currency="USD", title="CPI"),
        _event(_at(20, 30), impact="High", currency="AUD", title="AU Jobs"),
    ]

    rows = cal.armed_today([("A", {"High": 120}, ("USD",))])

    assert [r[3] for r in rows] == ["CPI"]


def test_armed_today_keeps_a_row_wanted_by_any_one_slave(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        _event(_at(19, 30), impact="High", currency="USD", title="CPI"),
        _event(_at(20, 30), impact="High", currency="JPY", title="BOJ"),
    ]

    rows = cal.armed_today([("A", {"High": 120}, ("USD",)), ("B", {"High": 180}, ("JPY",))])

    assert sorted(r[3] for r in rows) == ["BOJ", "CPI"]


def test_impact_and_currency_are_checked_together_per_slave(tmp_path):
    """The reason the rules cannot be merged into one impact set and one
    currency set: that would list a High EUR event for a config whose only EUR
    slave watches Low impacts — a row that can never block anything, in a table
    whose whole purpose is to say what will.
    """
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [_event(_at(19, 30), impact="High", currency="EUR", title="ECB")]

    rows = cal.armed_today([
        ("A", {"High": 120}, ("USD",)),   # watches High, but not EUR
        ("B", {"Low": 300}, ("EUR",)),    # watches EUR, but not High
    ])

    assert rows == []


def test_armed_today_widest_window_only_counts_slaves_that_care(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [_event(_at(19, 30), impact="High", currency="USD")]

    rows = cal.armed_today([
        ("A", {"High": 120}, ("USD",)),
        ("B", {"High": 1800}, ("JPY",)),  # a wide window, but not for this event
    ])

    assert rows[0][4] == 120.0
    assert rows[0][5] == (("A", 120.0),)


def test_armed_today_drops_a_global_event_a_slave_did_not_ask_for(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [_event(_at(19, 30), impact="Holiday", currency="All",
                         title="Bank Holiday")]

    assert cal.armed_today([("A", {"Holiday": 3600}, ("USD",))]) == []

    rows = cal.armed_today([("A", {"Holiday": 3600}, ("USD", "All"))])
    assert [r[3] for r in rows] == ["Bank Holiday"]


# ==================== the cached week, and the day rolling over ====================

def test_the_cache_holds_the_whole_week_not_just_today(tmp_path):
    """One weekly file is what the feed publishes; "today" is filtered locally."""
    path = tmp_path / "ff.json"
    week = [
        _event(_at(12, 0, day_offset=-1), title="Yesterday"),
        _event(_at(12, 0), title="Today"),
        _event(_at(12, 0, day_offset=1), title="Tomorrow"),
    ]
    cal = NewsCalendar(fetch=lambda: (week, "", 0.0), cache_path=path)
    cal.refresh()

    reloaded, _fetched_at = load_cache(path)
    assert sorted(e.title for e in reloaded) == ["Today", "Tomorrow", "Yesterday"]


def test_the_day_rolls_over_without_refetching(tmp_path):
    """Midnight changes the rows, not the data.

    `events_today` re-filters the cached week against the current clock on
    every cycle, so no request, no restart and no cache write is involved in
    the date changing.
    """
    calls = []
    week = [
        _event(_at(23, 30, day_offset=-1), title="Late yesterday"),
        _event(_at(0, 30), title="Early today"),
    ]

    def _fetch():
        calls.append(1)
        return week, "", 0.0

    cal = NewsCalendar(fetch=_fetch, cache_path=tmp_path / "ff.json")
    cal.refresh()

    before = cal.armed_today([("A", {"High": 300}, ())], now=_at(23, 45, day_offset=-1))
    after = cal.armed_today([("A", {"High": 300}, ())], now=_at(0, 15))

    assert [r[3] for r in before] == ["Late yesterday"]
    assert [r[3] for r in after] == ["Early today"]
    assert len(calls) == 1              # one fetch covered both days


# ==================== midnight ====================
#
# Two separate things happen at 00:00, and both were wrong:
#   - the calendar in hand was downloaded yesterday, for yesterday's idea of
#     today's schedule;
#   - a window that started before midnight kept blocking while disappearing
#     from the table.

def _day(y, mo, d, h=0, mi=0) -> float:
    """Local-clock epoch, because the day boundary that matters is local."""
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_a_new_local_day_forces_a_reload(tmp_path):
    """The feed revises times, adds events and moves them.

    Enforcing today from a file fetched yesterday means enforcing yesterday's
    idea of today, and the cost of being wrong is a copy taken into a print.
    """
    cal = NewsCalendar(
        fetch=lambda: ([_event(_at(19, 30))], "", 0.0),
        cache_path=tmp_path / "ff.json",
    )
    late = _day(2026, 8, 27, 23, 0)
    cal.refresh(now=late)

    assert cal.due(now=late + 60) is False          # a minute later, same day
    assert cal.due(now=_day(2026, 8, 28, 0, 0)) is True     # midnight exactly
    assert cal.due(now=_day(2026, 8, 28, 0, 1)) is True


def test_the_day_rollover_does_not_override_a_refusal(tmp_path):
    """A backoff is absolute. Midnight is no reason to hammer a 429."""
    cal = NewsCalendar(
        fetch=lambda: ([], "HTTP 429", 3600.0), cache_path=tmp_path / "ff.json"
    )
    late = _day(2026, 8, 27, 23, 30)
    cal.refresh(now=late)

    assert cal.due(now=_day(2026, 8, 28, 0, 0)) is False
    # ...but once the refusal expires, the stale day makes it due at once
    # rather than waiting out the normal interval on top.
    assert cal.due(now=late + 3610) is True     # backoff is retry_after + margin


def test_next_fetch_in_is_zero_when_the_day_has_rolled(tmp_path):
    cal = NewsCalendar(
        fetch=lambda: ([_event(_at(19, 30))], "", 0.0),
        cache_path=tmp_path / "ff.json",
    )
    cal.refresh(now=_day(2026, 8, 27, 23, 59))

    assert cal.next_fetch_in(now=_day(2026, 8, 28, 0, 0)) == 0.0


def test_a_window_open_across_midnight_stays_on_the_table(tmp_path):
    """Otherwise the gate refuses copies with nothing on screen to explain it."""
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        NewsEvent("Late print", "USD", "High", _day(2026, 8, 27, 23, 50)),
        NewsEvent("Today noon", "USD", "High", _day(2026, 8, 28, 12, 0)),
    ]
    rules = [("A", {"High": 1800}, ())]
    now = _day(2026, 8, 28, 0, 10)          # 20 min after the late print

    rows = cal.armed_today(rules, now=now)

    assert [r[3] for r in rows] == ["Late print", "Today noon"]
    # ...and the gate agrees, which is the whole point.
    assert cal.blocked_for({"High": 1800}, now=now)[0].title == "Late print"


def test_yesterdays_event_drops_off_once_its_window_closes(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        NewsEvent("Late print", "USD", "High", _day(2026, 8, 27, 23, 50)),
        NewsEvent("Today noon", "USD", "High", _day(2026, 8, 28, 12, 0)),
    ]
    now = _day(2026, 8, 28, 0, 25)          # 35 min after, window was 30

    rows = cal.armed_today([("A", {"High": 1800}, ())], now=now)

    assert [r[3] for r in rows] == ["Today noon"]
    assert cal.blocked_for({"High": 1800}, now=now) is None


def test_an_old_event_never_reappears(tmp_path):
    """Only a window that is genuinely open earns a row from another day."""
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [NewsEvent("Monday", "USD", "High", _day(2026, 8, 24, 10, 0))]

    assert cal.armed_today([("A", {"High": 1800}, ())], now=_day(2026, 8, 28, 0, 10)) == []


def test_rows_come_back_in_time_order(tmp_path):
    cal = NewsCalendar(cache_path=tmp_path / "ff.json")
    cal.events = [
        NewsEvent("Noon", "USD", "High", _day(2026, 8, 28, 12, 0)),
        NewsEvent("Late print", "USD", "High", _day(2026, 8, 27, 23, 50)),
        NewsEvent("Morning", "USD", "High", _day(2026, 8, 28, 9, 0)),
    ]

    rows = cal.armed_today([("A", {"High": 1800}, ())], now=_day(2026, 8, 28, 0, 10))

    assert [r[3] for r in rows] == ["Late print", "Morning", "Noon"]


# ==================== terminal lifecycle ====================
#
# Not news, but it lives in the same config file. Measured (traces 2026-08-28):
# 100% of an 11-45s warmup was `mt5.initialize` cold-launching terminals the
# previous Stop had just closed; the warmup work itself was 38-187ms.

def test_terminals_are_left_running_on_stop_by_default():
    from src.config import TerminalLifecycleConfig

    assert TerminalLifecycleConfig().close_on_stop is False


def test_close_on_stop_can_be_turned_back_on(tmp_path):
    from src.config import Config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "master:\n  terminal_path: x\nslaves: []\nsymbol_mapping: {}\n"
        "terminals:\n  close_on_stop: true\n",
        encoding="utf-8",
    )

    assert Config.load(cfg).terminals.close_on_stop is True


def test_a_config_without_the_section_keeps_the_fast_default(tmp_path):
    from src.config import Config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "master:\n  terminal_path: x\nslaves: []\nsymbol_mapping: {}\n",
        encoding="utf-8",
    )

    assert Config.load(cfg).terminals.close_on_stop is False
