"""1.1.0 operations of the pure core: maintain, re-freeze, the collection cap, the event record, the frozen-count cost."""

from __future__ import annotations

import logging

import pytest

from custom_components.gc_tuning import tuner as tuner_module
from custom_components.gc_tuning.tuner import GcTuner, Operation

from .conftest import FakeClock, FakeGc

LOG = logging.getLogger("test_gc_upkeep")


def _tuner(fake_gc: FakeGc, **kwargs: object) -> GcTuner:
    tuner = GcTuner(LOG, **kwargs)  # type: ignore[arg-type]
    tuner.install_callback()
    return tuner


def test_maintain_is_unfreeze_then_collect_then_freeze(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    fake_gc.calls.clear()

    result = tuner.maintain()

    assert fake_gc.calls == ["unfreeze", "collect", "freeze"]
    assert result["collected"] == 15_104
    assert result["frozen_before"] == 2_700_000
    assert result["frozen"] == 2_700_000
    assert tuner.frozen_by_us is True


def test_maintain_freezes_again_even_when_the_collection_fails(fake_gc: FakeGc, monkeypatch: pytest.MonkeyPatch) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()

    def boom(generation: int = 2) -> int:
        raise RuntimeError("collect failed")

    monkeypatch.setattr(fake_gc, "collect", boom)
    with pytest.raises(RuntimeError):
        tuner.maintain()
    assert fake_gc.get_freeze_count() == 2_700_000  # the heap is frozen again
    assert tuner.frozen_by_us is True
    assert tuner._forcing_tid is None


def test_every_operation_is_recorded_and_announced(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    seen: list[Operation] = []
    tuner.on_operation = seen.append

    tuner.apply("startup_apply")
    tuner.freeze()
    tuner.collect()
    tuner.maintain()
    tuner.unfreeze()
    tuner.restore((2000, 10, 10))

    assert [op.action for op in seen] == [
        "startup_apply",
        "freeze_service",
        "collect_service",
        "maintain_service",
        "unfreeze_service",
        "restore",
    ]
    assert tuner.last_operation is seen[-1]
    first = seen[0]
    assert (first.collected, first.frozen_before, first.frozen_after) == (15_104, 0, 2_700_000)
    assert first.duration_ms == pytest.approx(1500.0)  # the fake collection takes 1.5 s
    assert set(first.as_dict()) == {"action", "collected", "frozen_before", "frozen_after", "duration_ms", "at"}
    assert isinstance(first.as_dict()["at"], str) and first.as_dict()["at"].endswith("+00:00")


def test_only_maintenance_actions_set_the_maintenance_time(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    tuner.freeze("periodic_refreeze")
    tuner.collect("periodic_collection", log=False)
    assert tuner.last_maintenance_at is None
    tuner.maintain("daily_maintenance")
    assert tuner.last_maintenance_at is not None and tuner.last_maintenance_at == tuner.last_operation.at  # type: ignore[union-attr]


def test_a_failing_listener_does_not_fail_the_operation(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = _tuner(fake_gc)

    def bad(_op: Operation) -> None:
        raise ValueError("listener broke")

    tuner.on_operation = bad
    with caplog.at_level(logging.ERROR, logger="test_gc_upkeep"):
        assert tuner.freeze()["frozen"] == 2_700_000
    assert "operation listener failed" in caplog.text
    assert tuner.last_operation is not None


def test_retune_and_restore_announce_what_they_did(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc, thresholds=(50_000, 10, 10))
    seen: list[Operation] = []
    tuner.on_operation = seen.append
    tuner.apply()
    tuner.configure(freeze=True, thresholds=(60_000, 10, 10), log_min_ms=500)
    tuner.retune()
    retune = seen[-1]
    assert retune.action == "retune"
    assert retune.collected == 0 and retune.frozen_before == retune.frozen_after == 2_700_000

    tuner.configure(freeze=False, thresholds=(60_000, 10, 10), log_min_ms=500)
    tuner.retune()
    assert seen[-1].frozen_before == 2_700_000 and seen[-1].frozen_after == 0

    tuner.restore((2000, 10, 10))
    assert seen[-1].action == "restore"


def test_our_own_collections_do_not_count_as_the_automatic_peak(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    fake_gc.run(2, 0.9)  # Python started this one
    assert tuner.peak_auto_full == pytest.approx(0.9)
    tuner.collect()  # 1.5 s, ours
    tuner.apply()
    assert tuner.peak_auto_full == 0.0  # the freeze in apply() starts the count again

    fake_gc.run(2, 0.4)
    fake_gc.run(1, 5.0)  # a generation 1 collection is not a full one
    assert tuner.peak_auto_full == pytest.approx(0.4)
    tuner.collect("periodic_collection", log=False)
    assert tuner.peak_auto_full == pytest.approx(0.4)  # still: ours is left out


@pytest.mark.parametrize("operation", ["freeze", "maintain"])
def test_a_freeze_starts_the_peak_again(fake_gc: FakeGc, operation: str) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    fake_gc.run(2, 0.8)
    assert tuner.peak_auto_full == pytest.approx(0.8)
    getattr(tuner, operation)()
    assert tuner.peak_auto_full == 0.0


def test_the_peak_survives_a_collect_without_freeze_and_thresholds_only_retune(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    fake_gc.run(2, 0.8)
    tuner.collect()
    tuner.configure(freeze=True, thresholds=(60_000, 10, 10), log_min_ms=500)
    tuner.retune()
    assert tuner.peak_auto_full == pytest.approx(0.8)


def test_the_last_full_collection_end_counts_ours_too(fake_gc: FakeGc, clock: FakeClock) -> None:
    tuner = _tuner(fake_gc)
    assert tuner.last_full_end is None
    fake_gc.run(2, 0.5)
    first = tuner.last_full_end
    clock.now += 600
    tuner.collect("periodic_collection", log=False)
    assert first is not None and tuner.last_full_end is not None and tuner.last_full_end > first + 600


def test_the_frozen_count_is_reused_for_five_minutes(fake_gc: FakeGc, clock: FakeClock) -> None:
    """gc.get_freeze_count() walks the permanent generation: the sensors may not do it every poll."""
    tuner = _tuner(fake_gc)
    tuner.apply()
    reads = fake_gc.freeze_count_reads
    for _ in range(10):
        clock.now += 20
        assert tuner.frozen_count() == 2_700_000
    assert fake_gc.freeze_count_reads == reads  # 200 s, no new walk

    clock.now += 120  # 320 s since the read at the apply
    assert tuner.frozen_count() == 2_700_000
    assert fake_gc.freeze_count_reads == reads + 1


def test_the_stats_service_reads_the_frozen_count_fresh(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    reads = fake_gc.freeze_count_reads
    fake_gc.frozen = 2_600_000  # objects died inside the permanent generation
    assert tuner.stats()["frozen"] == 2_600_000
    assert fake_gc.freeze_count_reads == reads + 1
    assert tuner.frozen_count() == 2_600_000  # and the reuse cache followed


def test_the_slow_collection_warning_does_not_walk_the_frozen_set(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = _tuner(fake_gc, log_min_ms=500)
    tuner.apply()
    reads = fake_gc.freeze_count_reads
    with caplog.at_level(logging.WARNING, logger="test_gc_upkeep"):
        fake_gc.run(2, 1.4)
    assert any("took 1.400 s" in r.getMessage() and "frozen 2700000" in r.getMessage() for r in caplog.records)
    assert fake_gc.freeze_count_reads == reads


def test_the_collect_operation_cap_run_reads_nothing_and_logs_at_info(fake_gc: FakeGc, caplog: pytest.LogCaptureFixture) -> None:
    tuner = _tuner(fake_gc)
    tuner.apply()
    reads = fake_gc.freeze_count_reads
    with caplog.at_level(logging.INFO, logger="test_gc_upkeep"):
        tuner.collect("periodic_collection", log=False)
    assert fake_gc.freeze_count_reads == reads
    lines = [r for r in caplog.records if "gc operation periodic_collection" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert tuner.last_operation.frozen_before == tuner.last_operation.frozen_after == 2_700_000  # type: ignore[union-attr]


def test_the_service_collect_keeps_its_response_shape(fake_gc: FakeGc) -> None:
    tuner = _tuner(fake_gc)
    assert set(tuner.collect()) == {"collected", "seconds", "frozen"}
    assert set(tuner.freeze()) == {"collected", "collect_seconds", "frozen"}
    assert set(tuner.unfreeze()) == {"unfrozen", "frozen"}


def test_operations_run_on_the_real_collector(real_gc_guard: None) -> None:
    """No fakes: maintain really unfreezes, collects and freezes, and the record is honest."""
    import gc

    tuner = GcTuner(LOG, freeze=True, thresholds=(50_000, 10, 10))
    tuner.install_callback()
    try:
        tuner.apply()
        frozen = gc.get_freeze_count()
        assert frozen > 0
        result = tuner.maintain()
        assert gc.get_freeze_count() > 0
        assert result["frozen"] == gc.get_freeze_count() or abs(result["frozen"] - gc.get_freeze_count()) < 50_000
        assert tuner.last_operation is not None and tuner.last_operation.action == "maintain_service"
        assert tuner.last_operation.duration_ms > 0
        assert tuner.last_maintenance_at is not None
        assert tuner_module.gc is gc
    finally:
        tuner.remove_callback()
        gc.unfreeze()
