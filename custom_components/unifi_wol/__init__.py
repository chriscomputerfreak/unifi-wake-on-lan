"""UniFi Wake-on-LAN: wake clients through the UniFi gateway on their own VLAN."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    CannotConnect,
    GatewaySSH,
    UnifiApi,
    async_fetch_tls_fingerprint,
    normalize_mac,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_INTERFACE,
    ATTR_MAC,
    ATTR_TIMEOUT,
    ATTR_WAIT,
    CONF_SITE,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_TLS_FINGERPRINT,
    DEFAULT_WAIT_TIMEOUT,
    DOMAIN,
    SERVICE_WAKE,
)
from .coordinator import UnifiWolConfigEntry, UnifiWolCoordinator
from .entity import async_cleanup_registries
from .trust import async_delete_trust_issue

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

WAKE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MAC): cv.string,
        vol.Optional(ATTR_INTERFACE): vol.All(
            cv.string, vol.Match(r"^[A-Za-z0-9_.:-]{1,32}$")
        ),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_WAIT, default=False): cv.boolean,
        vol.Optional(ATTR_TIMEOUT, default=DEFAULT_WAIT_TIMEOUT): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=600)
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the wake service."""

    async def _async_handle_wake(call: ServiceCall) -> ServiceResponse:
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
            entry = next((e for e in loaded if e.entry_id == entry_id), None)
            if entry is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="entry_not_loaded"
                )
        elif len(loaded) == 1:
            entry = loaded[0]
        elif not loaded:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_entries"
            )
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="multiple_entries"
            )

        try:
            mac = normalize_mac(call.data[ATTR_MAC])
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_mac",
                translation_placeholders={"mac": call.data[ATTR_MAC]},
            ) from err

        coordinator: UnifiWolCoordinator = entry.runtime_data
        result = await coordinator.async_wake(mac, call.data.get(ATTR_INTERFACE))
        response: dict[str, Any] = {
            "mac": mac,
            "interface": result.interface,
            "method": result.method,
        }
        if call.data[ATTR_WAIT]:
            response["online"] = await coordinator.async_wait_online(
                mac, call.data[ATTR_TIMEOUT]
            )
        return response

    hass.services.async_register(
        DOMAIN,
        SERVICE_WAKE,
        _async_handle_wake,
        schema=WAKE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> bool:
    """Set up UniFi Wake-on-LAN from a config entry."""
    if not entry.data[CONF_VERIFY_SSL] and not entry.data.get(CONF_TLS_FINGERPRINT):
        # Entries created before 0.2.0: pin the current certificate now.
        try:
            fingerprint = await async_fetch_tls_fingerprint(entry.data[CONF_HOST])
        except CannotConnect as err:
            _LOGGER.debug("Could not pin the controller certificate yet: %s", err)
        else:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_TLS_FINGERPRINT: fingerprint}
            )
            _LOGGER.info("Pinned the UniFi controller TLS certificate")

    data = entry.data
    session = async_create_clientsession(
        hass,
        verify_ssl=data[CONF_VERIFY_SSL],
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    api = UnifiApi(
        session,
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_SITE],
        data.get(CONF_TLS_FINGERPRINT),
    )
    ssh = GatewaySSH(
        data[CONF_HOST],
        data[CONF_SSH_PORT],
        data[CONF_SSH_USERNAME],
        data[CONF_SSH_PASSWORD],
        data.get(CONF_SSH_HOST_KEY),
    )
    coordinator = UnifiWolCoordinator(hass, entry, api, ssh)

    # Waking only needs SSH + the stored targets, so a temporarily unreachable
    # controller API must not block the setup.
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        _LOGGER.warning(
            "UniFi controller not reachable, continuing with stored client data"
        )

    entry.runtime_data = coordinator
    async_cleanup_registries(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: UnifiWolConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> None:
    """Clean up when an entry is deleted."""
    async_delete_trust_issue(hass, entry)
