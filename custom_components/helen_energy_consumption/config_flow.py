"""Config flow for the Helen Energy Consumption integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

import voluptuous as vol
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidDeliverySiteException,
)
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector

from .const import CONF_DELIVERY_SITE_ID, DOMAIN

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)


class SiteInfo(NamedTuple):
    """A distinct Helen delivery site the user can import."""

    site_id: str
    gsrn: str
    address: str | None


def _extract_address(delivery_site: dict[str, Any]) -> str | None:
    """Return the street address for a delivery site, or None on any anomaly.

    The nested address shape is not modelled by the library and may be null, a
    list, or absent; any failure must fall back to a gsrn-only label rather
    than break the flow.
    """
    try:
        address = delivery_site.get("address") or {}
        street = address.get("street_address")
        return str(street) if street else None
    except Exception:  # noqa: BLE001 - address is best-effort, never fatal
        return None


def _site_label(info: SiteInfo) -> str:
    """Build the dropdown label: '<gsrn> - <address>', or gsrn alone."""
    if info.address:
        return f"{info.gsrn} - {info.address}"
    return info.gsrn


class HelenConsumptionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Helen Energy Consumption."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.api_client: HelenApiClient | None = None
        self._user_input: dict[str, Any] | None = None
        self._sites: list[SiteInfo] = []

    async def _authenticate(self, username: str, password: str) -> None:
        """Log in to Helen; raises HelenAuthenticationException on failure."""
        self.api_client = HelenApiClient()
        await self.hass.async_add_executor_job(
            self.api_client.login_and_init, username, password
        )

    async def _cleanup(self) -> None:
        """Close the API session if one was opened."""
        if self.api_client is not None:
            await self.hass.async_add_executor_job(self.api_client.close)
            self.api_client = None

    def _handle_error(self, exception: Exception) -> dict[str, str]:
        """Map an exception to a form error key."""
        if isinstance(exception, HelenAuthenticationException):
            _LOGGER.error("Authentication failed: %s", exception)
            return {"base": "invalid_auth"}
        if isinstance(exception, InvalidDeliverySiteException):
            return {"base": "invalid_delivery_site_id"}
        if isinstance(exception, (TimeoutError, ConnectionError)):
            return {"base": "cannot_connect"}
        _LOGGER.exception("Unexpected error during setup")
        return {"base": "cannot_connect"}

    def _collect_sites(self) -> list[SiteInfo]:
        """Collect distinct active delivery sites (blocking; run in executor).

        `get_all_delivery_site_ids()` is the library's source of truth for
        active sites but does not dedupe (one physical site can have several
        active contracts); gsrn and address are read from
        `get_contract_data_json()`.
        """
        site_ids = [str(s) for s in self.api_client.get_all_delivery_site_ids()]
        contracts = self.api_client.get_contract_data_json()

        by_id: dict[str, SiteInfo] = {}
        for contract in contracts:
            delivery_site = contract.get("delivery_site") or {}
            raw_id = delivery_site.get("id")
            if raw_id is None:
                continue
            sid = str(raw_id)
            gsrn = str(contract.get("gsrn") or sid)
            by_id.setdefault(sid, SiteInfo(sid, gsrn, _extract_address(delivery_site)))

        sites: list[SiteInfo] = []
        seen: set[str] = set()
        for sid in site_ids:
            if sid in seen:
                continue
            seen.add(sid)
            # gsrn-only fallback if the site is missing from the contract map.
            sites.append(by_id.get(sid) or SiteInfo(sid, sid, None))
        return sites

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial credentials step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._authenticate(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                self._user_input = user_input
                self._sites = await self.hass.async_add_executor_job(
                    self._collect_sites
                )
            except (
                HelenAuthenticationException,
                TimeoutError,
                ConnectionError,
            ) as ex:
                errors = self._handle_error(ex)
            except Exception:  # noqa: BLE001 - surfaced as cannot_connect
                errors = {"base": "cannot_connect"}
            else:
                _LOGGER.debug(
                    "Collected %d distinct delivery site(s)", len(self._sites)
                )
                if not self._sites:
                    await self._cleanup()
                    return self.async_abort(reason="no_delivery_sites")
                if len(self._sites) == 1:
                    return await self._create_entry(self._sites[0])
                return await self.async_step_select_site()
            await self._cleanup()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_select_site(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick a delivery site when the account has several."""
        if user_input is not None:
            chosen = user_input[CONF_DELIVERY_SITE_ID]
            info = next((s for s in self._sites if s.site_id == chosen), None)
            try:
                await self.hass.async_add_executor_job(
                    self.api_client.select_delivery_site_if_valid_id, chosen
                )
            except InvalidDeliverySiteException as ex:
                return self.async_show_form(
                    step_id="select_site",
                    data_schema=self._site_schema(),
                    errors=self._handle_error(ex),
                )
            return await self._create_entry(info or SiteInfo(chosen, chosen, None))

        return self.async_show_form(
            step_id="select_site", data_schema=self._site_schema()
        )

    def _site_schema(self) -> vol.Schema:
        """Build the delivery-site selection schema with gsrn+address labels."""
        return vol.Schema(
            {
                vol.Required(CONF_DELIVERY_SITE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=s.site_id, label=_site_label(s)
                            )
                            for s in self._sites
                        ],
                        mode="dropdown",
                    )
                )
            }
        )

    async def _create_entry(self, site: SiteInfo) -> FlowResult:
        """Create the config entry for the chosen delivery site."""
        username = self._user_input[CONF_USERNAME]
        unique_id = f"{username.lower()}_{site.site_id}"

        # Close our session first: the coordinator opens its own, and this also
        # releases it cleanly if the unique-id check aborts the flow.
        await self._cleanup()
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        title = site.address or f"Helen Consumption ({site.gsrn})"
        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: username,
                CONF_PASSWORD: self._user_input[CONF_PASSWORD],
                CONF_DELIVERY_SITE_ID: site.site_id,
            },
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
        """Handle re-authentication when the stored token is invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a new password and update the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if entry is None:
                return self.async_abort(reason="reauth_failed")
            try:
                await self._authenticate(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                new_data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            except (
                HelenAuthenticationException,
                TimeoutError,
                ConnectionError,
            ) as ex:
                errors = self._handle_error(ex)
            except Exception:  # noqa: BLE001 - surfaced as cannot_connect
                errors = {"base": "cannot_connect"}
            finally:
                await self._cleanup()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )
