"""The running state of the one config entry: start-up delay, apply, live option changes, restore."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_ENABLED,
    CONF_FREEZE,
    CONF_LOG_MIN_MS,
    CONF_START_DELAY,
    CONF_THRESHOLD0,
    CONF_THRESHOLD1,
    CONF_THRESHOLD2,
)
from .options import Options, normalize_options

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import GcCoordinator
    from .tuner import GcTuner

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
        if self.tuner.applied is not None or self.tuner.frozen_by_us:
            self.tuner.restore(self.original_thresholds)
        self.tuner.remove_callback()

    def _cancel(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    # ---- the one decision ---------------------------------------------------------------------
    @callback
    def reconcile(self) -> None:
        """Bring the collector in line with the options. Safe to call at any time and as often as needed.

        - disabled: restore (unfreeze what we froze, original thresholds)
        - enabled, never applied: wait out the start-up delay (counted from the start of Home Assistant), then
          apply once: collect, freeze, thresholds
        - enabled, already applied: new thresholds at once; no second heap-wide freeze
        """
        self._cancel()
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
            self.tuner.apply()
        except Exception:  # noqa: BLE001 - a failed tune must never take the entry down
            _LOGGER.exception("gc tuning apply failed")
            return
        self.publish()

    @callback
    def apply_now(self) -> dict[str, Any]:
        """The apply service: the same apply, on command, whatever the start-up delay."""
        self._cancel()
        result = self.tuner.apply()
        self.publish()
        return result

    @callback
    def publish(self) -> None:
        """Let the sensors show the new state now instead of at the next poll."""
        self.coordinator.publish_now()
