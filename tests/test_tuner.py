"""The pure core: the callback's book-keeping and its cost."""

from __future__ import annotations

import logging
import time

import pytest

from custom_components.gc_tuning import tuner as tuner_module
from custom_components.gc_tuning.tuner import GcTuner

from .conftest import FakeClock, FakeGc

LOG = logging.getLogger("test_gc_tuner")


def test_bad_thresholds_are_refused() -> None:
    for bad in ((699, 10, 10), (50000, 0, 10), (50000, 10, 0), (200_001 * 10, 10, 10)):
        with pytest.raises(ValueError):
            GcTuner(LOG, thresholds=bad)


def test_callback_records_but_does_not_log_fast_collections(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = GcTuner(LOG)
    tuner.install_callback()
    with caplog.at_level(logging.DEBUG, logger="test_gc_tuner"):
        for _ in range(50):
            fake_gc.run(0, 0.0002)
    assert tuner.count == [50, 0, 0]
    assert caplog.records == []  # no log line, nothing kept in `recent`
    assert len(tuner.recent) == 0


def test_every_full_collection_is_logged_at_info_and_slow_ones_at_warning(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = GcTuner(LOG, log_min_ms=500)
    tuner.install_callback()
    with caplog.at_level(logging.INFO, logger="test_gc_tuner"):
        fake_gc.run(2, 0.1)
        fake_gc.run(2, 2.2)
    levels = [(r.levelno, r.getMessage().split(" took ")[0]) for r in caplog.records]
    assert levels == [
        (logging.INFO, "gc generation 2 collection"),
        (logging.INFO, "gc generation 2 collection"),
        (logging.WARNING, "gc generation 2 collection"),
    ]


def test_log_threshold_changes_live(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = GcTuner(LOG, log_min_ms=500)
    tuner.install_callback()
    tuner.configure(freeze=True, thresholds=(50000, 10, 10), log_min_ms=10)
    with caplog.at_level(logging.WARNING, logger="test_gc_tuner"):
        fake_gc.run(0, 0.05)
    assert len(caplog.records) == 1


def test_callback_is_removed_and_idempotent(fake_gc: FakeGc) -> None:
    tuner = GcTuner(LOG)
    tuner.install_callback()
    tuner.install_callback()
    assert len(fake_gc.callbacks) == 1
    tuner.remove_callback()
    assert fake_gc.callbacks == []
    fake_gc.run(2, 1.0)
    assert tuner.count == [0, 0, 0]


def test_last_hour_window_edges(fake_gc: FakeGc, clock: FakeClock) -> None:
    tuner = GcTuner(LOG)
    tuner.install_callback()
    fake_gc.run(2, 3.0)
    assert tuner.last_hour() == {"full_collections": 1, "longest_pause_seconds": 3.0}
    clock.now += 3590
    assert tuner.last_hour()["full_collections"] == 1
    clock.now += 11  # now 3601 s after the end of that collection
    assert tuner.last_hour()["full_collections"] == 0
    clock.now += 20  # the 10 s bucket it was in is gone too
    assert tuner.last_hour()["longest_pause_seconds"] == 0.0


def test_the_ring_never_grows(fake_gc: FakeGc, clock: FakeClock) -> None:
    tuner = GcTuner(LOG)
    tuner.install_callback()
    for _ in range(20_000):
        clock.now += 7
        fake_gc.run(0, 0.001)
        fake_gc.run(2, 0.001)
    assert len(tuner._buckets) <= tuner._buckets.maxlen  # type: ignore[operator]
    assert len(tuner._fulls) <= 4096


def test_gc_phase_stop_without_start_is_ignored(fake_gc: FakeGc) -> None:
    tuner = GcTuner(LOG)
    tuner.install_callback()
    fake_gc.callbacks[0]("stop", {"generation": 2, "collected": 0})
    assert tuner.count == [0, 0, 0]


def test_callback_cost_is_microseconds(real_gc_guard: None) -> None:
    """The callback pair (start, stop) is arithmetic and a deque append; a generous bound that still proves 'tiny'."""
    tuner = GcTuner(LOG)
    tuner.install_callback()
    cb = tuner._cb
    assert cb is not None
    n = 200_000
    t = time.perf_counter()
    for _ in range(n):
        cb("start", {"generation": 0})
        cb("stop", {"generation": 0, "collected": 0})
    per_pair = (time.perf_counter() - t) / n
    tuner.remove_callback()
    assert per_pair < 30e-6, f"{per_pair * 1e6:.1f} us per start/stop pair"
    assert tuner_module.gc is not None


def test_last_hour_survives_collections_running_in_another_thread(fake_gc: FakeGc, clock: FakeClock) -> None:
    """Python runs gc callbacks in whichever thread triggers the collection, so the sensors read the rings while they change."""
    import threading

    tuner = GcTuner(LOG)
    tuner.install_callback()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(60_000):
                if stop.is_set():
                    return
                clock.now += 11  # every call opens a new 10 s bucket, so the rings keep changing
                fake_gc.run(2 if i % 2 else 0, 0.001)
        except BaseException as err:  # noqa: BLE001
            errors.append(err)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(30_000):
            tuner.last_hour()
    except RuntimeError as err:  # "deque mutated during iteration"
        errors.append(err)
    finally:
        stop.set()
        thread.join()
    assert errors == []
