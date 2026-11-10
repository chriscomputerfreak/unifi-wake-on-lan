"""Data coordinator for UniFi Wake-on-LAN."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CertificateMismatch,
    GatewaySSH,
    HostKeyMismatch,
    InvalidAuth,
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
from .trust import async_create_trust_issue

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
        # Last successful wake per MAC (for diagnostics)
        self.last_wakes: dict[str, dict[str, Any]] = {}

    async def _async_update_data(self) -> UnifiWolData:
        try:
            networks = await self.api.networks()
            clients = await self.api.clients()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="controller_auth_failed"
            ) from err
        except CertificateMismatch as err:
            async_create_trust_issue(self.hass, self.config_entry)
            raise UpdateFailed(f"UniFi controller certificate changed: {err}") from err
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
            result = await self.ssh.async_wake(
                mac,
                subnet=network.subnet if network else None,
                client_ip=ip,
                interface=interface,
            )
        except HostKeyMismatch as err:
            async_create_trust_issue(self.hass, self.config_entry)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="host_key_mismatch",
            ) from err
        except InvalidAuth as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ssh_auth_failed",
            ) from err
        except UnifiWolError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_failed",
                translation_placeholders={"mac": mac, "error": str(err)},
            ) from err
        self.last_wakes[mac] = {
            "time": dt_util.utcnow().isoformat(),
            "interface": result.interface,
            "method": result.method,
        }
        return result
