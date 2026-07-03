"""Shared pytest fixtures.

`enable_custom_integrations` comes from pytest-homeassistant-custom-component and
makes Home Assistant load this repo's `custom_components/` during tests. Enabling
it here keeps future Home Assistant based tests ready without per-test wiring.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration for every test."""
    yield
