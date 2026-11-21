# pyright: reportOptionalSubscript=false, reportIndexIssue=false, reportArgumentType=false, reportCallIssue=false, reportOperatorIssue=false, reportOptionalMemberAccess=false
"""1.1.0 in Home Assistant: the upkeep timers, their gates, the event and the new sensors."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.gc_tuning import runtime as runtime_module
from custom_components.gc_tuning.const import DEFAULT_OPTIONS, DOMAIN, EVENT_OPERATION
from custom_components.gc_tuning.runtime import GcRuntime

from .conftest import UPKEEP_ON, FakeClock, FakeGc, entity_id, setup_entry, state_of


class Timers:
    """Stands in for async_track_time_change / async_track_time_interval: records what was armed, fires on demand."""

    def __init__(self) -> None:
        self.changes: list[dict[str, Any]] = []
        self.intervals: list[dict[str, Any]] = []

    def live_changes(self) -> list[tuple[int, int, int]]:
        return [c["when"] for c in self.changes if not c["cancelled"]]

    def live_intervals(self) -> list[timedelta]:
        return [i["every"] for i in self.intervals if not i["cancelled"]]

    def fire_change(self, when: tuple[int, int, int]) -> None:
        (record,) = [c for c in self.changes if c["when"] == when and not c["cancelled"]]
        record["action"](dt_util.utcnow())

    def fire_interval(self) -> None:
        (record,) = [i for i in self.intervals if not i["cancelled"]]
        record["action"](dt_util.utcnow())


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> Timers:
    rec = Timers()

    def track_change(hass: HomeAssistant, action: Callable[[datetime], None], *, hour: int, minute: int, second: int) -> Callable[[], None]:
        record = {"when": (hour, minute, second), "action": action, "cancelled": False}
        rec.changes.append(record)
        return lambda: record.__setitem__("cancelled", True)

    def track_interval(hass: HomeAssistant, action: Callable[[datetime], None], interval: timedelta) -> Callable[[], None]:
        record = {"every": interval, "action": action, "cancelled": False}
        rec.intervals.append(record)
        return lambda: record.__setitem__("cancelled", True)

    monkeypatch.setattr(runtime_module, "async_track_time_change", track_change)
    monkeypatch.setattr(runtime_module, "async_track_time_interval", track_interval)
    return rec


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _applied(hass: HomeAssistant, options: dict[str, Any] | None = None) -> Any:
    """The entry with the upkeep on (unless `options` says otherwise), past its 300 s start-up apply."""
    entry = await setup_entry(hass, {**UPKEEP_ON, **(options or {})})
    await _advance(hass, 301)
    assert entry.runtime_data.tuner.applied is not None
    return entry


def _runtime(entry: Any) -> GcRuntime:
    return entry.runtime_data


def _events(hass: HomeAssistant) -> list[Event]:
    seen: list[Event] = []
    hass.bus.async_listen(EVENT_OPERATION, seen.append)
    return seen


# ---- which timers exist ---------------------------------------------------------------------------------------------
async def test_no_timers_before_the_apply_and_the_default_set_after_it(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    await setup_entry(hass, UPKEEP_ON)
    assert timers.live_changes() == [] and timers.live_intervals() == []

    await _advance(hass, 301)
    assert timers.live_changes() == [(4, 30, 0), (10, 30, 0), (16, 30, 0), (22, 30, 0)]  # daily + three re-freeze slots
    assert timers.live_intervals() == [timedelta(seconds=60)]


async def test_the_defaults_of_the_entry_are_the_documented_ones(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    await _applied(hass)
    assert UPKEEP_ON == {k: DEFAULT_OPTIONS[k] for k in ("daily_maintenance", "refreeze_interval_hours", "full_collection_minutes")}
    assert DEFAULT_OPTIONS["daily_time"] == "04:30:00" and DEFAULT_OPTIONS["refreeze_pause_ms"] == 500


async def test_each_feature_can_be_turned_off_on_its_own(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    runtime = _runtime(entry)

    hass.config_entries.async_update_entry(entry, options={**entry.options, "daily_maintenance": False})
    await hass.async_block_till_done()
    assert timers.live_changes() == [(4, 30, 0), (10, 30, 0), (16, 30, 0), (22, 30, 0)]  # the anchor slot does the maintenance now

    hass.config_entries.async_update_entry(entry, options={**entry.options, "refreeze_interval_hours": 0})
    await hass.async_block_till_done()
    assert timers.live_changes() == []
    assert timers.live_intervals() == [timedelta(seconds=60)]

    hass.config_entries.async_update_entry(entry, options={**entry.options, "full_collection_minutes": 0})
    await hass.async_block_till_done()
    assert runtime.schedules_armed == 0

    hass.config_entries.async_update_entry(entry, options={**entry.options, "daily_maintenance": True, "daily_time": "01:15:00", "refreeze_interval_hours": 12})
    await hass.async_block_till_done()
    assert timers.live_changes() == [(1, 15, 0), (13, 15, 0)]


async def test_an_options_change_replaces_the_timers_and_leaves_no_old_one(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    before = len(timers.changes)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "daily_time": "05:00:00"})
    await hass.async_block_till_done()
    assert len(timers.changes) == before + 4
    assert timers.live_changes() == [(5, 0, 0), (11, 0, 0), (17, 0, 0), (23, 0, 0)]
    assert len(timers.live_intervals()) == 1


@pytest.mark.parametrize(
    "options",
    [{"enabled": False}, {"freeze": False}],
    ids=["disabled", "freeze off: nothing is frozen, so there is nothing to keep small"],
)
async def test_no_upkeep_without_a_frozen_heap_of_ours(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, options: dict[str, Any]) -> None:
    entry = await setup_entry(hass, {**UPKEEP_ON, **options})
    await _advance(hass, 301)
    assert timers.live_changes() == [] and timers.live_intervals() == []
    assert _runtime(entry).upkeep_allowed() is False
    runtime = _runtime(entry)
    assert runtime.run_daily_maintenance() is None
    assert runtime.run_refreeze_slot() is None
    assert runtime.run_full_collection_check() is None
    assert fake_gc.unfreeze_calls == 0


async def test_an_apply_on_command_while_disabled_starts_no_upkeep(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    """Disabled means nothing runs by itself, even when the heap was frozen by hand."""
    entry = await setup_entry(hass, {**UPKEEP_ON, "enabled": False})
    await hass.services.async_call(DOMAIN, "apply", {}, blocking=True, return_response=True)
    runtime = _runtime(entry)
    assert runtime.tuner.frozen_by_us is True
    assert runtime.upkeep_allowed() is False
    assert timers.live_changes() == [] and timers.live_intervals() == []
    clock.now += 5 * 3600
    assert runtime.run_daily_maintenance() is None and runtime.run_full_collection_check() is None


async def test_disabling_and_unloading_cancel_every_timer(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "enabled": False})
    await hass.async_block_till_done()
    assert timers.live_changes() == [] and timers.live_intervals() == []

    hass.config_entries.async_update_entry(entry, options={**entry.options, "enabled": True})
    await hass.async_block_till_done()
    assert timers.live_changes() == []  # waiting for the start-up delay again, nothing is frozen yet
    await _advance(hass, 301)
    assert timers.live_changes() == [(4, 30, 0), (10, 30, 0), (16, 30, 0), (22, 30, 0)]  # the apply arms them
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert timers.live_changes() == [] and timers.live_intervals() == []


async def test_a_timer_that_fires_after_the_gate_closed_does_nothing(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    stale = [c["action"] for c in timers.changes]
    hass.config_entries.async_update_entry(entry, options={**entry.options, "enabled": False})
    await hass.async_block_till_done()
    calls = list(fake_gc.calls)
    for action in stale:
        action(dt_util.utcnow())
    assert fake_gc.calls == calls


# ---- daily maintenance -----------------------------------------------------------------------------------------------
async def test_the_daily_maintenance_runs_unfreeze_collect_freeze_and_says_so(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)
    seen = _events(hass)
    clock.now += 5 * 3600
    fake_gc.calls.clear()

    timers.fire_change((4, 30, 0))
    await hass.async_block_till_done()

    assert fake_gc.calls == ["unfreeze", "collect", "freeze"]
    assert len(seen) == 1
    data = seen[0].data
    assert data["action"] == "daily_maintenance"
    assert data["collected"] == 15_104
    assert data["frozen_before"] == 2_700_000 and data["frozen_after"] == 2_700_000
    assert data["duration_ms"] == pytest.approx(1500.0)
    assert dt_util.parse_datetime(data["at"]) is not None
    assert state_of(hass, entry, "last_operation") == "daily_maintenance"
    assert state_of(hass, entry, "last_maintenance_at") != "unknown"
    assert float(state_of(hass, entry, "last_operation_duration")) == pytest.approx(1.5)
    attrs = hass.states.get(entity_id(hass, entry, "last_operation")).attributes
    assert attrs["collected"] == 15_104 and attrs["frozen_after"] == 2_700_000 and attrs["duration_ms"] == pytest.approx(1500.0)


async def test_the_daily_maintenance_is_skipped_right_after_an_apply_or_another_maintenance(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)
    runtime = _runtime(entry)
    seen = _events(hass)

    clock.now += 1200  # 20 min after the apply
    assert runtime.run_daily_maintenance() is None
    assert fake_gc.unfreeze_calls == 0 and seen == []

    clock.now += 3000  # 70 min after it
    assert runtime.run_daily_maintenance() is not None
    assert fake_gc.unfreeze_calls == 1

    clock.now += 600
    assert runtime.run_daily_maintenance() is None  # 10 min after the maintenance
    assert fake_gc.unfreeze_calls == 1


async def test_a_plain_re_freeze_does_not_hold_back_the_daily_maintenance(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    """A re-freeze keeps frozen-then-dead objects; only the maintenance reclaims them, so it must still run."""
    entry = await _applied(hass, {"refreeze_pause_ms": 0})
    runtime = _runtime(entry)
    clock.now += 4 * 3600  # past the 3 h minimum gap of a 6 h interval
    timers.fire_change((10, 30, 0))
    assert fake_gc.unfreeze_calls == 0 and runtime.tuner.last_operation.action == "periodic_refreeze"
    clock.now += 600
    assert runtime.run_daily_maintenance() is not None
    assert fake_gc.unfreeze_calls == 1


async def test_the_maintenance_service_is_the_same_operation_on_command(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    seen = _events(hass)
    fake_gc.calls.clear()
    result = await hass.services.async_call(DOMAIN, "maintain", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert fake_gc.calls == ["unfreeze", "collect", "freeze"]  # no debounce: it was asked for
    assert set(result) == {"collected", "collect_seconds", "frozen_before", "frozen"}
    assert [e.data["action"] for e in seen] == ["maintain_service"]
    assert state_of(hass, entry, "last_operation") == "maintain_service"


# ---- periodic re-freeze -----------------------------------------------------------------------------------------------
async def test_a_slot_re_freezes_once_a_full_collection_reached_the_target(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)  # target 500 ms
    seen = _events(hass)
    clock.now += 6 * 3600
    fake_gc.run(2, 0.62)  # an automatic full collection of 620 ms
    fake_gc.calls.clear()

    timers.fire_change((10, 30, 0))
    await hass.async_block_till_done()

    assert fake_gc.calls == ["collect", "freeze"]  # no unfreeze: this is the cheap one
    assert [e.data["action"] for e in seen] == ["periodic_refreeze"]
    assert state_of(hass, entry, "last_operation") == "periodic_refreeze"
    assert float(state_of(hass, entry, "longest_auto_full_since_freeze")) == 0.0  # the freeze started the count again


async def test_a_slot_waits_while_every_full_collection_was_short(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    entry = await _applied(hass)
    seen = _events(hass)
    clock.now += 6 * 3600
    fake_gc.run(2, 0.31)
    fake_gc.run(2, 0.49)
    fake_gc.calls.clear()
    with caplog.at_level("INFO", logger="custom_components.gc_tuning.runtime"):
        timers.fire_change((10, 30, 0))
    await hass.async_block_till_done()
    assert fake_gc.calls == [] and seen == []
    assert "longest automatic full collection since the last freeze was 490 ms, target 500 ms" in caplog.text
    await entry.runtime_data.coordinator.async_refresh()  # the sensor follows the minute poll
    assert float(state_of(hass, entry, "longest_auto_full_since_freeze")) == pytest.approx(0.49)  # it shows what the decision saw


async def test_our_own_slow_collection_does_not_trigger_a_re_freeze(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)
    clock.now += 6 * 3600
    await hass.services.async_call(DOMAIN, "collect", {}, blocking=True, return_response=True)  # 1.5 s, ours
    fake_gc.calls.clear()
    assert _runtime(entry).run_refreeze_slot() is None
    assert fake_gc.calls == []


async def test_target_zero_means_every_slot_runs(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass, {"refreeze_pause_ms": 0})
    clock.now += 6 * 3600
    assert _runtime(entry).run_refreeze_slot() is not None


async def test_a_slot_right_after_a_freeze_is_skipped(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    entry = await _applied(hass, {"refreeze_pause_ms": 0})
    runtime = _runtime(entry)
    clock.now += 2 * 3600  # the interval is 6 h: the minimum gap is 3 h
    with caplog.at_level("INFO", logger="custom_components.gc_tuning.runtime"):
        assert runtime.run_refreeze_slot() is None
    assert "last freeze was 7200 s ago (minimum 10800 s)" in caplog.text
    clock.now += 3700
    assert runtime.run_refreeze_slot() is not None


async def test_with_the_daily_maintenance_off_the_slots_do_the_full_maintenance_without_a_target(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    """Re-freezing alone keeps what dies later in the permanent generation forever: something has to reclaim it."""
    entry = await _applied(hass, {"daily_maintenance": False, "refreeze_pause_ms": 900})
    seen = _events(hass)
    clock.now += 6 * 3600
    fake_gc.calls.clear()
    timers.fire_change((4, 30, 0))  # the anchor slot exists while the maintenance is off
    await hass.async_block_till_done()
    assert fake_gc.calls == ["unfreeze", "collect", "freeze"]  # no short collection was seen, the target (900 ms) is not applied
    assert [e.data["action"] for e in seen] == ["periodic_maintenance"]
    assert state_of(hass, entry, "last_maintenance_at") != "unknown"


# ---- a full collection at least every N minutes -------------------------------------------------------------------------
async def test_a_full_collection_runs_when_none_happened_for_the_set_time(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)  # 45 min
    seen = _events(hass)
    runtime = _runtime(entry)

    clock.now += 44 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 1  # only the apply's

    clock.now += 2 * 60
    fake_gc.calls.clear()
    timers.fire_interval()
    await hass.async_block_till_done()
    assert fake_gc.calls == ["collect"]  # a plain full collection: no freeze, no unfreeze
    assert [e.data["action"] for e in seen] == ["periodic_collection"]
    assert runtime.tuner.last_operation.frozen_before == runtime.tuner.last_operation.frozen_after

    clock.now += 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 2  # it counted as the newest full collection


async def test_any_full_collection_restarts_the_clock_of_the_cap(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass)
    clock.now += 40 * 60
    fake_gc.run(2, 0.5)  # one that Python started
    clock.now += 40 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 1  # 40 min since it: nothing to do
    clock.now += 6 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 2
    assert entry.runtime_data.tuner.last_operation.action == "periodic_collection"


async def test_the_cap_follows_its_option(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers, clock: FakeClock) -> None:
    entry = await _applied(hass, {"full_collection_minutes": 10})
    clock.now += 11 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 2
    hass.config_entries.async_update_entry(entry, options={**entry.options, "full_collection_minutes": 120})
    await hass.async_block_till_done()
    clock.now += 60 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 2  # 60 min of 120
    clock.now += 61 * 60
    timers.fire_interval()
    assert fake_gc.collect_calls == 3


# ---- the event -----------------------------------------------------------------------------------------------------------
async def test_every_operation_fires_one_event_after_the_sensors_changed(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await setup_entry(hass, UPKEEP_ON)
    states_at_event: list[tuple[str, str]] = []

    def on_event(event: Event) -> None:
        states_at_event.append((event.data["action"], state_of(hass, entry, "last_operation")))

    hass.bus.async_listen(EVENT_OPERATION, on_event)
    await _advance(hass, 301)
    await hass.services.async_call(DOMAIN, "freeze", {}, blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "collect", {}, blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "maintain", {}, blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "unfreeze", {}, blocking=True, return_response=True)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "threshold0": 61000})
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "enabled": False})
    await hass.async_block_till_done()

    actions = [a for a, _ in states_at_event]
    assert actions == ["startup_apply", "freeze_service", "collect_service", "maintain_service", "unfreeze_service", "retune", "restore"]
    assert all(action == sensor for action, sensor in states_at_event)  # the sensor already showed the operation


async def test_the_apply_service_fires_apply_service(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    await setup_entry(hass, {"enabled": False})
    seen = _events(hass)
    await hass.services.async_call(DOMAIN, "apply", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert [e.data["action"] for e in seen] == ["apply_service"]


async def test_unload_restores_and_says_so_once(hass: HomeAssistant, fake_gc: FakeGc, timers: Timers) -> None:
    entry = await _applied(hass)
    runtime = _runtime(entry)
    seen = _events(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [e.data["action"] for e in seen] == ["restore"]
    assert seen[0].data["frozen_before"] == 2_700_000 and seen[0].data["frozen_after"] == 0
    assert runtime.tuner.on_operation is None


async def test_the_new_sensors_before_anything_happened(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    assert state_of(hass, entry, "last_operation") == "unknown"
    assert state_of(hass, entry, "last_operation_duration") == "unknown"
    assert state_of(hass, entry, "last_maintenance_at") == "unknown"
    assert float(state_of(hass, entry, "longest_auto_full_since_freeze")) == 0.0


# ---- the real timers, end to end ---------------------------------------------------------------------------------------------
async def test_the_real_timer_fires_the_maintenance_at_the_set_time(hass: HomeAssistant, fake_gc: FakeGc, clock: FakeClock) -> None:
    """No Timers stand-in: Home Assistant's own time-change timer runs the maintenance at the configured local time."""
    now = dt_util.now()
    at = (now + timedelta(minutes=10)).replace(microsecond=0)
    entry = await setup_entry(hass, {"daily_maintenance": True, "daily_time": at.strftime("%H:%M:%S"), "refreeze_interval_hours": 0, "full_collection_minutes": 0})
    await _advance(hass, 301)
    clock.now += 2 * 3600  # long after the apply
    seen = _events(hass)
    fake_gc.calls.clear()

    async_fire_time_changed(hass, at + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert fake_gc.calls == ["unfreeze", "collect", "freeze"]
    assert [e.data["action"] for e in seen] == ["daily_maintenance"]
    assert state_of(hass, entry, "last_operation") == "daily_maintenance"
