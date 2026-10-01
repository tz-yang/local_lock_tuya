import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import (
    CONF_AUTO_RELOCK_DELAY,
    CONF_BATTERY_DP,
    CONF_COMMAND_DP,
    CONF_COMMAND_LOCK_VALUE,
    CONF_COMMAND_UNLOCK_VALUE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IP,
    CONF_DOORBELL_DP,
    CONF_BROADCAST_HISTORY,
    CONF_LOCAL_KEY,
    CONF_OPEN_DP,
    CONF_OPEN_VALUE,
    CONF_POLL_INTERVAL,
    CONF_PRODUCT_ID,
    CONF_PROTOCOL,
    CONF_STATE_DP,
    CONF_STATE_TRUE_IS_LOCKED,
    CONF_UNLOCK_DP_LIST,
    CONF_UNLOCK_USER_MAP,
    CONF_UUID,
    CONF_DEBUG_CAPTURE,
    DEFAULT_AUTO_RELOCK_DELAY,
    DEFAULT_BATTERY_DP,
    DEFAULT_COMMAND_DP,
    DEFAULT_COMMAND_LOCK_VALUE,
    DEFAULT_COMMAND_UNLOCK_VALUE,
    DEFAULT_OPEN_VALUE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROTOCOL,
    DEFAULT_STATE_DP,
    DEFAULT_STATE_TRUE_IS_LOCKED,
    DOMAIN,
    PROTOCOLS,
)
from .coordinator import CannotConnect, NoDeviceFound, validate_connection

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_DEVICE_ID): str,
        vol.Required(CONF_LOCAL_KEY): str,
        vol.Optional(CONF_DEVICE_IP, default=""): str,
        vol.Optional(CONF_UUID, default=""): str,
        vol.Optional(CONF_PRODUCT_ID, default=""): str,
        vol.Required(CONF_PROTOCOL, default=DEFAULT_PROTOCOL): vol.In(PROTOCOLS),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_STATE_DP, default=DEFAULT_STATE_DP): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Required(
            CONF_STATE_TRUE_IS_LOCKED, default=DEFAULT_STATE_TRUE_IS_LOCKED
        ): bool,
        vol.Required(CONF_COMMAND_DP, default=DEFAULT_COMMAND_DP): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Required(
            CONF_COMMAND_LOCK_VALUE, default=DEFAULT_COMMAND_LOCK_VALUE
        ): bool,
        vol.Required(
            CONF_COMMAND_UNLOCK_VALUE, default=DEFAULT_COMMAND_UNLOCK_VALUE
        ): bool,
        vol.Required(CONF_OPEN_DP, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Required(CONF_OPEN_VALUE, default=DEFAULT_OPEN_VALUE): bool,
        vol.Required(CONF_BATTERY_DP, default=DEFAULT_BATTERY_DP): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional(CONF_UNLOCK_DP_LIST, default=""): str,
        vol.Optional(CONF_UNLOCK_USER_MAP, default=""): TextSelector(
            TextSelectorConfig(multiline=True, type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_DOORBELL_DP, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Required(
            CONF_AUTO_RELOCK_DELAY, default=DEFAULT_AUTO_RELOCK_DELAY
        ): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=5)
        ),
        vol.Optional(CONF_DEBUG_CAPTURE, default=False): bool,
        vol.Optional(CONF_BROADCAST_HISTORY, default=False): bool,
    }
)


class TuyaLocalLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                ip = await validate_connection(self.hass, user_input)
            except NoDeviceFound:
                errors["base"] = "no_device_found"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("配置流程发生未处理异常")
                errors["base"] = "cannot_connect"
            else:
                data = dict(user_input)
                data[CONF_DEVICE_IP] = ip
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME], data=data)
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return TuyaLocalLockOptionsFlow()


class TuyaLocalLockOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
