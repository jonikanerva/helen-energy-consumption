"""The Helen Energy Consumption integration.

A minimal HACS custom component that imports hourly electricity consumption
from Helen into Home Assistant's statistics database so it can be added to the
Energy Dashboard. No entities, no cost tracking — consumption only.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_START_DATE,
    CONF_DELIVERY_SITE_ID,
    DOMAIN,
    SCAN_INTERVAL,
    SERVICE_BACKFILL,
)
from .coordinator import HelenConsumptionCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def _resolve_target_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """Resolve the backfill target: the given entry, or the sole loaded one."""
    if entry_id is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"{entry_id} is not a Helen Energy Consumption config entry"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"Config entry {entry_id} is not loaded")
        return entry

    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise ServiceValidationError(
            "No loaded Helen Energy Consumption config entry to backfill"
        )
    if len(loaded) > 1:
        raise ServiceValidationError(
            "Multiple delivery sites are configured; specify config_entry_id"
        )
    return loaded[0]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the backfill admin action once for the integration."""

    async def _handle_backfill(call: ServiceCall) -> None:
        """Rebuild a delivery site's chain from the requested start date."""
        start_date: date = call.data[ATTR_START_DATE]
        entry = _resolve_target_entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await entry.runtime_data.async_backfill(start_date)

    async_register_admin_service(
        hass, DOMAIN, SERVICE_BACKFILL, _handle_backfill, schema=_BACKFILL_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helen Energy Consumption from a config entry."""
    coordinator = HelenConsumptionCoordinator(
        hass,
        entry,
        credentials={
            "username": entry.data[CONF_USERNAME],
            "password": entry.data[CONF_PASSWORD],
        },
        delivery_site_id=entry.data[CONF_DELIVERY_SITE_ID],
    )

    # Run one import immediately so the Energy Dashboard has data right away.
    # raise_on_error=True makes a transient failure (API down) surface as
    # ConfigEntryNotReady so HA retries with backoff; bad credentials surface as
    # ConfigEntryAuthFailed so reauth starts.
    try:
        await coordinator.async_update(raise_on_error=True)
    except ConfigEntryAuthFailed:
        # Let HA start the reauth flow; do not mask it as a retryable setup.
        coordinator.close()
        raise
    except Exception as err:
        coordinator.close()
        raise ConfigEntryNotReady(
            f"Initial Helen consumption import failed: {err}"
        ) from err

    # There are no entities to drive a DataUpdateCoordinator, so poll on a timer.
    async def _scheduled_update(_now) -> None:
        try:
            await coordinator.async_update()
        except ConfigEntryAuthFailed:
            # The timer path has no setup machinery to catch this, so trigger
            # reauth explicitly instead of letting it be silently logged.
            entry.async_start_reauth(hass)

    entry.async_on_unload(
        async_track_time_interval(hass, _scheduled_update, SCAN_INTERVAL)
    )
    entry.async_on_unload(coordinator.close)
    entry.runtime_data = coordinator

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
