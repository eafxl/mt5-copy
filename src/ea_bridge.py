"""EA push bridge — localhost TCP server that receives master trade events
from the CopierBridge.mq5 Expert Advisor and turns them into PositionChange
objects for the copier.

Push-based, event-driven: the EA fires on OnTradeTransaction (the instant a
position opens/modifies/closes) and sends one newline-terminated JSON object
per event. This replaces the poll loop so copies happen in ~ms.

MQL5 sockets are client-only, so THIS is the server: it listens on
127.0.0.1:PORT and the EA connects out. Events are decoded on a background
thread and handed to the asyncio loop via a thread-safe queue.

DIAGNOSING THIS LINK
    Everything the bridge does is logged under its own logger, ``metact.tcp``,
    and tagged with a connection number and a per-connection sequence:

        [TCP#2] link up from 127.0.0.1:51844 (listener up 00:04:11)
        [TCP#2/17] open ticket=51203 EURUSD 0.10 -> queued
        [TCP#2] link down after 00:12:03 — 41 event(s), 812 heartbeat(s)

    ``#2`` is the second EA connection since the listener started; ``/17`` is
    the seventeenth line that connection sent. Those two numbers are enough to
    tell "the EA reconnected and replayed" from "we lost a message" — the two
    failures that look identical in a log that only says "event received".

    The same identity goes onto the PositionChange as ``signal_id``, so a trade
    in the copier's log can be traced back to the exact wire line that caused
    it, and into the trace file where ``ea_signal`` records it.

    Heartbeats are counted but never logged individually: the EA sends one
    every second, and a log that is 99% heartbeat is a log nobody reads. Their
    absence is what matters, and ``stats()`` exposes it as ``silent_for_s``.
"""
import asyncio
import json
import logging
import socket
import threading
import time

from .events import emit_ea_status
from .master import PositionChange
from .mt5_client import Position
from .utils.logging import LOGGER_NAME
from .utils.tracer import get_tracer


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47800

#: Own logger so the whole transport can be turned up to DEBUG, or grepped out,
#: without touching the copy path's logging.
tcp_logger = logging.getLogger(f"{LOGGER_NAME}.tcp")

#: A line longer than this is not something our EA sent. Without a cap, a
#: process that connects and streams without ever sending a newline grows the
#: receive buffer until the app dies — from a socket anything on the machine
#: can connect to.
MAX_LINE_BYTES = 64 * 1024


def _hms(seconds: float) -> str:
    """``00:12:03`` — durations in a log are read as durations, not as floats."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def position_from_event(ev: dict) -> Position:
    """Build a Position from an EA event payload (open/modify)."""
    tmsc = int(ev.get("time_msc", 0) or 0)
    price = float(ev.get("price", 0) or 0)
    return Position(
        ticket=int(ev["ticket"]),
        symbol=str(ev["symbol"]),
        type=int(ev["type"]),
        volume=float(ev.get("volume", 0) or 0),
        price_open=price,
        price_current=price,          # EA sends open price; current ≈ open at push time
        sl=float(ev.get("sl", 0) or 0),
        tp=float(ev.get("tp", 0) or 0),
        profit=0.0,
        magic=int(ev.get("magic", 0) or 0),
        comment=str(ev.get("comment", "")),
        time=tmsc // 1000 if tmsc else 0,
        time_msc=tmsc,
    )


def change_from_event(ev: dict) -> PositionChange | None:
    """Translate a decoded EA event into a PositionChange.

    Returns None for events that carry no position change (heartbeat, or
    malformed payloads).
    """
    kind = ev.get("event")
    if kind == "open":
        return PositionChange(new_positions=[position_from_event(ev)],
                              closed_tickets=[], modified_positions=[],
                              source="TCP")
    if kind == "modify":
        return PositionChange(new_positions=[], closed_tickets=[],
                              modified_positions=[position_from_event(ev)],
                              source="TCP")
    if kind == "close":
        return PositionChange(new_positions=[],
                              closed_tickets=[int(ev["ticket"])],
                              modified_positions=[], source="TCP")
    if kind == "reduce":
        return PositionChange(new_positions=[], closed_tickets=[],
                              modified_positions=[], source="TCP",
                              reduced_positions=[position_from_event(ev)])
    # heartbeat / unknown -> no change
    return None


def parse_line(line: str) -> PositionChange | None:
    """Parse one JSON line from the EA into a PositionChange (or None)."""
    line = line.strip()
    if not line:
        return None
    ev = json.loads(line)
    return change_from_event(ev)


def describe_event(ev: dict) -> str:
    """One-line summary of an EA payload, for the log."""
    kind = ev.get("event", "?")
    ticket = ev.get("ticket", "?")
    parts = [f"{kind} ticket={ticket}"]
    if ev.get("symbol"):
        parts.append(str(ev["symbol"]))
    if ev.get("volume"):
        parts.append(f"{float(ev['volume']):g} lots")
    for key in ("sl", "tp"):
        if ev.get(key):
            parts.append(f"{key}={ev[key]}")
    return " ".join(parts)


class _Connection:
    """Bookkeeping for one EA link. Exists to be reported, not to do work."""

    def __init__(self, number: int, peer):
        self.number = number
        self.peer = peer
        self.opened_at = time.time()
        self.lines = 0
        self.events = 0
        self.heartbeats = 0
        self.parse_errors = 0
        self.bytes_in = 0
        self.last_line_at = 0.0

    @property
    def uptime_s(self) -> float:
        return time.time() - self.opened_at

    def tag(self, seq: int | None = None) -> str:
        return f"[TCP#{self.number}]" if seq is None else f"[TCP#{self.number}/{seq}]"


class EABridge:
    """Localhost TCP listener for master EA push events.

    Usage (async):
        bridge = EABridge(loop)
        bridge.start()
        change = await bridge.next_change()   # blocks until an event arrives
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.loop = loop
        self.host = host
        self.port = port
        self.logger = tcp_logger
        self.queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self.connected = False
        self.events_received = 0
        self.parse_errors = 0

        # Lifetime counters, kept across reconnects — a link that flaps is the
        # thing worth seeing, and per-connection numbers hide it by resetting.
        self.started_at = 0.0
        self.connections = 0
        self.heartbeats = 0
        self.bytes_in = 0
        self.last_event_at = 0.0
        self.last_line_at = 0.0
        self._conn: _Connection | None = None

    # ---- lifecycle ----
    def start(self):
        """Bind the listener and start accepting. Raises OSError if the port
        is already in use (caller decides continue-without-EA vs abort)."""
        if self._running:
            return
        # Bind SYNCHRONOUSLY so a port clash surfaces to the caller immediately
        # (not swallowed on a background thread).
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
        except OSError as e:
            srv.close()
            self.logger.error(
                "[TCP] cannot listen on %s:%d — %s. The EA has nowhere to "
                "connect; copying falls back to polling.",
                self.host, self.port, e,
            )
            raise  # port taken → caller handles (dialog / fallback)
        srv.listen(1)
        srv.settimeout(1.0)
        self._server = srv
        self._running = True
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._serve, name="EABridge", daemon=True)
        self._thread.start()
        self.logger.info(
            "[TCP] listening on %s:%d — waiting for the master EA to connect",
            self.host, self.port,
        )
        get_tracer().event(
            "ea_bridge_start", host=self.host, port=self.port,
        )
        emit_ea_status(False)  # starts DOWN until the EA connects

    def stop(self):
        self._running = False
        try:
            if self._server:
                self._server.close()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.logger.info(
            "[TCP] stopped after %s — %d connection(s), %d event(s), %d parse error(s)",
            _hms(time.time() - self.started_at) if self.started_at else "00:00:00",
            self.connections, self.events_received, self.parse_errors,
        )

    async def next_change(self) -> PositionChange:
        """Await the next position change pushed by the EA."""
        return await self.queue.get()

    # ---- introspection ----
    def stats(self) -> dict:
        """Everything known about the link, for Diagnostics and the status bar.

        ``silent_for_s`` is the one to watch: the EA heartbeats every second,
        so a connected link that has said nothing for many seconds is a link
        that is up at the TCP level and dead at the application level — which
        a connection flag alone cannot tell you.
        """
        now = time.time()
        conn = self._conn
        return {
            "listening": bool(self._running),
            "endpoint": f"{self.host}:{self.port}",
            "uptime_s": round(now - self.started_at, 1) if self.started_at else 0.0,
            "connected": self.connected,
            "connections": self.connections,
            "events": self.events_received,
            "heartbeats": self.heartbeats,
            "parse_errors": self.parse_errors,
            "bytes_in": self.bytes_in,
            "silent_for_s": (
                round(now - self.last_line_at, 1) if self.last_line_at else None
            ),
            "since_last_event_s": (
                round(now - self.last_event_at, 1) if self.last_event_at else None
            ),
            "conn_number": conn.number if conn else 0,
            "conn_uptime_s": round(conn.uptime_s, 1) if conn else 0.0,
            "conn_peer": f"{conn.peer[0]}:{conn.peer[1]}" if conn else "",
            "conn_lines": conn.lines if conn else 0,
        }

    # ---- server thread ----
    def _serve(self):
        # Socket already bound + listening in start().
        while self._running:
            # Accept (re-accept on EA drop). settimeout keeps the loop
            # responsive to stop().
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            self.connections += 1
            link = _Connection(self.connections, addr)
            self._conn = link
            self.connected = True
            self.logger.info(
                "%s link up from %s:%s (listener up %s)",
                link.tag(), addr[0], addr[1], _hms(time.time() - self.started_at),
            )
            get_tracer().event(
                "ea_connect", conn=link.number, peer=f"{addr[0]}:{addr[1]}",
            )
            self._emit_status(True)

            error = None
            try:
                self._handle_client(conn, link)
            except Exception as e:
                error = str(e)
                self.logger.warning("%s client error: %s", link.tag(), e)
            finally:
                self.connected = False
                self._emit_status(False)
                try:
                    conn.close()
                except Exception:
                    pass
                self.logger.log(
                    logging.WARNING if self._running else logging.INFO,
                    "%s link down after %s — %d event(s), %d heartbeat(s), "
                    "%d parse error(s), %d byte(s)%s",
                    link.tag(), _hms(link.uptime_s), link.events, link.heartbeats,
                    link.parse_errors, link.bytes_in,
                    f" [{error}]" if error else "",
                )
                get_tracer().event(
                    "ea_disconnect", conn=link.number,
                    uptime_s=round(link.uptime_s, 1), events=link.events,
                    heartbeats=link.heartbeats, parse_errors=link.parse_errors,
                    error=error,
                )
                if self._running:
                    self.logger.warning(
                        "%s awaiting reconnect — trades are copied by polling "
                        "until the EA is back", link.tag(),
                    )

    def _emit_status(self, connected: bool):
        """Push an EA_STATUS event on the asyncio loop thread (edge-triggered)."""
        try:
            self.loop.call_soon_threadsafe(emit_ea_status, connected)
        except Exception:
            pass

    def _handle_client(self, conn: socket.socket, link: _Connection):
        conn.settimeout(1.0)
        buf = b""
        while self._running:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                return  # EA closed the link
            link.bytes_in += len(data)
            self.bytes_in += len(data)
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                link.lines += 1
                link.last_line_at = self.last_line_at = time.time()
                self._dispatch(raw.decode("utf-8", errors="replace"), link)
            if len(buf) > MAX_LINE_BYTES:
                self.logger.error(
                    "%s %d bytes with no newline — dropping the buffer. This is "
                    "not the EA's framing; check what is connected to %s:%d.",
                    link.tag(), len(buf), self.host, self.port,
                )
                buf = b""

    def _dispatch(self, line: str, link: _Connection):
        tracer = get_tracer()
        seq = link.lines
        try:
            payload = json.loads(line.strip()) if line.strip() else None
        except Exception as e:
            link.parse_errors += 1
            self.parse_errors += 1
            self.logger.warning("%s unreadable line: %s | %r", link.tag(seq), e, line)
            tracer.event(
                "ea_parse_error", conn=link.number, seq=seq,
                error=str(e), line=line[:200],
            )
            return

        if payload is None:
            return

        try:
            change = change_from_event(payload)
        except Exception as e:
            # Readable JSON, unusable content — a missing ticket, a symbol that
            # is not a string. Distinct from a parse error and worth saying so:
            # one means the framing broke, the other means the EA and this
            # bridge disagree about the payload.
            link.parse_errors += 1
            self.parse_errors += 1
            self.logger.warning(
                "%s rejected payload: %s | %r", link.tag(seq), e, payload
            )
            tracer.event(
                "ea_bad_payload", conn=link.number, seq=seq,
                error=str(e), event=str(payload.get("event")),
            )
            return

        if change is None:
            # Heartbeat, or an event kind this build does not act on. Counted
            # so silence is measurable; never logged line by line.
            link.heartbeats += 1
            self.heartbeats += 1
            if payload.get("event") not in (None, "heartbeat", "hb", "ping"):
                self.logger.debug(
                    "%s ignoring unknown event %r", link.tag(seq), payload.get("event")
                )
            return

        change.recv_ms = time.time() * 1000.0  # ingest stamp (host clock)
        change.signal_id = f"TCP#{link.number}/{seq}"
        link.events += 1
        self.events_received += 1
        self.last_event_at = time.time()

        self.logger.info("%s %s -> queued", link.tag(seq), describe_event(payload))
        tracer.event(
            "ea_signal",
            signal_id=change.signal_id,
            conn=link.number,
            seq=seq,
            event=str(payload.get("event")),
            ticket=payload.get("ticket"),
            symbol=payload.get("symbol"),
            volume=payload.get("volume"),
            recv_ms=round(change.recv_ms, 1),
        )
        # Hand off to the asyncio loop thread-safely.
        self.loop.call_soon_threadsafe(self.queue.put_nowait, change)
