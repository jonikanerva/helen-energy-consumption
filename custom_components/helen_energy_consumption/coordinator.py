"""Coordinator for the Helen Energy Consumption integration.

Owns the Helen API client and drives periodic consumption imports. This is a
deliberately thin coordinator: it has no entities, it only logs in and pushes
hourly consumption into the statistics database on a fixed interval.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidApiResponseException,
)
from homeassistant.exceptions import ConfigEntryAuthFailed

from .statistics import HelenConsumptionStatistics

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class HelenConsumptionCoordinator:
    """Log in to Helen and import hourly consumption statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        credentials: dict[str, str],
        delivery_site_id: str | None,
    ) -> None:
        """Initialize the coordinator and its statistics manager."""
        self.hass = hass
        self.entry = entry
        self.credentials = credentials
        self.delivery_site_id = delivery_site_id

        # Consumption is in kWh and does not depend on VAT or margin, so the
        # client is created without price parameters.
        self.api_client = HelenApiClient()
        self.statistics = HelenConsumptionStatistics(
            hass,
            self.api_client,
            entry.entry_id,
            entry.title,
        )

    async def async_update(self) -> None:
        """Log in if needed and import the latest consumption statistics."""
        try:
            await self._login_if_needed()
            if self.delivery_site_id is not None:
                await self.hass.async_add_executor_job(
                    self.api_client.select_delivery_site_if_valid_id,
                    self.delivery_site_id,
                )
            await self.statistics.import_recent_statistics()
        except HelenAuthenticationException as err:
            raise ConfigEntryAuthFailed from err
        except InvalidApiResponseException as err:
            _LOGGER.warning("Helen API error during consumption import: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error during consumption import")
        finally:
            self.api_client.close()

    async def _login_if_needed(self) -> None:
        """Ensure the API client has a valid session."""
        if self.api_client.is_session_valid():
            return
        self.api_client.close()
        await self.hass.async_add_executor_job(
            lambda: self.api_client.login_and_init(**self.credentials)
        )

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self.api_client.close()
