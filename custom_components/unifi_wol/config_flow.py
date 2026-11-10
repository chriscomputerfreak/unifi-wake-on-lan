"""Config and options flow for UniFi Wake-on-LAN."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    CannotConnect,
    GatewaySSH,
    InvalidAuth,
    UnifiApi,
    has_wol_method,
)
from .const import (
    CONF_RETRUST_HOST_KEY,
    CONF_SITE,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_TARGETS,
    DEFAULT_SITE,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USERNAME,
    DOMAIN,
    TARGET_IP,
    TARGET_NAME,
    TARGET_NETWORK_ID,
)
from .coordinator import UnifiWolConfigEntry

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_SITE, default=DEFAULT_SITE): str,
        vol.Required(CONF_VERIFY_SSL, default=False): BooleanSelector(),
    }
)

SSH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SSH_PORT, default=DEFAULT_SSH_PORT): vol.All(
            NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Coerce(int),
        ),
        vol.Required(CONF_SSH_USERNAME, default=DEFAULT_SSH_USERNAME): str,
        vol.Required(CONF_SSH_PASSWORD): PASSWORD_SELECTOR,
    }
)


class UnifiWolConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two step setup: controller API, then gateway SSH."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._api_data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: UnifiWolConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return UnifiWolOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Controller credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST].lower()}_{user_input[CONF_SITE]}"
            )
            self._abort_if_unique_id_configured()

            api = UnifiApi(
                async_create_clientsession(
                    self.hass,
                    verify_ssl=user_input[CONF_VERIFY_SSL],
                    cookie_jar=aiohttp.CookieJar(unsafe=True),
                ),
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_SITE],
            )
            try:
                await api.login()
                await api.networks()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect as err:
                _LOGGER.debug("Controller not reachable: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                self._api_data = user_input
                return await self.async_step_ssh()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_ssh(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gateway SSH credentials."""
        errors: dict[str, str] = {}
        host = self._api_data[CONF_HOST]
        if user_input is not None:
            port = int(user_input[CONF_SSH_PORT])
            try:
                host_key = await GatewaySSH.async_fetch_host_key(host, port)
                tools = await GatewaySSH(
                    host,
                    port,
                    user_input[CONF_SSH_USERNAME],
                    user_input[CONF_SSH_PASSWORD],
                    host_key,
                ).async_probe()
            except InvalidAuth:
                errors["base"] = "invalid_auth_ssh"
            except CannotConnect as err:
                _LOGGER.debug("Gateway SSH not reachable: %s", err)
                errors["base"] = "cannot_connect_ssh"
            except Exception:
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                _LOGGER.info("Wake-on-LAN tools found on gateway: %s", tools)
                if not has_wol_method(tools):
                    errors["base"] = "no_wol_method"
                else:
                    return self.async_create_entry(
                        title=f"UniFi WoL ({host})",
                        data={
                            **self._api_data,
                            **user_input,
                            CONF_SSH_PORT: port,
                            CONF_SSH_HOST_KEY: host_key,
                        },
                    )

        return self.async_show_form(
            step_id="ssh",
            data_schema=self.add_suggested_values_to_schema(SSH_SCHEMA, user_input),
            errors=errors,
            description_placeholders={"host": host},
        )


class UnifiWolOptionsFlow(OptionsFlow):
    """Select the clients that get a wake button."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Client selection."""
        entry: UnifiWolConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        coordinator = entry.runtime_data
        if coordinator.data is None:
            await coordinator.async_refresh()
        if coordinator.data is None:
            return self.async_abort(reason="no_data")

        data = coordinator.data
        current: dict[str, dict[str, Any]] = dict(entry.options.get(CONF_TARGETS, {}))
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_RETRUST_HOST_KEY):
                try:
                    key = await GatewaySSH.async_fetch_host_key(
                        entry.data[CONF_HOST], entry.data[CONF_SSH_PORT]
                    )
                except CannotConnect:
                    errors["base"] = "cannot_connect_ssh"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_SSH_HOST_KEY: key}
                    )
            if not errors:
                targets: dict[str, dict[str, Any]] = {}
                for mac in user_input.get(CONF_TARGETS, []):
                    if client := data.clients.get(mac):
                        targets[mac] = {
                            TARGET_NAME: client.name,
                            TARGET_NETWORK_ID: client.network_id,
                            TARGET_IP: client.ip,
                        }
                    elif mac in current:
                        targets[mac] = current[mac]
                return self.async_create_entry(
                    data={**entry.options, CONF_TARGETS: targets}
                )

        options: list[SelectOptionDict] = []
        for client in sorted(
            data.clients.values(), key=lambda c: (not c.online, c.name.lower())
        ):
            network = data.networks.get(client.network_id or "")
            net_label = "?"
            if network:
                net_label = network.name + (
                    f" (VLAN {network.vlan})" if network.vlan else ""
                )
            state = "●" if client.online else "○"
            options.append(
                SelectOptionDict(
                    value=client.mac,
                    label=(
                        f"{state} {client.name} – {net_label} – "
                        f"{client.ip or '-'} – {client.mac}"
                    ),
                )
            )
        for mac, info in current.items():
            if mac not in data.clients:
                options.append(
                    SelectOptionDict(
                        value=mac, label=f"? {info.get(TARGET_NAME, mac)} – {mac}"
                    )
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_TARGETS, default=list(current)): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                        sort=False,
                    )
                ),
                vol.Optional(CONF_RETRUST_HOST_KEY, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
