"""Diagnostics download: options, state of the collector, the stats the service returns, and the sensor values."""

from __future__ import annotations

from dataclasses import asdict
import platform
import sys
from typing import TYPE_CHECKING, Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .options import normalize_options

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Nothing here is a secret: the integration holds no credentials. Reads counters only (no heap walk)."""
    runtime = entry.runtime_data
    tuner = runtime.tuner
    snapshot = asdict(runtime.coordinator.data) if runtime.coordinator.data is not None else None
    if snapshot is not None:
        snapshot["applied_at"] = snapshot["applied_at"].isoformat() if snapshot["applied_at"] else None
        snapshot["thresholds"] = list(snapshot["thresholds"])
        snapshot["last_operation"] = tuner.last_operation.as_dict() if tuner.last_operation else None
        snapshot["last_maintenance_at"] = snapshot["last_maintenance_at"].isoformat() if snapshot["last_maintenance_at"] else None
    return {
        "entry": {
            "title": entry.title,
            "source": entry.source,
            "version": entry.version,
            "options": dict(entry.options),
            "options_effective": normalize_options(entry.options),
        },
        "runtime": {
            "home_assistant": HA_VERSION,
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "started_at_loop_time": runtime.domain_data.started_at,
            "loop_time_now": hass.loop.time(),
            "apply_pending": runtime._cancel_timer is not None,  # noqa: SLF001 - diagnostics read the timer state
            "frozen_by_this_integration": tuner.frozen_by_us,
            "upkeep_timers_armed": runtime.schedules_armed,
            "upkeep_allowed": runtime.upkeep_allowed(),
            "longest_automatic_full_collection_since_freeze_s": round(tuner.peak_auto_full, 3),
            "original_thresholds": list(runtime.original_thresholds),
        },
        "stats": tuner.stats(recent=50),
        "last_hour": tuner.last_hour(),
        "sensors": snapshot,
    }
