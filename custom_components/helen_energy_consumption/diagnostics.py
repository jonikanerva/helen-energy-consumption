"""Diagnostics support for the Helen Energy Consumption integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import CONF_DELIVERY_SITE_ID, ROLLING_WINDOW_HOURS, SCAN_INTERVAL

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import HelenConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DELIVERY_SITE_ID,
    # Derived carriers of the §8-sensitive fields: the entry title is the
    # delivery-site street address; the unique_id embeds username + site id.
    "title",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: HelenConfigEntry,
) -> dict[str, Any]:  # reason: HA's canonical diagnostics signature
    """Return redacted diagnostics for a config entry.

    Read-only and in-memory: no Helen call, no recorder read, and the
    statistic_id is deliberately absent — it embeds the delivery_site_id,
    which STACK.md §8 names sensitive.
    """
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "entry_state": str(entry.state),
        "scan_interval_hours": SCAN_INTERVAL.total_seconds() / 3600,
        "rolling_window_hours": ROLLING_WINDOW_HOURS,
    }
