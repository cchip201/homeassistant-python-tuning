"""One cheap poll a minute: read the counters the gc callback recorded, plus the process RSS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL_SECONDS
from . import tuner as tuner_module

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .tuner import GcTuner, Operation

_LOGGER = logging.getLogger(__name__)

_STATM = "/proc/self/statm"


def read_process_memory_mb() -> float | None:
    """Resident set size of this process in MB (Linux /proc; None where it cannot be read). Runs in the executor."""
    try:
        with open(_STATM, encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class GcSnapshot:
    """What the sensors show; equal snapshots do not rewrite entity states."""

    full_collections_last_hour: int
    longest_pause_last_hour: float
    last_full_collection_seconds: float | None
    frozen_objects: int
    thresholds: tuple[int, int, int]
    applied_at: datetime | None
    process_memory_mb: float | None
    last_operation: Operation | None
    last_maintenance_at: datetime | None
    longest_auto_full_since_freeze: float


class GcCoordinator(DataUpdateCoordinator[GcSnapshot]):
    """Builds a GcSnapshot from counters; the only blocking read (RSS) goes through the executor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tuner: GcTuner) -> None:
        """Set the 60 s poll."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            always_update=False,
        )
        self.tuner = tuner

    async def _async_update_data(self) -> GcSnapshot:
        """Read counters on the loop (no heap walk), RSS in the executor."""
        rss = await self.hass.async_add_executor_job(read_process_memory_mb)
        return self.snapshot(rss)

    @callback
    def publish_now(self) -> None:
        """Push the current counters to the sensors at once (after apply, restore or an options change).

        Counters only: the last RSS reading is reused, no executor job, no heap walk.
        """
        rss = self.data.process_memory_mb if self.data is not None else None
        self.async_set_updated_data(self.snapshot(rss))

    def snapshot(self, rss: float | None) -> GcSnapshot:
        """Counter reads only."""
        tuner = self.tuner
        hour = tuner.last_hour()
        t0, t1, t2 = tuner_module.gc.get_threshold()
        return GcSnapshot(
            full_collections_last_hour=hour["full_collections"],
            longest_pause_last_hour=round(hour["longest_pause_seconds"], 3),
            last_full_collection_seconds=None if tuner.last_full_seconds is None else round(tuner.last_full_seconds, 3),
            frozen_objects=tuner.frozen_count(),
            thresholds=(t0, t1, t2),
            applied_at=tuner.applied_at,
            process_memory_mb=rss,
            last_operation=tuner.last_operation,
            last_maintenance_at=tuner.last_maintenance_at,
            longest_auto_full_since_freeze=round(tuner.peak_auto_full, 3),
        )
