"""Shared pytest fixtures.

`enable_custom_integrations` comes from pytest-homeassistant-custom-component and
makes Home Assistant load this repo's `custom_components/` during tests. Enabling
it here keeps future Home Assistant based tests ready without per-test wiring.
"""

from __future__ import annotations

import pytest

from custom_components.helen_energy_consumption import statistics


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration for every test."""
    yield


@pytest.fixture(autouse=True)
def _clear_chain_locks():
    """Reset the module-level chain locks around each test.

    _CHAIN_LOCKS caches an asyncio.Lock per statistic_id; a cached lock binds to
    the event loop that created it, and pytest-asyncio uses a fresh loop per
    test, so a stale entry would raise "attached to a different loop" or wedge.
    """
    statistics._CHAIN_LOCKS.clear()
    yield
    statistics._CHAIN_LOCKS.clear()
