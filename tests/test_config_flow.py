"""Tests for the Helen Energy Consumption config flow.

Drive the flow end-to-end with a mocked HelenApiClient (its sync methods run
via the executor). Cover the credentials step, the single/multi/zero-site
branches, the delivery-site dedupe guard, address fallback, error mapping, and
the unique-id dedupe abort. `build_statistic_id` is unit-tested directly.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helenservice.api_exceptions import HelenAuthenticationException
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helen_energy_consumption.config_flow import (
    HelenConsumptionConfigFlow,
    SiteInfo,
    _site_label,
)
from custom_components.helen_energy_consumption.const import (
    CONF_DELIVERY_SITE_ID,
    DOMAIN,
)
from custom_components.helen_energy_consumption.statistics import build_statistic_id

_MISSING = object()
_PKG = "custom_components.helen_energy_consumption.config_flow"
_CREDS = {CONF_USERNAME: "user", CONF_PASSWORD: "pass"}


def _contract(gsrn: str, site_id: str, address: Any = "Testikatu 1") -> dict[str, Any]:
    """Build a contract payload like get_contract_data_json returns.

    A str `address` is wrapped as the modelled `{"street_address": ...}` shape;
    `_MISSING` omits the address key; anything else (None, a list) is stored
    raw to exercise the defensive extraction.
    """
    delivery_site: dict[str, Any] = {"id": site_id}
    if address is _MISSING:
        pass
    elif isinstance(address, str):
        delivery_site["address"] = {"street_address": address}
    else:
        delivery_site["address"] = address
    return {"gsrn": gsrn, "delivery_site": delivery_site}


def _client(site_ids: list[str], contracts: list[dict[str, Any]]) -> MagicMock:
    """Build a mocked HelenApiClient with the given site list and contracts."""
    client = MagicMock()
    client.get_all_delivery_site_ids.return_value = site_ids
    client.get_contract_data_json.return_value = contracts
    return client


def _dropdown_options(result: dict[str, Any]) -> list[dict[str, str]]:
    """Extract the SelectSelector options from a shown form's schema."""
    schema = result["data_schema"].schema
    for key, validator in schema.items():
        if str(key) == CONF_DELIVERY_SITE_ID:
            return validator.config["options"]
    raise AssertionError("delivery_site_id not present in the form schema")


async def _run_user_step(hass: HomeAssistant, client: MagicMock) -> dict[str, Any]:
    """Start the flow and submit valid credentials with `client` patched in."""
    with patch(f"{_PKG}.HelenApiClient", return_value=client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], dict(_CREDS)
        )


# --- build_statistic_id -----------------------------------------------------


def test_build_statistic_id_canonical() -> None:
    assert build_statistic_id("12345678") == (
        "helen_energy_consumption:consumption_12345678"
    )


def test_build_statistic_id_sanitizes_non_canonical() -> None:
    stat_id = build_statistic_id("AB-12:CD")
    assert stat_id == "helen_energy_consumption:consumption_ab_12_cd"
    object_id = stat_id.split(":consumption_", 1)[1]
    assert object_id and re.fullmatch(r"[a-z0-9_]+", object_id)


# --- _collect_sites / labels ------------------------------------------------


def test_site_label_gsrn_only_without_address() -> None:
    assert _site_label(SiteInfo("1001", "637100", None)) == "637100"
    assert _site_label(SiteInfo("1001", "637100", "Katu 1")) == "637100 - Katu 1"


@pytest.mark.parametrize("address", [None, _MISSING, ["a", "list"]])
def test_collect_sites_address_anomalies_fall_back_to_gsrn(address: Any) -> None:
    """A null/absent/list address must not fail; the label falls back to gsrn."""
    flow = HelenConsumptionConfigFlow()
    flow.api_client = _client(["1001"], [_contract("637100", "1001", address)])

    sites = flow._collect_sites()

    assert len(sites) == 1
    assert sites[0].address is None
    assert _site_label(sites[0]) == "637100"


# --- flow branches ----------------------------------------------------------


async def test_single_site_happy_path(hass: HomeAssistant) -> None:
    """A single-site account skips the dropdown and creates the entry."""
    client = _client(["1001"], [_contract("637100", "1001", "Alppikatu 1")])

    result = await _run_user_step(hass, client)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alppikatu 1"
    assert result["data"] == {**_CREDS, CONF_DELIVERY_SITE_ID: "1001"}
    assert "custom_name" not in result["data"]
    assert result["result"].unique_id == "user_1001"
    # Single-site fast path validates via the API's own site list, not select.
    client.select_delivery_site_if_valid_id.assert_not_called()


async def test_multi_site_pick(hass: HomeAssistant) -> None:
    """A multi-site account shows a labelled dropdown and persists the choice."""
    client = _client(
        ["1001", "1002"],
        [
            _contract("637100", "1001", "Alppikatu 1"),
            _contract("637200", "1002", _MISSING),  # no address -> gsrn only
        ],
    )

    with patch(f"{_PKG}.HelenApiClient", return_value=client):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        form = await hass.config_entries.flow.async_configure(
            result["flow_id"], dict(_CREDS)
        )

        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "select_site"
        labels = {o["value"]: o["label"] for o in _dropdown_options(form)}
        assert labels == {"1001": "637100 - Alppikatu 1", "1002": "637200"}

        created = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_DELIVERY_SITE_ID: "1002"}
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_DELIVERY_SITE_ID] == "1002"
    assert created["result"].unique_id == "user_1002"
    client.select_delivery_site_if_valid_id.assert_called_once_with("1002")


async def test_two_contracts_one_site_dedupe_fast_path(hass: HomeAssistant) -> None:
    """Two active contracts sharing a delivery-site id must not show a picker."""
    client = _client(
        ["1001", "1001"],  # energy + electricity-transfer, same site
        [
            _contract("637100", "1001", "Katu 1"),
            _contract("637101", "1001", "Katu 1"),
        ],
    )

    result = await _run_user_step(hass, client)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DELIVERY_SITE_ID] == "1001"


async def test_zero_sites_aborts(hass: HomeAssistant) -> None:
    """A production-only account with no delivery site aborts, no broken entry."""
    client = _client([], [])

    result = await _run_user_step(hass, client)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_delivery_sites"


async def test_invalid_auth_shows_form_error(hass: HomeAssistant) -> None:
    """Bad credentials re-show the credentials form with invalid_auth."""
    client = MagicMock()
    client.login_and_init.side_effect = HelenAuthenticationException("bad")

    result = await _run_user_step(hass, client)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_site_aborts_already_configured(hass: HomeAssistant) -> None:
    """A (username, site) already configured aborts with already_configured."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="user_1001",
        data={**_CREDS, CONF_DELIVERY_SITE_ID: "1001"},
    ).add_to_hass(hass)

    client = _client(["1001"], [_contract("637100", "1001", "Katu 1")])

    result = await _run_user_step(hass, client)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- Seam 4: config-flow reauth (issue #20) ---------------------------------


def _reauth_entry() -> MockConfigEntry:
    """A configured entry with an old password and a delivery site to preserve."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user_12345",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "old",
            CONF_DELIVERY_SITE_ID: "12345",
        },
    )


async def test_reauth_success_updates_password_preserves_site(
    hass: HomeAssistant,
) -> None:
    """A successful reauth swaps the password, keeps the site, and reloads."""
    entry = _reauth_entry()
    entry.add_to_hass(hass)
    client = MagicMock()  # login_and_init + close succeed

    with (
        patch(f"{_PKG}.HelenApiClient", return_value=client),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    # Password updated AND delivery_site_id preserved.
    assert entry.data == {
        CONF_USERNAME: "user",
        CONF_PASSWORD: "new",
        CONF_DELIVERY_SITE_ID: "12345",
    }
    reload.assert_awaited_once_with(entry.entry_id)
    # Re-authenticated with the stored username and the NEW password.
    client.login_and_init.assert_called_once_with("user", "new")


async def test_reauth_invalid_auth_keeps_entry_and_closes(hass: HomeAssistant) -> None:
    """Bad credentials re-show the form, leave the entry unchanged, close session."""
    entry = _reauth_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.login_and_init.side_effect = HelenAuthenticationException("bad")

    with (
        patch(f"{_PKG}.HelenApiClient", return_value=client),
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
    ):
        result = await entry.start_reauth_flow(hass)
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert done["type"] is FlowResultType.FORM
    assert done["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == "old"  # unchanged
    client.close.assert_called()  # session cleaned up


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionError(), RuntimeError("boom")]
)
async def test_reauth_cannot_connect(hass: HomeAssistant, error: Exception) -> None:
    """Connection errors (and any generic error) map to cannot_connect, unchanged."""
    entry = _reauth_entry()
    entry.add_to_hass(hass)
    client = MagicMock()
    client.login_and_init.side_effect = error

    with (
        patch(f"{_PKG}.HelenApiClient", return_value=client),
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
    ):
        result = await entry.start_reauth_flow(hass)
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert done["type"] is FlowResultType.FORM
    assert done["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_PASSWORD] == "old"


async def test_reauth_missing_entry_aborts(hass: HomeAssistant) -> None:
    """If the entry vanished before confirm, the flow aborts reauth_failed."""
    entry = _reauth_entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch.object(hass.config_entries, "async_get_entry", return_value=None):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_failed"
