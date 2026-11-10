"""Online (connectivity) sensors for selected UniFi clients."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_TARGETS
from .coordinator import UnifiWolConfigEntry, UnifiWolCoordinator
from .entity import UnifiWolEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UnifiWolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one online sensor per selected client."""
    coordinator = entry.runtime_data
    targets: dict[str, dict[str, Any]] = entry.options.get(CONF_TARGETS, {})
    async_add_entities(
        UnifiWolOnlineSensor(coordinator, mac, info) for mac, info in targets.items()
    )


class UnifiWolOnlineSensor(UnifiWolEntity, BinarySensorEntity):
    """Whether the client is currently connected according to the controller."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self, coordinator: UnifiWolCoordinator, mac: str, info: dict[str, Any]
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, mac, info, "online")

    @property
    def available(self) -> bool:
        """Only known while the controller data is current."""
        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool:
        """Return True if the client is connected."""
        return self.coordinator.is_online(self._mac)
