"""Config, reauth, reconfigure and options flow for UniFi Wake-on-LAN."""

from __future__ import annotations

from collections.abc import Mapping
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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
    CertificateMismatch,
    GatewaySSH,
    HostKeyMismatch,
    InvalidAuth,
    UnifiApi,
    async_fetch_tls_fingerprint,
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
    CONF_TLS_FINGERPRINT,
    DEFAULT_SITE,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USERNAME,
    DOMAIN,
    TARGET_IP,
    TARGET_NAME,
    TARGET_NETWORK_ID,
)
from .coordinator import UnifiWolConfigEntry
from .trust import (
    TrustProposal,
    async_apply_trust,
    async_delete_trust_issue,
    async_prepare_trust,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
PORT_SELECTOR = vol.All(
    NumberSelector(NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)),
    vol.Coerce(int),
)

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
        vol.Required(CONF_SSH_PORT, default=DEFAULT_SSH_PORT): PORT_SELECTOR,
        vol.Required(CONF_SSH_USERNAME, default=DEFAULT_SSH_USERNAME): str,
        vol.Required(CONF_SSH_PASSWORD): PASSWORD_SELECTOR,
    }
)

# Empty password fields keep the stored password.
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Optional(CONF_SSH_PASSWORD): PASSWORD_SELECTOR,
    }
)

RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_SITE): str,
        vol.Required(CONF_VERIFY_SSL): BooleanSelector(),
        vol.Required(CONF_SSH_PORT): PORT_SELECTOR,
        vol.Required(CONF_SSH_USERNAME): str,
        vol.Optional(CONF_SSH_PASSWORD): PASSWORD_SELECTOR,
    }
)

SECRETS = (CONF_PASSWORD, CONF_SSH_PASSWORD)


def _unique_id(data: Mapping[str, Any]) -> str:
    return f"{data[CONF_HOST].lower()}_{data[CONF_SITE]}"


def _without_secrets(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in SECRETS}


async def _async_check_api(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """Log in to the controller; return an error key or None."""
    session = async_create_clientsession(
        hass,
        verify_ssl=data[CONF_VERIFY_SSL],
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        auto_cleanup=False,
    )
    api = UnifiApi(
        session,
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data[CONF_SITE],
        data.get(CONF_TLS_FINGERPRINT),
    )
    try:
        await api.login()
        await api.networks()
    except InvalidAuth:
        return "invalid_auth"
    except CertificateMismatch:
        return "certificate_mismatch"
    except CannotConnect as err:
        _LOGGER.debug("Controller not reachable: %s", err)
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error")
        return "unknown"
    finally:
        session.detach()
    return None


async def _async_check_ssh(data: Mapping[str, Any]) -> str | None:
    """Probe the gateway via SSH; return an error key or None."""
    try:
        tools = await GatewaySSH(
            data[CONF_HOST],
            data[CONF_SSH_PORT],
            data[CONF_SSH_USERNAME],
            data[CONF_SSH_PASSWORD],
            data[CONF_SSH_HOST_KEY],
        ).async_probe()
    except HostKeyMismatch:
        return "host_key_mismatch"
    except InvalidAuth:
        return "invalid_auth_ssh"
    except CannotConnect as err:
        _LOGGER.debug("Gateway SSH not reachable: %s", err)
        return "cannot_connect_ssh"
    except Exception:
        _LOGGER.exception("Unexpected error")
        return "unknown"
    _LOGGER.info("Wake-on-LAN tools found on gateway: %s", tools)
    return None if has_wol_method(tools) else "no_wol_method"


@callback
def _async_migrate_unique_ids(
    hass: HomeAssistant, entry: UnifiWolConfigEntry, old: str, new: str
) -> None:
    """Keep entity ids/customizations when the entry's unique id changes."""
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.unique_id.startswith(f"{old}_"):
            ent_reg.async_update_entity(
                ent.entity_id, new_unique_id=f"{new}_{ent.unique_id[len(old) + 1 :]}"
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
            await self.async_set_unique_id(_unique_id(user_input))
            self._abort_if_unique_id_configured()

            data = dict(user_input)
            if not data[CONF_VERIFY_SSL]:
                # Self-signed certificate: pin it on first use
                try:
                    data[CONF_TLS_FINGERPRINT] = await async_fetch_tls_fingerprint(
                        data[CONF_HOST]
                    )
                except CannotConnect as err:
                    _LOGGER.debug("Controller not reachable: %s", err)
                    errors["base"] = "cannot_connect"
            if not errors and (error := await _async_check_api(self.hass, data)):
                errors["base"] = error
            if not errors:
                self._api_data = data
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
            data = {**self._api_data, **user_input}
            data[CONF_SSH_PORT] = int(user_input[CONF_SSH_PORT])
            try:
                data[CONF_SSH_HOST_KEY] = await GatewaySSH.async_fetch_host_key(
                    host, data[CONF_SSH_PORT]
                )
            except CannotConnect as err:
                _LOGGER.debug("Gateway SSH not reachable: %s", err)
                errors["base"] = "cannot_connect_ssh"
            if not errors and (error := await _async_check_ssh(data)):
                errors["base"] = error
            if not errors:
                return self.async_create_entry(title=f"UniFi WoL ({host})", data=data)

        return self.async_show_form(
            step_id="ssh",
            data_schema=self.add_suggested_values_to_schema(SSH_SCHEMA, user_input),
            errors=errors,
            description_placeholders={"host": host},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new passwords."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = {k: v for k, v in user_input.items() if k in SECRETS and v}
            data = {**entry.data, **updates}
            if error := await _async_check_api(self.hass, data):
                errors["base"] = error
            elif error := await _async_check_ssh(data):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "host": entry.data[CONF_HOST],
                "username": entry.data[CONF_USERNAME],
                "ssh_username": entry.data[CONF_SSH_USERNAME],
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host, credentials or SSH settings without losing the clients."""
        entry = self._get_reconfigure_entry()
        old = entry.data
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **old,
                **{k: v for k, v in user_input.items() if k not in SECRETS or v},
            }
            data[CONF_HOST] = data[CONF_HOST].strip()
            data[CONF_SSH_PORT] = int(data[CONF_SSH_PORT])
            new_uid = _unique_id(data)
            same_host = data[CONF_HOST].lower() == old[CONF_HOST].lower()

            if new_uid != entry.unique_id and any(
                other.unique_id == new_uid
                for other in self._async_current_entries(include_ignore=False)
                if other.entry_id != entry.entry_id
            ):
                errors["base"] = "already_configured"

            # Keep pinned keys for the same gateway; a changed key must be
            # re-trusted explicitly (options / repair), not silently here.
            if not errors:
                if data[CONF_VERIFY_SSL]:
                    data.pop(CONF_TLS_FINGERPRINT, None)
                elif not (same_host and old.get(CONF_TLS_FINGERPRINT)):
                    try:
                        data[CONF_TLS_FINGERPRINT] = await async_fetch_tls_fingerprint(
                            data[CONF_HOST]
                        )
                    except CannotConnect:
                        errors["base"] = "cannot_connect"
            if not errors and not (
                same_host
                and data[CONF_SSH_PORT] == old[CONF_SSH_PORT]
                and old.get(CONF_SSH_HOST_KEY)
            ):
                try:
                    data[CONF_SSH_HOST_KEY] = await GatewaySSH.async_fetch_host_key(
                        data[CONF_HOST], data[CONF_SSH_PORT]
                    )
                except CannotConnect:
                    errors["base"] = "cannot_connect_ssh"

            if not errors and (error := await _async_check_api(self.hass, data)):
                errors["base"] = error
            if not errors and (error := await _async_check_ssh(data)):
                errors["base"] = error

            if not errors:
                kwargs: dict[str, Any] = {"data": data}
                if new_uid != entry.unique_id:
                    _async_migrate_unique_ids(self.hass, entry, entry.unique_id, new_uid)
                    kwargs["unique_id"] = new_uid
                if entry.title == f"UniFi WoL ({old[CONF_HOST]})":
                    kwargs["title"] = f"UniFi WoL ({data[CONF_HOST]})"
                if not same_host:
                    async_delete_trust_issue(self.hass, entry)
                return self.async_update_reload_and_abort(entry, **kwargs)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA,
                _without_secrets(user_input or old),
            ),
            errors=errors,
        )


class UnifiWolOptionsFlow(OptionsFlow):
    """Select the clients that get a wake button; re-trust changed keys."""

    def __init__(self) -> None:
        """Initialize."""
        self._options: dict[str, Any] = {}
        self._proposal: TrustProposal | None = None

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
        # Without controller data, the stored clients can still be managed.
        data = coordinator.data
        current: dict[str, dict[str, Any]] = dict(entry.options.get(CONF_TARGETS, {}))
        errors: dict[str, str] = {}

        if user_input is not None:
            targets: dict[str, dict[str, Any]] = {}
            for mac in user_input.get(CONF_TARGETS, []):
                if data and (client := data.clients.get(mac)):
                    targets[mac] = {
                        TARGET_NAME: client.name,
                        TARGET_NETWORK_ID: client.network_id,
                        TARGET_IP: client.ip,
                    }
                elif mac in current:
                    targets[mac] = current[mac]
            self._options = {**entry.options, CONF_TARGETS: targets}

            if not user_input.get(CONF_RETRUST_HOST_KEY):
                return self.async_create_entry(data=self._options)
            try:
                self._proposal = await async_prepare_trust(entry)
            except CannotConnect as err:
                _LOGGER.debug("Gateway not reachable: %s", err)
                errors["base"] = "cannot_connect_gateway"
            else:
                return await self.async_step_trust()

        if data is None:
            errors.setdefault("base", "controller_unavailable")

        options: list[SelectOptionDict] = []
        clients = data.clients if data else {}
        for client in sorted(
            clients.values(), key=lambda c: (not c.online, c.name.lower())
        ):
            network = data.networks.get(client.network_id or "") if data else None
            net_label = "?"
            if network:
                net_label = network.name + (
                    f" (VLAN {network.vlan})" if network.vlan else ""
                )
            state = "●" if client.online else "○"
            wifi = " 📶" if client.wired is False else ""
            options.append(
                SelectOptionDict(
                    value=client.mac,
                    label=(
                        f"{state} {client.name}{wifi} – {net_label} – "
                        f"{client.ip or '-'} – {client.mac}"
                    ),
                )
            )
        for mac, info in current.items():
            if mac not in clients:
                options.append(
                    SelectOptionDict(
                        value=mac, label=f"? {info.get(TARGET_NAME, mac)} – {mac}"
                    )
                )

        selected = (
            user_input.get(CONF_TARGETS, list(current)) if user_input else list(current)
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_TARGETS, default=selected): SelectSelector(
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

    async def async_step_trust(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show old and new fingerprints and ask for confirmation."""
        assert self._proposal is not None
        if user_input is not None:
            # One update for data + options -> a single reload
            async_apply_trust(
                self.hass, self.config_entry, self._proposal, options=self._options
            )
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="trust",
            data_schema=vol.Schema({}),
            description_placeholders=self._proposal.placeholders,
        )
