"""The Helen Energy Consumption integration.

A minimal HACS custom component that imports hourly electricity consumption
from Helen into Home Assistant's statistics database so it can be added to the
Energy Dashboard. No entities, no cost tracking — consumption only.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DELIVERY_SITE_ID, SCAN_INTERVAL
from .coordinator import HelenConsumptionCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helen Energy Consumption from a config entry."""
    coordinator = HelenConsumptionCoordinator(
        hass,
        entry,
        credentials={
            "username": entry.data[CONF_USERNAME],
            "password": entry.data[CONF_PASSWORD],
        },
        delivery_site_id=entry.data.get(CONF_DELIVERY_SITE_ID),
    )

    # Run one import immediately so the Energy Dashboard has data right away.
    # A failure here (bad credentials, API down) surfaces as a retryable setup.
    try:
        await coordinator.async_update()
    except Exception as err:
        coordinator.close()
        raise ConfigEntryNotReady(
            f"Initial Helen consumption import failed: {err}"
        ) from err

    # There are no entities to drive a DataUpdateCoordinator, so poll on a timer.
    async def _scheduled_update(_now) -> None:
        await coordinator.async_update()

    entry.async_on_unload(
        async_track_time_interval(hass, _scheduled_update, SCAN_INTERVAL)
    )
    entry.async_on_unload(coordinator.close)
    entry.runtime_data = coordinator

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
