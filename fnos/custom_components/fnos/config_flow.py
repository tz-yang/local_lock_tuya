"""fnOS 配置流程。"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_HOST,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class FnosConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """fnOS 配置流程。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """处理用户输入。"""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_HOST):
                errors[CONF_HOST] = "required"
            if not user_input.get(CONF_USERNAME):
                errors[CONF_USERNAME] = "required"
            if not user_input.get(CONF_PASSWORD):
                errors[CONF_PASSWORD] = "required"

            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"fnOS ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_MAC, default=""): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "FnosOptionsFlow":
        return FnosOptionsFlow()


class FnosOptionsFlow(config_entries.OptionsFlow):
    """fnOS 选项流程。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """处理选项。"""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # 默认值优先取 options（上次保存的选项），其次 data（初始配置）
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        data_schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): int,
            }
        )

        return self.async_show_form(step_id="init", data_schema=data_schema)
