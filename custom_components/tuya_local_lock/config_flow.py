import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
    CONF_TCP_PUSH_PROBE,
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
    CONF_WAKE_REFRESH_DELAY,
    CONF_WAKE_REFRESH_RETRY,
    CONF_EXTERNAL_STATE_ENTITY,
    CONF_EXTERNAL_STATE_INVERT,
    CONF_MANUAL_UNLOCK_RECORD,
    CONF_MANUAL_UNLOCK_NAME,
    CONF_MANUAL_UNLOCK_WINDOW,
    CONF_SESSION_TIMEOUT,
    DEFAULT_AUTO_RELOCK_DELAY,
    DEFAULT_BATTERY_DP,
    DEFAULT_COMMAND_DP,
    DEFAULT_COMMAND_LOCK_VALUE,
    DEFAULT_COMMAND_UNLOCK_VALUE,
    DEFAULT_MANUAL_UNLOCK_NAME,
    DEFAULT_MANUAL_UNLOCK_WINDOW,
    DEFAULT_OPEN_VALUE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROTOCOL,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_STATE_DP,
    DEFAULT_STATE_TRUE_IS_LOCKED,
    DEFAULT_WAKE_REFRESH_DELAY,
    DEFAULT_WAKE_REFRESH_RETRY,
    DOMAIN,
    PROTOCOLS,
)
from .coordinator import CannotConnect, NoDeviceFound, validate_connection
from .tuya_sharing_auth import (
    TuyaSharingError,
    async_fetch_devices,
    async_mint_qr_token,
    async_poll_login,
    qr_png_b64,
)

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
        vol.Required(
            CONF_WAKE_REFRESH_DELAY, default=DEFAULT_WAKE_REFRESH_DELAY
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        vol.Required(
            CONF_WAKE_REFRESH_RETRY, default=DEFAULT_WAKE_REFRESH_RETRY
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=30)),
        vol.Optional(CONF_DEBUG_CAPTURE, default=False): bool,
        vol.Optional(CONF_BROADCAST_HISTORY, default=False): bool,
        vol.Optional(CONF_TCP_PUSH_PROBE, default=False): bool,
        vol.Optional(CONF_EXTERNAL_STATE_ENTITY, default=""): str,
        vol.Optional(CONF_EXTERNAL_STATE_INVERT, default=False): bool,
        vol.Optional(CONF_MANUAL_UNLOCK_RECORD, default=True): bool,
        vol.Optional(
            CONF_MANUAL_UNLOCK_NAME, default=DEFAULT_MANUAL_UNLOCK_NAME
        ): str,
        vol.Required(
            CONF_MANUAL_UNLOCK_WINDOW, default=DEFAULT_MANUAL_UNLOCK_WINDOW
        ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=10)),
        vol.Required(
            CONF_SESSION_TIMEOUT, default=DEFAULT_SESSION_TIMEOUT
        ): vol.All(vol.Coerce(float), vol.Range(min=1, max=30)),
    }
)


class TuyaLocalLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._qr_token: str | None = None
        self._qr_user_code: str = ""
        self._qr_error: str = ""
        self._session: dict[str, Any] | None = None
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ):
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "qr"],
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ):
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
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=data
                )
        return self.async_show_form(
            step_id="manual", data_schema=USER_SCHEMA, errors=errors
        )

    # ---- 扫码登录流程 ----

    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ):
        """输入 Smart Life 用户码，申请二维码。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_code = (user_input.get("user_code") or "").strip()
            if not user_code:
                errors["user_code"] = "required"
            else:
                try:
                    self._qr_token = await async_mint_qr_token(self.hass, user_code)
                except TuyaSharingError as exc:
                    errors["base"] = "qr_failed"
                    _LOGGER.warning("申请二维码失败: %s", exc)
                    self._qr_error = str(exc)
                else:
                    self._qr_user_code = user_code
                    return await self.async_step_qr_show()
        return self.async_show_form(
            step_id="qr",
            data_schema=vol.Schema({vol.Required("user_code"): str}),
            errors=errors,
            description_placeholders={"detail": self._qr_error},
        )

    async def async_step_qr_show(
        self, user_input: dict[str, Any] | None = None
    ):
        """显示二维码，用户扫码确认后轮询登录结果。"""
        errors: dict[str, str] = {}
        if user_input is not None and self._qr_token:
            session = await async_poll_login(
                self.hass, self._qr_token, self._qr_user_code
            )
            if session:
                self._session = session
                try:
                    self._devices = await async_fetch_devices(self.hass, session)
                except TuyaSharingError:
                    errors["base"] = "fetch_devices_failed"
                else:
                    if self._devices:
                        return await self.async_step_select_device()
                    errors["base"] = "no_devices"
            else:
                errors["base"] = "login_not_confirmed"

        if self._qr_token:
            qr_b64 = qr_png_b64(self._qr_token)
            qr_html = f'<img src="{qr_b64}" style="max-width:100%"/>'
        else:
            qr_html = ""

        return self.async_show_form(
            step_id="qr_show",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"qr_image": qr_html},
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ):
        """选择设备，自动填入 device_id / local_key。"""
        errors: dict[str, str] = {}
        options = [
            SelectOptionDict(
                value=d["id"],
                label=f'{d["name"]} ({"在线" if d["online"] else "离线"})',
            )
            for d in self._devices
            if d.get("id")
        ]
        if user_input is not None:
            dev_id = user_input.get("device_id")
            dev = next((d for d in self._devices if d["id"] == dev_id), None)
            if dev:
                name = user_input.get(CONF_NAME) or dev["name"] or dev_id
                data = {
                    CONF_NAME: name,
                    CONF_DEVICE_ID: dev["id"],
                    CONF_LOCAL_KEY: dev["local_key"],
                    CONF_DEVICE_IP: "",
                    CONF_UUID: "",
                    CONF_PRODUCT_ID: dev.get("product_id", ""),
                    CONF_PROTOCOL: DEFAULT_PROTOCOL,
                }
                try:
                    ip = await validate_connection(self.hass, data)
                except NoDeviceFound:
                    errors["base"] = "no_device_found"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("配置流程发生未处理异常")
                    errors["base"] = "cannot_connect"
                else:
                    data[CONF_DEVICE_IP] = ip
                    await self.async_set_unique_id(dev["id"])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=name, data=data)

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Optional(CONF_NAME, default=""): str,
                }
            ),
            errors=errors,
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
