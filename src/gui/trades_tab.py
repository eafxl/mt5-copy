"""Trading tab - service controls + live trade table.

CRITICAL: UI runs in Qt main thread, polls events at its own pace.
Copy operations run independently - UI never blocks them.

Open trades sit above a separator row; on TRADE_CLOSED a trade moves below it
into the grayed-out closed-trades section, carrying its realized profit.
"""
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QTransform,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..paths import icons_dir as _icons_dir

_ICONS = _icons_dir()

from ..deviation_history import (
    DeviationHistory,
    bounds,
    fmt_seconds_ago,
    x_ticks,
    y_ticks,
)
from ..events import EventType, TradeEvent, get_ui_queue
from ..utils.logging import get_logger


@dataclass
class TradeRow:
    """Data for a single trade row."""
    trade_id: str
    slave_name: str
    slave_ticket: int
    master_ticket: int
    symbol: str
    order_type: int  # 0=BUY, 1=SELL
    volume: float
    open_time: float
    open_price: float
    profit: float = 0.0
    is_closed: bool = False
    close_time: float = 0.0
    transport: str = ""       # "TCP" (EA push) or "Polling"
    delay_ms: float = 0.0     # copy latency master→slave, ms (total)
    local_ms: float = 0.0     # our processing: dispatch + connect + calc
    ping_ms: float = 0.0      # transport: master fill → signal ingested
    broker_ms: float = 0.0    # broker: order send → fill


def _leg_parts(trade: "TradeRow") -> list[str]:
    """Compact waterfall legs for the Signal cell, e.g. ['L12', 'P3', 'B205']."""
    parts = []
    if trade.local_ms > 0:
        parts.append(f"L{trade.local_ms:.0f}")
    if trade.ping_ms > 0:
        parts.append(f"P{trade.ping_ms:.0f}")
    if trade.broker_ms > 0:
        parts.append(f"B{trade.broker_ms:.0f}")
    return parts


def _leg_tooltip(trade: "TradeRow") -> str:
    """Full labelled breakdown for the Signal cell tooltip."""
    lines = [f"Transport: {trade.transport or '—'}"]
    if trade.delay_ms > 0:
        lines.append(f"Total copy: {trade.delay_ms:.0f} ms")
    lines.append(f"Local (dispatch+connect+calc): {trade.local_ms:.0f} ms")
    lines.append(f"Ping (master fill→ingest): {trade.ping_ms:.0f} ms")
    lines.append(f"Broker (send→fill): {trade.broker_ms:.0f} ms")
    return "\n".join(lines)


def _fmt_delay(seconds: float) -> str:
    """'live' / '10m' / '45s' — how far behind the TradingView feed is."""
    if seconds <= 0:
        return "live"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def _fmt_clock(seconds: float) -> str:
    """'3s' / '1:42' / '17:03' — a running counter, not a rounded description.

    Deliberately finer than `_fmt_delay`: these numbers exist to be compared
    against each other, and "10m vs 10m" hides the very gap they are there to
    expose.
    """
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _fmt_age(seconds: float) -> str:
    """Human age of a cached offset."""
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


class DeviationChart(QWidget):
    """Raw TV↔master deviation over time, for holding next to a real chart.

    A plain linear line: one point per measurement, joined in order. Plots the
    RAW deviation, not the median-smoothed offset the converter uses — the
    question is whether the smoothing is discarding noise or hiding a real move,
    and the smoothed line cannot answer that.

    The y axis is percentile-scaled (see deviation_history.bounds), so a single
    mis-paired sample cannot flatten everything else into a line with one spike.
    Such a point is still drawn, clipped to the frame.

    Hand-painted with QPainter. No plotting library is installed and none is
    wanted for one line and five gridlines; all the arithmetic lives in
    src/deviation_history.py, where it is testable without Qt.
    """

    #: Clearance between the span control and the value axis it sits under.
    _AXIS_GAP = 12

    #: Visible spans offered in the selector. The buffer holds 30 min.
    SPANS = [("1m", 60.0), ("5m", 300.0), ("30m", 1800.0)]

    _BG = "#ffffff"
    _FRAME = "#dddddd"
    _GRID = "#f0f0f0"
    _AXIS_TEXT = "#999999"
    _LINE = "#1565C0"
    _LAST = "#0D47A1"
    _HOVER = "#E65100"

    _PAD_L = 62      # room for a price label at 5 decimals
    _PAD_R = 10
    _PAD_T = 8
    _PAD_B = 24      # room for the seconds axis AND the span control below it

    def __init__(self, history: DeviationHistory, parent=None):
        super().__init__(parent)
        self._history = history
        self._ticker = ""
        self._label = ""
        self._digits = 5
        self._span_s = 300.0
        self._hover: object | None = None      # Sample under the cursor
        self._hover_miss = ""                  # why the last hover read nothing
        self.logger = get_logger()
        # Fixed height, full width: a compact section, not a resizable pane.
        self.setFixedHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Hover readout needs the cursor position without a click.
        self.setMouseTracking(True)

        # --- Span selector, in the CORNER where the two axes meet: the one
        # piece of the chart that belongs to neither of them, and the natural
        # home for the control that scales the time axis.
        #
        # It has a border and a background because it is a control. Drawn
        # transparent and borderless it read as a stray "5m" label — nothing
        # about it invited a click — while sitting across both axes' label
        # tracks at once.
        self.span_combo = QComboBox(self)
        for label, seconds in self.SPANS:
            self.span_combo.addItem(label, seconds)
        self.span_combo.setCurrentIndex(1)          # 5m
        self.span_combo.setToolTip("Visible time span")
        self.span_combo.setFixedSize(46, 17)
        # The drop-down sub-control is left to the platform style. Overriding
        # it to a bare 11px box removed the arrow entirely on macOS — a
        # stylesheet that touches ::drop-down owns drawing the indicator too,
        # and there was nothing to draw it with.
        self.span_combo.setStyleSheet(
            "QComboBox { border: 1px solid #d0d0d0; border-radius: 2px;"
            " background: #fafafa; color: #666; font-size: 9px;"
            " padding: 0 0 0 4px; }"
            "QComboBox:hover { border-color: #999; color: #333; }"
        )
        self.span_combo.currentIndexChanged.connect(self._on_span_combo)

    # ------------------------------------------------------------- inputs
    def set_pair(self, ticker: str, label: str, digits: int):
        """Follow the pair selected in the strip above."""
        if ticker != self._ticker:
            self._hover = None          # a held readout would name the old pair
        self._ticker = ticker
        self._label = label
        self._digits = max(0, min(8, int(digits or 5)))
        self.update()

    def set_span(self, span_s: float):
        self._span_s = max(1.0, float(span_s))
        self._hover = None              # the point under the cursor just moved
        self.update()

    def _on_span_combo(self):
        self.set_span(self.span_combo.currentData() or 300.0)

    def resizeEvent(self, event):
        """Park the span selector in the corner between the two axes.

        Bounded by the y axis on the right and the x axis above, so it cannot
        overlap either one's labels — the previous position ran under both.
        """
        super().resizeEvent(event)
        plot_l, _r, _t, plot_b = self._plot_rect()
        w, h = self.span_combo.width(), self.span_combo.height()
        # A real gap from the value axis, not a hairline: at 4px the control
        # read as part of the axis furniture rather than as a thing to press.
        x = max(2, plot_l - w - self._AXIS_GAP)
        y = plot_b + max(2, (self._PAD_B - h) // 2)
        self.span_combo.setGeometry(x, y, w, h)

    # -------------------------------------------------------------- hover
    def _plot_rect(self) -> tuple[int, int, int, int]:
        return (
            self._PAD_L, self.width() - self._PAD_R,
            self._PAD_T, self.height() - self._PAD_B,
        )

    def mouseMoveEvent(self, event):
        """Latch the nearest sample to the cursor, or record why there is none.

        Every miss is logged with its reason. "Sometimes it does not work at
        all" is unanswerable otherwise: an empty ticker, a cleared buffer, a
        cursor in the axis margin and a span with no points in it all look
        identical from the outside.
        """
        pos = event.position()
        plot_l, plot_r, plot_t, plot_b = self._plot_rect()
        previous, self._hover = self._hover, None

        samples = self._history.series(self._ticker, span_s=self._span_s)
        if not self._ticker:
            self._miss("no pair selected")
        elif not samples:
            self._miss(
                f"no samples for {self._ticker} in the last "
                f"{self._span_s:.0f}s (buffer holds {self._history.count(self._ticker)})"
            )
        elif not (plot_l <= pos.x() <= plot_r and plot_t <= pos.y() <= plot_b):
            self._miss("cursor outside the plot area")
        else:
            # Nearest in x, with NO proximity radius. A radius was the reason
            # hovering "sometimes did nothing": points bunch up when the series
            # is short or the span is wide, and a cursor a few pixels off the
            # last dot read as an empty chart. Inside the plot there is always
            # a nearest sample, so there is always a readout.
            plot_w = plot_r - plot_l
            best, best_dx = None, None
            for s in samples:
                dx = abs((plot_r - s.ago / self._span_s * plot_w) - pos.x())
                if best_dx is None or dx < best_dx:
                    best, best_dx = s, dx
            self._hover, self._hover_miss = best, ""

        if self._hover is not None or previous is not None:
            self.update()
        super().mouseMoveEvent(event)

    def _miss(self, reason: str):
        """Log a hover that read nothing — once per distinct reason."""
        if reason != self._hover_miss:
            self._hover_miss = reason
            self.logger.debug("[DeviationChart] hover read nothing: %s", reason)

    def leaveEvent(self, event):
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    # -------------------------------------------------------------- paint
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(self._BG))
        # No frame of its own: the chart sits inside the panel's border now, and
        # a second rectangle 1px in reads as a box in a box.
        plot_l, plot_r, plot_t, plot_b = self._plot_rect()
        if plot_r <= plot_l or plot_b <= plot_t:
            return

        points = self._history.series(self._ticker, span_s=self._span_s)
        lo, hi = bounds(points)

        font = QFont()
        font.setPointSize(7)
        p.setFont(font)

        self._draw_grid(p, plot_l, plot_r, plot_t, plot_b, lo, hi)

        if not points:
            p.setPen(QPen(QColor(self._AXIS_TEXT), 1))
            p.drawText(
                plot_l + 8, (plot_t + plot_b) // 2,
                "waiting for the first measurement…",
            )
            return

        self._draw_series(p, points, plot_l, plot_r, plot_t, plot_b, lo, hi)

    def _draw_grid(self, p, plot_l, plot_r, plot_t, plot_b, lo, hi):
        span_v = hi - lo
        for value in y_ticks(lo, hi, count=3):
            y = plot_b - (value - lo) / span_v * (plot_b - plot_t)
            p.setPen(QPen(QColor(self._GRID), 1))
            p.drawLine(plot_l, int(y), plot_r, int(y))
            p.setPen(QPen(QColor(self._AXIS_TEXT), 1))
            # Kept above the axis line. The lowest tick sits exactly ON plot_b,
            # so the usual +3 baseline dropped its label into the corner and
            # straight onto the span control below.
            p.drawText(4, min(int(y) + 3, plot_b - 2), f"{value:+.{self._digits}f}")

        for ago in x_ticks(self._span_s, count=4):
            # 0s on the right edge, growing into the past leftwards.
            x = plot_r - ago / self._span_s * (plot_r - plot_l)
            p.setPen(QPen(QColor(self._GRID), 1))
            p.drawLine(int(x), plot_t, int(x), plot_b)
            p.setPen(QPen(QColor(self._AXIS_TEXT), 1))
            label = fmt_seconds_ago(ago)
            # Nudge the labels inside the frame — but never the leftmost one
            # past the axis, where it would collide with the span control
            # sitting in the corner.
            offset = 0 if (ago == 0 or ago >= self._span_s) else 6
            p.drawText(int(x) - offset, plot_b + 13, label)

    def _draw_series(self, p, samples, plot_l, plot_r, plot_t, plot_b, lo, hi):
        """Plain line through the samples, plus a dot on each measurement.

        The dots matter: a failed cycle leaves a gap in the samples, and only
        the dots show that the segment spanning it is interpolation.
        """
        span_v = hi - lo
        plot_w = plot_r - plot_l
        plot_h = plot_b - plot_t

        def x_of(ago: float) -> float:
            return plot_r - ago / self._span_s * plot_w

        def y_of(value: float) -> float:
            y = plot_b - (value - lo) / span_v * plot_h
            # Clip: the axis is percentile-scaled, so an outlier is expected to
            # land outside it and must not paint over the frame or the labels.
            return min(max(y, float(plot_t)), float(plot_b))

        xy = [QPointF(x_of(s.ago), y_of(s.value)) for s in samples]

        p.setPen(QPen(QColor(self._LINE), 1.5))
        for a, b in zip(xy, xy[1:]):
            p.drawLine(a, b)

        p.setBrush(QBrush(QColor(self._LINE)))
        p.setPen(Qt.PenStyle.NoPen)
        for point in xy:
            p.drawEllipse(point, 1.6, 1.6)

        # Newest point marked, but NOT labelled. The panel above already states
        # the current reading; a second number here was a second source of
        # truth, and the two disagreed by design — the panel shows the median
        # offset, this series is the raw deviation.
        last = samples[-1]
        p.setBrush(QBrush(QColor(self._LAST)))
        p.drawEllipse(QPointF(x_of(last.ago), y_of(last.value)), 3.0, 3.0)

        if self._hover is not None:
            self._draw_hover(p, x_of, y_of, plot_l, plot_r, plot_t, plot_b)

    def _draw_hover(self, p, x_of, y_of, plot_l, plot_r, plot_t, plot_b):
        """Crosshair and readout for the sample under the cursor."""
        s = self._hover
        x, y = x_of(s.ago), y_of(s.value)

        p.setPen(QPen(QColor(self._HOVER), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(x, float(plot_t)), QPointF(x, float(plot_b)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(self._HOVER)))
        p.drawEllipse(QPointF(x, y), 3.0, 3.0)

        when = time.strftime("%H:%M:%S", time.localtime(s.at)) if s.at else ""
        text = f"{s.value:+.{self._digits}f}"
        if when:
            text += f"   {when}  ({s.ago:.0f}s ago)"

        font = p.font()
        font.setBold(True)
        p.setFont(font)
        metrics = p.fontMetrics()
        tw = metrics.horizontalAdvance(text)
        # Flip to the left of the crosshair when the label would run off.
        tx = x + 6 if x + 6 + tw <= plot_r else x - 6 - tw
        ty = plot_t + 9
        p.setBrush(QBrush(QColor(255, 255, 255, 220)))
        p.drawRect(int(tx) - 2, ty - 9, tw + 4, 12)
        p.setPen(QPen(QColor(self._HOVER), 1))
        p.drawText(int(tx), ty, text)


class PriceCalcStrip(QWidget):
    """TradingView ↔ master price converter, as a peer section of the trade table.

    Shows the live offset for the selected pair and converts in both
    directions: type a TradingView price to get the master equivalent, or a
    master price to get the TradingView one. The field you did NOT type in is
    the derived one, and it re-derives on every offset update.

    Laid out like the Active Trades section it sits above — bold section title,
    bordered panel below — so the tab reads as two sections in one language
    rather than a table with a toolbar bolted on. The instrument name and quote
    age are part of the reading, so they sit inside the panel with the prices,
    not out beside the title.

    Grayed out whenever the offset can't be trusted — service stopped, master
    disconnected, or the scanner failing — with the reason in the tooltip.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._diffs: dict[str, TradeEvent] = {}  # tv_ticker -> latest PRICE_DIFF
        self._diff_at: dict[str, float] = {}     # tv_ticker -> receipt monotonic
        self._last_edited = "tv"                 # "tv" or "mt" — which side leads
        self._service_running = False
        self._history = DeviationHistory()       # 30 min of raw measurements
        # Vertically Fixed: this section is exactly as tall as its contents,
        # whether or not the chart is expanded. The trade table below carries
        # the only stretch factor in the column, and without this it would
        # squeeze every neighbour down to a sliver to get the space.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._setup_ui()
        self.setVisible(False)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)  # matches the tab's own section spacing

        # --- Section header: same shape as "Active Trades" below it. Title
        # only. Everything that describes the reading — the instrument, the
        # ages, the chart's own disclosure — lives in the panel with the
        # prices; a control up here governs a panel it does not visibly touch.
        header = QHBoxLayout()
        title = QLabel("Real Price")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        # --- Panel: borrows the trade table's frame so the two sections read
        # as the same kind of surface. Scoped by object name, otherwise the
        # border would be inherited by every child widget.
        #
        # The chart lives INSIDE this frame, under the reading row, so expanding
        # it grows the panel rather than adding a second floating surface.
        panel = QFrame()
        panel.setObjectName("pricePanel")
        panel.setStyleSheet(
            "QFrame#pricePanel { border: 1px solid #ddd; background: #ffffff; }"
        )
        panel_col = QVBoxLayout(panel)
        panel_col.setContentsMargins(8, 6, 8, 6)
        panel_col.setSpacing(6)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.pair_combo = QComboBox()
        self.pair_combo.setMinimumWidth(190)
        self.pair_combo.currentIndexChanged.connect(self._on_pair_changed)
        row.addWidget(self.pair_combo)

        # Disclosure for the deviation chart, in the row it expands. Collapsed
        # by default — most of the time the offset number is all anyone wants.
        self.chart_toggle = QToolButton()
        self.chart_toggle.setCheckable(True)
        self.chart_toggle.setChecked(False)
        self.chart_toggle.setText("👁")
        self.chart_toggle.setToolTip("Expand")
        self.chart_toggle.setStyleSheet(
            "QToolButton { border: none; color: #888; font-size: 13px; }"
            "QToolButton:checked { color: #1565C0; }"
        )
        eye = _ICONS / "eye.svg"
        if eye.exists():
            self.chart_toggle.setIcon(QIcon(str(eye)))
            self.chart_toggle.setText("")
        self.chart_toggle.toggled.connect(self._on_chart_toggled)
        row.addWidget(self.chart_toggle)

        # The instant BOTH prices are for, as wall-clock. Without it the row
        # showed two counters ticking every second beside prices that only
        # change once a minute, which reads as a stuck feed. It is not: both
        # sides are quantized to M1, so a new reading exists once per minute and
        # this is the minute it belongs to.
        self.at_label = QLabel("")
        self.at_label.setStyleSheet("color: #666;")
        row.addWidget(self.at_label)

        # --- The reading: for each side, the delay IN USE and the price read at
        # it. Two independent counters off two independent readings — if the
        # clocks ever part company the numbers disagree on screen, rather than
        # being inferred from an offset that merely looks wrong.
        self.tv_reading = QLabel("—")
        row.addWidget(self.tv_reading)

        self.mt_reading = QLabel("")
        row.addWidget(self.mt_reading)

        self.diff_label = QLabel("")
        self.diff_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.diff_label)

        # Instrument name, next to the prices it describes.
        self.desc_label = QLabel("")
        self.desc_label.setStyleSheet("color: #666;")
        row.addWidget(self.desc_label)

        row.addStretch()

        # Cached-offset warning only. This used to show the feed's STATED delay
        # ("10m"), which is not the delay in use and not the age of anything on
        # screen — the real lookback is a minute longer and is now on the two
        # counters. Two different "10m"-ish numbers meaning different things is
        # worse than one; the stated delay moved to the tooltip.
        self.delay_label = QLabel("")
        row.addWidget(self.delay_label)

        sep = QLabel("│")
        sep.setStyleSheet("color: #ddd;")
        row.addWidget(sep)

        row.addWidget(QLabel("TV"))
        self.tv_input = self._make_input()
        self.tv_input.valueChanged.connect(self._on_tv_typed)
        row.addWidget(self.tv_input)

        swap = QLabel("⇄")
        swap.setStyleSheet("color: #888;")
        row.addWidget(swap)

        row.addWidget(QLabel("MT"))
        self.mt_input = self._make_input()
        self.mt_input.valueChanged.connect(self._on_mt_typed)
        row.addWidget(self.mt_input)

        panel_col.addLayout(row)

        # --- Deviation chart: part of the panel, hidden until the eye asks for
        # it. Carries its own span selector at the left of its time axis.
        self.chart = DeviationChart(self._history)
        self.chart.setVisible(False)
        panel_col.addWidget(self.chart)

        layout.addWidget(panel)

        # Slides the time axis between the ~5s measurements, so the chart reads
        # as live rather than frozen. Only runs while the chart is visible.
        self._chart_timer = QTimer(self)
        self._chart_timer.setInterval(1000)
        self._chart_timer.timeout.connect(self.chart.update)

        # The counters must keep counting between the ~5s measurements —
        # a frozen "10:03" is indistinguishable from a stalled feed, which is
        # exactly the condition they exist to reveal.
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self._render_readings)
        self._age_timer.start()

    # --------------------------------------------------------------- chart
    def _on_chart_toggled(self, shown: bool):
        self.chart.setVisible(shown)
        self.chart_toggle.setToolTip("Collapse" if shown else "Expand")
        if shown:
            self._sync_chart_pair()
            self.chart.update()
            self._chart_timer.start()
        else:
            self._chart_timer.stop()

    def _sync_chart_pair(self):
        """Point the chart at whatever pair the panel is showing."""
        ticker = self.pair_combo.currentData() or ""
        ev = self._diffs.get(ticker)
        self.chart.set_pair(
            ticker,
            ev.symbol if ev else "",
            ev.digits if ev else 5,
        )

    @staticmethod
    def _make_input() -> QDoubleSpinBox:
        """Converter field: free typing, no stepper clutter, wide range."""
        box = QDoubleSpinBox()
        box.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        box.setDecimals(5)
        box.setRange(0.0, 10_000_000.0)
        box.setFixedWidth(110)
        box.setKeyboardTracking(True)
        return box

    # ------------------------------------------------------------- config
    def apply_config(self, data: dict):
        """Show/hide the strip from the saved ``price_calc`` config section."""
        pc = (data or {}).get("price_calc", {}) or {}
        pairs = [p for p in (pc.get("pairs") or []) if p.get("tv") and p.get("master")]
        enabled = bool(pc.get("enabled", False)) and bool(pairs)
        self.setVisible(enabled)
        if not enabled:
            return

        # Seed the combo so the strip is present (grayed) before the first scan.
        wanted = [(p["tv"], p["master"]) for p in pairs]
        current = [
            (self.pair_combo.itemData(i), self.pair_combo.itemText(i).split(" → ")[-1])
            for i in range(self.pair_combo.count())
        ]
        if wanted != current:
            keep = self.pair_combo.currentData()
            self.pair_combo.blockSignals(True)
            self.pair_combo.clear()
            for tv, master in wanted:
                self.pair_combo.addItem(f"{tv} → {master}", tv)
            idx = self.pair_combo.findData(keep)
            self.pair_combo.setCurrentIndex(max(0, idx))
            self.pair_combo.blockSignals(False)
        # Drop cached readings for pairs that no longer exist.
        live = {tv for tv, _ in wanted}
        self._diffs = {k: v for k, v in self._diffs.items() if k in live}
        self._diff_at = {k: v for k, v in self._diff_at.items() if k in live}
        self._history.forget(live)
        self._render()

    def set_service_running(self, running: bool):
        """Offsets need a live master connection — gray out without one."""
        self._service_running = running
        if not running:
            self._diffs.clear()
            self._diff_at.clear()
            # History is deliberately kept: it is the record of what the offset
            # did, and it stays worth looking at after a stop. Only the live
            # repaint stops.
            self._chart_timer.stop()
        elif self.chart_toggle.isChecked():
            self._chart_timer.start()
        self._render()

    # ------------------------------------------------------------- events
    def handle_event(self, event: TradeEvent):
        """Consume a PRICE_DIFF event."""
        if not event.tv_ticker:
            return
        self._diffs[event.tv_ticker] = event
        self._diff_at[event.tv_ticker] = time.monotonic()

        # Only real measurements go in the history. A stale cycle reused its
        # cached offset and carries price_diff_raw == 0.0, so recording it would
        # draw a spike to zero; the gap it leaves instead is the truth.
        if not event.stale:
            self._history.append(
                event.tv_ticker, time.time(), event.price_diff_raw
            )

        if self.pair_combo.findData(event.tv_ticker) < 0:
            self.pair_combo.blockSignals(True)
            self.pair_combo.addItem(
                f"{event.tv_ticker} → {event.symbol}", event.tv_ticker
            )
            self.pair_combo.blockSignals(False)
            self.setVisible(True)

        if event.tv_ticker == self.pair_combo.currentData():
            self._render()
            if self.chart.isVisible():
                self._sync_chart_pair()
                self.chart.update()

    def _current(self) -> TradeEvent | None:
        return self._diffs.get(self.pair_combo.currentData())

    def _on_pair_changed(self):
        self._last_edited = "tv"
        ev = self._current()
        if ev and ev.tv_price:
            self._set_input(self.tv_input, ev.tv_price)
        self._render()
        self._sync_chart_pair()

    # --------------------------------------------------------- conversion
    def _on_tv_typed(self, value: float):
        self._last_edited = "tv"
        self._recalc()

    def _on_mt_typed(self, value: float):
        self._last_edited = "mt"
        self._recalc()

    @staticmethod
    def _set_input(box: QDoubleSpinBox, value: float):
        """Write a derived value without re-triggering the other direction."""
        box.blockSignals(True)
        box.setValue(value)
        box.blockSignals(False)

    def _recalc(self):
        """MT = TV + diff. The side last typed in wins; the other derives."""
        ev = self._current()
        if ev is None:
            return
        diff = ev.price_diff
        if self._last_edited == "tv":
            self._set_input(self.mt_input, self.tv_input.value() + diff)
        else:
            self._set_input(self.tv_input, self.mt_input.value() - diff)

    # ------------------------------------------------------------- render
    def _render(self):
        ev = self._current()
        usable = bool(ev) and self._service_running

        if ev is None:
            self.tv_reading.setText("—")
            self.mt_reading.setText("")
            self.diff_label.setText("")
            self.delay_label.setText("")
            self.desc_label.setText("")
            self.at_label.setText("")
            self.setEnabled(False)
            self.setToolTip(
                "Waiting for the first TradingView scan.\n"
                "Needs the service running (the master MT5 connection lives there)."
                if self._service_running else
                "Start the service — the master price comes from its MT5 connection."
            )
            return

        digits = ev.digits or 5
        self.tv_input.setDecimals(digits)
        self.mt_input.setDecimals(digits)

        sign = "+" if ev.price_diff >= 0 else ""
        # Red whenever the two prices are not from the same instant — a missing
        # side means the number beside it is not a venue offset at all, and that
        # has to be visible on the collapsed row, without opening anything.
        # Red when the two prices are not from the same instant — that number
        # is then not a venue offset at all, and it has to be visible on the
        # collapsed row without opening anything.
        #
        # Except while warming: the same absence of data, but it is the first
        # minute of a run and it clears itself. Orange, not red — red for a
        # condition that always ends on its own trains the reader to ignore it.
        broken = bool(ev.tv_missing or ev.master_missing)
        warming = bool(ev.tv_warming) and not ev.master_missing
        self.diff_label.setText(f"Δ {sign}{ev.price_diff:.{digits}f}")
        self.diff_label.setStyleSheet(
            "font-weight: bold; color: %s;" % (
                "#E65100" if warming else
                "#C62828" if broken else
                "#888" if ev.stale else "#1565C0"
            )
        )

        self.desc_label.setText(ev.description)

        if ev.stale:
            age = _fmt_age(ev.quote_age_s) if ev.quote_age_s else "not yet resolved"
            self.delay_label.setText(f"⏱ cached · {age}")
            self.delay_label.setStyleSheet("color: #E65100; font-weight: bold;")
        else:
            self.delay_label.setText("")

        self._render_readings()

        self.setEnabled(usable)
        self.setToolTip(self._tooltip(ev, digits))

        # Keep the derived field consistent with the new offset.
        if self._last_edited == "tv" and not self.tv_input.value() and ev.tv_price:
            self._set_input(self.tv_input, ev.tv_price)
        self._recalc()

    def _render_readings(self):
        """The two readings: delay in use, then the price read at it.

        Each side's counter comes from the timestamp that side ANSWERED with,
        never from what was asked for. They are equal by construction, so the
        pair is a live check rather than a label: if the broker's clock shifts,
        or a bar comes back from the wrong minute, the two numbers part company
        on screen. Nothing to infer from a strange-looking offset.

        Both keep counting between the ~5s cycles — a frozen counter and a
        stalled feed have to look different.
        """
        ev = self._current()
        if ev is None:
            return
        digits = ev.digits or 5
        since = max(0.0, time.monotonic() - self._diff_at.get(ev.tv_ticker, 0.0))

        # The minute both readings are for, carried on the event rather than
        # derived from the lookback here — derived, it drifted a second or two
        # off the boundary and printed @20:23:01, which invites the reader to
        # wonder which of the two numbers is lying.
        self.at_label.setText(
            f"@{time.strftime('%H:%M:%S', time.localtime(ev.common_at))}"
            if ev.common_at and not ev.stale else ""
        )

        def cell(label, age, price, missing, has_age):
            if missing or not has_age:
                return f"{label} —", True
            return f"{label} {_fmt_clock(age + since)} {price:.{digits}f}", False

        tv_text, tv_bad = cell(
            "TV", ev.tv_age_s, ev.tv_price, ev.tv_missing, bool(ev.tv_age_s)
        )
        if ev.tv_warming:
            tv_text = "TV warming"
        mt_text, mt_bad = cell(
            "MT5", ev.master_age_s, ev.master_price,
            ev.master_missing, bool(ev.master_age_s),
        )
        if ev.stale:
            # A cached offset has no reading of its own to age.
            tv_text, mt_text = "TV —", "MT5 —"
            tv_bad = mt_bad = False
            self.at_label.setText("")

        self.tv_reading.setText(tv_text)
        self.mt_reading.setText(mt_text)
        for widget, bad in ((self.tv_reading, tv_bad), (self.mt_reading, mt_bad)):
            widget.setStyleSheet(
                ("color: #E65100; font-weight: bold;" if ev.tv_warming
                 else "color: #C62828; font-weight: bold;") if bad
                else "color: #444;"
            )
        tip = self._readings_tooltip(ev, digits, since)
        self.tv_reading.setToolTip(tip)
        self.mt_reading.setToolTip(tip)

    def _readings_tooltip(self, ev: TradeEvent, digits: int, since: float) -> str:
        lines = [
            "Both sides are read at ONE instant, a whole minute further back "
            "than the feed's delay so the data is settled on both.",
            "Both are quantized to M1, so a new reading appears once a MINUTE "
            "while these counters tick every second.",
            "",
            f"Delay in use: {_fmt_clock(ev.lookback_s + since)}"
            f"  (feed's stated delay {_fmt_delay(ev.delay_s)} + 1m margin)",
        ]
        if ev.tv_warming:
            lines.append(
                "TradingView: still warming up — this run has not been watching "
                "the feed long enough to know its value for that instant"
            )
        elif ev.tv_missing:
            lines.append("TradingView: no published value for that instant")
        elif ev.tv_age_s:
            lines.append(
                f"TradingView: {ev.tv_price:.{digits}f}, "
                f"{_fmt_clock(ev.tv_age_s + since)} old"
            )
        if ev.master_missing:
            lines.append(f"{ev.symbol}: no M1 bar closing at that instant")
        elif ev.master_age_s:
            lines.append(
                f"{ev.symbol}: {ev.master_price:.{digits}f} (M1 close), "
                f"{_fmt_clock(ev.master_age_s + since)} old"
            )
        if ev.tv_age_s and ev.master_age_s:
            gap = abs(ev.master_age_s - ev.tv_age_s)
            lines.append(f"Gap between the two: {gap:.0f}s")
            if gap > self._AGE_GAP_WARN_S:
                lines.append(
                    "⚠ More than a minute apart — the offset is measuring the "
                    "market moving across that gap, not the venue difference."
                )
        if ev.tv_published_age_s:
            # Separate from tv_age_s: this is how long the FEED has been silent,
            # which is what tells a quiet market from a dead one.
            lines.append(
                f"TradingView last published: "
                f"{_fmt_clock(ev.tv_published_age_s + since)} ago"
            )
        if ev.master_quote_age_s:
            lines.append(
                f"{ev.symbol} latest tick: "
                f"{_fmt_clock(ev.master_quote_age_s + since)} old"
            )
        if ev.tv_missing or ev.master_missing:
            lines.append("")
            lines.append("No comparison is made until both sides answer.")
            if ev.tv_warming:
                lines.append("This clears within a minute of the service starting.")
        return "\n".join(lines)

    # Both sides are quantized to whole minutes, so a sub-minute difference is
    # the sampler working as designed. Past that it is a fault.
    _AGE_GAP_WARN_S = 60.0

    def _tooltip(self, ev: TradeEvent, digits: int) -> str:
        lines = [f"{ev.tv_ticker} → {ev.symbol}"]
        if ev.description:
            lines.append(ev.description)
        lines.append("")
        when = time.strftime("%H:%M:%S", time.localtime(time.time() - ev.lookback_s))
        lines.append(
            f"Both read at {when} — {_fmt_clock(ev.lookback_s)} back "
            f"({_fmt_delay(ev.delay_s)} feed delay + 1m settle margin)"
        )
        lines.append(
            f"  TradingView: "
            + ("no value for that instant" if ev.tv_missing
               else f"{ev.tv_price:.{digits}f}")
        )
        lines.append(
            f"  {ev.symbol} M1 close: "
            + ("no bar closing then" if ev.master_missing
               else f"{ev.master_price:.{digits}f}")
        )
        # One price series on each side. Bid, ask and the mid between them are
        # never part of the measurement — shown only because the trader asks.
        if ev.master_bid or ev.master_ask:
            lines.append(
                f"{ev.symbol} now: {ev.master_bid:.{digits}f} / "
                f"{ev.master_ask:.{digits}f} (not used)"
            )
        lines.append(f"Offset (master − TV): {ev.price_diff:.{digits}f}")
        if ev.price_diff_samples > 1:
            # The offset is a median: say so, and show how much the raw
            # measurements disagree so a wandering basis is visible.
            lines.append(
                f"  median of {ev.price_diff_samples} samples "
                f"(this one {ev.price_diff_raw:.{digits}f}, "
                f"range {ev.price_diff_spread:.{digits}f})"
            )
        lines.append("")
        lines.append("MT = TV + offset · type in either field to convert.")
        if ev.stale:
            lines.append("")
            lines.append(
                "STALE — showing the last known offset"
                + (f", measured {_fmt_age(ev.quote_age_s)}." if ev.quote_age_s else ".")
            )
        if ev.error:
            lines.append("")
            lines.append(f"⚠ {ev.error}")
        if not self._service_running:
            lines.append("")
            lines.append("Service stopped — no master price available.")
        return "\n".join(lines)


_IMPACT_COLORS = {
    "High": "#E53935",
    "Medium": "#FB8C00",
    "Low": "#7CB342",
    "Holiday": "#8E8E8E",
}


class NewsStrip(QWidget):
    """Today's news windows that will block copying, as a peer section.

    Lists only events some slave is actually armed against — an unarmed
    calendar row is noise, and the whole strip hides when nothing is armed.

    Status ticks locally off a 1 s timer rather than waiting for the service:
    the block boundary is a wall-clock fact, so the row must turn red the
    second it opens, not on the next 15-minute feed refresh.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: tuple = ()
        self._stale = False
        self._enabled = False
        # As tall as its contents — see PriceCalcStrip.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._setup_ui()
        self.setVisible(False)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._render_status)
        self._tick.setInterval(1000)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)  # matches the tab's own section spacing

        header = QHBoxLayout()
        title = QLabel("News Blocks")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color: #666;")
        header.addWidget(self.meta_label)
        layout.addLayout(header)

        panel = QFrame()
        panel.setObjectName("newsPanel")
        panel.setStyleSheet(
            "QFrame#newsPanel { border: 1px solid #ddd; background: #ffffff; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Impact", "Cur", "Event", "Window", "Status"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 2, 4, 5):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        # Height follows the CONTENT — see _fit_table_height. A fixed 150px
        # reserved room for six rows whether or not they existed, so a
        # single-event day left an empty band about one and a half rows deep
        # under the only row on screen.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        panel_layout.addWidget(self.table)
        layout.addWidget(panel)

    # ---- config / events ----

    def apply_config(self, data: dict):
        """Enable from the saved ``news`` section. Rows still come from events."""
        self._enabled = bool(((data or {}).get("news", {}) or {}).get("enabled", False))
        self._refresh_visibility()

    def handle_event(self, event: TradeEvent):
        """Consume a NEWS_UPDATE event."""
        self._rows = tuple(event.news_rows or ())
        self._stale = bool(event.news_stale)
        self._rebuild()
        self._refresh_visibility()

    def set_service_running(self, running: bool):
        """Rows are only meaningful while the service is feeding them."""
        if not running:
            self._rows = ()
            self._rebuild()
        self._refresh_visibility()

    def _refresh_visibility(self):
        visible = self._enabled and bool(self._rows)
        self.setVisible(visible)
        if visible and not self._tick.isActive():
            self._tick.start()
        elif not visible and self._tick.isActive():
            self._tick.stop()

    # ---- rendering ----

    #: Beyond this the table scrolls instead of growing. A calendar day can
    #: carry a dozen armed events, and the strip must not push the trade table
    #: off the screen to show them all.
    _MAX_VISIBLE_ROWS = 6

    def _fit_table_height(self):
        """Size the table to exactly the rows it holds, capped.

        The strip's own size policy is Fixed, so whatever this sets is what the
        section occupies. A flat 150px reserved room for six rows whether or
        not they existed, leaving an empty band about one and a half rows deep
        under the only row on a quiet day.
        """
        header = self.table.horizontalHeader().height()
        rows = self.table.rowCount()
        row_h = (
            self.table.rowHeight(0) if rows
            else self.table.verticalHeader().defaultSectionSize()
        )
        visible = min(rows, self._MAX_VISIBLE_ROWS)
        # +2 so the last row's bottom edge is not clipped by the frame.
        self.table.setFixedHeight(header + visible * row_h + 2)
        self.updateGeometry()

    @staticmethod
    def _window_text(per_slave, widest: float) -> str:
        """The Window cell: every slave's own guard, not one merged figure.

        Two slaves watching the same print for 124s and 300s is the normal
        case, and showing only the widest stated a number that was wrong for
        every slave but one. Collapsed to a single value only when they all
        agree, because "±124s" repeated five times is noise.
        """
        if not per_slave:
            return f"±{widest:g}s"
        spans = {sec for _name, sec in per_slave}
        if len(spans) == 1:
            return f"±{spans.pop():g}s"
        return " · ".join(f"{name} ±{sec:g}s" for name, sec in per_slave)

    def _rebuild(self):
        self.table.setRowCount(len(self._rows))
        for row, (ts, impact, currency, title, widest, per_slave) in enumerate(self._rows):
            time_item = QTableWidgetItem(time.strftime("%H:%M", time.localtime(ts)))
            self.table.setItem(row, 0, time_item)

            impact_item = QTableWidgetItem(impact)
            impact_item.setForeground(QBrush(QColor(_IMPACT_COLORS.get(impact, "#666"))))
            self.table.setItem(row, 1, impact_item)

            self.table.setItem(row, 2, QTableWidgetItem(currency))
            self.table.setItem(row, 3, QTableWidgetItem(title))
            window_item = QTableWidgetItem(self._window_text(per_slave, widest))
            if per_slave:
                window_item.setToolTip("\n".join(
                    f"{name}: ±{sec:g}s" for name, sec in per_slave
                ))
            self.table.setItem(row, 4, window_item)
            self.table.setItem(row, 5, QTableWidgetItem(""))
        self._fit_table_height()
        self._render_status()

        self.meta_label.setText(
            "cached calendar — feed unreachable" if self._stale else ""
        )
        self.meta_label.setStyleSheet(
            "color: #E53935;" if self._stale else "color: #666;"
        )

    def _render_status(self):
        """Upcoming → BLOCKING → passed, plus the row highlight.

        Against the WIDEST window on the row: the block is open somewhere as
        long as any slave still guards it.
        """
        now = time.time()
        for row, (ts, _impact, _cur, _title, widest, _per) in enumerate(self._rows):
            half = widest
            delta = now - ts
            if abs(delta) <= half:
                text, color = "BLOCKING", "#E53935"
            elif delta < 0:
                text, color = f"in {_fmt_countdown(-delta - half)}", "#666"
            else:
                text, color = "passed", "#bbb"

            item = self.table.item(row, 5)
            if item is None:
                item = QTableWidgetItem("")
                self.table.setItem(row, 5, item)
            item.setText(text)
            item.setForeground(QBrush(QColor(color)))
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setBackground(
                        QBrush(QColor("#FFEBEE")) if text == "BLOCKING"
                        else QBrush(QColor(0, 0, 0, 0))
                    )


def _fmt_countdown(seconds: float) -> str:
    """Rough time-until for the news table: 45s, 12m, 3h."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class StatusPill(QWidget):
    """One subsystem: its own mark, plus a dot for how it is doing.

    IDENTITY IN THE MARK, STATE IN THE DOT
        The mark never changes — it is what the pill is ABOUT, so it can be
        learned once and then recognised without reading. Everything that
        varies lives in a small dot beside it. An earlier version encoded state
        by mutating the mark (a slash through it, a hollow variant); that made
        five glyphs into fifteen, and none of them could be learned.

    WHY DRAWN AND NOT AN ICON FONT OR EMOJI
        These must render identically on the Windows box this ships to, and
        symbol-font coverage is exactly what differs between machines — a
        missing glyph is a tofu box where a status should be. Vector paths have
        no such dependency. Each mark evokes the thing it stands for:
        TradingView's own mark, a MetaTrader candle chart, a factory for
        ForexFactory, a chain link for the transport.

    THE TRADE-OFF, STATED
        State is now colour-only, which a colourblind reader cannot resolve and
        a greyscale screenshot loses. Two things carry it otherwise: every
        tooltip opens with ``Name: VALUE``, and the mark itself desaturates to
        grey when the subsystem is not running at all.
    """

    _COLORS = {
        "ok": "#2E7D32",       # green — working
        "warn": "#E65100",     # orange — degraded but still copying
        "bad": "#C62828",      # red — broken
        "off": "#9E9E9E",      # gray — not running / not configured
    }
    _MARK = "#5A6472"          # the mark's own colour while the service is up
    _MARK_IDLE = "#B6BCC4"     # ...and while it is not

    #: One box per pill: mark plus dot. Uniform, so the gaps between pills are
    #: the layout's spacing and nothing else — the old text pills were each
    #: padded to their own longest word, which no spacing value could even out.
    _W = 22
    _H = 20
    _MARK_BOX = 16.0
    _DOT_R = 2.6

    def __init__(self, kind: str, name: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._name = name
        self._level = "off"
        self.setFixedSize(self._W, self._H)
        self.set("—", "off", f"{name}: no information yet")

    def set(self, value: str, level: str, tooltip: str):
        self._level = level
        self.setToolTip(f"{self._name}: {value}\n\n{tooltip}")
        self.update()

    # ------------------------------------------------------------- painting
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        box = QRectF(0, (self._H - self._MARK_BOX) / 2, self._MARK_BOX, self._MARK_BOX)
        mark = QColor(self._MARK_IDLE if self._level == "off" else self._MARK)
        {
            "service": self._draw_service,
            "master": self._draw_master,
            "news": self._draw_news,
            "tv": self._draw_tv,
            "transport": self._draw_transport,
        }[self._kind](p, box, mark)

        # The state dot, hugging the mark's top-right corner. Close enough to
        # read as part of it rather than as a separate item on the line, and a
        # ring of background around it so it never touches a stroke.
        dot = QPointF(box.right() + 1.0, box.top() + 1.5)
        p.setPen(QPen(QColor(self.palette().window().color()), 1.6))
        p.setBrush(QBrush(QColor(self._COLORS.get(self._level, self._COLORS["off"]))))
        p.drawEllipse(dot, self._DOT_R, self._DOT_R)

    def _draw_service(self, p, box, color):
        """A power mark: the ring-and-stem every device uses for on/off."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        arc = box.adjusted(3, 3.5, -3, -2.5)
        p.drawArc(arc, -60 * 16, 300 * 16)
        cx = box.center().x()
        p.drawLine(QPointF(cx, box.top() + 2.0), QPointF(cx, box.center().y() - 0.5))

    def _draw_master(self, p, box, color):
        """MetaTrader: the candlestick chart on the terminal's own icon."""
        p.setPen(QPen(color, 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.setBrush(QBrush(color))
        # Two candles, the right one higher — the rising pair MetaTrader uses.
        # Kept well apart: at 16px, bodies any closer read as one blob.
        for x, wick_t, wick_b, body_t, body_b in (
            (box.left() + 3.6, 5.0, 14.5, 7.5, 12.5),
            (box.left() + 11.4, 1.5, 11.0, 3.5, 9.0),
        ):
            p.drawLine(QPointF(x, box.top() + wick_t), QPointF(x, box.top() + wick_b))
            p.drawRect(QRectF(x - 1.8, box.top() + body_t, 3.6, body_b - body_t))

    #: ForexFactory's own mark, traced off the 80x80 favicon their site serves
    #: (the only asset of theirs Cloudflare will hand out): a factory wall under
    #: a three-tooth sawtooth roof, with two F's cut out of it as slanted
    #: parallelograms. Their blue is #36558B; the pill draws it in the line's
    #: own colour instead, because state here is carried by the dot and a second
    #: hue on the mark would compete with it.
    _FF_BODY = ((0.0, 20.0), (26.0, 7.0), (26.0, 20.0), (52.0, 7.0),
                (52.0, 20.0), (79.0, 7.0), (79.0, 74.0), (0.0, 74.0))
    #: Each cut is the same parallelogram, placed by its top-right corner.
    _FF_CUTS = ((27.0, 20.0), (40.0, 26.5), (27.0, 46.0))

    def _draw_news(self, p, box, color):
        """ForexFactory: their factory mark, cut-outs and all."""
        path = QPainterPath()
        path.moveTo(*self._FF_BODY[0])
        for pt in self._FF_BODY[1:]:
            path.lineTo(*pt)
        path.closeSubpath()
        # Odd-even filling turns these into holes rather than more ink.
        for x, y in self._FF_CUTS:
            path.moveTo(x, y)
            path.lineTo(x - 14.0, y + 7.0)
            path.lineTo(x - 14.0, y + 20.0)
            path.lineTo(x, y + 13.0)
            path.closeSubpath()
        # Solid ink over the whole box out-weighs the stroked marks either side
        # of it, so this one is drawn a little under size to even the line out.
        self._fill_fitted(p, box, color, path, 0.86)

    @staticmethod
    def _fill_fitted(p, box, color, path, size=1.0):
        """Scale a mark from its own coordinates into the pill, and fill it.

        Marks are held in the coordinates their published asset uses, so they
        can be diffed against it; this is the one place that knows about the
        pill's size. Aspect is preserved — a squashed logo is worse than a
        small one. ``size`` trims the fit for optical weight.
        """
        src = path.boundingRect()
        scale = min(box.width() / src.width(), box.height() / src.height()) * size
        fitted = QTransform().scale(scale, scale).map(path)
        got = fitted.boundingRect()
        fitted.translate(box.center().x() - got.center().x(),
                         box.center().y() - got.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPath(fitted)

    #: TradingView's own mark, in the 24x24 coordinates their brand SVG uses:
    #: a blocky T, the dot above its shoulder, and the single diagonal stroke
    #: that stands for the V. Kept as source geometry rather than as numbers
    #: pre-scaled to this pill, so it survives a change of pill size and can be
    #: diffed against the published asset.
    _TV_T = ((9.75, 6.0), (0.0, 6.0), (0.0, 10.9038),
             (4.8462, 10.9038), (4.8462, 18.173), (9.75, 18.173))
    _TV_V = ((18.3462, 6.0), (24.0, 6.0), (18.8942, 18.173), (13.2404, 18.173))
    _TV_DOT = (13.4134, 8.2789, 2.4519)          # cx, cy, r

    def _draw_tv(self, p, box, color):
        """TradingView: their published mark, filled.

        The earlier drawing was a hand-made T-and-chevron monogram — close
        enough to read as TradingView but not actually theirs, and the odd one
        out on a line where every other pill borrows the real thing. This is
        the brand geometry, scaled into the same 16px box: solid shapes rather
        than strokes, which is how the mark is drawn and what keeps it legible
        once it is this small.
        """
        path = QPainterPath()
        for poly in (self._TV_T, self._TV_V):
            path.moveTo(*poly[0])
            for pt in poly[1:]:
                path.lineTo(*pt)
            path.closeSubpath()
        cx, cy, r = self._TV_DOT
        path.addEllipse(QPointF(cx, cy), r, r)
        self._fill_fitted(p, box, color, path)

    def _draw_transport(self, p, box, color):
        """A chain link: the connection mark Qt's own icon set uses."""
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        # Two interlocking links on a diagonal, as the standard connection
        # icon draws them.
        p.save()
        p.translate(box.center())
        p.rotate(-30)
        for dx in (-2.6, 2.6):
            p.drawRoundedRect(QRectF(dx - 3.4, -2.6, 6.8, 5.2), 2.6, 2.6)
        p.restore()


class StatusBar(QWidget):
    """The five things that decide whether a copy happens, on the button line.

    WHY HERE
        Each of these already existed somewhere — a toast, a self-hiding strip,
        a log line — so "is it working right now" had to be assembled from five
        places. Next to the buttons that change them, it is one glance.

    Every pill is driven from the same events the rest of the tab consumes;
    nothing here polls anything.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        # Every pill is the same width, so one spacing value is the whole
        # rhythm — the uneven gaps came from text-width pills, not from here.
        row.setSpacing(10)

        # Service first: the other four are meaningless while it is off.
        self.service = StatusPill("service", "Status")
        self.master = StatusPill("master", "Master")
        self.news = StatusPill("news", "News")
        self.tv = StatusPill("tv", "TV")
        self.transport = StatusPill("transport", "Transport")
        for pill in (self.service, self.master, self.news,
                     self.tv, self.transport):
            row.addWidget(pill)

        self._ea_connected: bool | None = None
        self.set_service("stopped")

    # ------------------------------------------------------------- setters
    def set_service(self, status: str):
        """Service state, in the words the buttons use.

        "Running" alone did not say running WHAT — it sat beside a green dot
        with no label, so it read as a heading rather than as one subsystem's
        state among five.
        """
        value, level, note = {
            "running": ("On", "ok",
                        "Copying. Master is polled and slaves are armed."),
            "paused": ("Paused", "warn",
                       "Connected, but new copies are suppressed.\n"
                       "Press play to resume."),
            "warming_up": ("Warming", "warn",
                           "Opening terminals and logging in to slaves."),
            "warm": ("Ready", "warn",
                     "Terminals connected and idle. Press play to start copying."),
            "starting": ("Starting", "warn", "Bringing the copy loop up."),
            "stopped": ("Off", "off",
                        "Service stopped. Nothing is being copied."),
            "setup_failed": ("Failed", "bad",
                             "Setup did not complete — see the log for which "
                             "terminal or account refused."),
            "error": ("Error", "bad",
                      "The service stopped on an error — see the log."),
        }.get(status, (status or "—", "off", ""))
        self.service.set(value, level, note)

    def set_master(self, connected: bool, detail: str = ""):
        """The master MT5 terminal. Nothing copies without it."""
        if connected:
            self.master.set("OK", "ok",
                            detail or "Master MT5 terminal connected — "
                                      "positions are being read.")
        else:
            self.master.set("NOK", "bad",
                            detail or "Master MT5 terminal not connected. "
                                      "Nothing is being copied.")

    def set_news(self, enabled: bool, armed: bool, stale: bool, rows: int = 0):
        """ForexFactory blocking: on, armed, and trustworthy?

        ``armed`` is CONFIGURATION — does any slave set a window — and is not
        the same question as whether there are events today. A quiet day, a
        weekend, or a calendar whose impacts nobody arms all give zero rows
        with the feature fully on, and reporting that as off was wrong.
        """
        if not enabled:
            self.news.set("Off", "off",
                          "News blocking is switched off.\n"
                          "Copies are never skipped for economic events.")
        elif not armed:
            self.news.set("NOK", "warn",
                          "News blocking is on, but no slave arms a window.\n"
                          "Set news_windows on a slave for it to have any effect.")
        elif stale:
            self.news.set("Cached", "warn",
                          "ForexFactory feed unreachable — blocking is running "
                          "off the last calendar fetched.\nTimes may have moved.")
        else:
            self.news.set("OK", "ok",
                          f"ForexFactory calendar loaded; {rows} armed event"
                          f"{'' if rows == 1 else 's'} today.\n"
                          "Copies inside a window will be skipped.")

    def set_news_idle(self, enabled: bool, armed: bool):
        """Configured but not running — a fact about setup, not about health."""
        if not enabled:
            self.news.set("Off", "off",
                          "News blocking is switched off.\n"
                          "Copies are never skipped for economic events.")
        elif not armed:
            self.news.set("NOK", "warn",
                          "News blocking is on, but no slave arms a window.\n"
                          "Set news_windows on a slave for it to have any effect.")
        else:
            self.news.set("Ready", "off",
                          "News blocking is configured and armed.\n"
                          "It starts enforcing when the service runs.")

    def set_tv_idle(self, configured: bool):
        """Configured but not running — no scanner calls are being made."""
        if configured:
            self.tv.set("Ready", "off",
                        "Real-price comparison is configured.\n"
                        "It starts measuring when the service runs.")
        else:
            self.tv.set("Off", "off",
                        "Real-price comparison is not configured.\n"
                        "No TradingView calls are being made.")

    def set_tv(self, error: str | None, stale: bool = False, warming: bool = False):
        """TradingView scanner: OK / NETWORK / API.

        The two failures stay separate because the fix differs: NETWORK is on
        this side of the wire, API means TradingView answered and the answer was
        unusable — a moved endpoint, an unknown ticker, a 403.
        """
        if error is None:
            self.tv.set("Off", "off",
                        "Real-price comparison is not configured.\n"
                        "No TradingView calls are being made.")
            return
        if warming:
            # Not a fault and not worth alarming about: the first minute of a
            # run, while the feed-history log grows back far enough to answer
            # for the instant being measured. It clears itself.
            self.tv.set(
                "Warming", "warn",
                "Watching the TradingView feed.\n"
                "The first reading needs its value from ~11 minutes ago, and "
                "this run has not been listening that long yet.\n"
                "Clears within a minute.",
            )
            return
        if not error:
            self.tv.set(
                "Cached" if stale else "OK", "warn" if stale else "ok",
                "TradingView scanner answering.\n" + (
                    "Showing the last known offset — no fresh measurement."
                    if stale else "Offsets are fresh."),
            )
            return
        lowered = error.lower()
        network = any(t in lowered for t in
                      ("unreachable", "timed out", "timeout", "connection",
                       "dns", "ssl"))
        self.tv.set(
            "NETWORK" if network else "API", "bad",
            ("Cannot reach TradingView from this machine.\n" if network else
             "TradingView answered, but not with a usable quote.\n") + error,
        )

    def set_transport(self, ea_connected: bool | None):
        """How master fills arrive: the EA's TCP push, or polling.

        Polling is not an error — it is the fallback and it copies, just later.
        Orange, not red.
        """
        self._ea_connected = ea_connected
        if ea_connected is None:
            self.transport.set("—", "off",
                               "Service not running — no signal path in use.")
        elif ea_connected:
            self.transport.set("TCP", "ok",
                               "CopierBridge EA is pushing fills over TCP — "
                               "the fast path.")
        else:
            self.transport.set("Poll", "warn",
                               "EA push is down; copying from MT5 API polling.\n"
                               "Still copying, with more latency.\n"
                               "Check: EA attached to a master chart + Algo "
                               "Trading enabled.")


class TradesTable(QTableWidget):
    """Table of copied positions — open above the separator, closed below."""

    COLUMNS = ["Trade ID", "Slave", "Symbol", "Type", "Volume", "Time", "Profit", "Signal"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_table()
        self._trades: dict[tuple[str, str], TradeRow] = {}  # (trade_id, slave_name) -> TradeRow
        self._row_map: dict[tuple[str, str], int] = {}
        self._separator_row = -1

    def _setup_table(self):
        """Setup table appearance."""
        self.setColumnCount(len(self.COLUMNS))
        self.setHorizontalHeaderLabels(self.COLUMNS)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)  # Signal

        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.setStyleSheet("""
            QTableWidget { gridline-color: #e0e0e0; border: 1px solid #ddd; }
            QTableWidget::item { padding: 4px; }
        """)

    def add_trade(self, trade: TradeRow, rebuild_map: bool = True):
        """Add a newly opened trade at the top (above the separator)."""
        key = (trade.trade_id, trade.slave_name)
        if key in self._trades:
            self.update_profit(trade.trade_id, trade.slave_name, trade.profit)
            return

        self._trades[key] = trade
        self.insertRow(0)
        self._populate_row(0, trade)

        # Fix the separator index BEFORE rebuilding the map: inserting at 0
        # pushed the separator down one, and _rebuild_row_map used to read the
        # stale value and skip a real trade row instead.
        if self._separator_row >= 0:
            self._separator_row += 1

        if rebuild_map:
            self._rebuild_row_map()

    def _populate_row(self, row: int, trade: TradeRow):
        """Populate table row."""
        self.setItem(row, 0, QTableWidgetItem(trade.trade_id))
        self.setItem(row, 1, QTableWidgetItem(trade.slave_name))
        self.setItem(row, 2, QTableWidgetItem(trade.symbol))

        type_str = "BUY" if trade.order_type == 0 else "SELL"
        type_item = QTableWidgetItem(type_str)
        type_item.setForeground(QColor("#4CAF50") if trade.order_type == 0 else QColor("#f44336"))
        self.setItem(row, 3, type_item)

        self.setItem(row, 4, QTableWidgetItem(f"{trade.volume:.2f}"))

        time_str = datetime.fromtimestamp(trade.open_time).strftime("%H:%M:%S") if trade.open_time else ""
        self.setItem(row, 5, QTableWidgetItem(time_str))

        profit_item = QTableWidgetItem(f"${trade.profit:.2f}")
        profit_item.setForeground(QColor("#4CAF50") if trade.profit >= 0 else QColor("#f44336"))
        profit_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, 6, profit_item)

        # Signal: transport + copy delay + waterfall breakdown
        # (e.g. "TCP 220ms · L12/P3/B205"). Full labels in the tooltip.
        if trade.transport:
            sig = f"{trade.transport} {trade.delay_ms:.0f}ms" if trade.delay_ms > 0 else trade.transport
            legs = _leg_parts(trade)
            if legs:
                sig += "  ·  " + "/".join(legs)
        else:
            sig = ""
        sig_item = QTableWidgetItem(sig)
        sig_item.setForeground(QColor("#2E7D32") if trade.transport == "TCP" else QColor("#888"))
        sig_item.setToolTip(_leg_tooltip(trade))
        self.setItem(row, 7, sig_item)

    def update_profit(self, trade_id: str, slave_name: str, profit: float):
        """Update profit for a trade."""
        key = (trade_id, slave_name)
        if key not in self._trades:
            return
        self._trades[key].profit = profit
        if key in self._row_map:
            row = self._row_map[key]
            item = QTableWidgetItem(f"${profit:.2f}")
            item.setForeground(QColor("#4CAF50") if profit >= 0 else QColor("#f44336"))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setItem(row, 6, item)

    def update_volume(self, trade_id: str, slave_name: str, volume: float):
        """Update the displayed volume after a partial scale-out."""
        key = (trade_id, slave_name)
        if key not in self._trades:
            return
        self._trades[key].volume = volume
        if key in self._row_map:
            self.setItem(self._row_map[key], 4, QTableWidgetItem(f"{volume:.2f}"))

    def close_trade(self, trade_id: str, slave_name: str, profit: float,
                    rebuild_map: bool = True):
        """Move a trade below the separator into the closed section."""
        key = (trade_id, slave_name)
        if key not in self._trades:
            return

        trade = self._trades[key]
        if trade.is_closed:
            # Already moved. TRADE_CLOSED can arrive twice for one position —
            # the success and failure branches of _close_copied_positions both
            # emit it — and re-running would insert a second closed row.
            return

        trade.is_closed = True
        trade.profit = profit
        trade.close_time = time.time()

        if key not in self._row_map:
            # Map went stale. Rebuild rather than skip the removal: leaving the
            # open row in place is what strands a closed position in the open
            # section, and it never gets another chance to be removed.
            self._rebuild_row_map()

        if key in self._row_map:
            old_row = self._row_map[key]
            self.removeRow(old_row)
            if self._separator_row > old_row:
                self._separator_row -= 1

        if self._separator_row < 0:
            self._add_separator()

        new_row = self._separator_row + 1
        self.insertRow(new_row)
        self._populate_row(new_row, trade)

        for col in range(self.columnCount()):
            item = self.item(new_row, col)
            if item:
                item.setForeground(QColor("#888"))

        if rebuild_map:
            self._rebuild_row_map()

    def _add_separator(self):
        """Add separator row at the end of the open rows.

        Uses rowCount(), not a count derived from `_trades`: the separator is
        only ever created when none exists, which means no closed rows are on
        screen yet, so every current row is an open one. Counting `_trades`
        instead put the separator a row too high whenever the model and the
        table had drifted apart — pushing a genuinely open position into the
        closed section.
        """
        sep_row = self.rowCount()
        self.insertRow(sep_row)
        self._separator_row = sep_row

        sep_item = QTableWidgetItem("─── Closed Trades ───")
        sep_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        sep_item.setForeground(QColor("#999"))
        sep_item.setBackground(QBrush(QColor("#f5f5f5")))
        self.setItem(sep_row, 0, sep_item)
        self.setSpan(sep_row, 0, 1, self.columnCount())

    def _rebuild_row_map(self):
        """Rebuild row map.

        Deliberately does NOT consult `_separator_row`: the separator sets only
        column 0 (spanned across the rest), so `slave_item is None` already
        excludes it. Keying off the index instead made this function wrong
        whenever the index was mid-update, and a trade row skipped here loses
        its entry — after which close_trade cannot find the row to remove and
        the position stays visible in the open section forever.
        """
        self._row_map.clear()
        for row in range(self.rowCount()):
            id_item = self.item(row, 0)
            slave_item = self.item(row, 1)
            if id_item and slave_item:
                self._row_map[(id_item.text(), slave_item.text())] = row

    def clear_trades(self):
        """Drop every row — open, closed and the separator.

        Called when a new service session starts. Rows describe positions the
        copier is currently managing; a previous run's rows are stale the moment
        it stops, and the incoming session repopulates the genuinely-open ones
        via emit_existing_positions().
        """
        self.setRowCount(0)
        self._trades.clear()
        self._row_map.clear()
        self._separator_row = -1

    def get_active_trades(self) -> list[TradeRow]:
        return [t for t in self._trades.values() if not t.is_closed]


class TradesTab(QWidget):
    """Service controls + active trades table."""

    # Sections are stacked in one column; only the trade table carries a stretch
    # factor, so a new strip added anywhere in _setup_ui takes its natural height
    # and the table gives up exactly that much. Nothing to rebalance.
    #
    # The one requirement on a new section: a vertically Fixed size policy, so
    # it states how tall it is. Without that the table's stretch factor wins and
    # compresses it to a sliver.

    # Service control signals
    warmup_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)  # True = paused

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_paused = False
        self._config_data: dict = {}
        self._state = "stopped"  # stopped | warming_up | warm | starting | running | paused
        # The master terminal as the service last measured it. None means it
        # has not answered yet — distinct from False, which is a dead terminal.
        self._master_measured: bool | None = None
        # Set for good when the daily loss limit trips — see lock_out().
        self._locked_out = False
        self._setup_ui()

        # Poll timer - UI polls events at its own pace (never blocks copier)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_events)

    def _setup_ui(self):
        """Setup UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Service control bar
        ctrl = QHBoxLayout()

        self._warmup_icon = QIcon(str(_ICONS / "chevron-smooth-right.svg"))
        self._play_icon = QIcon(str(_ICONS / "play.svg"))
        self._pause_icon = QIcon(str(_ICONS / "pause.svg"))
        self._stop_icon = QIcon(str(_ICONS / "stop.svg"))

        self.warmup_btn = QToolButton()
        self.warmup_btn.setIcon(self._warmup_icon)
        self.warmup_btn.setToolTip("Warm Up")
        self.warmup_btn.clicked.connect(self.warmup_requested.emit)
        ctrl.addWidget(self.warmup_btn)

        self.play_pause_btn = QToolButton()
        self.play_pause_btn.setIcon(self._play_icon)
        self.play_pause_btn.setToolTip("Start")
        self.play_pause_btn.setEnabled(False)
        self.play_pause_btn.clicked.connect(self._on_play_pause)
        ctrl.addWidget(self.play_pause_btn)

        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(self._stop_icon)
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        ctrl.addWidget(self.stop_btn)

        ctrl.addStretch()

        # Every subsystem state, in one shape, on the line with the buttons that
        # change them. Replaces a coloured dot with an unlabelled word plus a
        # separate bold "EA: TCP" — two designs for the same kind of fact.
        self.status_bar = StatusBar()
        ctrl.addWidget(self.status_bar)

        layout.addLayout(ctrl)

        # Daily stop limit banner. Hidden until the limit trips, then permanent
        # for the rest of the session: the modal that announces it is dismissed
        # in a second and the state it announced lasts all day, so the state
        # needs somewhere to live that is not a memory of a dialog.
        self.lockout_banner = QLabel()
        self.lockout_banner.setWordWrap(True)
        self.lockout_banner.setVisible(False)
        self.lockout_banner.setStyleSheet(
            "QLabel {"
            "  background-color: #C62828; color: #FFFFFF;"
            "  padding: 8px 12px; border-radius: 4px; font-weight: bold;"
            "}"
        )
        layout.addWidget(self.lockout_banner)

        # Real price calculator — one compact line, hidden unless configured.
        self.price_strip = PriceCalcStrip()
        layout.addWidget(self.price_strip)

        self.news_strip = NewsStrip()
        layout.addWidget(self.news_strip)

        # Header
        header = QHBoxLayout()
        title = QLabel("Active Trades")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()

        self.total_label = QLabel("Total: 0")
        self.total_label.setStyleSheet("color: #666;")
        header.addWidget(self.total_label)

        self.profit_label = QLabel("P/L: $0.00")
        self.profit_label.setStyleSheet("color: #666; margin-left: 15px;")
        header.addWidget(self.profit_label)

        layout.addLayout(header)

        # Table takes ALL the height the strips above it do not want.
        #
        # It is the only child with a stretch factor, and that is the point: a
        # widget added anywhere in this column gets its natural height and the
        # table gives up exactly that much. The previous version paired
        # `addWidget(table, 1)` with a trailing `addStretch(1)`, which split the
        # free space evenly with a piece of nothing — the table was pinned at
        # half the tab and every new strip needed the numbers rebalanced.
        self.table = TradesTable()
        layout.addWidget(self.table, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch()
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #999; font-size: 10px;")
        footer.addWidget(self.stats_label)
        layout.addLayout(footer)

    def start_polling(self, interval_ms: int = 33):
        """Start polling events from queue.

        Default 33ms (~30 fps) for smooth UI updates.
        """
        self._poll_timer.start(interval_ms)

    def stop_polling(self):
        """Stop polling."""
        self._poll_timer.stop()

    def load_trades(self, trades: list[TradeRow]):
        """Load existing trades (crash recovery / history restore).

        Open trades are added above the separator, closed ones below it
        (grayed out). Batched: paint suppression + single row-map rebuild.
        """
        self.table.setUpdatesEnabled(False)
        try:
            open_trades = [t for t in trades if not t.is_closed]
            closed_trades = [t for t in trades if t.is_closed]

            for trade in open_trades:
                self.table.add_trade(trade, rebuild_map=False)

            for trade in closed_trades:
                self.table.add_trade(trade, rebuild_map=False)
                self.table.close_trade(
                    trade.trade_id, trade.slave_name, trade.profit,
                    rebuild_map=False,
                )

            self.table._rebuild_row_map()
        finally:
            self.table.setUpdatesEnabled(True)

        self._update_stats()

    def _poll_events(self):
        """Poll events from queue and update UI."""
        queue = get_ui_queue()
        events = queue.poll_all()

        for event in events:
            self._handle_event(event)

        if events:
            self._update_stats()

        # Show dropped count if any
        dropped = queue.dropped_count
        if dropped > 0:
            self.stats_label.setText(f"Events dropped: {dropped}")

    def _handle_event(self, event: TradeEvent):
        """Handle a single event."""
        if event.event_type == EventType.TRADE_OPENED:
            trade = TradeRow(
                trade_id=event.trade_id,
                slave_name=event.slave_name,
                slave_ticket=event.slave_ticket,
                master_ticket=event.master_ticket,
                symbol=event.symbol,
                order_type=event.order_type,
                volume=event.volume,
                open_time=event.open_time or event.timestamp,
                open_price=event.open_price,
                profit=event.profit,
                transport=event.transport,
                delay_ms=event.delay_ms,
                local_ms=event.local_ms,
                ping_ms=event.ping_ms,
                broker_ms=event.broker_ms,
            )
            self.table.add_trade(trade)

        elif event.event_type == EventType.TRADE_CLOSED:
            self.table.close_trade(event.trade_id, event.slave_name, event.profit)

        elif event.event_type == EventType.TRADE_REDUCED:
            self.table.update_volume(event.trade_id, event.slave_name, event.volume)

        elif event.event_type == EventType.PROFIT_UPDATE:
            self.table.update_profit(event.trade_id, event.slave_name, event.profit)

        elif event.event_type == EventType.NEWS_UPDATE:
            self.news_strip.handle_event(event)
            # From the event's own flags, not from whether there are rows: an
            # empty table is the normal state on a quiet day and said nothing
            # about whether blocking was on — which is why an enabled, armed
            # calendar reported itself as off.
            self.status_bar.set_news(
                enabled=bool(event.news_enabled),
                armed=bool(event.news_armed),
                stale=bool(event.news_stale),
                rows=len(event.news_rows or ()),
            )
        elif event.event_type == EventType.MASTER_STATUS:
            self.set_master_status(
                bool(event.master_connected), event.master_algo
            )
        elif event.event_type == EventType.PRICE_DIFF:
            self.price_strip.handle_event(event)
            # Warming is reported as its own state, not as an error: measured
            # over three restarts it lasts 35-75s and then resolves for the rest
            # of the run, with the feed answering perfectly throughout.
            self.status_bar.set_tv(
                "" if event.tv_warming else (event.error or ""),
                stale=bool(event.stale),
                warming=bool(event.tv_warming),
            )

    def _update_stats(self):
        """Update stats labels."""
        active = self.table.get_active_trades()
        total_profit = sum(t.profit for t in active)

        self.total_label.setText(f"Total: {len(active)}")
        color = "#4CAF50" if total_profit >= 0 else "#f44336"
        self.profit_label.setText(f"P/L: ${total_profit:.2f}")
        self.profit_label.setStyleSheet(f"color: {color}; margin-left: 15px; font-weight: bold;")

    def _on_play_pause(self):
        """Handle play/pause button click — state machine.

        Only handles warm → start, running → pause, paused → resume.
        Warmup is triggered separately via the Warm Up button.
        """
        if self._state == "warm":
            self.start_requested.emit()
        elif self._state == "running":
            self.pause_toggled.emit(True)
            self.update_status("paused")
        elif self._state == "paused":
            self.pause_toggled.emit(False)
            self.update_status("running")

    def apply_config(self, data: dict):
        """Seed the status pills from the saved config.

        Without this the News and TV pills sit at "no information yet" until the
        service runs and emits, which is when they matter least — the question
        "is blocking even switched on" is asked before pressing play.

        The config is remembered so the same pills can go back to reporting it
        when the service stops: a live reading from a run that has ended is not
        news, it is a leftover.
        """
        data = self._config_data = data or {}
        news = data.get("news", {}) or {}
        slaves = data.get("slaves", []) or []
        armed = any(
            any(float(v or 0) > 0 for v in (
                s.get("news_windows_s") or s.get("news_windows") or {}
            ).values())
            for s in slaves if isinstance(s, dict)
        )
        pc = data.get("price_calc", {}) or {}
        pairs = [p for p in (pc.get("pairs") or []) if p.get("tv") and p.get("master")]

        # Two different facts, told apart deliberately:
        #   not configured        -> "Off", and it will stay off
        #   configured, not live  -> "—",  it will report once the service runs
        # Collapsing them into one green "OK" while nothing is running would be
        # the only reassuring thing on the line that had not been checked.
        news_on = bool(news.get("enabled", False))
        tv_on = bool(pc.get("enabled")) and bool(pairs)
        if self._state in ("running", "paused"):
            self.status_bar.set_news(enabled=news_on, armed=armed, stale=False)
            self.status_bar.set_tv("" if tv_on else None)
            return
        self.status_bar.set_news_idle(news_on, armed)
        self.status_bar.set_tv_idle(tv_on)

    def lock_out(self, message: str):
        """Freeze the controls for the rest of the day. Not reversible.

        WHY IT STILL RUNS WHEN THE APP IS ABOUT TO CLOSE
            A trip raises a full-screen wall whose button closes the app, so
            this freeze lasts seconds. It is applied first anyway: the wall is
            modal, and for the moment between reading it and the process
            ending, nothing behind it should be a control that starts copying.

            It also carries the state on its own whenever the wall does not —
            a launch that finds the day already spent locks these controls
            before the service refuses to start.

        There is deliberately no way to undo this from the UI. Restarting the
        app does not help either — the count is re-read from the broker's
        history — and that is the point of the rule.
        """
        self._locked_out = True
        self.lockout_banner.setText(message)
        self.lockout_banner.setVisible(True)
        self._apply_lockout()

    def _apply_lockout(self):
        """Disable everything that could start copying again.

        Called after every status change as well, because update_status
        re-enables these buttons per state and would otherwise hand them back.
        Stop stays live: ending the session must always be possible.
        """
        if not self._locked_out:
            return
        for button in (self.warmup_btn, self.play_pause_btn):
            button.setEnabled(False)
            button.setToolTip("Daily stop limit reached — copying is finished for today.")

    def set_master_status(self, connected: bool, algo: bool | None):
        """The master as the SERVICE measured it — the authoritative source.

        Until this arrives the pill falls back to "is the service running",
        which is a guess: it was reporting the master as OK with the terminal
        closed, because nothing had ever asked the terminal.
        """
        self._master_measured = connected
        if not connected:
            self.status_bar.set_master(
                False,
                "Master MT5 terminal is not responding. Nothing is being "
                "copied — relaunch it from the Terminals tab.",
            )
        elif algo is False:
            # Connected, and useless: orders will not be placed. Worth its own
            # wording, since "OK" here would be a lie of the expensive kind.
            self.status_bar.master.set(
                "ALGO OFF", "warn",
                "Master terminal is connected but Algo Trading is OFF.\n"
                "Enable it in the terminal's toolbar or nothing will trade.",
            )
        else:
            self.status_bar.set_master(
                True, "Master MT5 terminal connected — positions are being read."
            )

    def set_ea_status(self, connected: bool):
        """EA push up (TCP, fast) or down (polling fallback, still copying)."""
        self._ea_connected = connected
        self.status_bar.set_transport(connected)

    def update_status(self, status: str):
        """Update status indicator from service status changes.

        States: stopped, warming_up, warm, starting, running, paused, setup_failed, error.
        """
        previous, self._state = self._state, status
        # Whatever this method decides about the buttons below, the lockout
        # overrides it at the end — see _apply_lockout.
        # The price calculator reads the master through the service — no
        # service, no offset.
        live = status in ("running", "paused")
        self.price_strip.set_service_running(live)
        self.news_strip.set_service_running(live)

        # One pill owns the service state now, in the same shape as the other
        # four — the old dot-plus-word had no label and read as a heading.
        self.status_bar.set_service(status)

        # A stopped service means no connection, so the master is down by
        # definition. A RUNNING service says nothing about the terminal —
        # that answer comes from MASTER_STATUS, which measures it. This used
        # to light the pill green off `live` alone, so killing the master
        # terminal left it reporting OK.
        if not live:
            self._master_measured = None
            self.status_bar.set_master(
                False,
                f"Service is {status.replace('_', ' ')}; the master connection "
                "is not open, so nothing is being copied.",
            )
        elif self._master_measured is None:
            self.status_bar.master.set(
                "…", "off", "Waiting for the first master health check.",
            )
        if not live:
            self.status_bar.set_transport(None)
            # Back to what the config says: the last live reading belonged to a
            # run that has ended.
            self.apply_config(self._config_data)
        elif getattr(self, "_ea_connected", None) is not None:
            self.status_bar.set_transport(self._ea_connected)

        # A new session owns the table. Without this, rows from the previous
        # run linger in "Active Trades" forever — their closes were emitted
        # while that run was alive and will never be emitted again, so the
        # table keeps showing copies of positions that are long gone.
        if status == "starting" and previous != "starting":
            self.table.clear_trades()
            self._update_stats()

        if status == "running":
            self._is_paused = False
            self.play_pause_btn.setIcon(self._pause_icon)
            self.play_pause_btn.setToolTip("Pause")
            self.warmup_btn.setEnabled(False)
            self.play_pause_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
        elif status == "warming_up":
            self.warmup_btn.setEnabled(False)
            self.play_pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
        elif status == "warm":
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setToolTip("Start")
            self.warmup_btn.setEnabled(False)
            self.play_pause_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
        elif status == "stopped":
            self._is_paused = False
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setToolTip("Start")
            self.warmup_btn.setEnabled(True)
            self.play_pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
        elif status in ("setup_failed", "error"):
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setToolTip("Start")
            self.warmup_btn.setEnabled(True)
            self.play_pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
        elif status == "starting":
            self.warmup_btn.setEnabled(False)
            self.play_pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
        elif status == "paused":
            self._is_paused = True
            self.play_pause_btn.setIcon(self._play_icon)
            self.play_pause_btn.setToolTip("Resume")
            self.warmup_btn.setEnabled(False)
            self.play_pause_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)

        # Last word, always: a status change must never hand back a control the
        # daily stop limit took away.
        self._apply_lockout()
