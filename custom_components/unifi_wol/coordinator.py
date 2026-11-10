"""Data coordinator for UniFi Wake-on-LAN."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    GatewaySSH,
    HostKeyMismatch,
    UnifiApi,
    UnifiClient,
    UnifiNetwork,
    UnifiWolError,
    WakeResult,
    normalize_mac,
)
from .const import (
    CONF_TARGETS,
    DOMAIN,
    TARGET_IP,
    TARGET_NETWORK_ID,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type UnifiWolConfigEntry = ConfigEntry[UnifiWolCoordinator]


@dataclass(slots=True)
class UnifiWolData:
    """Snapshot of the UniFi controller state."""

    clients: dict[str, UnifiClient]
    networks: dict[str, UnifiNetwork]


class UnifiWolCoordinator(DataUpdateCoordinator[UnifiWolData]):
    """Polls clients/networks and sends magic packets."""

    config_entry: UnifiWolConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: UnifiWolConfigEntry,
        api: UnifiApi,
        ssh: GatewaySSH,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.ssh = ssh

    async def _async_update_data(self) -> UnifiWolData:
        try:
            networks = await self.api.networks()
            clients = await self.api.clients()
        except UnifiWolError as err:
            raise UpdateFailed(f"UniFi controller: {err}") from err
        return UnifiWolData(clients=clients, networks=networks)

    def client_context(
        self, mac: str
    ) -> tuple[UnifiClient | None, UnifiNetwork | None, str | None]:
        """Return client, its network and last IP (live data, then stored)."""
        stored = self.config_entry.options.get(CONF_TARGETS, {}).get(mac, {})
        client = self.data.clients.get(mac) if self.data else None
        network_id = (client.network_id if client else None) or stored.get(
            TARGET_NETWORK_ID
        )
        ip = (client.ip if client else None) or stored.get(TARGET_IP)
        network = (
            self.data.networks.get(network_id) if self.data and network_id else None
        )
        return client, network, ip

    async def async_wake(self, mac: str, interface: str | None = None) -> WakeResult:
        """Wake a client, raising HomeAssistantError on failure."""
        mac = normalize_mac(mac)
        _, network, ip = self.client_context(mac)
        try:
            return await self.ssh.async_wake(
                mac,
                subnet=network.subnet if network else None,
                client_ip=ip,
                interface=interface,
            )
        except HostKeyMismatch as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="host_key_mismatch",
            ) from err
        except UnifiWolError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_failed",
                translation_placeholders={"mac": mac, "error": str(err)},
            ) from err
