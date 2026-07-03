"""Config flow for the Helen Energy Consumption integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import (
    HelenAuthenticationException,
    InvalidDeliverySiteException,
)
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector

from .const import CONF_CUSTOM_NAME, CONF_DELIVERY_SITE_ID, DOMAIN

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)


class HelenConsumptionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Helen Energy Consumption."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.api_client: HelenApiClient | None = None
        self._user_input: dict[str, Any] | None = None
        self._gsrn_ids: list[str] = []

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
                self._gsrn_ids = [
                    str(g)
                    for g in await self.hass.async_add_executor_job(
                        self.api_client.get_all_gsrn_ids
                    )
                ]
                if len(self._gsrn_ids) > 1:
                    self._user_input = user_input
                    return await self.async_step_select_site()
                return await self._create_entry(user_input)
            except (
                HelenAuthenticationException,
                InvalidDeliverySiteException,
                TimeoutError,
                ConnectionError,
            ) as ex:
                errors = self._handle_error(ex)
            except Exception:  # noqa: BLE001 - surfaced as cannot_connect
                errors = {"base": "cannot_connect"}
            await self._cleanup()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_CUSTOM_NAME): str,
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
            return await self._create_entry(
                {**self._user_input, CONF_DELIVERY_SITE_ID: chosen}
            )

        return self.async_show_form(
            step_id="select_site", data_schema=self._site_schema()
        )

    def _site_schema(self) -> vol.Schema:
        """Build the delivery-site (GSRN) selection schema."""
        return vol.Schema(
            {
                vol.Required(CONF_DELIVERY_SITE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=g, label=g)
                            for g in self._gsrn_ids
                        ],
                        mode="dropdown",
                    )
                )
            }
        )

    async def _create_entry(self, user_input: dict[str, Any]) -> FlowResult:
        """Create the config entry from validated input."""
        username = user_input[CONF_USERNAME]
        delivery_site_id = user_input.get(CONF_DELIVERY_SITE_ID)

        if delivery_site_id:
            unique_id = f"{username.lower()}_{delivery_site_id}"
        else:
            unique_id = username.lower()
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        title = user_input.get(CONF_CUSTOM_NAME, "").strip() or "Helen Consumption"
        if delivery_site_id:
            title = f"{title} ({delivery_site_id})"

        data = {
            CONF_USERNAME: username,
            CONF_PASSWORD: user_input[CONF_PASSWORD],
        }
        if delivery_site_id:
            data[CONF_DELIVERY_SITE_ID] = delivery_site_id

        # The coordinator opens its own session, so close this one.
        await self._cleanup()
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
        """Handle re-authentication when the stored token is invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a new password and update the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(
                self.context["entry_id"]
            )
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
