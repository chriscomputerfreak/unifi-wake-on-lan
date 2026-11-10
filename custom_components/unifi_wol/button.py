"""Wake buttons for selected UniFi clients."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import WakeResult
from .const import CONF_TARGETS, TARGET_NAME
from .coordinator import UnifiWolConfigEntry, UnifiWolCoordinator

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UnifiWolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one wake button per selected client."""
    coordinator = entry.runtime_data
    targets: dict[str, dict[str, Any]] = entry.options.get(CONF_TARGETS, {})
    _async_cleanup_registries(hass, entry, set(targets))
    async_add_entities(
        UnifiWolButton(coordinator, mac, info) for mac, info in targets.items()
    )


def _unique_id(entry: UnifiWolConfigEntry, mac: str) -> str:
    return f"{entry.unique_id}_{mac}_wake"


@callback
def _async_cleanup_registries(
    hass: HomeAssistant, entry: UnifiWolConfigEntry, macs: set[str]
) -> None:
    """Remove buttons/devices of clients that were deselected."""
    wanted = {_unique_id(entry, mac) for mac in macs}
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.domain == "button" and ent.unique_id not in wanted:
            ent_reg.async_remove(ent.entity_id)

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        device_macs = {
            value for kind, value in device.connections if kind == CONNECTION_NETWORK_MAC
        }
        if not device_macs & macs:
            dev_reg.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


class UnifiWolButton(CoordinatorEntity[UnifiWolCoordinator], ButtonEntity):
    """Sends a magic packet to one client."""

    _attr_has_entity_name = True
    _attr_translation_key = "wake"

    def __init__(
        self, coordinator: UnifiWolCoordinator, mac: str, info: dict[str, Any]
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._mac = mac
        self._last_result: WakeResult | None = None
        self._attr_unique_id = _unique_id(coordinator.config_entry, mac)
        # Linking via MAC merges with existing devices of other integrations.
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=info.get(TARGET_NAME) or mac,
        )

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
        }
        if self._last_result:
            attrs["last_interface"] = self._last_result.interface
            attrs["last_method"] = self._last_result.method
        return attrs

    async def async_press(self) -> None:
        """Send the magic packet."""
        self._last_result = await self.coordinator.async_wake(self._mac)
        self.async_write_ha_state()
