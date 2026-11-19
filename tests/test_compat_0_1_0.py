"""The new tuner against the 0.1.0 module that runs on the instance today, on the REAL collector.

Both modules apply the proven fix in turn; what they do to the collector and what they return must match.
"""

from __future__ import annotations

import gc
import logging

from custom_components.gc_tuning.tuner import GcTuner

from .reference.gc_tuning_0_1_0 import GcTuner as OldGcTuner

LOG = logging.getLogger("test_gc_compat")


def _reset() -> None:
    gc.unfreeze()
    gc.set_threshold(2000, 10, 10)


def _shape(value: object) -> object:
    """The structure of a response: keys and value types, not the measured numbers."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value[:1]]
    return type(value).__name__


def test_apply_does_the_same_to_the_collector(real_gc_guard: None) -> None:
    results = []
    for cls in (OldGcTuner, GcTuner):
        _reset()
        tuner = cls(LOG, freeze=True, thresholds=(50_000, 10, 10), log_min_ms=500)
        tuner.install_callback()
        result = tuner.apply()
        results.append((result, gc.get_threshold(), gc.get_freeze_count() > 0))
        tuner.remove_callback()
    (old, old_thresholds, old_frozen), (new, new_thresholds, new_frozen) = results
    assert old_thresholds == new_thresholds == (50_000, 10, 10)
    assert old_frozen and new_frozen
    assert set(old) == set(new)
    assert _shape(old) == _shape(new)
    assert old["thresholds"] == new["thresholds"] == [50_000, 10, 10]
    assert old["thresholds_before"] == new["thresholds_before"] == [2000, 10, 10]
    assert len(old["at"]) == len(new["at"]) == 12  # HH:MM:SS.mmm


def test_stats_response_matches_before_and_after_apply(real_gc_guard: None) -> None:
    shapes = []
    for cls in (OldGcTuner, GcTuner):
        _reset()
        tuner = cls(LOG, freeze=True, thresholds=(50_000, 10, 10), log_min_ms=500)
        tuner.install_callback()
        before = tuner.stats(recent=20)
        tuner.apply()
        gc.collect()
        after = tuner.stats(recent=20)
        tuner.remove_callback()
        shapes.append((before, after))
    (old_before, old_after), (new_before, new_after) = shapes
    assert set(old_before) == set(new_before)
    assert set(old_after) == set(new_after)
    assert _shape(old_before) == _shape(new_before)
    assert _shape(old_after) == _shape(new_after)
    assert old_before["applied"] is None and new_before["applied"] is None
    assert [o["generation"] for o in old_after["observed"]] == [o["generation"] for o in new_after["observed"]] == [0, 1, 2]
    assert set(old_after["recent"][-1]) == set(new_after["recent"][-1])


def test_freeze_unfreeze_collect_responses_match(real_gc_guard: None) -> None:
    out = []
    for cls in (OldGcTuner, GcTuner):
        _reset()
        tuner = cls(LOG)
        out.append((_shape(tuner.freeze()), _shape(tuner.unfreeze()), _shape(tuner.collect())))
    assert out[0] == out[1]
