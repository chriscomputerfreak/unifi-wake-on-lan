"""Re-trusting a changed SSH host key / TLS certificate and the related repair issue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import (
    GatewaySSH,
    async_fetch_tls_fingerprint,
    format_tls_fingerprint,
    ssh_fingerprint,
)
from .const import (
    CONF_SSH_HOST_KEY,
    CONF_SSH_PORT,
    CONF_TLS_FINGERPRINT,
    DOMAIN,
    ISSUE_FINGERPRINT_CHANGED,
)

if TYPE_CHECKING:
    from .coordinator import UnifiWolConfigEntry

NONE = "–"


@dataclass(slots=True)
class TrustProposal:
    """Freshly fetched fingerprints, waiting for the user's confirmation."""

    data_updates: dict[str, Any]
    placeholders: dict[str, str]


def ssh_fingerprint_or_unknown(key: str | None) -> str:
    """SHA256 fingerprint of a stored host key, '–' if none, '?' if unreadable."""
    if not key:
        return NONE
    try:
        return ssh_fingerprint(key)
    except Exception:  # noqa: BLE001 - stored key unreadable, show it as unknown
        return "?"


async def async_prepare_trust(entry: UnifiWolConfigEntry) -> TrustProposal:
    """Fetch the gateway's current SSH host key and TLS certificate fingerprint.

    Raises CannotConnect if the gateway is not reachable.
    """
    host = entry.data[CONF_HOST]
    new_key = await GatewaySSH.async_fetch_host_key(host, entry.data[CONF_SSH_PORT])
    updates: dict[str, Any] = {CONF_SSH_HOST_KEY: new_key}
    tls_old = tls_new = NONE
    if not entry.data.get(CONF_VERIFY_SSL):
        new_fp = await async_fetch_tls_fingerprint(host)
        updates[CONF_TLS_FINGERPRINT] = new_fp
        if old_fp := entry.data.get(CONF_TLS_FINGERPRINT):
            tls_old = format_tls_fingerprint(old_fp)
        tls_new = format_tls_fingerprint(new_fp)
    return TrustProposal(
        data_updates=updates,
        placeholders={
            "ssh_old": ssh_fingerprint_or_unknown(entry.data.get(CONF_SSH_HOST_KEY)),
            "ssh_new": ssh_fingerprint_or_unknown(new_key),
            "tls_old": tls_old,
            "tls_new": tls_new,
        },
    )


@callback
def async_apply_trust(
    hass: HomeAssistant,
    entry: UnifiWolConfigEntry,
    proposal: TrustProposal,
    options: dict[str, Any] | None = None,
) -> None:
    """Store the confirmed fingerprints (and options) in one update."""
    kwargs: dict[str, Any] = {"data": {**entry.data, **proposal.data_updates}}
    if options is not None:
        kwargs["options"] = options
    hass.config_entries.async_update_entry(entry, **kwargs)
    async_delete_trust_issue(hass, entry)


def _issue_id(entry: UnifiWolConfigEntry) -> str:
    return f"{ISSUE_FINGERPRINT_CHANGED}_{entry.entry_id}"


@callback
def async_create_trust_issue(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> None:
    """Raise a fixable repair issue for a changed host key / certificate."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_FINGERPRINT_CHANGED,
        translation_placeholders={"title": entry.title},
        data={"entry_id": entry.entry_id},
    )


@callback
def async_delete_trust_issue(hass: HomeAssistant, entry: UnifiWolConfigEntry) -> None:
    """Remove the repair issue of an entry."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
