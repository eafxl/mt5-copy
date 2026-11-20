"""Tests for SlaveStateMachine — pure logic, no GUI."""
import threading

import pytest

from src.slave_state import SlaveStateMachine, SlaveStatus, get_slave_state


@pytest.fixture(autouse=True)
def reset_state():
    """Reset singleton before and after every test."""
    SlaveStateMachine.reset()
    yield
    SlaveStateMachine.reset()


@pytest.fixture
def state():
    return get_slave_state()


# ---- Basic operations ----

class TestAddRemoveGet:
    def test_add_slave_default_on(self, state):
        state.add_slave("S1")
        assert state.get_status("S1") == SlaveStatus.ON

    def test_add_slave_custom_status(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.get_status("S1") == SlaveStatus.OFF

    def test_get_status_unknown(self, state):
        assert state.get_status("nonexistent") is None

    def test_remove_slave(self, state):
        state.add_slave("S1")
        state.remove_slave("S1")
        assert state.get_status("S1") is None

    def test_remove_unknown_noop(self, state):
        state.remove_slave("nonexistent")  # should not raise

    def test_get_all(self, state):
        state.add_slave("A", SlaveStatus.ON)
        state.add_slave("B", SlaveStatus.OFF)
        all_slaves = state.get_all()
        assert all_slaves == {"A": SlaveStatus.ON, "B": SlaveStatus.OFF}

    def test_get_all_is_snapshot(self, state):
        state.add_slave("S1")
        snap = state.get_all()
        state.remove_slave("S1")
        assert "S1" in snap  # snapshot not affected


# ---- Valid transitions ----

class TestValidTransitions:
    def test_pause_on_to_paused(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        assert state.pause("S1") is True
        assert state.get_status("S1") == SlaveStatus.PAUSED

    def test_resume_paused_to_on(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        assert state.resume("S1") is True
        assert state.get_status("S1") == SlaveStatus.ON

    def test_resume_off_to_on(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.resume("S1") is True
        assert state.get_status("S1") == SlaveStatus.ON

    def test_stop_on_to_off(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        assert state.stop("S1") is True
        assert state.get_status("S1") == SlaveStatus.OFF

    def test_stop_paused_to_off(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        assert state.stop("S1") is True
        assert state.get_status("S1") == SlaveStatus.OFF

    def test_disconnect_on_to_off(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        state.disconnect("S1")
        assert state.get_status("S1") == SlaveStatus.OFF

    def test_disconnect_not_on_noop(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        state.disconnect("S1")
        assert state.get_status("S1") == SlaveStatus.PAUSED


# ---- Invalid transitions ----

class TestInvalidTransitions:
    def test_pause_off_fails(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.pause("S1") is False
        assert state.get_status("S1") == SlaveStatus.OFF

    def test_pause_paused_fails(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        assert state.pause("S1") is False
        assert state.get_status("S1") == SlaveStatus.PAUSED

    def test_resume_on_fails(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        assert state.resume("S1") is False

    def test_stop_off_fails(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.stop("S1") is False

    def test_pause_unknown_fails(self, state):
        assert state.pause("ghost") is False

    def test_resume_unknown_fails(self, state):
        assert state.resume("ghost") is False

    def test_stop_unknown_fails(self, state):
        assert state.stop("ghost") is False


# ---- stop_all ----

class TestStopAll:
    def test_stop_all(self, state):
        state.add_slave("A", SlaveStatus.ON)
        state.add_slave("B", SlaveStatus.PAUSED)
        state.add_slave("C", SlaveStatus.OFF)
        state.stop_all()
        assert state.get_status("A") == SlaveStatus.OFF
        assert state.get_status("B") == SlaveStatus.OFF
        assert state.get_status("C") == SlaveStatus.OFF

    def test_stop_all_empty(self, state):
        state.stop_all()  # should not raise


# ---- can_delete ----

class TestCanDelete:
    def test_can_delete_off(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.can_delete("S1") is True

    def test_cannot_delete_on(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        assert state.can_delete("S1") is False

    def test_cannot_delete_paused(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        assert state.can_delete("S1") is False

    def test_can_delete_untracked(self, state):
        assert state.can_delete("nonexistent") is True


# ---- is_copy_active ----

class TestIsCopyActive:
    def test_on_is_active(self, state):
        state.add_slave("S1", SlaveStatus.ON)
        assert state.is_copy_active("S1") is True

    def test_off_not_active(self, state):
        state.add_slave("S1", SlaveStatus.OFF)
        assert state.is_copy_active("S1") is False

    def test_paused_not_active(self, state):
        state.add_slave("S1", SlaveStatus.PAUSED)
        assert state.is_copy_active("S1") is False

    def test_untracked_is_active(self, state):
        """Untracked slaves are active (CLI mode compatibility)."""
        assert state.is_copy_active("nonexistent") is True


# ---- Callbacks ----

class TestCallbacks:
    def test_callback_on_pause(self, state):
        calls = []
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.add_slave("S1", SlaveStatus.ON)
        state.pause("S1")
        assert len(calls) == 1
        assert calls[0] == ("S1", SlaveStatus.ON, SlaveStatus.PAUSED)

    def test_callback_on_resume(self, state):
        calls = []
        state.add_slave("S1", SlaveStatus.OFF)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.resume("S1")
        assert calls == [("S1", SlaveStatus.OFF, SlaveStatus.ON)]

    def test_callback_on_stop(self, state):
        calls = []
        state.add_slave("S1", SlaveStatus.ON)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.stop("S1")
        assert calls == [("S1", SlaveStatus.ON, SlaveStatus.OFF)]

    def test_callback_on_disconnect(self, state):
        calls = []
        state.add_slave("S1", SlaveStatus.ON)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.disconnect("S1")
        assert calls == [("S1", SlaveStatus.ON, SlaveStatus.OFF)]

    def test_callback_on_stop_all(self, state):
        calls = []
        state.add_slave("A", SlaveStatus.ON)
        state.add_slave("B", SlaveStatus.PAUSED)
        state.add_slave("C", SlaveStatus.OFF)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.stop_all()
        # C was already OFF, no callback for it
        names = {c[0] for c in calls}
        assert names == {"A", "B"}

    def test_no_callback_on_invalid_transition(self, state):
        calls = []
        state.add_slave("S1", SlaveStatus.OFF)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.pause("S1")  # invalid
        assert calls == []

    def test_unregister_callback(self, state):
        calls = []
        cb = lambda n, o, new: calls.append((n, o, new))
        state.register_callback(cb)
        state.add_slave("S1", SlaveStatus.ON)
        state.pause("S1")
        assert len(calls) == 1

        state.unregister_callback(cb)
        state.resume("S1")
        assert len(calls) == 1  # no new callback

    def test_callback_exception_swallowed(self, state):
        """Callback errors don't break state transitions."""
        def bad_cb(n, o, new):
            raise RuntimeError("boom")

        state.register_callback(bad_cb)
        state.add_slave("S1", SlaveStatus.ON)
        state.pause("S1")  # should not raise
        assert state.get_status("S1") == SlaveStatus.PAUSED

    def test_callback_on_add_slave_with_different_status(self, state):
        """add_slave fires callback if slave exists with different status."""
        calls = []
        state.add_slave("S1", SlaveStatus.ON)
        state.register_callback(lambda n, o, new: calls.append((n, o, new)))
        state.add_slave("S1", SlaveStatus.OFF)
        assert calls == [("S1", SlaveStatus.ON, SlaveStatus.OFF)]


# ---- Singleton ----

class TestSingleton:
    def test_singleton(self):
        a = get_slave_state()
        b = get_slave_state()
        assert a is b

    def test_reset(self):
        a = get_slave_state()
        a.add_slave("S1")
        SlaveStateMachine.reset()
        b = get_slave_state()
        assert a is not b
        assert b.get_status("S1") is None


# ---- Thread safety ----

class TestThreadSafety:
    def test_concurrent_modifications(self, state):
        """Multiple threads adding/modifying slaves concurrently."""
        errors = []

        def worker(idx):
            try:
                name = f"S{idx}"
                state.add_slave(name, SlaveStatus.ON)
                state.pause(name)
                state.resume(name)
                state.stop(name)
                state.resume(name)
                final = state.get_status(name)
                if final != SlaveStatus.ON:
                    errors.append(f"{name} ended in {final}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"
