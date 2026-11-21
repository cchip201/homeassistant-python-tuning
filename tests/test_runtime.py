# pyright: reportOptionalSubscript=false, reportIndexIssue=false, reportArgumentType=false, reportCallIssue=false, reportOperatorIssue=false
"""Start-up delay, freeze once, live options, disable, unload."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.gc_tuning.const import DEFAULT_OPTIONS, DOMAIN

from .conftest import FakeGc, make_entry, setup_entry


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _options(hass: HomeAssistant, entry: Any, **changes: Any) -> None:
    """Change options the way the options flow does, then let the update listener run."""
    hass.config_entries.async_update_entry(entry, options={**entry.options, **changes})
    await hass.async_block_till_done()


async def test_default_behaviour_is_the_proven_fix(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    """300 s after start: gc.collect(), gc.freeze(), thresholds (50000, 10, 10). Nothing before."""
    entry = await setup_entry(hass)

    # the observer is installed, nothing else touched yet
    assert len(fake_gc.callbacks) == 1
    assert fake_gc.collect_calls == 0
    assert fake_gc.freeze_calls == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)

    await _advance(hass, 299)
    assert fake_gc.freeze_calls == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)

    await _advance(hass, 301)
    assert fake_gc.collect_calls == 1
    assert fake_gc.freeze_calls == 1
    assert fake_gc.get_threshold() == (50000, 10, 10)
    assert fake_gc.get_freeze_count() == 2_700_000
    assert entry.runtime_data.tuner.applied is not None


async def test_the_delay_counts_from_home_assistant_started(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    hass.set_state(CoreState.starting)
    await setup_entry(hass)

    await _advance(hass, 10_000)  # not started yet: no timer exists, so nothing can fire
    assert fake_gc.freeze_calls == 0

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await _advance(hass, 299)
    assert fake_gc.freeze_calls == 0
    await _advance(hass, 301)
    assert fake_gc.freeze_calls == 1
    assert fake_gc.get_threshold() == (50000, 10, 10)


async def test_freeze_runs_once_after_the_delay(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)
    assert fake_gc.freeze_calls == 1

    # time passes, more timers fire: still one freeze
    await _advance(hass, 900)
    await _advance(hass, 3600)
    assert fake_gc.freeze_calls == 1
    assert fake_gc.collect_calls == 1

    # new thresholds do not freeze a second time
    await _options(hass, entry, threshold0=70000, threshold1=15)
    assert fake_gc.freeze_calls == 1
    assert fake_gc.collect_calls == 1
    assert fake_gc.get_threshold() == (70000, 15, 10)


async def test_options_apply_live_without_a_reload(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)
    runtime = entry.runtime_data

    await _options(hass, entry, threshold0=60000, threshold1=20, threshold2=15)

    assert fake_gc.get_threshold() == (60000, 20, 15)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is runtime  # same object: not reloaded, no restart
    assert runtime.tuner.applied is not None
    assert runtime.tuner.applied["thresholds"] == [60000, 20, 15]
    assert fake_gc.freeze_calls == 1
    assert len(fake_gc.callbacks) == 1


async def test_a_shorter_delay_before_the_apply_moves_the_apply_up(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _options(hass, entry, start_delay=60)

    await _advance(hass, 59)
    assert fake_gc.freeze_calls == 0
    await _advance(hass, 62)
    assert fake_gc.freeze_calls == 1


async def test_a_longer_delay_before_the_apply_moves_it_back(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _options(hass, entry, start_delay=900)

    await _advance(hass, 301)
    assert fake_gc.freeze_calls == 0
    await _advance(hass, 901)
    assert fake_gc.freeze_calls == 1


async def test_a_delay_already_over_applies_at_once_when_enabled_later(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass, {"enabled": False})
    await _advance(hass, 400)
    assert fake_gc.freeze_calls == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)

    # the 300 s deadline of this start is long gone (loop time moves for real only a little in a test): move it back
    entry.runtime_data.domain_data.started_at -= 1000
    await _options(hass, entry, enabled=True)

    assert fake_gc.freeze_calls == 1
    assert fake_gc.get_threshold() == (50000, 10, 10)


async def test_disabled_never_applies(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass, {"enabled": False})
    await _advance(hass, 400)
    await _advance(hass, 4000)

    assert fake_gc.freeze_calls == 0
    assert fake_gc.collect_calls == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)
    assert len(fake_gc.callbacks) == 1  # still observing
    assert entry.runtime_data.tuner.applied is None


async def test_disabling_unfreezes_and_restores_the_thresholds(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)
    assert fake_gc.get_freeze_count() == 2_700_000
    assert fake_gc.get_threshold() == (50000, 10, 10)

    await _options(hass, entry, enabled=False)

    assert fake_gc.unfreeze_calls == 1
    assert fake_gc.get_freeze_count() == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)  # what Python had before
    assert entry.runtime_data.tuner.applied is None
    assert entry.state is ConfigEntryState.LOADED


async def test_enabling_again_freezes_again(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)
    await _options(hass, entry, enabled=False)
    assert fake_gc.freeze_calls == 1

    entry.runtime_data.domain_data.started_at -= 1000  # the 300 s of this start are over (a test only lives milliseconds)
    await _options(hass, entry, enabled=True)  # so it applies now

    assert fake_gc.freeze_calls == 2
    assert fake_gc.get_threshold() == (50000, 10, 10)
    assert fake_gc.get_freeze_count() == 2_700_000


async def test_freeze_switched_off_and_on_while_applied(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)

    await _options(hass, entry, freeze=False)
    assert fake_gc.get_freeze_count() == 0
    assert fake_gc.get_threshold() == (50000, 10, 10)

    await _options(hass, entry, freeze=True)
    assert fake_gc.get_freeze_count() == 2_700_000


async def test_reset_to_defaults_applies_live(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass, {"threshold0": 3000, "threshold1": 3, "threshold2": 4})
    await _advance(hass, 301)
    assert fake_gc.get_threshold() == (3000, 3, 4)

    await _options(hass, entry, **DEFAULT_OPTIONS)

    assert fake_gc.get_threshold() == (50000, 10, 10)


async def test_unload_gives_the_process_its_own_settings_back(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert fake_gc.get_threshold() == (2000, 10, 10)
    assert fake_gc.get_freeze_count() == 0
    assert fake_gc.callbacks == []
    # no apply is left pending for a removed entry
    await _advance(hass, 5000)
    assert fake_gc.freeze_calls == 1


async def test_unload_before_the_apply_cancels_the_timer(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await _advance(hass, 5000)
    assert fake_gc.freeze_calls == 0
    assert fake_gc.get_threshold() == (2000, 10, 10)


async def test_reload_applies_again_and_keeps_the_original_thresholds(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    entry = await setup_entry(hass)
    await _advance(hass, 301)
    entry.runtime_data.domain_data.started_at -= 1000  # the start-up delay of this run is long over

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert fake_gc.get_threshold() == (50000, 10, 10)
    assert entry.runtime_data.original_thresholds == (2000, 10, 10)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_gc.get_threshold() == (2000, 10, 10)  # not 50000: the original survived the reload


async def test_a_failing_apply_does_not_break_the_entry(hass: HomeAssistant, fake_gc: FakeGc, caplog: Any) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)

    def boom() -> int:
        raise RuntimeError("collector refused")

    fake_gc.collect = boom  # type: ignore[method-assign]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _advance(hass, 301)

    assert entry.state is ConfigEntryState.LOADED
    assert "gc tuning apply failed" in caplog.text
    assert fake_gc.get_threshold() == (2000, 10, 10)


async def test_domain_has_no_services_left_unregistered(hass: HomeAssistant, fake_gc: FakeGc) -> None:
    await setup_entry(hass)
    assert set(hass.services.async_services_for_domain(DOMAIN)) == {"apply", "freeze", "unfreeze", "collect", "maintain", "stats"}


async def test_the_proven_fix_on_the_real_collector(hass: HomeAssistant) -> None:
    """No fakes: after 300 s the real gc is frozen with thresholds (50000, 10, 10); disabling puts both back."""
    import gc

    before_threshold = gc.get_threshold()
    before_frozen = gc.get_freeze_count()
    entry = await setup_entry(hass)
    assert gc.get_threshold() == before_threshold

    await _advance(hass, 301)
    assert gc.get_threshold() == (50000, 10, 10)
    assert gc.get_freeze_count() > before_frozen
    stats = await hass.services.async_call(DOMAIN, "stats", {}, blocking=True, return_response=True)
    # frozen objects that die later are freed by reference counting and leave the count: it can only shrink
    assert 0 < stats["frozen"] <= stats["applied"]["frozen"]
    assert stats["thresholds"] == [50000, 10, 10]

    await _options(hass, entry, enabled=False)
    assert gc.get_threshold() == before_threshold
    # gc.unfreeze() has no partial form: it returns everything frozen by anyone (the test plugin freezes per module)
    assert gc.get_freeze_count() == 0
