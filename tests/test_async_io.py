"""Tests for async I/O components (logging, tracing, debug).

Verifies that all I/O operations are non-blocking and never affect the copy path.
"""
import gc
import json
import queue
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAsyncFileWriter:
    """Test AsyncFileWriter non-blocking behavior."""

    def test_write_never_blocks(self):
        """Write should return immediately even under load."""
        from src.utils.async_writer import AsyncFileWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.log"
            writer = AsyncFileWriter(file_path=path)

            # Write many messages
            start = time.perf_counter()
            for i in range(1000):
                writer.write(f"Message {i}")
            elapsed = time.perf_counter() - start

            # All writes should complete in < 100ms (they're just queued)
            assert elapsed < 0.1, f"Writes took {elapsed:.3f}s (should be < 0.1s)"

            writer.close()

            # Verify file has content
            assert path.exists()
            content = path.read_text()
            assert "Message 0" in content

    def test_full_queue_drops_messages(self):
        """Full queue should drop messages, not block."""
        from src.utils.async_writer import AsyncFileWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.log"
            # Small queue to force drops
            writer = AsyncFileWriter(file_path=path, max_queue_size=10, flush_interval=10.0)

            # Flood the queue
            for i in range(100):
                writer.write(f"Message {i}")

            # Should have dropped some
            assert writer.dropped_count > 0

            writer.close()

    def test_dynamic_file_path(self):
        """Dynamic file path should rotate files."""
        from src.utils.async_writer import AsyncFileWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            counter = [0]

            def get_path() -> Path:
                return Path(tmpdir) / f"log_{counter[0]}.txt"

            writer = AsyncFileWriter(get_file_path=get_path)

            writer.write("First file")
            time.sleep(0.3)  # Allow flush

            counter[0] = 1
            writer.write("Second file")
            time.sleep(0.3)

            writer.close()

            assert (Path(tmpdir) / "log_0.txt").exists()
            assert (Path(tmpdir) / "log_1.txt").exists()


class TestAsyncLogging:
    """Test async logging handler."""

    def test_logging_never_blocks(self):
        """Log calls should never block."""
        from src.utils.logging import setup_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            logger = setup_logging("DEBUG", Path(tmpdir))

            start = time.perf_counter()
            for i in range(1000):
                logger.info(f"Test message {i}")
            elapsed = time.perf_counter() - start

            # All logs should complete in < 100ms
            assert elapsed < 0.1, f"Logging took {elapsed:.3f}s (should be < 0.1s)"

            # Close handlers
            for handler in logger.handlers[:]:
                handler.close()


class TestAsyncTracer:
    """Test async tracer."""

    def test_trace_never_blocks(self):
        """Trace operations should never block."""
        from src.utils.tracer import Tracer

        with tempfile.TemporaryDirectory() as tmpdir:
            tracer = Tracer(trace_dir=Path(tmpdir))

            start = time.perf_counter()
            for i in range(100):
                with tracer.trace("test_op", trade_id=f"TRADE_{i}") as span:
                    span.set_tag("iteration", i)
            elapsed = time.perf_counter() - start

            # 100 traces should complete in < 100ms (excluding actual work)
            assert elapsed < 0.1, f"Tracing took {elapsed:.3f}s (should be < 0.1s)"

            tracer.close()

    def test_nested_spans(self):
        """Nested spans should work correctly."""
        from src.utils.tracer import Tracer

        with tempfile.TemporaryDirectory() as tmpdir:
            tracer = Tracer(trace_dir=Path(tmpdir))

            with tracer.trace("outer") as outer:
                outer.set_tag("level", "outer")
                with tracer.span("inner") as inner:
                    inner.set_tag("level", "inner")

            tracer.close()

            # Check trace file
            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) == 1

            lines = files[0].read_text().strip().split("\n")
            records = [json.loads(line) for line in lines]

            # Should have 2 spans
            assert len(records) == 2

            # Inner span should have parent
            inner_record = next(r for r in records if r["operation"] == "inner")
            outer_record = next(r for r in records if r["operation"] == "outer")
            assert inner_record["parent_span_id"] == outer_record["span_id"]


class TestDebugDumper:
    """Test debug dumper."""

    def test_dump_system_info(self):
        """System info dump should work."""
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper = DebugDumper(dump_dir=Path(tmpdir))

            info = dumper.dump_system_info()

            assert "python" in info
            assert "os" in info
            assert "process" in info
            assert info["python"]["version"]

            dumper.close()

    def test_dump_thread_info(self):
        """Thread info dump should capture all threads."""
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper = DebugDumper(dump_dir=Path(tmpdir))

            # Start a named thread
            stop_event = threading.Event()

            def worker():
                while not stop_event.is_set():
                    time.sleep(0.01)

            thread = threading.Thread(target=worker, name="TestWorker")
            thread.start()

            try:
                info = dumper.dump_thread_info()

                assert info["active_count"] >= 2
                thread_names = [t["name"] for t in info["threads"]]
                assert "TestWorker" in thread_names
            finally:
                stop_event.set()
                thread.join()
                dumper.close()

    def test_dump_memory_stats(self):
        """Memory stats dump should work."""
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper = DebugDumper(dump_dir=Path(tmpdir))

            info = dumper.dump_memory_stats()

            assert "gc" in info
            assert "gc_enabled" in str(info) or "enabled" in info["gc"]

            dumper.close()

    def test_dump_exception(self):
        """Exception dump should capture full context."""
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper = DebugDumper(dump_dir=Path(tmpdir))

            try:
                raise ValueError("Test error")
            except ValueError:
                import sys
                exc_info = sys.exc_info()
                info = dumper.dump_exception(*exc_info, context="test")

            assert info["exception"]["type"] == "ValueError"
            assert "Test error" in info["exception"]["message"]
            assert info["context"] == "test"

            dumper.close()

    def test_dump_never_blocks(self):
        """Dump operations should never block."""
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper = DebugDumper(dump_dir=Path(tmpdir))

            start = time.perf_counter()
            for i in range(100):
                dumper.dump_state({"iteration": i}, label=f"test_{i}")
            elapsed = time.perf_counter() - start

            # All dumps should complete in < 50ms
            assert elapsed < 0.05, f"Dumps took {elapsed:.3f}s (should be < 0.05s)"

            dumper.close()


class TestIntegration:
    """Integration tests for all async components together."""

    def test_all_components_non_blocking(self):
        """All I/O components running together should not block."""
        from src.utils.logging import setup_logging
        from src.utils.tracer import Tracer
        from src.utils.debug import DebugDumper

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            # Setup all components
            logger = setup_logging("DEBUG", base / "logs")
            tracer = Tracer(trace_dir=base / "traces")
            dumper = DebugDumper(dump_dir=base / "dumps")

            # Simulate a busy copy loop
            start = time.perf_counter()

            for i in range(50):
                # Log
                logger.info(f"Processing trade {i}")

                # Trace
                with tracer.trace("copy_trade", trade_id=f"T{i}") as span:
                    span.set_tag("symbol", "EURUSD")

                    # Simulate work (should be the only thing taking time)
                    time.sleep(0.001)  # 1ms

                # Periodic debug dump
                if i % 10 == 0:
                    dumper.dump_state({"iteration": i}, "progress")

            elapsed = time.perf_counter() - start

            # 50 iterations with 1ms work each = 50ms minimum
            # All I/O should add < 50ms overhead
            assert elapsed < 0.15, f"Total took {elapsed:.3f}s (expected < 0.15s)"

            # Cleanup
            for handler in logger.handlers[:]:
                handler.close()
            tracer.close()
            dumper.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
