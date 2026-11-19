"""Sensors: what the collector is doing, read from counters the gc callback recorded."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TITLE
from .coordinator import GcCoordinator, GcSnapshot

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import GcTuningConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class GcSensorDescription(SensorEntityDescription):
    """A sensor and the function that reads its value from the snapshot."""

    value_fn: Callable[[GcSnapshot], Any]
    attrs_fn: Callable[[GcSnapshot], dict[str, Any]] | None = None


SENSORS: tuple[GcSensorDescription, ...] = (
    GcSensorDescription(
        key="full_collections_last_hour",
        translation_key="full_collections_last_hour",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.full_collections_last_hour,
    ),
    GcSensorDescription(
        key="longest_pause_last_hour",
        translation_key="longest_pause_last_hour",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.longest_pause_last_hour,
    ),
    GcSensorDescription(
        key="last_full_collection_duration",
        translation_key="last_full_collection_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.last_full_collection_seconds,
    ),
    GcSensorDescription(
        key="frozen_objects",
        translation_key="frozen_objects",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.frozen_objects,
    ),
    GcSensorDescription(
        key="thresholds",
        translation_key="thresholds",
        value_fn=lambda s: " / ".join(str(t) for t in s.thresholds),
        attrs_fn=lambda s: {"generation_0": s.thresholds[0], "generation_1": s.thresholds[1], "generation_2": s.thresholds[2]},
    ),
    GcSensorDescription(
        key="applied_at",
        translation_key="applied_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.applied_at,
    ),
    GcSensorDescription(
        key="process_memory",
        translation_key="process_memory",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.process_memory_mb,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GcTuningConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One sensor per description, all on one service device."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(GcSensor(coordinator, entry.entry_id, description) for description in SENSORS)


class GcSensor(CoordinatorEntity[GcCoordinator], SensorEntity):
    """Reads one value from the coordinator's snapshot."""

    entity_description: GcSensorDescription
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GcCoordinator, entry_id: str, description: GcSensorDescription) -> None:
        """Link to the shared device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=TITLE,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="cchip201",
            model="Python garbage collector",
        )

    @property
    def native_value(self) -> Any:
        """The value for this sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Only the thresholds sensor has attributes."""
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None
