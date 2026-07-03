"""Regression tests for setup, the timer poll, and the reauth path.

These pin the fail-quiet-vs-surface contract introduced alongside the reauth
wiring:

- the coordinator swallows a transient error on the timer poll but re-raises it
  when the initial setup import asks it to (``raise_on_error=True``);
- ``async_setup_entry`` lets ConfigEntryAuthFailed propagate (so HA starts
  reauth) and wraps a transient failure as ConfigEntryNotReady (so HA retries);
- the scheduled timer callback starts reauth on an auth failure.

The coordinator tests drive the real coordinator with a stubbed statistics
import; the setup/timer tests mock the coordinator at its smallest seam.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidApiResponseException,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.helen_energy_consumption import async_setup_entry
from custom_components.helen_energy_consumption.coordinator import (
    HelenConsumptionCoordinator,
)

_PKG = "custom_components.helen_energy_consumption"


def _entry() -> MagicMock:
    """Build a config-entry double with the data setup reads."""
    entry = MagicMock()
    entry.data = {CONF_USERNAME: "user", CONF_PASSWORD: "pass"}
    entry.entry_id = "test-entry-id"
    entry.title = "Helen"
    return entry


def _coordinator() -> HelenConsumptionCoordinator:
    """Build a real coordinator with a mocked API client and stubbed login."""
    with patch(f"{_PKG}.coordinator.HelenApiClient", return_value=MagicMock()):
        coord = HelenConsumptionCoordinator(
            MagicMock(),
            _entry(),
            credentials={"username": "user", "password": "pass"},
            delivery_site_id=None,
        )
    coord._login_if_needed = AsyncMock()
    return coord


async def test_timer_poll_swallows_transient_error() -> None:
    """The default (timer) update stays fail-quiet on a transient API error."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=InvalidApiResponseException("Helen down")
    )

    # Must not raise.
    await coord.async_update()


async def test_setup_import_reraises_transient_error() -> None:
    """raise_on_error=True surfaces a transient error for ConfigEntryNotReady."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=InvalidApiResponseException("Helen down")
    )

    with pytest.raises(InvalidApiResponseException):
        await coord.async_update(raise_on_error=True)


async def test_setup_import_reraises_generic_error() -> None:
    """raise_on_error=True surfaces an unexpected error; the default swallows it."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    await coord.async_update()  # fail-quiet on the timer path

    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    with pytest.raises(RuntimeError):
        await coord.async_update(raise_on_error=True)


async def test_auth_error_always_raises_config_entry_auth_failed() -> None:
    """An auth failure raises ConfigEntryAuthFailed regardless of raise_on_error."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=HelenAuthenticationException("bad creds")
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord.async_update()
    with pytest.raises(ConfigEntryAuthFailed):
        await coord.async_update(raise_on_error=True)


async def test_setup_propagates_auth_failure_unchanged() -> None:
    """ConfigEntryAuthFailed from the initial import is not re-wrapped."""
    coord = MagicMock()
    coord.async_update = AsyncMock(side_effect=ConfigEntryAuthFailed)
    coord.close = MagicMock()

    with (
        patch(f"{_PKG}.HelenConsumptionCoordinator", return_value=coord),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(MagicMock(), _entry())

    coord.close.assert_called_once()


async def test_setup_wraps_transient_failure_as_not_ready() -> None:
    """A transient initial-import failure surfaces as ConfigEntryNotReady."""
    coord = MagicMock()
    coord.async_update = AsyncMock(side_effect=InvalidApiResponseException("down"))
    coord.close = MagicMock()

    with (
        patch(f"{_PKG}.HelenConsumptionCoordinator", return_value=coord),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(MagicMock(), _entry())

    # Setup must ask the coordinator to surface transient errors.
    coord.async_update.assert_awaited_once_with(raise_on_error=True)
    coord.close.assert_called_once()


async def test_timer_callback_starts_reauth_on_auth_failure() -> None:
    """The scheduled poll triggers reauth when async_update raises auth failure."""
    hass = MagicMock()
    entry = _entry()
    coord = MagicMock()
    coord.async_update = AsyncMock()  # initial setup import succeeds
    coord.close = MagicMock()

    captured: dict[str, object] = {}

    def _fake_track(_hass, action, _interval):
        captured["action"] = action
        return MagicMock()

    with (
        patch(f"{_PKG}.HelenConsumptionCoordinator", return_value=coord),
        patch(f"{_PKG}.async_track_time_interval", side_effect=_fake_track),
    ):
        assert await async_setup_entry(hass, entry) is True

    # The scheduled poll now hits an auth failure and must start reauth.
    coord.async_update = AsyncMock(side_effect=ConfigEntryAuthFailed)
    await captured["action"](None)

    entry.async_start_reauth.assert_called_once_with(hass)
