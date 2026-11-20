"""Toast notification system for trade events.

Modern dark-themed notifications with colored accents, icons, slide-in
animations, and PnL badges. Positioned at bottom-left of primary screen.
Works even when main window is minimized (Qt.Tool flag).
"""
import logging
from enum import Enum

_logger = logging.getLogger(__name__)

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QColor, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..events import EventType, TradeEvent


def _signal_suffix(event: TradeEvent) -> str:
    """Compact transport + copy-delay tag for toasts, e.g. '  ·  TCP 8ms'.

    When the latency waterfall legs are available, appends the breakdown:
    local (our processing), ping (transport), broker (fill), e.g.
    '  ·  TCP 220ms  (local 12 · ping 3 · broker 205)'.
    """
    if not event.transport:
        return ""
    head = event.transport
    if event.delay_ms and event.delay_ms > 0:
        head = f"{event.transport} {event.delay_ms:.0f}ms"
    legs = _latency_legs(event)
    return f"  ·  {head}{legs}"


def _latency_legs(event: TradeEvent) -> str:
    """Waterfall breakdown '  (local 12 · ping 3 · broker 205)' or '' if none."""
    parts = []
    if event.local_ms and event.local_ms > 0:
        parts.append(f"local {event.local_ms:.0f}")
    if event.ping_ms and event.ping_ms > 0:
        parts.append(f"ping {event.ping_ms:.0f}")
    if event.broker_ms and event.broker_ms > 0:
        parts.append(f"broker {event.broker_ms:.0f}")
    if not parts:
        return ""
    return "  (" + " · ".join(parts) + ")"


class ToastType(Enum):
    OPEN = "open"
    CLOSE = "close"
    UPDATE = "update"  # SL/TP moved or partial scale-out
    ERROR = "error"


class ToastPosition(Enum):
    OFF = "Off"
    BOTTOM_LEFT = "Bottom-Left"
    BOTTOM_RIGHT = "Bottom-Right"
    TOP_LEFT = "Top-Left"
    TOP_RIGHT = "Top-Right"


# Visual style per toast type
_STYLE = {
    ToastType.OPEN: {
        "accent": "#00E676",
        "icon_bg": "#1B5E20",
        "icon_fg": "#00E676",
        "card_bg": "#1A1A2E",
        "card_border": "#2A2A3E",
    },
    ToastType.CLOSE: {
        "accent": "#448AFF",
        "icon_bg": "#0D47A1",
        "icon_fg": "#82B1FF",
        "card_bg": "#1A1A2E",
        "card_border": "#2A2A3E",
    },
    ToastType.UPDATE: {
        "accent": "#FFB300",
        "icon_bg": "#7A5200",
        "icon_fg": "#FFD54F",
        "card_bg": "#1A1A2E",
        "card_border": "#2A2A3E",
    },
    ToastType.ERROR: {
        "accent": "#FF5252",
        "icon_bg": "#B71C1C",
        "icon_fg": "#FF8A80",
        "card_bg": "#2E1A1A",
        "card_border": "#3E2A2A",
    },
}

_CARD_WIDTH = 370
_SHADOW_PAD = 10  # extra padding so the drop shadow isn't clipped


class ToastWidget(QWidget):
    """Single dark-themed toast notification with icon and accent bar."""

    closed = None  # callback set by ToastManager

    def __init__(
        self,
        title: str,
        message: str,
        toast_type: ToastType = ToastType.OPEN,
        duration_ms: int = 4000,
        icon: str = "",
        pnl: float | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.toast_type = toast_type
        self.duration_ms = duration_ms

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(_CARD_WIDTH + _SHADOW_PAD * 2)

        self._build(
            title, message, toast_type,
            icon or self._default_icon(toast_type), pnl,
        )

        # Auto-dismiss timer
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._timer.start(duration_ms)

        # prevent GC of animation objects
        self._anim_slide = None
        self._anim_fade = None

    # ------------------------------------------------------------------ build
    def _build(self, title, message, toast_type, icon, pnl):
        style = _STYLE[toast_type]

        # -- card (dark rounded rectangle with colored top accent)
        card = QWidget(self)
        card.setObjectName("tc")
        card.setStyleSheet(
            f"#tc {{"
            f"  background-color: {style['card_bg']};"
            f"  border: 1px solid {style['card_border']};"
            f"  border-top: 3px solid {style['accent']};"
            f"  border-radius: 10px;"
            f"}}"
        )

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setXOffset(0)
        shadow.setYOffset(6)
        shadow.setColor(QColor(0, 0, 0, 140))
        card.setGraphicsEffect(shadow)

        row = QHBoxLayout(card)
        row.setContentsMargins(14, 14, 14, 14)
        row.setSpacing(14)

        # -- icon circle (44 x 44)
        ic = QLabel(icon)
        ic.setFixedSize(44, 44)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ic.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        ic.setStyleSheet(
            f"background-color: {style['icon_bg']};"
            f"color: {style['icon_fg']};"
            f"border-radius: 22px; border: none;"
        )
        row.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)

        # -- text column
        col = QVBoxLayout()
        col.setSpacing(4)
        col.setContentsMargins(0, 0, 0, 0)

        t = QLabel(title)
        t.setStyleSheet(
            "color: #FFFFFF; font-weight: bold; font-size: 13px;"
            "background: transparent; border: none;"
        )
        col.addWidget(t)

        if message:
            m = QLabel(message)
            m.setWordWrap(True)
            m.setStyleSheet(
                "color: #999; font-size: 12px;"
                "background: transparent; border: none;"
            )
            col.addWidget(m)

        # -- PnL badge (close events only)
        if pnl is not None:
            positive = pnl >= 0
            text = f"+${pnl:.2f}" if positive else f"-${abs(pnl):.2f}"
            fg = "#00E676" if positive else "#FF5252"
            bg = "#1B3A1B" if positive else "#3A1B1B"
            bd = "#2B5A2B" if positive else "#5A2B2B"

            badge = QLabel(text)
            badge.setStyleSheet(
                f"color: {fg}; background-color: {bg};"
                f"border: 1px solid {bd}; border-radius: 4px;"
                f"padding: 3px 10px; font-weight: bold; font-size: 14px;"
            )
            badge.setMaximumWidth(badge.sizeHint().width() + 24)
            col.addWidget(badge)

        row.addLayout(col, 1)

        # -- outer layout with padding for shadow
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW_PAD, _SHADOW_PAD, _SHADOW_PAD, _SHADOW_PAD)
        outer.addWidget(card)

    # --------------------------------------------------------- default icons
    @staticmethod
    def _default_icon(toast_type):
        return {
            ToastType.OPEN: "\u25b2",   # ▲
            ToastType.CLOSE: "\u2713",  # ✓
            ToastType.UPDATE: "\u270e",  # \u270e
            ToastType.ERROR: "!",
        }.get(toast_type, "\u25cf")

    # ------------------------------------------------------------ animations
    def slide_in_from(self, start: QPoint, end: QPoint):
        """Animate the toast sliding from *start* to *end*."""
        self.move(start)
        self._anim_slide = QPropertyAnimation(self, b"pos")
        self._anim_slide.setDuration(350)
        self._anim_slide.setStartValue(start)
        self._anim_slide.setEndValue(end)
        self._anim_slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_slide.start()

    def _fade_out(self):
        self._anim_fade = QPropertyAnimation(self, b"windowOpacity")
        self._anim_fade.setDuration(400)
        self._anim_fade.setStartValue(1.0)
        self._anim_fade.setEndValue(0.0)
        self._anim_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_fade.finished.connect(self.close)
        self._anim_fade.start()

    # -------------------------------------------------------------- events
    def closeEvent(self, event):
        if self.closed:
            self.closed(self)
        super().closeEvent(event)

    def mousePressEvent(self, event):
        """Click anywhere to dismiss."""
        self._timer.stop()
        self.close()


# ======================================================================
# ToastManager
# ======================================================================

class ToastManager:
    """Manages toast stack: positioning, capacity, slide-in animations."""

    def __init__(self, max_visible: int = 4, margin: int = 16, spacing: int = 8):
        self.max_visible = max_visible
        self.margin = margin
        self.spacing = spacing
        self._active: list[ToastWidget] = []
        self._position = ToastPosition.BOTTOM_RIGHT

    def set_position(self, pos_str: str):
        """Set toast position from string (e.g. 'Bottom-Right'). Fallback BOTTOM_RIGHT."""
        for p in ToastPosition:
            if p.value == pos_str:
                self._position = p
                return
        self._position = ToastPosition.BOTTOM_RIGHT

    def show_toast(
        self,
        title: str,
        message: str,
        toast_type: ToastType = ToastType.OPEN,
        duration_ms: int = 4000,
        icon: str = "",
        pnl: float | None = None,
    ):
        """Show a toast notification with slide-in animation."""
        if self._position == ToastPosition.OFF:
            return

        # Drop oldest when at capacity
        while len(self._active) >= self.max_visible:
            oldest = self._active[0]
            oldest.closed = None
            oldest.close()
            self._active.pop(0)

        toast = ToastWidget(title, message, toast_type, duration_ms, icon, pnl)
        toast.closed = self._on_closed
        self._active.append(toast)

        # Compute target position, slide from off-screen (left or right)
        target = self._target_position(toast, len(self._active) - 1)
        is_left = self._position in (ToastPosition.BOTTOM_LEFT, ToastPosition.TOP_LEFT)
        if is_left:
            off_screen = QPoint(target.x() - _CARD_WIDTH - 40, target.y())
        else:
            off_screen = QPoint(target.x() + _CARD_WIDTH + 40, target.y())
        toast.show()
        toast.slide_in_from(off_screen, target)

        # Reposition older toasts (shift up) with animation
        self._reposition()

    def _on_closed(self, toast: ToastWidget):
        if toast in self._active:
            self._active.remove(toast)
            self._reposition()

    def _get_x(self, geom) -> int:
        """X coordinate based on position corner."""
        if self._position in (ToastPosition.BOTTOM_LEFT, ToastPosition.TOP_LEFT):
            return geom.left() + self.margin
        return geom.right() - self.margin - _CARD_WIDTH - _SHADOW_PAD * 2

    def _is_bottom(self) -> bool:
        return self._position in (ToastPosition.BOTTOM_LEFT, ToastPosition.BOTTOM_RIGHT)

    def _target_position(self, toast: ToastWidget, index: int) -> QPoint:
        """Target screen position for toast at *index*."""
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return QPoint(0, 0)
        geom = screen.availableGeometry()
        x = self._get_x(geom)

        if self._is_bottom():
            # Stack from bottom: newest at bottom, older above
            y = geom.bottom() - self.margin
            for i in range(len(self._active) - 1, -1, -1):
                t = self._active[i]
                t.adjustSize()
                h = t.sizeHint().height()
                y -= h
                if i == index:
                    return QPoint(x, y)
                y -= self.spacing
            return QPoint(x, y)
        else:
            # Stack from top: newest at top, older below
            y = geom.top() + self.margin
            for i in range(len(self._active) - 1, -1, -1):
                t = self._active[i]
                t.adjustSize()
                h = t.sizeHint().height()
                if i == index:
                    return QPoint(x, y)
                y += h + self.spacing
            return QPoint(x, y)

    def _reposition(self):
        """Smoothly reposition all active toasts."""
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geom = screen.availableGeometry()
        x = self._get_x(geom)

        if self._is_bottom():
            y = geom.bottom() - self.margin
            for toast in reversed(self._active):
                toast.adjustSize()
                h = toast.sizeHint().height()
                y -= h
                target = QPoint(x, y)
                self._animate_to(toast, target)
                y -= self.spacing
        else:
            y = geom.top() + self.margin
            for toast in reversed(self._active):
                toast.adjustSize()
                h = toast.sizeHint().height()
                target = QPoint(x, y)
                self._animate_to(toast, target)
                y += h + self.spacing

    def _animate_to(self, toast: ToastWidget, target: QPoint):
        """Animate toast to target position (or move instantly if not yet visible)."""
        if toast.isVisible() and toast.pos() != target:
            anim = QPropertyAnimation(toast, b"pos")
            anim.setDuration(200)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start()
            toast._anim_slide = anim  # prevent GC
        else:
            toast.move(target)

    # ============================================================ events
    def on_trade_event(self, event: TradeEvent):
        """Convert TradeEvent → toast (called from main thread via Qt signal)."""
        _logger.debug("Toast event: %s %s %s", event.event_type.name, event.symbol, event.slave_name)

        try:
            if event.event_type == EventType.TRADE_OPENED:
                is_buy = event.order_type == 0
                side = "BUY" if is_buy else "SELL"
                icon = "\u25b2" if is_buy else "\u25bc"  # ▲ or ▼

                if event.master_volume and event.master_volume != event.volume:
                    vol = f"{event.master_volume:.2f} \u2192 {event.volume:.2f} lots"
                else:
                    vol = f"{event.volume:.2f} lots"

                self.show_toast(
                    title=f"{side}  {event.symbol}",
                    message=f"{vol}  \u00b7  {event.slave_name}{_signal_suffix(event)}",
                    toast_type=ToastType.OPEN,
                    duration_ms=4000,
                    icon=icon,
                )

            elif event.event_type == EventType.TRADE_CLOSED:
                side = "BUY" if event.order_type == 0 else "SELL"
                self.show_toast(
                    title=f"CLOSED {side}  {event.symbol}",
                    message=f"{event.volume:.2f} lots  \u00b7  {event.slave_name}{_signal_suffix(event)}",
                    toast_type=ToastType.CLOSE,
                    duration_ms=5000,
                    icon="\u2713",  # ✓
                    pnl=event.profit,
                )

            elif event.event_type == EventType.TRADE_MODIFIED:
                side = "BUY" if event.order_type == 0 else "SELL"
                bits = []
                if event.sl:
                    bits.append(f"SL {event.sl:g}")
                if event.tp:
                    bits.append(f"TP {event.tp:g}")
                levels = "  ·  ".join(bits) if bits else "SL/TP cleared"
                self.show_toast(
                    title=f"SL/TP  {side}  {event.symbol}",
                    message=f"{levels}  ·  {event.slave_name}{_signal_suffix(event)}",
                    toast_type=ToastType.UPDATE,
                    duration_ms=4000,
                    icon="✎",  # ✎
                )

            elif event.event_type == EventType.TRADE_REDUCED:
                side = "BUY" if event.order_type == 0 else "SELL"
                self.show_toast(
                    title=f"REDUCED  {side}  {event.symbol}",
                    message=(
                        f"-{event.reduced_volume:.2f} → {event.volume:.2f} lots"
                        f"  ·  {event.slave_name}{_signal_suffix(event)}"
                    ),
                    toast_type=ToastType.UPDATE,
                    duration_ms=4000,
                    icon="▼",  # ▼
                )

            elif event.event_type == EventType.NEWS_BLOCK:
                window = (f"  (±{event.news_window_s:g}s)"
                          if event.news_window_s else "")
                self.show_toast(
                    title=f"NEWS BLOCK  ·  {event.slave_name}",
                    message=f"{event.symbol} not copied\n{event.error}{window}",
                    toast_type=ToastType.ERROR,
                    duration_ms=7000,
                    icon="\U0001F4F0",  # 📰
                )

            elif event.event_type == EventType.ERROR:
                msg = event.error or "Unknown error"
                if event.symbol and event.symbol_mapped and event.symbol != event.symbol_mapped:
                    msg += f"\n{event.symbol} \u2192 {event.symbol_mapped}"
                elif event.symbol_mapped:
                    msg += f"\n{event.symbol_mapped}"

                self.show_toast(
                    title=f"ERROR  \u00b7  {event.slave_name}",
                    message=msg,
                    toast_type=ToastType.ERROR,
                    duration_ms=7000,
                    icon="\u26a0",  # ⚠
                )
        except Exception:
            _logger.error("Toast display error", exc_info=True)
