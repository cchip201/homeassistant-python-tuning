"""The running state of the one config entry: start-up delay, apply, live option changes, restore."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.start import async_at_started

from . import tuner as tuner_module
from .const import (
    ACTION_APPLY_SERVICE,
    ACTION_DAILY_MAINTENANCE,
    ACTION_PERIODIC_COLLECTION,
    ACTION_PERIODIC_MAINTENANCE,
    ACTION_PERIODIC_REFREEZE,
    ACTION_STARTUP_APPLY,
    CONF_DAILY_MAINTENANCE,
    CONF_DAILY_TIME,
    CONF_ENABLED,
    CONF_FREEZE,
    CONF_FULL_MINUTES,
    CONF_LOG_MIN_MS,
    CONF_REFREEZE_HOURS,
    CONF_REFREEZE_PAUSE_MS,
    CONF_START_DELAY,
    CONF_THRESHOLD0,
    CONF_THRESHOLD1,
    CONF_THRESHOLD2,
    EVENT_OPERATION,
    FULL_COLLECTION_TICK_SECONDS,
    MIN_RESET_GAP_SECONDS,
)
from .options import Options, normalize_options
from .schedule import parse_hms, refreeze_min_gap, refreeze_slots

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import GcCoordinator
    from .tuner import GcTuner, Operation

_LOGGER = logging.getLogger(__name__)


@dataclass
class DomainData:
    """Process-wide facts that outlive an unload/reload of the entry."""

    started_at: float | None = None  # hass.loop.time() when Home Assistant started (or the first load on a running one)
    original_thresholds: tuple[int, int, int] | None = None  # what gc.get_threshold() was before this integration touched it
    runtime: GcRuntime | None = None


class GcRuntime:
    """Makes the collector match the entry's options, once started and again on every options change."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        tuner: GcTuner,
        coordinator: GcCoordinator,
        domain_data: DomainData,
    ) -> None:
        """Hold the pieces; start() begins the work."""
        self.hass = hass
        self.entry = entry
        self.tuner = tuner
        self.coordinator = coordinator
        self.domain_data = domain_data
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._schedule_cancels: list[CALLBACK_TYPE] = []
        tuner.on_operation = self._on_operation

    # ---- options ----------------------------------------------------------------------------
    @property
    def options(self) -> Options:
        """The entry's options, completed with the proven defaults and bounded."""
        return normalize_options(self.entry.options)

    @property
    def original_thresholds(self) -> tuple[int, int, int]:
        """Python's own thresholds as found at the first load."""
        assert self.domain_data.original_thresholds is not None
        return self.domain_data.original_thresholds

    def _configure_tuner(self, opts: Options) -> None:
        self.tuner.configure(
            freeze=bool(opts[CONF_FREEZE]),
            thresholds=(int(opts[CONF_THRESHOLD0]), int(opts[CONF_THRESHOLD1]), int(opts[CONF_THRESHOLD2])),
            log_min_ms=float(opts[CONF_LOG_MIN_MS]),
        )

    # ---- lifecycle ----------------------------------------------------------------------------
    def start(self) -> CALLBACK_TYPE:
        """Wait for Home Assistant to have started, then follow the options. Returns the cancel callable."""
        self._configure_tuner(self.options)
        return async_at_started(self.hass, self._on_started)

    @callback
    def _on_started(self, hass: HomeAssistant) -> None:
        if self.domain_data.started_at is None:
            self.domain_data.started_at = hass.loop.time()
            _LOGGER.info("Home Assistant started; gc tuning applies in %d s when enabled", self.options[CONF_START_DELAY])
        self.reconcile()

    @callback
    def stop(self) -> None:
        """Entry unload: cancel the timer, give the process its own settings back, unhook the observer."""
        self._cancel()
        self._cancel_schedules()
        if self.tuner.applied is not None or self.tuner.frozen_by_us:
            self.tuner.restore(self.original_thresholds)
        self.tuner.on_operation = None
        self.tuner.remove_callback()

    def _cancel(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def _cancel_schedules(self) -> None:
        for cancel in self._schedule_cancels:
            cancel()
        self._schedule_cancels.clear()

    @property
    def schedules_armed(self) -> int:
        """How many upkeep timers are running (daily, re-freeze slots, the full-collection check)."""
        return len(self._schedule_cancels)

    # ---- the one decision ---------------------------------------------------------------------
    @callback
    def reconcile(self) -> None:
        """Bring the collector in line with the options. Safe to call at any time and as often as needed.

        - disabled: restore (unfreeze what we froze, original thresholds)
        - enabled, never applied: wait out the start-up delay (counted from the start of Home Assistant), then
          apply once: collect, freeze, thresholds
        - enabled, already applied: new thresholds at once; no second heap-wide freeze; the upkeep timers follow
          the options
        """
        self._cancel()
        self._cancel_schedules()
        opts = self.options
        self._configure_tuner(opts)
        tuner = self.tuner
        if not opts[CONF_ENABLED]:
            if tuner.applied is not None or tuner.frozen_by_us:
                tuner.restore(self.original_thresholds)
            self.publish()
            return
        if tuner.applied is not None:
            tuner.retune()
            self._arm_schedules()
            self.publish()
            return
        started_at = self.domain_data.started_at
        if started_at is None:
            return  # _on_started calls reconcile() again
        remaining = started_at + int(opts[CONF_START_DELAY]) - self.hass.loop.time()
        if remaining <= 0:
            self._apply()
        else:
            self._cancel_timer = async_call_later(self.hass, remaining, self._timer_fired)

    @callback
    def _timer_fired(self, _now: Any) -> None:
        self._cancel_timer = None
        self._apply()

    def _apply(self) -> None:
        try:
            self.tuner.apply(ACTION_STARTUP_APPLY)
        except Exception:  # noqa: BLE001 - a failed tune must never take the entry down
            _LOGGER.exception("gc tuning apply failed")
            return
        self._arm_schedules()
        self.publish()

    @callback
    def apply_now(self) -> dict[str, Any]:
        """The apply service: the same apply, on command, whatever the start-up delay."""
        self._cancel()
        result = self.tuner.apply(ACTION_APPLY_SERVICE)
        self._arm_schedules()
        self.publish()
        return result

    # ---- the upkeep schedules (1.1.0) ------------------------------------------------------------
    def upkeep_allowed(self) -> bool:
        """The upkeep acts only while the tuning is enabled, applied, and the heap is frozen by this integration."""
        return bool(self.options[CONF_ENABLED]) and self.tuner.applied is not None and self.tuner.frozen_by_us

    @callback
    def _arm_schedules(self) -> None:
        """(Re)create the timers for the current options; none while the upkeep is not allowed."""
        self._cancel_schedules()
        if not self.upkeep_allowed():
            return
        opts = self.options
        base = parse_hms(opts[CONF_DAILY_TIME])
        maintenance = bool(opts[CONF_DAILY_MAINTENANCE])
        hass = self.hass
        if maintenance:
            self._schedule_cancels.append(
                async_track_time_change(hass, self._daily_fired, hour=base[0], minute=base[1], second=base[2])
            )
        slots = refreeze_slots(base, int(opts[CONF_REFREEZE_HOURS]), maintenance)
        for hour, minute, second in slots:
            self._schedule_cancels.append(async_track_time_change(hass, self._slot_fired, hour=hour, minute=minute, second=second))
        if int(opts[CONF_FULL_MINUTES]) > 0:
            self._schedule_cancels.append(
                async_track_time_interval(hass, self._tick_fired, timedelta(seconds=FULL_COLLECTION_TICK_SECONDS))
            )
        _LOGGER.info(
            "gc upkeep armed: daily maintenance %s, re-freeze slots %s, full collection at least every %s min",
            f"at {opts[CONF_DAILY_TIME]}" if maintenance else "off",
            ", ".join(f"{h:02d}:{m:02d}" for h, m, _ in slots) or "none",
            opts[CONF_FULL_MINUTES] or "off",
        )

    @callback
    def _daily_fired(self, _now: datetime) -> None:
        self.run_daily_maintenance()

    @callback
    def _slot_fired(self, _now: datetime) -> None:
        self.run_refreeze_slot()

    @callback
    def _tick_fired(self, _now: datetime) -> None:
        self.run_full_collection_check()

    @callback
    def run_daily_maintenance(self) -> Operation | None:
        """unfreeze + collect + freeze, unless an apply or another maintenance ran within the last hour."""
        tuner = self.tuner
        if not self.upkeep_allowed():
            return None
        reset = tuner.last_reset_at
        if reset is not None and tuner_module.perf_counter() - reset < MIN_RESET_GAP_SECONDS:
            _LOGGER.info("gc daily maintenance skipped: the heap was reset %.0f s ago", tuner_module.perf_counter() - reset)
            return None
        tuner.maintain(ACTION_DAILY_MAINTENANCE)
        return tuner.last_operation

    @callback
    def run_refreeze_slot(self) -> Operation | None:
        """One re-freeze slot: collect + freeze, once the last freeze is old enough and (if set) a full collection was long.

        With the daily maintenance off, the slot does the maintenance itself (unfreeze + collect + freeze) so that objects
        frozen and dead since do not pile up, and the pause target does not apply.
        """
        tuner = self.tuner
        if not self.upkeep_allowed():
            return None
        opts = self.options
        hours = int(opts[CONF_REFREEZE_HOURS])
        since_freeze = None if tuner.last_freeze_at is None else tuner_module.perf_counter() - tuner.last_freeze_at
        min_gap = refreeze_min_gap(hours).total_seconds()
        if since_freeze is not None and since_freeze < min_gap:
            _LOGGER.info("gc re-freeze skipped: the last freeze was %.0f s ago (minimum %.0f s)", since_freeze, min_gap)
            return None
        if not opts[CONF_DAILY_MAINTENANCE]:
            tuner.maintain(ACTION_PERIODIC_MAINTENANCE)
            return tuner.last_operation
        target_ms = int(opts[CONF_REFREEZE_PAUSE_MS])
        peak_ms = tuner.peak_auto_full * 1000.0
        if target_ms and peak_ms < target_ms:
            _LOGGER.info(
                "gc re-freeze skipped: the longest automatic full collection since the last freeze was %.0f ms, target %d ms",
                peak_ms,
                target_ms,
            )
            return None
        tuner.freeze(ACTION_PERIODIC_REFREEZE)
        return tuner.last_operation

    @callback
    def run_full_collection_check(self) -> Operation | None:
        """A full collection when none ran for `full_collection_minutes` (small and often beats large and rare)."""
        tuner = self.tuner
        if not self.upkeep_allowed():
            return None
        minutes = int(self.options[CONF_FULL_MINUTES])
        if minutes <= 0:
            return None
        last = tuner.last_full_end
        if last is not None and tuner_module.perf_counter() - last < minutes * 60:
            return None
        tuner.collect(ACTION_PERIODIC_COLLECTION, log=False)
        return tuner.last_operation

    @callback
    def _on_operation(self, op: Operation) -> None:
        """After every operation: the sensors first, then the event, so an automation reads the new values."""
        self.publish()
        self.hass.bus.async_fire(EVENT_OPERATION, op.as_dict())

    @callback
    def publish(self) -> None:
        """Let the sensors show the new state now instead of at the next poll."""
        self.coordinator.publish_now()
