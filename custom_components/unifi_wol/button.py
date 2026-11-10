"""Wake buttons for selected UniFi clients."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import WakeResult
from .const import CONF_TARGETS
from .coordinator import UnifiWolConfigEntry, UnifiWolCoordinator
from .entity import UnifiWolEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UnifiWolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one wake button per selected client."""
    coordinator = entry.runtime_data
    targets: dict[str, dict[str, Any]] = entry.options.get(CONF_TARGETS, {})
    async_add_entities(
        UnifiWolButton(coordinator, mac, info) for mac, info in targets.items()
    )


class UnifiWolButton(UnifiWolEntity, ButtonEntity):
    """Sends a magic packet to one client."""

    def __init__(
        self, coordinator: UnifiWolCoordinator, mac: str, info: dict[str, Any]
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, mac, info, "wake")
        self._last_result: WakeResult | None = None

    @property
    def available(self) -> bool:
        """Waking works via SSH even if the controller API is down."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose what the button will use."""
        client, network, ip = self.coordinator.client_context(self._mac)
        attrs: dict[str, Any] = {
            "mac": self._mac,
            "last_ip": ip,
            "network": network.name if network else None,
            "vlan": network.vlan if network else None,
            "online": client.online if client else None,
            "wired": client.wired if client else None,
        }
        if self._last_result:
            attrs["last_interface"] = self._last_result.interface
            attrs["last_method"] = self._last_result.method
        return attrs

    async def async_press(self) -> None:
        """Send the magic packet."""
        self._last_result = await self.coordinator.async_wake(self._mac)
        self.async_write_ha_state()
