"""Tests for the redacted config-entry diagnostics.

Direct-call tests (pytest-homeassistant-custom-component ships no diagnostics
helper): they pin the §8 redaction set — credentials, delivery-site id, and
their derived carriers (title = street address, unique_id = username_siteid) —
and that the payload stays PII-free metadata only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from custom_components.helen_energy_consumption.const import (
    CONF_DELIVERY_SITE_ID,
    DOMAIN,
    ROLLING_WINDOW_HOURS,
)
from custom_components.helen_energy_consumption.diagnostics import (
    async_get_config_entry_diagnostics,
)


def _entry() -> MagicMock:
    """Build a config-entry double whose as_dict carries the sensitive fields."""
    entry = MagicMock()
    entry.as_dict.return_value = {
        "title": "Esimerkkikatu 1 A",
        "unique_id": "user@example.com_1001",
        "domain": DOMAIN,
        "data": {
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_DELIVERY_SITE_ID: "1001",
        },
    }
    entry.state = ConfigEntryState.LOADED
    return entry


async def test_diagnostics_redacts_sensitive_fields() -> None:
    """Credentials, site id, and their derived carriers come back redacted."""
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry())

    redacted = result["entry"]
    assert redacted["data"][CONF_USERNAME] == REDACTED
    assert redacted["data"][CONF_PASSWORD] == REDACTED
    assert redacted["data"][CONF_DELIVERY_SITE_ID] == REDACTED
    # Derived PII carriers: street-address title, username+site unique_id.
    assert redacted["title"] == REDACTED
    assert redacted["unique_id"] == REDACTED
    # Non-sensitive keys survive; the statistic_id (embeds the site id) is
    # deliberately absent from the payload.
    assert redacted["domain"] == DOMAIN
    assert "statistic_id" not in result
    assert result["rolling_window_hours"] == ROLLING_WINDOW_HOURS


async def test_diagnostics_reports_entry_state() -> None:
    """The config-entry state is reported in its string form."""
    result = await async_get_config_entry_diagnostics(MagicMock(), _entry())

    assert result["entry_state"] == str(ConfigEntryState.LOADED)
