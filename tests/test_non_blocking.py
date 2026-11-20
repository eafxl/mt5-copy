"""Tests to verify the copy path is NEVER blocked by UI.

CRITICAL: These tests ensure the event system maintains separation between
the fast copy path and the slow UI path.
"""
import time
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.events import (
    UIEventQueue,
    TradeEvent,
    EventType,
    emit_trade_opened,
    emit_trade_closed,
    get_ui_queue,
)


class TestUIEventQueue:
    """Test the UI event queue behavior."""

    def setup_method(self):
        """Reset singleton before each test."""
        UIEventQueue.reset()

    def test_emit_never_blocks(self):
        """Verify that emit() never blocks, even with full queue."""
        q = UIEventQueue(maxsize=10)

        # Fill the queue
        for i in range(10):
            q.emit(TradeEvent(event_type=EventType.TRADE_OPENED, trade_id=f"t{i}"))

        # Queue is now full - emit should NOT block
        start = time.perf_counter()
        for i in range(100):
            q.emit(TradeEvent(event_type=EventType.TRADE_OPENED, trade_id=f"extra{i}"))
        elapsed = time.perf_counter() - start

        # Should complete almost instantly (< 10ms for 100 emits)
        assert elapsed < 0.01, f"emit() took {elapsed*1000:.2f}ms - might be blocking!"

        # Events should have been dropped
        assert q.dropped_count == 100, f"Expected 100 drops, got {q.dropped_count}"

    def test_emit_is_non_blocking_under_load(self):
        """Simulate high-frequency trading while UI is slow."""
        q = UIEventQueue(maxsize=100)

        results = {"emit_times": [], "poll_count": 0}

        def fast_emitter():
            """Simulates the copy path - must never be slowed down."""
            for i in range(1000):
                start = time.perf_counter()
                q.emit(TradeEvent(event_type=EventType.TRADE_OPENED, trade_id=f"t{i}"))
                elapsed = time.perf_counter() - start
                results["emit_times"].append(elapsed)
                time.sleep(0.0001)  # 0.1ms between emits

        def slow_poller():
            """Simulates slow UI - polls slowly, should never affect emitter."""
            while results["poll_count"] < 50:
                event = q.poll(timeout=0.1)
                if event:
                    time.sleep(0.05)  # UI takes 50ms to process each event
                    results["poll_count"] += 1

        # Run emitter and poller concurrently
        with ThreadPoolExecutor(max_workers=2) as executor:
            emit_future = executor.submit(fast_emitter)
            poll_future = executor.submit(slow_poller)

            emit_future.result()
            poll_future.result()

        # Check emit times - none should be > 1ms (non-blocking)
        max_emit_time = max(results["emit_times"])
        avg_emit_time = sum(results["emit_times"]) / len(results["emit_times"])

        assert max_emit_time < 0.001, f"Max emit time {max_emit_time*1000:.2f}ms - blocking detected!"
        assert avg_emit_time < 0.0001, f"Avg emit time {avg_emit_time*1000:.3f}ms - too slow!"

        print(f"\nEmit stats: max={max_emit_time*1000:.3f}ms, avg={avg_emit_time*1000:.4f}ms")
        print(f"Events dropped: {q.dropped_count}")
        print(f"Events polled: {results['poll_count']}")

    def test_callback_exception_doesnt_affect_emit(self):
        """Verify that callback exceptions don't block or crash the emitter."""
        q = UIEventQueue(maxsize=100)

        callback_count = 0

        def bad_callback(event):
            nonlocal callback_count
            callback_count += 1
            raise Exception("Callback crashed!")

        q.register_callback(bad_callback)

        # Emit should still work despite callback exceptions
        for i in range(10):
            q.emit(TradeEvent(event_type=EventType.TRADE_OPENED, trade_id=f"t{i}"))

        assert callback_count == 10, "Callback should have been called 10 times"
        assert q.pending_count == 10, "All events should be in queue"

    def test_concurrent_emit_and_poll(self):
        """Test concurrent access from multiple threads."""
        q = UIEventQueue(maxsize=1000)

        emit_count = 0
        poll_count = 0
        lock = threading.Lock()

        def emitter(n):
            nonlocal emit_count
            for i in range(n):
                q.emit(TradeEvent(event_type=EventType.TRADE_OPENED, trade_id=f"t{threading.current_thread().name}_{i}"))
                with lock:
                    emit_count += 1

        def poller(duration):
            nonlocal poll_count
            end_time = time.time() + duration
            while time.time() < end_time:
                event = q.poll(timeout=0.01)
                if event:
                    with lock:
                        poll_count += 1

        # Start multiple emitters and pollers
        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=emitter, args=(100,)))
        for i in range(2):
            threads.append(threading.Thread(target=poller, args=(0.5,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print(f"\nConcurrent test: emitted={emit_count}, polled={poll_count}, dropped={q.dropped_count}")

        # All emits should have completed
        assert emit_count == 500, f"Expected 500 emits, got {emit_count}"


class TestGlobalFunctions:
    """Test the global emit functions."""

    def setup_method(self):
        """Reset singleton before each test."""
        UIEventQueue.reset()

    def test_emit_trade_opened(self):
        """Test emit_trade_opened convenience function."""
        emit_trade_opened(
            trade_id="ABC123",
            master_ticket=12345,
            slave_name="Slave1",
            slave_ticket=67890,
            symbol="EURUSD",
            order_type=0,
            volume=1.0,
            open_price=1.1000,
            open_time=time.time(),
        )

        event = get_ui_queue().poll(timeout=0.1)
        assert event is not None
        assert event.event_type == EventType.TRADE_OPENED
        assert event.trade_id == "ABC123"
        assert event.symbol == "EURUSD"

    def test_emit_trade_closed(self):
        """Test emit_trade_closed convenience function."""
        emit_trade_closed(
            trade_id="ABC123",
            master_ticket=12345,
            slave_name="Slave1",
            slave_ticket=67890,
            profit=100.50,
        )

        event = get_ui_queue().poll(timeout=0.1)
        assert event is not None
        assert event.event_type == EventType.TRADE_CLOSED
        assert event.profit == 100.50


class TestCopyPathIndependence:
    """Test that the copy path remains independent of UI."""

    def setup_method(self):
        """Reset singleton before each test."""
        UIEventQueue.reset()

    def test_copy_simulation_not_blocked_by_ui_freeze(self):
        """
        Simulate a scenario where UI freezes (stops polling) but copy
        operations continue unaffected.
        """
        q = UIEventQueue(maxsize=50)

        copy_times = []
        copy_complete = threading.Event()

        def simulate_copy_operations():
            """Simulate 100 copy operations with event emission."""
            for i in range(100):
                # Simulate copy operation
                start = time.perf_counter()

                # The actual copy work (simulated)
                time.sleep(0.001)  # 1ms per copy

                # Emit event (fire-and-forget)
                q.emit(TradeEvent(
                    event_type=EventType.TRADE_OPENED,
                    trade_id=f"copy{i}",
                ))

                elapsed = time.perf_counter() - start
                copy_times.append(elapsed)

            copy_complete.set()

        def simulate_frozen_ui():
            """UI that freezes for 500ms, then resumes."""
            time.sleep(0.2)  # Let some events accumulate
            time.sleep(0.5)  # UI freeze!
            # Now resume polling
            while not copy_complete.is_set():
                q.poll(timeout=0.01)
            # Drain remaining
            while q.poll(timeout=0.01):
                pass

        # Run both
        with ThreadPoolExecutor(max_workers=2) as executor:
            copy_future = executor.submit(simulate_copy_operations)
            ui_future = executor.submit(simulate_frozen_ui)

            copy_future.result()
            ui_future.result()

        # Verify copy operations were not slowed down
        max_copy_time = max(copy_times)
        avg_copy_time = sum(copy_times) / len(copy_times)

        # Each copy should be ~1ms (the simulated work time)
        # If emit was blocking, times would be much higher
        assert max_copy_time < 0.005, f"Max copy time {max_copy_time*1000:.2f}ms - emit might be blocking!"

        print(f"\nCopy times: max={max_copy_time*1000:.2f}ms, avg={avg_copy_time*1000:.2f}ms")
        print(f"Events dropped: {q.dropped_count}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
