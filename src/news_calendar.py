"""ForexFactory economic calendar — news windows that block new copies.

WHY
    Copying a master trade straight into a high-impact print is how a prop
    account dies: the spread blows out, the slave fills far from the master's
    price, and the entry-deviation guard cannot tell "slow broker" from "NFP".
    So around a scheduled event, new copies are simply not opened.

    Exits are never blocked. Refusing to close during news is strictly more
    dangerous than closing — see trade_copier._close_copied_positions.

SOURCE
    ``GET https://nfs.faireconomy.media/ff_calendar_thisweek.json`` — the JSON
    feed behind the ForexFactory calendar:

        {"title": "CPI m/m", "country": "AUD", "impact": "High",
         "date": "2026-08-25T21:30:00-04:00", ...}

    ``date`` carries an explicit UTC offset, so timestamps need no timezone
    guessing anywhere in this module. The weekly file is the only one published
    (``ff_calendar_today.json`` 404s), so "today" is filtered locally.

FAILURE
    Fail OPEN. The last good calendar is cached on disk and keeps being
    enforced (flagged stale); with no cache at all nothing is blocked and one
    error is reported. Copying never halts because a website is down.

This module holds NO MetaTrader5 import and does no clock magic — ``now`` is
always passed in, so the whole thing is testable off-Windows.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import cache_dir as _cache_dir
from .utils.logging import get_logger

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_USER_AGENT = "Mozilla/5.0 (compatible; MetaCT)"

# Smallest interval between fetches, whatever the config asks for.
#
# The file is THIS WEEK's calendar: it changes a handful of times a day, and
# faireconomy.media rate-limits accordingly — HTTP 429 is what a caller polling
# it every minute gets, and observed here. A 60s floor was an invitation to do
# exactly that, so the floor is the interval the data actually justifies.
MIN_REFRESH_S = 900.0

# After a failed fetch, wait longer each time instead of retrying on the normal
# cadence. Without this a 429 becomes a loop that keeps earning 429s — the one
# response where retrying on schedule is the wrong move.
# Twice the floor, so the first retry after a refusal is genuinely later than
# the normal cadence rather than equal to it.
BACKOFF_START_S = 1800.0
BACKOFF_MAX_S = 7200.0

# Impact levels as the feed spells them. Anything else is ignored rather than
# guessed at — an unknown level must never silently block trading.
IMPACTS = ("High", "Medium", "Low", "Holiday")

# Windows are configured in SECONDS and applied exactly as written.
#
# They used to be minutes with an automatic ±5s widening bolted on for "feed
# rounding and dispatch lag". Two problems: the real window was never the
# number on screen (a "2m" setting blocked for 4m10s), and a minute is too
# coarse to express what an operator actually wants. Seconds, taken literally,
# mean "120 + 4 = 124" is exactly what you type and exactly what runs.

CACHE_PATH = _cache_dir() / "ff_calendar.json"


@dataclass(frozen=True)
class NewsEvent:
    """One calendar entry, normalized."""
    title: str
    currency: str      # "USD", or "All" for global entries
    impact: str        # one of IMPACTS
    ts: float          # epoch seconds (UTC), from the feed's offset-aware date

    def describe(self, now: float = 0.0) -> str:
        """Short human line for toasts and logs."""
        hhmm = time.strftime("%H:%M", time.localtime(self.ts))
        return f"{self.impact} {self.currency} · {self.title} at {hhmm}"


def normalize_impact(raw) -> str:
    """Map a feed impact string onto IMPACTS, or "" if unrecognized."""
    text = str(raw or "").strip().lower()
    for impact in IMPACTS:
        if impact.lower() == text:
            return impact
    return ""


#: Currency code the feed uses for events that belong to no single currency —
#: bank holidays, Jackson Hole, OPEC.
#:
#: It is an ordinary code as far as the filter is concerned: listed means
#: blocked, not listed means not. It used to match every filter automatically,
#: on the reasoning that these events move everything — but that made a
#: USD-only slave stop for a Japanese bank holiday it had no interest in, and
#: there was no way to say no. A filter that admits things you did not ask for
#: is not a filter. Tick "All" to keep them.
GLOBAL_CURRENCY = "All"


def normalize_currencies(raw) -> tuple[str, ...]:
    """Clean a per-slave currency filter into an ordered, deduped tuple.

    Empty means EVERY currency, not none: a slave that has never been told
    which currencies to care about must keep blocking on all of them, or
    adding this field would silently switch protection off for every existing
    config.
    """
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw:
        code = str(item or "").strip().upper()
        if code and code not in out:
            out.append(code)
    return tuple(out)


def currency_matches(event_currency: str, currencies) -> bool:
    """Whether ``event_currency`` is one this slave cares about.

    An empty filter still means EVERY currency — including the global "All"
    entries. Once a filter is set, nothing gets in that is not named in it.

    ``currencies`` must already be through `normalize_currencies`. This runs
    once per event per slave per cycle, so the normalization is hoisted to the
    caller rather than repeated here; passing raw config in would silently
    fail to match on case.
    """
    if not currencies:
        return True                       # no filter = every currency
    return str(event_currency or "").strip().upper() in currencies


def normalize_windows(raw) -> dict[str, float]:
    """Clean a per-slave ``{impact: seconds}`` map.

    Unknown impacts and non-positive values are dropped, so an empty result
    means exactly what the operator means by leaving the fields blank: this
    slave has no news restrictions at all.
    """
    out: dict[str, float] = {}
    if not raw:
        return out
    for key, value in dict(raw).items():
        impact = normalize_impact(key)
        if not impact:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            out[impact] = seconds
    return out


def minutes_to_seconds(raw) -> dict[str, float]:
    """Migrate a legacy ``{impact: minutes}`` map to seconds.

    Configs written before windows became seconds hold minutes. Reading those
    as seconds would turn a 2-minute guard into a 2-second one — protection
    silently all but removed — so the conversion is explicit and one-way, keyed
    off which field is present in the file (see SlaveConfig).
    """
    return {k: v * 60.0 for k, v in normalize_windows(raw).items()}


def parse_events(payload) -> list[NewsEvent]:
    """Turn the decoded feed into NewsEvents, dropping rows we can't trust.

    A row with an unparsable date or an unknown impact is skipped rather than
    defaulted — a mis-dated event would block trading at the wrong minute.
    """
    events: list[NewsEvent] = []
    if not isinstance(payload, list):
        return events

    for row in payload:
        if not isinstance(row, dict):
            continue
        impact = normalize_impact(row.get("impact"))
        if not impact:
            continue
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            # fromisoformat handles the feed's "-04:00" offset directly. Guard
            # anyway: a naive value would be read as host-local and land the
            # window hours away from the real print.
            dt = datetime.fromisoformat(str(raw_date))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
        except (ValueError, TypeError):
            continue
        events.append(NewsEvent(
            title=str(row.get("title") or "").strip(),
            currency=str(row.get("country") or "").strip(),
            impact=impact,
            ts=ts,
        ))

    events.sort(key=lambda e: e.ts)
    return events


def fetch_calendar(timeout: float = 10.0) -> tuple[list[NewsEvent], str, float]:
    """Fetch this week's calendar. Never raises.

    Returns ``(events, error, retry_after_s)``. A non-empty error with an empty
    list means the caller should fall back to its cache. ``retry_after_s`` is
    the server's own instruction when it sends one, 0.0 otherwise — it is not
    advice to weigh up, it is the only number that reliably says when this feed
    will answer again.

    RAW. This is the only function that touches the network, and it honours no
    throttle of its own — every rate-limit rule lives in NewsCalendar. Nothing
    outside this module should call it: a UI button wired straight to here
    bypassed the interval, the cross-restart cooldown and the backoff all at
    once, which is how the 429 outlived three separate fixes for it. Use
    `NewsCalendar.probe()`.
    """
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        retry = _retry_after_seconds(e.headers)
        if e.code == 429:
            # Name it: the cause is ours (we asked too soon) and the remedy is
            # the server's own interval, not a guess.
            return [], (
                "ForexFactory feed HTTP 429 (rate limited) — asked again too "
                "soon" + (f"; server asks for {retry:.0f}s" if retry else "")
            ), retry
        return [], f"ForexFactory feed HTTP {e.code}", retry
    except urllib.error.URLError as e:
        return [], f"ForexFactory feed unreachable: {e.reason}", 0.0
    except Exception as e:  # malformed JSON, socket timeout, ...
        return [], f"ForexFactory feed error: {e}", 0.0

    events = parse_events(payload)
    if not events:
        return [], "ForexFactory feed returned no usable events", 0.0
    return events, "", 0.0


def _retry_after_seconds(headers) -> float:
    """``Retry-After`` as seconds. 0.0 when absent or unparseable.

    The header is allowed to be either a delay in seconds or an HTTP date; only
    the seconds form has ever been observed here, and a date that fails to parse
    must degrade to "no hint" rather than to an exception on the copy path.
    """
    raw = str((headers or {}).get("Retry-After", "")).strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        return max(0.0, when.timestamp() - time.time())
    except Exception:
        return 0.0


# ---------------------------------------------------------------- disk cache

def save_cache(
    events: list[NewsEvent],
    path: Path = CACHE_PATH,
    fetched_at: float | None = None,
    last_attempt: float | None = None,
    backoff_s: float = 0.0,
) -> None:
    """Persist the calendar AND when we last asked for it.

    The attempt is recorded even when the fetch failed, and that is the point.
    An in-memory backoff dies with the process, so a run of restarts becomes a
    run of requests to a file that changes a few times a day — which is how the
    429 kept coming back after the first fix. On disk, the cooldown outlives the
    process that earned it.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = {
            "fetched_at": now if fetched_at is None else fetched_at,
            "last_attempt": now if last_attempt is None else last_attempt,
            "backoff_s": float(backoff_s),
            "events": [
                {"title": e.title, "currency": e.currency,
                 "impact": e.impact, "ts": e.ts}
                for e in events
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        # WARNING, not debug: without this file the cooldown cannot survive a
        # restart, and the feed starts refusing again. A silent failure here is
        # indistinguishable from the bug it causes.
        get_logger().warning(
            "News cache write failed (%s) — the fetch cooldown will not "
            "survive a restart: %s", path, e,
        )


def load_state(path: Path = CACHE_PATH) -> tuple[float, float]:
    """``(last_attempt, backoff_s)`` from the cache file, ``(0.0, 0.0)`` if none.

    Separate from `load_cache` so the events and the rate-limit state can be
    read independently: a fetch that failed has no events to cache but still
    has an attempt worth remembering.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            float(data.get("last_attempt") or data.get("fetched_at") or 0.0),
            float(data.get("backoff_s") or 0.0),
        )
    except Exception:
        return 0.0, 0.0


def load_cache(path: Path = CACHE_PATH) -> tuple[list[NewsEvent], float]:
    """Read the cached calendar. Returns ``([], 0.0)`` when there is none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        events = [
            NewsEvent(
                title=str(r.get("title") or ""),
                currency=str(r.get("currency") or ""),
                impact=normalize_impact(r.get("impact")),
                ts=float(r.get("ts") or 0.0),
            )
            for r in data.get("events", [])
        ]
        events = [e for e in events if e.impact and e.ts]
        events.sort(key=lambda e: e.ts)
        return events, float(data.get("fetched_at") or 0.0)
    except Exception:
        return [], 0.0


# --------------------------------------------------------------- the maths

def active_block(
    events: list[NewsEvent],
    windows: dict,
    now: float,
    currencies=(),
) -> tuple[NewsEvent, float] | None:
    """``(event, seconds)`` currently blocking new copies, or None.

    ``windows`` maps impact -> SECONDS, applied literally — no padding is added
    anywhere. An impact absent from it, or set to 0, is unrestricted. The earliest matching event wins, so a toast names the one
    that actually started the block rather than whichever came last in the file.

    ``currencies`` narrows it further: a slave trading NAS100 has no reason to
    stop for Australian retail sales. Empty means every currency, so an existing
    config keeps behaving exactly as before. Once set, only what it names gets
    through — including the global "All" entries, which are opt-in like any
    other code (see GLOBAL_CURRENCY).
    """
    live = normalize_windows(windows)
    if not live:
        return None
    wanted = normalize_currencies(currencies)

    for event in sorted(events, key=lambda e: e.ts):
        seconds = live.get(event.impact, 0.0)
        if not seconds or not currency_matches(event.currency, wanted):
            continue
        if abs(now - event.ts) <= seconds:
            return event, seconds
    return None


def events_today(events: list[NewsEvent], now: float) -> list[NewsEvent]:
    """Events falling on the host's local calendar day containing ``now``."""
    today = time.localtime(now)
    return [
        e for e in events
        if time.localtime(e.ts)[:3] == today[:3]
    ]


class NewsCalendar:
    """Holds the current calendar for the service and the copy path.

    The copy path calls :meth:`blocked_for` on every open, so that method does
    no I/O — it walks an in-memory list of ~70 events. Refreshing is driven
    separately from the poll loop and self-throttles via :meth:`due`.
    """

    def __init__(self, refresh_sec: float = 900.0, fetch=fetch_calendar,
                 cache_path: Path = CACHE_PATH):
        self.refresh_sec = max(MIN_REFRESH_S, float(refresh_sec))
        self._fetch = fetch
        self._cache_path = cache_path
        self._logger = get_logger()

        self.events: list[NewsEvent] = []
        self.fetched_at: float = 0.0
        self.stale: bool = False       # events came from cache, not a live fetch
        self._last_attempt: float = 0.0
        self._last_error: str = ""
        self._backoff_s: float = 0.0     # grows while the feed keeps refusing
        #: Set from the feed's own Retry-After when it sends one.

        # Seed from disk so a service started while the feed is down still
        # enforces this morning's calendar.
        cached, fetched_at = load_cache(self._cache_path)
        if cached:
            self.events = cached
            self.fetched_at = fetched_at
            self.stale = True
        # Inherit the rate-limit state from disk whether or not there are
        # events. Otherwise every start fetches immediately, and a morning of
        # restarts is a morning of requests for a file that changes a few times
        # a day — which is how the 429 was earned, and why it survived a
        # memory-only backoff. Read even with no cached events: a refusal
        # leaves nothing to cache but is exactly when the cooldown matters.
        self._last_attempt, self._backoff_s = load_state(self._cache_path)

    @staticmethod
    def _local_day(ts: float) -> tuple:
        """The local calendar day containing ``ts``, as ``(y, m, d)``.

        LOCAL, to match `events_today` — the table and the gate must agree on
        where the day ends, and the operator's midnight is the one that counts.
        """
        return time.localtime(ts)[:3]

    def _day_is_stale(self, now: float) -> bool:
        """True when today's calendar was downloaded on an earlier day."""
        if not self.fetched_at:
            return True
        return self._local_day(self.fetched_at) != self._local_day(now)

    def _wait_s(self) -> float:
        """How long to leave between attempts, right now.

        One place, because `due` and `next_fetch_in` disagreeing is a bug that
        looks like a rate limit: the backoff was set to the server's 124s and
        then `due` re-imposed the 15-minute floor on top of it, so the app sat
        out the full interval anyway with no calendar in hand.

        The floor only applies while we HOLD a calendar — that is what it is
        for, stopping pointless re-fetches of data we already have. With
        nothing cached, news blocking is not running, and the only wait worth
        serving is the one the server asked for.
        """
        floor = self.refresh_sec if self.events else 0.0
        return max(floor, self._backoff_s)

    def due(self, now: float | None = None) -> bool:
        """Whether to fetch now. Three rules, in order of authority.

        1. A refusal is absolute. If the server (or an outage) put us in a
           backoff, nothing overrides it — not even the day rolling over.
           Ignoring that is how a rate limit becomes a loop.
        2. A NEW LOCAL DAY needs a fresh calendar, whatever the interval says.
           At 00:00 the whole table becomes tomorrow's, and answering it from a
           file downloaded yesterday means enforcing yesterday's idea of
           today's schedule — the feed revises times, adds events and moves
           them. One extra request a day is nothing against a 15-minute
           interval, and the cost of being wrong is a copy taken into a print.
        3. Otherwise the normal interval.
        """
        now = time.time() if now is None else now
        since = now - self._last_attempt
        if since < self._backoff_s:
            return False
        if self._day_is_stale(now):
            return True
        return since >= self._wait_s()

    def set_refresh(self, refresh_sec: float) -> None:
        self.refresh_sec = max(MIN_REFRESH_S, float(refresh_sec))

    def refresh(self, now: float | None = None) -> str:
        """Fetch and adopt a fresh calendar. Returns an error string ("" = ok).

        On failure the previous list stays in force and is marked stale — the
        events themselves don't expire just because the website did.
        """
        now = time.time() if now is None else now
        self._last_attempt = now

        events, error, retry_after = self._fetch()
        if error:
            self.stale = bool(self.events)
            # How long to wait depends on whether we have a calendar at all,
            # and the difference is the whole user experience of this feature.
            #
            #   Holding one -> refreshing is optional. Wait the full interval;
            #     nothing is degraded meanwhile, and asking sooner is what got
            #     us refused.
            #   Holding NOTHING -> news blocking is not running. Come back at
            #     the earliest moment permitted, because every extra minute is a
            #     minute of unprotected copying. The interval floor exists to
            #     stop pointless re-fetching of data we already have; with no
            #     data it is protecting nothing and merely prolongs the outage.
            #     Observed: the server asked for 119s and we sat out 15 minutes.
            floor = self.refresh_sec if self.events else 0.0
            if retry_after:
                # The server stated when it will answer again. That beats any
                # guess of ours — measured live, this feed permits about one
                # request per five minutes per IP (Cloudflare, Retry-After 300).
                # A small margin so we do not land on the boundary itself.
                self._backoff_s = max(retry_after + 5.0, floor)
            else:
                self._backoff_s = min(
                    BACKOFF_MAX_S,
                    self._backoff_s * 2 if self._backoff_s
                    else (BACKOFF_START_S if self.events else MIN_REFRESH_S),
                )
            # Persist the refusal, so a restart inherits the cooldown instead of
            # starting the cycle over.
            save_cache(
                self.events, self._cache_path,
                fetched_at=self.fetched_at, last_attempt=now,
                backoff_s=self._backoff_s,
            )
            self._logger.warning(
                "News calendar: %s — next attempt in %.0f min",
                error, self._backoff_s / 60.0,
            )
            return error

        rolled = self._day_is_stale(now)
        self._backoff_s = 0.0
        self.events = events
        self.fetched_at = now
        self.stale = False
        if rolled:
            self._logger.info(
                "News calendar reloaded for %s: %d events this week",
                time.strftime("%Y-%m-%d", time.localtime(now)), len(events),
            )
        save_cache(
            events, self._cache_path,
            fetched_at=now, last_attempt=now, backoff_s=0.0,
        )
        self._logger.info("News calendar refreshed: %d events", len(events))
        return ""

    def next_fetch_in(self, now: float | None = None) -> float:
        """Seconds until a fetch is permitted. 0 when one is due now."""
        now = time.time() if now is None else now
        if self.due(now):
            return 0.0
        since = now - self._last_attempt
        # Inside a backoff the wait is the backoff; otherwise the interval.
        wait = self._backoff_s if since < self._backoff_s else self._wait_s()
        return max(0.0, wait - since)

    def probe(self) -> tuple[list[NewsEvent], str, str]:
        """For a manual "Test": ``(events, error, note)``.

        Fetches only when a fetch is allowed; otherwise answers from the cache
        and says when the next download may happen. A Test button that fetched
        on every press is a rate limit waiting to happen — and it was one: the
        settings tab called `fetch_calendar` directly, so clicking through a
        news setup burned the allowance while the service's own cooldown never
        saw a thing.

        Answering from cache still proves what the button is for — that the
        calendar is readable and what is in it.
        """
        wait = self.next_fetch_in()
        if wait > 0 and self.events:
            return self.events, "", (
                f"cached copy — next download allowed in {wait / 60:.0f} min"
            )
        if wait > 0:
            return [], "", (
                f"no calendar cached yet; next download allowed in "
                f"{wait / 60:.0f} min"
            )
        error = self.refresh()
        return self.events, error, ""

    def take_error_edge(self, error: str) -> str:
        """Return ``error`` only when it differs from the last one reported.

        A feed down for an hour must produce one toast, not one per cycle.
        """
        if error == self._last_error:
            return ""
        self._last_error = error
        return error

    def blocked_for(self, windows: dict, now: float | None = None,
                    currencies=()) -> tuple[NewsEvent, float] | None:
        """``(event, minutes)`` blocking this slave now, or None. Hot path — no I/O."""
        if not self.events:
            return None
        now = time.time() if now is None else now
        return active_block(self.events, windows, now, currencies)

    def armed_today(self, slave_rules, now: float | None = None) -> list[tuple]:
        """Today's events that some slave would actually block on.

        ``slave_rules`` is an iterable of ``(name, windows, currencies)``.

        Each row carries EVERY slave's own window, not one merged number.
        Two slaves guarding the same event for 124s and 300s is the normal
        case, and collapsing that to the widest told the operator a figure that
        was wrong for every slave but one — while the table's whole job is to
        say what will happen. The widest is still reported alongside, because
        it is the span during which copying is degraded *somewhere*.

        The impact and the currency are checked TOGETHER, per slave. Merging
        every slave's impacts into one set and every slave's currencies into
        another would list a High EUR event for a config whose only EUR slave
        watches Low impacts — a row that can never block anything.

        Today's events, PLUS any event whose window is still open right now
        even though it printed on another day. A 23:50 print with a 300s window
        is still blocking at 23:54, and listing only "today" made it vanish
        from the table at midnight while the gate went on refusing copies.

        Returns ``(ts, impact, currency, title, widest_s, per_slave)`` tuples,
        where ``per_slave`` is a tuple of ``(slave_name, seconds)`` — plain, so
        they can ride on a frozen TradeEvent.
        """
        now = time.time() if now is None else now
        rules = []
        for name, windows, currencies in slave_rules:
            live = normalize_windows(windows)
            if live:
                rules.append((str(name or "?"), live, normalize_currencies(currencies)))
        if not rules:
            return []

        today = {id(e) for e in events_today(self.events, now)}
        rows = []
        for event in self.events:
            per_slave = []
            for name, windows, currencies in rules:
                if not currency_matches(event.currency, currencies):
                    continue
                seconds = windows.get(event.impact, 0.0)
                if seconds > 0:
                    per_slave.append((name, seconds))
            if not per_slave:
                continue
            widest = max(sec for _n, sec in per_slave)
            still_open = abs(now - event.ts) <= widest
            if id(event) in today or still_open:
                rows.append((event.ts, event.impact, event.currency,
                             event.title, widest, tuple(per_slave)))
        rows.sort(key=lambda r: r[0])
        return rows
