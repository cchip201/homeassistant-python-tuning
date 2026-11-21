"""Fixtures: a fake `gc` (nothing in the test process is frozen or retuned) with a fake clock, and helpers."""

from __future__ import annotations

from collections.abc import Callable, Generator
import gc
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.gc_tuning import tuner as tuner_module
from custom_components.gc_tuning.const import (
    CONF_DAILY_MAINTENANCE,
    CONF_FULL_MINUTES,
    CONF_REFREEZE_HOURS,
    DEFAULT_OPTIONS,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the test Home Assistant load custom_components/gc_tuning."""


class FakeClock:
    """Replaces the tuner's perf counter; the tests advance it by hand."""

    def __init__(self) -> None:
        self.now = 100_000.0

    def __call__(self) -> float:
        return self.now


class FakeGc:
    """The parts of the `gc` module the tuner and the coordinator use, with counters for the assertions."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.callbacks: list[Callable[[str, dict[str, Any]], None]] = []
        self.thresholds = [2000, 10, 10]
        self.heap = 2_700_000
        self.frozen = 0
        self.collect_calls = 0
        self.freeze_calls = 0
        self.unfreeze_calls = 0
        self.collect_seconds = 1.5
        self.enabled = True
        self.calls: list[str] = []  # "unfreeze", "collect", "freeze" in the order they were made
        self.freeze_count_reads = 0

    # -- the gc API ---------------------------------------------------------------------------
    def get_threshold(self) -> tuple[int, int, int]:
        return (self.thresholds[0], self.thresholds[1], self.thresholds[2])

    def set_threshold(self, t0: int, t1: int = 10, t2: int = 10) -> None:
        self.thresholds = [t0, t1, t2]

    def get_freeze_count(self) -> int:
        self.freeze_count_reads += 1
        return self.frozen

    def freeze(self) -> None:
        self.calls.append("freeze")
        self.freeze_calls += 1
        self.frozen += self.heap
        self.heap = 0

    def unfreeze(self) -> None:
        self.calls.append("unfreeze")
        self.unfreeze_calls += 1
        self.heap += self.frozen
        self.frozen = 0

    def collect(self, generation: int = 2) -> int:
        self.calls.append("collect")
        self.collect_calls += 1
        self.run(generation, self.collect_seconds, collected=15_104)
        return 15_104

    def get_count(self) -> tuple[int, int, int]:
        return (11, 2, 3)

    def isenabled(self) -> bool:
        return self.enabled

    def get_stats(self) -> list[dict[str, int]]:
        return [{"collections": 100, "collected": 1, "uncollectable": 0}] * 3

    # -- test helper --------------------------------------------------------------------------
    def run(self, generation: int, seconds: float, collected: int = 0) -> None:
        """One collection as CPython reports it to gc.callbacks: start, `seconds` pass, stop."""
        for cb in list(self.callbacks):
            cb("start", {"generation": generation})
        self.clock.now += seconds
        for cb in list(self.callbacks):
            cb("stop", {"generation": generation, "collected": collected, "uncollectable": 0})


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(tuner_module, "perf_counter", clock)
    return clock


@pytest.fixture
def fake_gc(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> FakeGc:
    fake = FakeGc(clock)
    monkeypatch.setattr(tuner_module, "gc", fake)
    return fake


@pytest.fixture(autouse=True)
def real_gc_guard() -> Generator[None]:
    """Whatever a test does to the real collector is undone: thresholds, freeze, and any callback it left."""
    thresholds = gc.get_threshold()
    freeze_before = gc.get_freeze_count()
    callbacks_before = list(gc.callbacks)
    yield
    gc.callbacks[:] = callbacks_before
    if gc.get_freeze_count() > freeze_before:
        gc.unfreeze()
    gc.set_threshold(*thresholds)


# The 1.0.0 behaviour: the apply and nothing else. Most tests count gc calls exactly, so the upkeep timers (which fire
# whenever a test moves the clock) are off unless a test asks for them with UPKEEP_ON.
UPKEEP_OFF: dict[str, Any] = {CONF_DAILY_MAINTENANCE: False, CONF_REFREEZE_HOURS: 0, CONF_FULL_MINUTES: 0}
UPKEEP_ON: dict[str, Any] = {k: DEFAULT_OPTIONS[k] for k in UPKEEP_OFF}


def make_entry(options: dict[str, Any] | None = None, **kwargs: Any) -> MockConfigEntry:
    """The single entry: the proven defaults with the upkeep off, unless `options` says otherwise."""
    merged = dict(DEFAULT_OPTIONS)
    merged.update(UPKEEP_OFF)
    merged.update(options or {})
    return MockConfigEntry(domain=DOMAIN, title="GC tuning", data={}, options=merged, **kwargs)


async def setup_entry(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Add and load the entry; Home Assistant counts as already started (the `hass` fixture is running)."""
    entry = make_entry(options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """The sensor's entity id, looked up by its unique id."""
    found = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
    assert found is not None, f"no sensor {key}"
    return found


def state_of(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """The sensor's state string."""
    state = hass.states.get(entity_id(hass, entry, key))
    assert state is not None
    return state.state


@pytest.fixture
async def setup_yaml(hass: HomeAssistant) -> Callable[[dict[str, Any]], Any]:
    """Load the component with a `gc_tuning:` YAML block, as configuration.yaml does."""

    async def _go(block: dict[str, Any]) -> None:
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: block})
        await hass.async_block_till_done()

    return _go
