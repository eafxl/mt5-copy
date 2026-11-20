"""The daily loss limit: four losing closes and the day is over.

WHY IT READS HISTORY INSTEAD OF COUNTING
    There was already a limit — ``MAX_STOPS = 4`` in trade_copier — but it
    counted stops in a SESSION variable. Four stops, close the app, reopen, and
    the counter was zero again. Restarting the app is precisely what a person
    does on a bad day, so the limit was strongest when it was least needed.

    The broker's own history cannot be reset by restarting anything, so that is
    what is counted. No state file of ours, nothing to migrate, nothing to
    forget to write, and it agrees with the terminal's own History tab.

WHAT COUNTS
    Any position on the MASTER account that closed net-negative today —
    realized profit including swap and commission. Not only deals the broker
    marked ``DEAL_REASON_SL``: a position closed manually at a loss cost the
    same money, and the point of the rule is the money.

    "TODAY" IS THE BROKER'S DAY — the day the limit itself resets on. An MT5
    timestamp is the server's wall clock stored as an epoch that reads as UTC,
    so each deal already carries the server's calendar date, and
    MT5Client.server_today reads the current one off a live quote. No clock
    offset, nothing to cache or invalidate.

    An earlier version counted the LOCAL machine's day, which cannot be read
    off the data and so needed the host-to-server conversion. That conversion
    was measured from the last tick, and on a closed market the last tick is
    hours stale: on a Saturday it read -9h08m for a broker at UTC+3, moved
    Friday's ten losses into Saturday, and would have stopped trading on a day
    that had not traded. The whole apparatus existed to answer a question
    nobody needed asked.

    The broker's day is also the rule a prop firm's own daily reset uses, and
    what the terminal's History tab shows — so the count can be checked by
    looking at it.

    Both sides of the comparison are therefore the broker's own calendar, which
    is the calendar the prop firm resets on and the one the terminal's History
    tab displays. The host clock is used only as a floor when the market is
    shut and there is no live quote to read — see MT5Client.server_today for
    why that direction is the safe one.

WHAT HAPPENS WHEN IT TRIPS
    New copies stop, the UI freezes, and a full-screen wall goes up above every
    other application — see gui/stop_overlay.py. Its only button closes the
    app, so the day ends with the process rather than with a dialog someone can
    click past on the way back to a chart.

    The count is re-read on EVERY master close, including closes of positions
    this app was not mirroring, so the trip lands on the loss that reached the
    limit instead of on the next copied close.

    An earlier version froze the controls and left the app open, so that exits
    and SL/TP moves kept mirroring. That is the cost of closing: positions
    still open on the slaves keep the SL and TP they already carry at the
    broker, but nothing mirrors a further change the master makes to them. The
    wall says so before it closes anything.

DELIBERATELY NOT CONFIGURABLE
    No setting, no Settings-tab field. A limit that can be raised from inside
    the app during a drawdown is not a limit; it is a suggestion with extra
    steps.
"""
import time
from dataclasses import dataclass, field

from .mt5_client import ClosedTrade, MT5Client
from .utils.logging import get_logger
from .utils.tracer import get_tracer

#: Losing closes allowed per day. Four, and the rule ends there.
MAX_DAILY_STOPS = 4


def host_today(now: float | None = None) -> str:
    """The host machine's calendar date, ``YYYY-MM-DD``.

    Only a fallback: used when the broker cannot be asked. The day that counts
    is the broker's — see MT5Client.server_today.
    """
    now = time.time() if now is None else now
    return time.strftime("%Y-%m-%d", time.localtime(now))


@dataclass
class DailyStopGuard:
    """Counts today's losing closes on the master, and says whether to stop.

    ``count`` is None until a successful read. None means "could not tell",
    which is NOT the same as zero and NOT the same as tripped: refusing to
    trade because the terminal was briefly unreachable would be an outage of
    our own making, so an unreadable history logs loudly and lets trading
    continue.
    """

    mt5: MT5Client
    limit: int = MAX_DAILY_STOPS
    count: int | None = None
    losses: list[ClosedTrade] = field(default_factory=list)
    checked_at: float = 0.0
    day: str = ""
    _announced: bool = False

    def __post_init__(self):
        self.logger = get_logger()

    # ------------------------------------------------------------- reading
    def refresh(self, now: float | None = None) -> int | None:
        """Re-read the master's history for today. Returns the count, or None."""
        now = time.time() if now is None else now
        # The broker's day, because that is the day the limit resets on.
        try:
            self.day = self.mt5.server_today()
        except Exception:
            self.day = host_today(now)
        trades = self.mt5.closed_trades(days_back=2)
        if trades is None:
            self.logger.warning(
                "Daily stop guard: could not read the master's history — "
                "the limit cannot be enforced on this cycle."
            )
            get_tracer().event("daily_stops_unreadable", limit=self.limit)
            return None

        # Only today's, by the broker's own dating of each trade.
        today = [t for t in trades if t.server_day == self.day]
        # Sorted here rather than trusted from upstream: this list is read out
        # to a person, and "oldest first" is part of what makes it checkable
        # against the History tab.
        self.losses = sorted(
            (t for t in today if t.is_loss), key=lambda t: t.closed_at
        )
        self.count = len(self.losses)
        self.checked_at = now
        # The losses themselves go in the record, not just the count: "3 of 4"
        # in a trace is only checkable against the terminal if it says WHICH
        # three. There are at most a handful by construction.
        get_tracer().event(
            "daily_stops",
            count=self.count,
            limit=self.limit,
            tripped=self.tripped,
            closed_today=len(today),
            closed_seen=len(trades),
            losses=self.describe_losses(),
            day=self.day,
        )
        return self.count

    # ------------------------------------------------------------- verdict
    @property
    def tripped(self) -> bool:
        """True only on a COUNTED breach — never on an unreadable history."""
        return self.count is not None and self.count >= self.limit

    @property
    def remaining(self) -> int | None:
        if self.count is None:
            return None
        return max(0, self.limit - self.count)

    def announce_once(self) -> bool:
        """True the first time the trip is reported, False afterwards.

        The trip is discovered on every close and on every blocked copy; the
        modal that explains it must appear once.
        """
        if not self.tripped or self._announced:
            return False
        self._announced = True
        return True

    # ------------------------------------------------------------ wording
    def describe(self) -> str:
        """One line for a log: the count and what made it up."""
        if self.count is None:
            return "daily stop count unknown (history unreadable)"
        head = f"{self.count} of {self.limit} losing close(s) today"
        if not self.losses:
            return head
        return f"{head}: {', '.join(self.describe_losses())}"

    def describe_losses(self) -> list[str]:
        """``14:03 NAS100 -122.50`` per losing trade, oldest first.

        The time is the BROKER's, so it matches the terminal's History tab
        line for line — which is how the count gets checked.
        """
        return [
            f"{t.server_time} {t.symbol or '?'} {t.net:.2f}" for t in self.losses
        ]
