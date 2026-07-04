"""Tests for the backfill admin action and its coordinator entry point.

Covers target-entry resolution (single/multi/unknown/unloaded), the registered
service handler dispatching to the right delivery site, and the range
validation that rejects a start_date before the contract start or in the
future without touching the write path.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidApiResponseException,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.helen_energy_consumption import (
    _resolve_target_entry,
    async_setup,
)
from custom_components.helen_energy_consumption.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_START_DATE,
    DOMAIN,
)
from custom_components.helen_energy_consumption.coordinator import (
    HelenConsumptionCoordinator,
)
from custom_components.helen_energy_consumption.statistics import build_statistic_id

_PKG = "custom_components.helen_energy_consumption"


def _loaded_entry(entry_id: str, site_id: str) -> MagicMock:
    """Build a loaded config-entry double whose coordinator targets site_id."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.domain = DOMAIN
    entry.state = ConfigEntryState.LOADED
    coordinator = MagicMock()
    coordinator.async_backfill = AsyncMock()
    coordinator.statistics.consumption_statistic_id = build_statistic_id(site_id)
    entry.runtime_data = coordinator
    return entry


def _hass_with(entries: list[MagicMock]) -> MagicMock:
    """Build a hass double whose config_entries expose the given entries."""
    hass = MagicMock()
    by_id = {entry.entry_id: entry for entry in entries}
    hass.config_entries.async_get_entry.side_effect = by_id.get
    hass.config_entries.async_entries.return_value = list(entries)
    return hass


# --- target resolution ------------------------------------------------------


def test_resolve_specific_entry_targets_that_site() -> None:
    entry_a = _loaded_entry("A", "1001")
    entry_b = _loaded_entry("B", "2002")
    hass = _hass_with([entry_a, entry_b])

    resolved = _resolve_target_entry(hass, "B")

    assert resolved is entry_b
    assert resolved.runtime_data.statistics.consumption_statistic_id == (
        build_statistic_id("2002")
    )


def test_resolve_single_entry_without_id() -> None:
    entry = _loaded_entry("A", "1001")
    assert _resolve_target_entry(_hass_with([entry]), None) is entry


def test_resolve_multiple_without_id_raises() -> None:
    hass = _hass_with([_loaded_entry("A", "1001"), _loaded_entry("B", "2002")])
    with pytest.raises(ServiceValidationError, match="config_entry_id"):
        _resolve_target_entry(hass, None)


def test_resolve_unknown_entry_raises() -> None:
    hass = _hass_with([_loaded_entry("A", "1001")])
    with pytest.raises(ServiceValidationError):
        _resolve_target_entry(hass, "missing")


def test_resolve_unloaded_entry_raises() -> None:
    entry = _loaded_entry("A", "1001")
    entry.state = ConfigEntryState.NOT_LOADED
    with pytest.raises(ServiceValidationError):
        _resolve_target_entry(_hass_with([entry]), "A")


def test_resolve_no_entries_raises() -> None:
    with pytest.raises(ServiceValidationError):
        _resolve_target_entry(_hass_with([]), None)


# --- registered service handler ---------------------------------------------


async def test_backfill_service_targets_selected_entry() -> None:
    """The registered handler dispatches to the chosen entry's coordinator."""
    entry_a = _loaded_entry("A", "1001")
    entry_b = _loaded_entry("B", "2002")
    hass = _hass_with([entry_a, entry_b])

    captured: dict[str, object] = {}

    def _fake_register(_hass, _domain, _service, func, schema=None) -> None:
        captured["func"] = func

    with patch(f"{_PKG}.async_register_admin_service", side_effect=_fake_register):
        assert await async_setup(hass, {}) is True

    call = MagicMock()
    call.data = {ATTR_START_DATE: date(2026, 1, 15), ATTR_CONFIG_ENTRY_ID: "B"}
    await captured["func"](call)

    entry_b.runtime_data.async_backfill.assert_awaited_once_with(date(2026, 1, 15))
    entry_a.runtime_data.async_backfill.assert_not_called()


# --- range validation (coordinator.async_backfill) --------------------------


def _coordinator(contract_start: date | None) -> HelenConsumptionCoordinator:
    """Build a real coordinator; the executor returns the given contract start."""
    entry = MagicMock()
    entry.title = "Helen"
    entry.entry_id = "test-entry-id"
    with patch(f"{_PKG}.coordinator.HelenApiClient", return_value=MagicMock()):
        coord = HelenConsumptionCoordinator(
            MagicMock(),
            entry,
            credentials={"username": "user", "password": "pass"},
            delivery_site_id="1001",
        )
    coord._login_if_needed = AsyncMock()
    coord.statistics.rebuild_range = AsyncMock()

    async def _exec(func, *args):
        if func is coord.api_client.get_contract_start_date:
            return contract_start
        return None  # select_delivery_site_if_valid_id

    coord.hass.async_add_executor_job = _exec
    return coord


async def test_backfill_rejects_start_before_contract() -> None:
    """A start_date before the contract start is rejected, naming the earliest."""
    coord = _coordinator(contract_start=date(2026, 2, 1))

    with pytest.raises(ServiceValidationError, match="2026-02-01"):
        await coord.async_backfill(date(2026, 1, 15))

    coord.statistics.rebuild_range.assert_not_called()


async def test_backfill_rejects_future_start() -> None:
    """A future start_date is rejected before any rebuild."""
    coord = _coordinator(contract_start=None)

    with pytest.raises(ServiceValidationError):
        await coord.async_backfill(date.today() + timedelta(days=1))

    coord.statistics.rebuild_range.assert_not_called()


async def test_backfill_allows_unknown_contract_start() -> None:
    """A missing contract start is an unknowable bound -> proceed best-effort."""
    coord = _coordinator(contract_start=None)

    await coord.async_backfill(date(2026, 1, 15))

    coord.statistics.rebuild_range.assert_awaited_once_with(date(2026, 1, 15))


# --- error taxonomy (coordinator.async_backfill, STACK.md §8) -----------------


async def test_backfill_auth_failure_starts_reauth_and_wraps() -> None:
    """An auth failure starts reauth and surfaces as HomeAssistantError."""
    coord = _coordinator(contract_start=None)
    coord._login_if_needed = AsyncMock(
        side_effect=HelenAuthenticationException("bad creds")
    )

    with pytest.raises(HomeAssistantError, match="re-authentication started"):
        await coord.async_backfill(date(2026, 1, 15))

    coord.entry.async_start_reauth.assert_called_once_with(coord.hass)
    coord.statistics.rebuild_range.assert_not_called()


async def test_backfill_api_error_wraps_without_reauth() -> None:
    """An API error surfaces as HomeAssistantError; reauth is not started."""
    coord = _coordinator(contract_start=None)
    coord.statistics.rebuild_range = AsyncMock(
        side_effect=InvalidApiResponseException("Helen down")
    )

    with pytest.raises(HomeAssistantError, match="Helen API error during backfill"):
        await coord.async_backfill(date(2026, 1, 15))

    coord.entry.async_start_reauth.assert_not_called()


async def test_backfill_unexpected_error_wraps_as_home_assistant_error() -> None:
    """An unexpected error is wrapped as HomeAssistantError, not raised raw."""
    coord = _coordinator(contract_start=None)
    coord.statistics.rebuild_range = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(HomeAssistantError, match="Unexpected error during backfill"):
        await coord.async_backfill(date(2026, 1, 15))


async def test_backfill_home_assistant_error_passes_through_untouched() -> None:
    """A HomeAssistantError from the rebuild is re-raised, not re-wrapped."""
    coord = _coordinator(contract_start=None)
    sentinel = HomeAssistantError("sentinel")
    coord.statistics.rebuild_range = AsyncMock(side_effect=sentinel)

    with pytest.raises(HomeAssistantError) as excinfo:
        await coord.async_backfill(date(2026, 1, 15))

    assert excinfo.value is sentinel


async def test_backfill_closes_client_in_executor() -> None:
    """The client close is submitted to the executor, never run on the loop."""
    coord = _coordinator(contract_start=None)
    submitted: list[object] = []

    async def _recording_exec(func, *args):
        submitted.append(func)
        return None  # contract start unknown -> proceed best-effort

    coord.hass.async_add_executor_job = _recording_exec

    await coord.async_backfill(date(2026, 1, 15))

    assert coord.api_client.close in submitted
    coord.api_client.close.assert_not_called()
