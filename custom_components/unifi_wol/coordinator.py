"""Data coordinator for UniFi Wake-on-LAN."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
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

# Extra controller polls after a wake so the online sensor follows quickly
FOLLOWUP_REFRESH_DELAYS = (30, 60, 120)
# Poll interval while an action waits for the client to come online
WAIT_POLL_INTERVAL = 10.0


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
        self._followup_unsubs: list[CALLBACK_TYPE] = []

    async def async_shutdown(self) -> None:
        """Cancel pending follow-up refreshes."""
        self._cancel_followups()
        await super().async_shutdown()

    @callback
    def _cancel_followups(self) -> None:
        for unsub in self._followup_unsubs:
            unsub()
        self._followup_unsubs.clear()

    @callback
    def _schedule_followups(self) -> None:
        """Poll the controller a few times shortly after a wake."""
        self._cancel_followups()

        def _make(delay: int) -> None:
            async def _refresh(_now: datetime) -> None:
                self._followup_unsubs = [
                    u for u in self._followup_unsubs if u is not unsub
                ]
                await self.async_refresh()

            unsub = async_call_later(self.hass, delay, _refresh)
            self._followup_unsubs.append(unsub)

        for delay in FOLLOWUP_REFRESH_DELAYS:
            _make(delay)

    def is_online(self, mac: str) -> bool:
        """Whether the controller currently lists the client as connected."""
        client = self.data.clients.get(mac) if self.data else None
        return bool(client and client.online)

    async def async_wait_online(self, mac: str, timeout: float) -> bool:
        """Poll the controller until the client is online or the timeout passes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            await asyncio.sleep(max(0.0, min(WAIT_POLL_INTERVAL, remaining)))
            await self.async_refresh()
            if self.is_online(mac):
                return True
            if loop.time() >= deadline:
                return False

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
        self._schedule_followups()
        return result
