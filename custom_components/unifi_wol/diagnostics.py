"""Diagnostics for UniFi Wake-on-LAN (passwords and MAC/IP details redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import UnifiWolError
from .const import (
    CONF_SSH_HOST_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_TARGETS,
)
from .coordinator import UnifiWolConfigEntry
from .trust import ssh_fingerprint_or_unknown

TO_REDACT = {CONF_PASSWORD, CONF_SSH_PASSWORD, CONF_USERNAME, CONF_SSH_USERNAME}


def _mask_mac(mac: str) -> str:
    """Keep the vendor part (OUI) only."""
    return mac[:8] + ":**:**:**"


def _mask_ip(ip: str | None) -> str | None:
    return ip.rsplit(".", 1)[0] + ".x" if ip else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UnifiWolConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = async_redact_data(dict(entry.data), TO_REDACT)
    if host_key := data.pop(CONF_SSH_HOST_KEY, None):
        data["ssh_host_key_fingerprint"] = ssh_fingerprint_or_unknown(host_key)

    targets: dict[str, Any] = {}
    for index, mac in enumerate(entry.options.get(CONF_TARGETS, {}), start=1):
        client, network, ip = coordinator.client_context(mac)
        last = coordinator.last_wakes.get(mac)
        targets[f"target_{index}"] = {
            "mac": _mask_mac(mac),
            "known_to_controller": client is not None,
            "online": client.online if client else None,
            "wired": client.wired if client else None,
            "ip": _mask_ip(ip),
            "network": (
                {
                    "vlan": network.vlan,
                    "subnet": str(network.subnet) if network.subnet else None,
                }
                if network
                else None
            ),
            "last_wake": last,
        }

    controller: dict[str, Any] = {
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception)
        if coordinator.last_exception
        else None,
    }
    if coordinator.data:
        controller["clients_total"] = len(coordinator.data.clients)
        controller["clients_online"] = sum(
            c.online for c in coordinator.data.clients.values()
        )
        controller["networks"] = [
            {"vlan": n.vlan, "subnet": str(n.subnet) if n.subnet else None}
            for n in coordinator.data.networks.values()
        ]

    try:
        gateway: dict[str, Any] = await coordinator.ssh.async_diagnostics()
    except UnifiWolError as err:
        gateway = {"error": f"{type(err).__name__}: {err}"}

    return {
        "entry": {
            "title": entry.title,
            "data": data,
            "options": {
                k: v for k, v in entry.options.items() if k != CONF_TARGETS
            },
        },
        "controller": controller,
        "targets": targets,
        "gateway": gateway,
    }
