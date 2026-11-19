"""The services of 0.1.0 and the shape of the `stats` response (callers read applied.at, thresholds, frozen, recent, observed)."""

from __future__ import annotations

from datetime import timedelta
import re

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.gc_tuning.const import DOMAIN

from .conftest import FakeGc, setup_entry

# The 0.1.0 response, key by key (taken from custom_components/gc_tuning/gc_tuning.py in the HA config repo).
STATS_KEYS = {"thresholds", "count_pending", "frozen", "enabled", "python_gc_stats", "observed", "recent", "applied"}
OBSERVED_KEYS = {"generation", "collections", "seconds", "slowest_s"}
RECENT_KEYS = {"started", "generation", "seconds", "collected"}
APPLIED_KEYS = {"collected", "collect_seconds", "frozen", "thresholds_before", "thresholds", "at"}


async def _call(hass: HomeAssistant, service: str, data: dict | None = None) -> dict:
    result = await hass.services.async_call(DOMAIN, service, data or {}, blocking=True, return_response=True)
    assert isinstance(result, dict)
    return result


async def test_stats_shape_is_unchanged_before_apply(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    await setup_entry(hass)
    fake_gc.run(0, 0.001)
    fake_gc.run(1, 0.02, collected=7)
    fake_gc.run(2, 0.9, collected=100)

    stats = await _call(hass, "stats")

    assert set(stats) == STATS_KEYS
    assert stats["applied"] is None
    assert stats["thresholds"] == [2000, 10, 10]
    assert stats["frozen"] == 0
    assert [set(o) for o in stats["observed"]] == [OBSERVED_KEYS] * 3
    assert [o["generation"] for o in stats["observed"]] == [0, 1, 2]
    assert [o["collections"] for o in stats["observed"]] == [1, 1, 1]
    assert stats["observed"][2]["slowest_s"] == 0.9
    assert all(set(r) == RECENT_KEYS for r in stats["recent"])
    assert [r["generation"] for r in stats["recent"]] == [1, 2]  # generation 0 below log_min_ms is not kept, as before
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", stats["recent"][0]["started"])


async def test_stats_shape_is_unchanged_after_apply(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    await setup_entry(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()

    stats = await _call(hass, "stats", {"recent": 5})

    assert set(stats) == STATS_KEYS
    applied = stats["applied"]
    assert set(applied) == APPLIED_KEYS
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", applied["at"])  # what callers read: applied.at
    assert applied["thresholds_before"] == [2000, 10, 10]
    assert applied["thresholds"] == [50000, 10, 10]
    assert applied["frozen"] == 2_700_000
    assert applied["collected"] == 15_104
    assert stats["thresholds"] == [50000, 10, 10]
    assert stats["frozen"] == 2_700_000
    assert len(stats["recent"]) <= 5
    assert stats["recent"][-1]["generation"] == 2  # the apply's own full collection is observed


async def test_apply_service_is_the_same_apply_on_command(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)

    result = await _call(hass, "apply")

    assert set(result) == APPLIED_KEYS
    assert fake_gc.get_threshold() == (50000, 10, 10)
    assert fake_gc.freeze_calls == 1
    assert entry.runtime_data.tuner.applied is not None
    # the pending start-up apply is cancelled by the explicit one: no second freeze 300 s later
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    assert fake_gc.freeze_calls == 1


async def test_freeze_unfreeze_collect_services(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    await setup_entry(hass, {"enabled": False})

    assert set(await _call(hass, "freeze")) == {"collected", "collect_seconds", "frozen"}
    assert fake_gc.get_freeze_count() == 2_700_000
    assert set(await _call(hass, "collect")) == {"collected", "seconds", "frozen"}
    assert await _call(hass, "unfreeze") == {"unfrozen": 2_700_000, "frozen": 0}


async def test_services_refuse_cleanly_when_the_entry_is_not_loaded(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "stats", {}, blocking=True, return_response=True)
