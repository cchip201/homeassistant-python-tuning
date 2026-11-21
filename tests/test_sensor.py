# pyright: reportTypedDictNotRequiredAccess=false, reportIndexIssue=false, reportOptionalSubscript=false, reportArgumentType=false, reportOperatorIssue=false, reportCallIssue=false, reportReturnType=false, reportOptionalMemberAccess=false, reportPrivateImportUsage=false
"""Sensors: values, update cost, and what a poll is allowed to do."""

from __future__ import annotations

from datetime import timedelta
import sys
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.gc_tuning.const import DOMAIN
from custom_components.gc_tuning.coordinator import read_process_memory_mb
from custom_components.gc_tuning.sensor import SENSORS

from .conftest import FakeClock, FakeGc, entity_id, setup_entry, state_of


async def _poll(hass: HomeAssistant, seconds: float = 61) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """One coordinator poll, made directly: the timer-driven poll depends on the coordinator's random jitter and flakes."""
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_eleven_sensors_on_one_device(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {e.unique_id.removeprefix(f"{entry.entry_id}_") for e in entries} == {
        "full_collections_last_hour",
        "longest_pause_last_hour",
        "last_full_collection_duration",
        "frozen_objects",
        "thresholds",
        "applied_at",
        "last_operation",
        "last_operation_duration",
        "last_maintenance_at",
        "longest_auto_full_since_freeze",
        "process_memory",
    }
    assert len(SENSORS) == 11
    assert len({e.device_id for e in entries}) == 1
    device = dr.async_get(hass).async_get(entries[0].device_id)
    assert device is not None and device.name == "GC tuning"
    assert all(e.entity_category is er.EntityCategory.DIAGNOSTIC for e in entries)


async def test_sensors_before_anything_happened(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    assert state_of(hass, entry, "full_collections_last_hour") == "0"
    assert state_of(hass, entry, "longest_pause_last_hour") == "0.0"
    assert state_of(hass, entry, "last_full_collection_duration") == "unknown"
    assert state_of(hass, entry, "frozen_objects") == "0"
    assert state_of(hass, entry, "thresholds") == "2000 / 10 / 10"
    assert state_of(hass, entry, "applied_at") == "unknown"


async def test_sensors_follow_the_collections(hass: HomeAssistant, fake_gc: FakeGc, clock: FakeClock) -> None:
    entry = await setup_entry(hass)

    fake_gc.run(2, 2.4)  # a full collection, 2.4 s
    clock.now += 60
    fake_gc.run(0, 0.01)
    fake_gc.run(1, 0.35)
    clock.now += 60
    fake_gc.run(2, 0.5)  # the most recent full collection
    await _refresh(hass, entry)

    assert state_of(hass, entry, "full_collections_last_hour") == "2"
    assert float(state_of(hass, entry, "longest_pause_last_hour")) == pytest.approx(2.4)
    assert float(state_of(hass, entry, "last_full_collection_duration")) == pytest.approx(0.5)


async def test_the_hour_window_slides(hass: HomeAssistant, fake_gc: FakeGc, clock: FakeClock) -> None:
    entry = await setup_entry(hass)
    fake_gc.run(2, 2.4)
    clock.now += 1800
    fake_gc.run(2, 0.4)
    await _refresh(hass, entry)
    assert state_of(hass, entry, "full_collections_last_hour") == "2"
    assert float(state_of(hass, entry, "longest_pause_last_hour")) == pytest.approx(2.4)

    clock.now += 1830  # the first one is now over an hour old (the longest pause is exact to 10 s)
    await _refresh(hass, entry)
    assert state_of(hass, entry, "full_collections_last_hour") == "1"
    assert float(state_of(hass, entry, "longest_pause_last_hour")) == pytest.approx(0.4)

    clock.now += 3700  # both gone; the last duration stays what it was
    await _refresh(hass, entry)
    assert state_of(hass, entry, "full_collections_last_hour") == "0"
    assert state_of(hass, entry, "longest_pause_last_hour") == "0.0"
    assert float(state_of(hass, entry, "last_full_collection_duration")) == pytest.approx(0.4)


async def test_sensors_after_apply(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    before = dt_util.utcnow()
    await _poll(hass, 301)

    assert state_of(hass, entry, "frozen_objects") == "2700000"
    assert state_of(hass, entry, "thresholds") == "50000 / 10 / 10"
    attrs = hass.states.get(entity_id(hass, entry, "thresholds")).attributes
    assert (attrs["generation_0"], attrs["generation_1"], attrs["generation_2"]) == (50000, 10, 10)
    applied = dt_util.parse_datetime(state_of(hass, entry, "applied_at"))
    assert applied is not None and applied >= before.replace(microsecond=0)  # a timestamp state has whole seconds
    assert float(state_of(hass, entry, "last_full_collection_duration")) == pytest.approx(1.5)  # the apply's own collection


async def test_sensors_show_a_live_option_change_at_once(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _poll(hass, 301)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "threshold0": 61000})
    await hass.async_block_till_done()  # no poll in between
    assert state_of(hass, entry, "thresholds") == "61000 / 10 / 10"

    hass.config_entries.async_update_entry(entry, options={**entry.options, "enabled": False})
    await hass.async_block_till_done()
    assert state_of(hass, entry, "thresholds") == "2000 / 10 / 10"
    assert state_of(hass, entry, "frozen_objects") == "0"
    assert state_of(hass, entry, "applied_at") == "unknown"


async def test_process_memory_is_the_rss_in_mb(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    with patch("custom_components.gc_tuning.coordinator.read_process_memory_mb", return_value=1234.5):
        entry = await setup_entry(hass)
        assert state_of(hass, entry, "process_memory") == "1234.5"
    state = hass.states.get(entity_id(hass, entry, "process_memory"))
    assert state is not None
    assert state.attributes["unit_of_measurement"] == "MB"
    assert state.attributes["device_class"] == "data_size"


async def test_process_memory_unreadable_is_unknown_not_an_error(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    with patch("custom_components.gc_tuning.coordinator.read_process_memory_mb", return_value=None):
        entry = await setup_entry(hass)
    assert state_of(hass, entry, "process_memory") == "unknown"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/statm")
def test_the_rss_reader_reads_this_process() -> None:
    mb = read_process_memory_mb()
    assert mb is not None and 20 < mb < 20_000


async def test_a_poll_walks_no_heap_and_runs_one_executor_job(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    """The update cost: counters only. No collect, no freeze, one executor call (the RSS read)."""
    entry = await setup_entry(hass, {"enabled": False})
    collect_before = fake_gc.collect_calls
    coordinator = entry.runtime_data.coordinator
    with patch.object(hass, "async_add_executor_job", wraps=hass.async_add_executor_job) as executor:
        for _ in range(5):
            await coordinator.async_refresh()
    assert fake_gc.collect_calls == collect_before
    assert fake_gc.freeze_calls == 0
    assert executor.call_count == 5
    assert all(c.args[0] is read_process_memory_mb or c.args[0].__name__ == "read_process_memory_mb" for c in executor.call_args_list)


async def test_the_poll_interval_is_one_minute(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    assert entry.runtime_data.coordinator.update_interval == timedelta(seconds=60)


async def test_the_sensors_are_real_with_the_real_collector(hass: HomeAssistant, real_gc_guard: None) -> None:
    """No fakes: real gc.collect() calls are seen by the observer and counted by the sensors."""
    entry = await setup_entry(hass, {"enabled": False})
    import gc

    first = int(state_of(hass, entry, "full_collections_last_hour"))
    for _ in range(3):
        gc.collect()
    await _refresh(hass, entry)
    assert int(state_of(hass, entry, "full_collections_last_hour")) >= first + 3
    # a collection of an already frozen heap can take under half a millisecond, which a 3-decimal sensor shows as 0.0
    assert float(state_of(hass, entry, "longest_pause_last_hour")) >= 0
    assert state_of(hass, entry, "last_full_collection_duration") != "unknown"
    assert entry.runtime_data.tuner.last_full_seconds is not None and entry.runtime_data.tuner.last_full_seconds > 0
    assert DOMAIN in hass.config.components
