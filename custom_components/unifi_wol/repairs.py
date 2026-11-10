"""Repair flow: re-trust a changed SSH host key / TLS certificate."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .api import CannotConnect
from .trust import TrustProposal, async_apply_trust, async_prepare_trust


class FingerprintChangedRepairFlow(RepairsFlow):
    """Show the new fingerprints and store them after confirmation."""

    def __init__(self, entry_id: str) -> None:
        """Initialize."""
        self._entry_id = entry_id
        self._proposal: TrustProposal | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start the flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm the new fingerprints."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_removed")
        if user_input is not None and self._proposal is not None:
            async_apply_trust(self.hass, entry, self._proposal)
            return self.async_create_entry(data={})
        try:
            self._proposal = await async_prepare_trust(entry)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect_gateway")
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "title": entry.title,
                **self._proposal.placeholders,
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for a repair issue."""
    assert data is not None
    return FingerprintChangedRepairFlow(str(data["entry_id"]))
