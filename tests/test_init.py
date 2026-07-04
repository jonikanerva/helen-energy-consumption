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

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidApiResponseException,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.helen_energy_consumption import async_setup_entry
from custom_components.helen_energy_consumption.const import CONF_DELIVERY_SITE_ID
from custom_components.helen_energy_consumption.coordinator import (
    HelenConsumptionCoordinator,
)

_PKG = "custom_components.helen_energy_consumption"


def _entry() -> MagicMock:
    """Build a config-entry double with the data setup reads."""
    entry = MagicMock()
    entry.data = {
        CONF_USERNAME: "user",
        CONF_PASSWORD: "pass",
        CONF_DELIVERY_SITE_ID: "12345678",
    }
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
            delivery_site_id="12345678",
        )
    coord._login_if_needed = AsyncMock()
    # async_update selects the delivery site via the executor; make it awaitable.
    coord.hass.async_add_executor_job = AsyncMock()
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


async def test_update_closes_client_in_executor() -> None:
    """The poll-path client close is executor-routed, never run on the loop."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock()

    await coord.async_update()

    exec_calls = coord.hass.async_add_executor_job.await_args_list
    assert call(coord.api_client.close) in exec_calls
    coord.api_client.close.assert_not_called()


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


# --- overlapping-poll guard (issue #7) --------------------------------------


async def test_concurrent_poll_is_skipped_not_queued() -> None:
    """A poll running while another holds the lock is skipped, not queued."""
    coord = _coordinator()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _blocking_import() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    coord.statistics.import_recent_statistics = _blocking_import

    # A acquires the lock and blocks inside the import.
    task_a = asyncio.create_task(coord.async_update())
    await started.wait()
    assert coord._update_lock.locked() is True

    # B runs while A holds the lock: it must return immediately without a
    # second import (skip, not queue).
    await coord.async_update()
    assert calls == 1
    assert coord._update_lock.locked() is True

    # Let A finish; no extra import happened.
    release.set()
    await task_a
    assert calls == 1
    assert coord._update_lock.locked() is False


async def test_sequential_polls_both_run() -> None:
    """Two non-overlapping polls both execute; the lock frees between them."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock()

    await coord.async_update()
    assert coord._update_lock.locked() is False
    await coord.async_update()

    assert coord.statistics.import_recent_statistics.await_count == 2


async def test_exception_releases_the_lock() -> None:
    """An error inside a poll still releases the lock (async with unwinds)."""
    coord = _coordinator()
    coord.statistics.import_recent_statistics = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    # Fail-quiet path swallows the error, but the lock must release.
    await coord.async_update()
    assert coord._update_lock.locked() is False

    # A following poll runs, proving the lock was freed on the error path.
    coord.statistics.import_recent_statistics = AsyncMock()
    await coord.async_update()
    assert coord.statistics.import_recent_statistics.await_count == 1


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
