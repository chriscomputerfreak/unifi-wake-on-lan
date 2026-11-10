"""Shared entity base and registry cleanup for UniFi Wake-on-LAN."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TARGETS, TARGET_NAME
from .coordinator import UnifiWolConfigEntry, UnifiWolCoordinator

# Entity kinds created per selected client (suffix of the unique id)
KINDS = ("wake", "online")


def entity_unique_id(entry: UnifiWolConfigEntry, mac: str, kind: str) -> str:
    """Unique id of a per-client entity."""
    return f"{entry.unique_id}_{mac}_{kind}"


@callback
def async_cleanup_registries(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> None:
    """Remove entities/devices of clients that were deselected."""
    macs = set(entry.options.get(CONF_TARGETS, {}))
    wanted = {entity_unique_id(entry, mac, kind) for mac in macs for kind in KINDS}
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.unique_id not in wanted:
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


class UnifiWolEntity(CoordinatorEntity[UnifiWolCoordinator]):
    """Entity that belongs to one selected client."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UnifiWolCoordinator,
        mac: str,
        info: dict[str, Any],
        kind: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._mac = mac
        self._attr_translation_key = kind
        self._attr_unique_id = entity_unique_id(coordinator.config_entry, mac, kind)
        # Linking via MAC merges with existing devices of other integrations.
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=info.get(TARGET_NAME) or mac,
        )
