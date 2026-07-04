"""Coordinator for the Helen Energy Consumption integration.

Owns the Helen API client and drives periodic consumption imports. This is a
deliberately thin coordinator: it has no entities, it only logs in and pushes
hourly consumption into the statistics database on a fixed interval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import TYPE_CHECKING

from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidApiResponseException,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)

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
        delivery_site_id: str,
    ) -> None:
        """Initialize the coordinator and its statistics manager."""
        self.hass = hass
        self.entry = entry
        self.credentials = credentials
        self.delivery_site_id = delivery_site_id
        # Created on the event loop (async_setup_entry runs __init__), so it is
        # bound to the running loop. Guards against overlapping polls.
        self._update_lock = asyncio.Lock()

        # Consumption is in kWh and does not depend on VAT or margin, so the
        # client is created without price parameters.
        self.api_client = HelenApiClient()
        self.statistics = HelenConsumptionStatistics(
            hass,
            self.api_client,
            delivery_site_id,
            entry.title,
        )

    async def async_update(self, raise_on_error: bool = False) -> None:
        """Log in if needed and import the latest consumption statistics.

        The timer poll stays fail-quiet (``raise_on_error=False``) so a
        transient failure never crashes the integration (VISION principle 5).
        The initial setup import passes ``raise_on_error=True`` so transient
        failures surface as ConfigEntryNotReady and HA retries with backoff.
        ConfigEntryAuthFailed always propagates so reauth can start.

        A poll already in progress is skipped (not queued): the shared
        api_client session must not be touched by two overlapping runs. The
        locked() check and the acquire are kept adjacent with no await between
        them so the test-and-acquire is atomic on the single event loop.
        """
        if self._update_lock.locked():
            _LOGGER.debug("Consumption poll already in progress; skipping this tick")
            return
        async with self._update_lock:
            try:
                await self._login_if_needed()
                await self.hass.async_add_executor_job(
                    self.api_client.select_delivery_site_if_valid_id,
                    self.delivery_site_id,
                )
                await self.statistics.import_recent_statistics()
            except HelenAuthenticationException as err:
                raise ConfigEntryAuthFailed from err
            except InvalidApiResponseException as err:
                _LOGGER.warning("Helen API error during consumption import: %s", err)
                if raise_on_error:
                    raise
            except Exception:
                _LOGGER.exception("Unexpected error during consumption import")
                if raise_on_error:
                    raise
            finally:
                await self.hass.async_add_executor_job(self.api_client.close)

    async def async_backfill(self, start_date: date) -> None:
        """Rebuild this delivery site's chain for [start_date, now] from Helen.

        A user-initiated repair/import action: it WAITS for the poll lock
        (reusing #7's lock) so it runs to completion and never interleaves with
        a scheduled poll. Validates the requested range against the contract
        start before rebuilding. All Helen calls run in the executor; any
        failure raises before the single statistics write, leaving prior data
        intact (VISION principle 5).

        Failures are mapped to the HA taxonomy (STACK.md §8) so no raw
        helenservice exception escapes into HA: HomeAssistantError subclasses
        (ServiceValidationError, ConfigEntryAuthFailed) pass through untouched;
        an auth failure starts reauth and surfaces as HomeAssistantError; every
        other failure is wrapped as HomeAssistantError for the action UI.
        """
        async with self._update_lock:
            try:
                await self._login_if_needed()
                await self.hass.async_add_executor_job(
                    self.api_client.select_delivery_site_if_valid_id,
                    self.delivery_site_id,
                )
                await self._validate_backfill_range(start_date)
                await self.statistics.rebuild_range(start_date)
            except HomeAssistantError:
                # Must stay first: ServiceValidationError and
                # ConfigEntryAuthFailed are HomeAssistantError subclasses; a
                # later generic clause would re-wrap them and break the
                # action-UI rendering.
                raise
            except HelenAuthenticationException as err:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    "Helen authentication failed during backfill; "
                    "re-authentication started"
                ) from err
            except InvalidApiResponseException as err:
                raise HomeAssistantError(
                    f"Helen API error during backfill: {err}"
                ) from err
            except Exception as err:
                _LOGGER.exception("Unexpected error during Helen backfill")
                raise HomeAssistantError(
                    f"Unexpected error during backfill: {err}"
                ) from err
            finally:
                await self.hass.async_add_executor_job(self.api_client.close)

    async def _validate_backfill_range(self, start_date: date) -> None:
        """Reject a start_date in the future or before available history.

        A missing contract start (None) is treated as an unknowable bound and
        allowed through, so the rebuild proceeds best-effort.
        """
        if start_date > date.today():
            raise ServiceValidationError(f"start_date {start_date} is in the future")

        contract_start = await self.hass.async_add_executor_job(
            self.api_client.get_contract_start_date
        )
        if contract_start is not None and start_date < contract_start:
            raise ServiceValidationError(
                f"start_date {start_date} is before earliest available data "
                f"({contract_start})"
            )

    async def _login_if_needed(self) -> None:
        """Ensure the API client has a valid session."""
        if self.api_client.is_session_valid():
            return
        await self.hass.async_add_executor_job(self.api_client.close)
        await self.hass.async_add_executor_job(
            lambda: self.api_client.login_and_init(**self.credentials)
        )

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self.api_client.close()
