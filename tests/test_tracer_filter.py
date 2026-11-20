"""Trace subscription: what reaches the file, and what is merely repetition.

WHY THIS EXISTS
    A trace nobody can open is not observability. Measured on one 11-hour run,
    three heartbeat operations were 100% of an 11.8 MB file, and the one record
    that mattered — the price comparison — was 83% duplicates of itself, each
    restating the same minute with a larger age counter.
"""
import json

import pytest

from src.utils.tracer import Tracer


class _Capture:
    """Stands in for the async writer; keeps the records in memory."""

    def __init__(self):
        self.lines: list[str] = []

    def write(self, line: str):
        self.lines.append(line)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    def ops(self) -> list[str]:
        return [r["operation"] for r in self.records()]


@pytest.fixture
def tracer(tmp_path):
    t = Tracer(trace_dir=tmp_path, enabled=True)
    t._writer = _Capture()
    return t


# ==================== the subscription ====================

def test_heartbeats_are_muted_by_default(tracer):
    tracer.event("mt5_get_price", bid=1.0)
    tracer.event("price_calc_cycle", pairs=1)
    tracer.event("price_calc", raw=-64.0)

    assert tracer._writer.ops() == ["price_calc"]


def test_an_allowlist_narrows_to_exactly_one_thing(tracer):
    """What chasing a specific bug wants: a file containing only that."""
    tracer.set_filter(include=["copy_new_position"])

    tracer.event("copy_new_position", ticket=1)
    tracer.event("price_calc", raw=-64.0)
    tracer.event("mt5_get_price", bid=1.0)

    assert tracer._writer.ops() == ["copy_new_position"]


def test_an_allowlist_beats_the_default_mute(tracer):
    """Asking for a heartbeat explicitly must actually produce it."""
    tracer.set_filter(include=["mt5_get_price"])

    tracer.event("mt5_get_price", bid=1.0)

    assert tracer._writer.ops() == ["mt5_get_price"]


def test_an_empty_exclude_records_everything(tracer):
    tracer.set_filter(exclude=[])

    tracer.event("mt5_get_price", bid=1.0)
    tracer.event("price_calc_cycle", pairs=1)

    assert tracer._writer.ops() == ["mt5_get_price", "price_calc_cycle"]


def test_a_disabled_tracer_writes_nothing(tmp_path):
    t = Tracer(trace_dir=tmp_path, enabled=False)
    t._writer = _Capture()
    t.set_filter(exclude=[])

    t.event("price_calc", raw=-64.0)

    assert t._writer.ops() == []


# ==================== spans, and not orphaning them ====================

def test_a_muted_span_does_not_write(tracer):
    with tracer.span("mt5_get_price"):
        pass

    assert tracer._writer.ops() == []


def test_a_muted_parent_still_parents_its_children(tracer):
    """Muting one operation must not detach the spans beneath it.

    The alternative — skipping the span object entirely — reparents its
    children to whatever is above, and a trace whose tree is a lie is worse
    than a noisy one.
    """
    with tracer.span("price_calc_cycle") as parent:      # muted by default
        with tracer.span("copy_new_position") as child:
            child_parent = child.parent_span_id
            parent_id = parent.span_id

    assert tracer._writer.ops() == ["copy_new_position"]
    assert child_parent == parent_id


# ==================== dedupe ====================

def test_an_unchanged_event_is_written_once(tracer):
    for _ in range(12):
        tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-64.0, at=100)

    assert len(tracer._writer.ops()) == 1


def test_a_changed_event_is_written_again(tracer):
    tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-64.0, at=100)
    tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-64.0, at=100)
    tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-61.5, at=160)

    assert [r["tags"]["raw"] for r in tracer._writer.records()] == [-64.0, -61.5]


def test_the_clocks_do_not_count_as_a_change(tracer):
    """Ages move every cycle by construction — they are clocks, not findings.

    Counting them would defeat the dedupe entirely, which is the whole reason
    the file was 83% repetition.
    """
    for age in (600, 605, 610, 615):
        tracer.event(
            "price_calc", dedupe_key="NQ->NAS100",
            raw=-64.0, tv_age_s=age, master_age_s=age, lookback_s=age,
            srv_offset=10800 + age,
        )

    assert len(tracer._writer.ops()) == 1


def test_each_pair_dedupes_independently(tracer):
    tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-64.0)
    tracer.event("price_calc", dedupe_key="GC->XAUUSD", raw=+11.5)
    tracer.event("price_calc", dedupe_key="NQ->NAS100", raw=-64.0)

    assert [r["tags"]["raw"] for r in tracer._writer.records()] == [-64.0, 11.5]


def test_a_state_change_always_gets_through(tracer):
    """Going missing, or coming back, is exactly what must never be swallowed."""
    common = dict(dedupe_key="NQ->NAS100", raw=-64.0)
    tracer.event("price_calc", **common, tv_missing=False, error="")
    tracer.event("price_calc", **common, tv_missing=False, error="")
    tracer.event("price_calc", **common, tv_missing=True, error="feed stopped")
    tracer.event("price_calc", **common, tv_missing=False, error="")

    assert [r["tags"]["tv_missing"] for r in tracer._writer.records()] == [
        False, True, False,
    ]


def test_without_a_key_nothing_is_deduped(tracer):
    for _ in range(3):
        tracer.event("price_calc", raw=-64.0)

    assert len(tracer._writer.ops()) == 3


def test_the_live_quote_is_context_not_news(tracer):
    """bid/ask tick constantly and are explicitly not part of the measurement.

    Treating a change in them as a new finding defeated the dedupe almost
    entirely — 68% of the file saved instead of 89%, measured by replaying a
    real run through it.
    """
    for bid in (29565.15, 29565.20, 29566.01, 29564.88):
        tracer.event(
            "price_calc", dedupe_key="NQ->NAS100",
            raw=-64.0, master_bid=bid, master_ask=bid + 2.1, spread_now=2.1,
        )

    assert len(tracer._writer.ops()) == 1
    # ...and the record still carries the quote it was written with.
    assert tracer._writer.records()[0]["tags"]["master_bid"] == 29565.15
