"""涂鸦本地门锁 - 网络开锁按钮。

与锁实体职责分离：锁实体只负责显示/记录状态；本按钮只负责触发开锁动作。
按钮仅在门铃唤醒窗口（WAKE_OPERABLE_WINDOW 秒）内可用，窗口结束自动禁用。
ButtonEntity 无状态历史，频繁 available/unavailable 不产生历史断点。
"""

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COMMAND_DP,
    CONF_COMMAND_UNLOCK_VALUE,
    CONF_DEVICE_ID,
    CONF_PRODUCT_ID,
    DOMAIN,
    WAKE_OPERABLE_WINDOW,
)
from .coordinator import TuyaLockCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaLockRemoteUnlockButton(coordinator, entry)])


class TuyaLockRemoteUnlockButton(
    CoordinatorEntity[TuyaLockCoordinator], ButtonEntity
):
    """网络开锁按钮：available 跟随门铃唤醒窗口。"""

    _attr_has_entity_name = True
    _attr_name = "网络开锁"
    _attr_icon = "mdi:lock-open-variant-outline"

    def __init__(
        self, coordinator: TuyaLockCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        opts = coordinator._data
        self._entry = entry
        self._command_dp = str(opts.get(CONF_COMMAND_DP, 1))
        self._command_unlock_value = opts.get(CONF_COMMAND_UNLOCK_VALUE, False)
        # 是否处于唤醒可操作窗口（初始为 False，非门铃状态按钮禁用）
        self._in_window = False
        self._expire_unsub: Any = None
        self._attr_unique_id = (
            f"{entry.data[CONF_DEVICE_ID]}_remote_unlock_button"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
            model=entry.data.get(CONF_PRODUCT_ID) or None,
        )

    @property
    def available(self) -> bool:
        # 完全由唤醒窗口决定，不继承 coordinator.last_update_success：
        # 收到唤醒广播即证明门锁此刻在线可操作，即使上次轮询失败
        return self._in_window

    @callback
    def _handle_coordinator_update(self) -> None:
        triggered_at = self.coordinator.wake_triggered_at
        if triggered_at is not None and self.coordinator.wake_triggered_within(
            WAKE_OPERABLE_WINDOW
        ):
            elapsed = (dt_util.utcnow() - triggered_at).total_seconds()
            remaining = max(WAKE_OPERABLE_WINDOW - elapsed, 0.05)
            self._set_in_window(True)
            # 按本次窗口剩余时间重新安排到期（新广播会重置窗口）
            self._cancel_expire()
            self._expire_unsub = async_call_later(
                self.hass, remaining, self._async_expire
            )
        elif self._expire_unsub is None:
            # 窗口外的普通轮询，且无待执行到期计时
            self._set_in_window(False)
        super()._handle_coordinator_update()

    @callback
    def _set_in_window(self, value: bool) -> None:
        if self._in_window != value:
            self._in_window = value
            self.async_write_ha_state()

    @callback
    def _cancel_expire(self) -> None:
        if self._expire_unsub is not None:
            self._expire_unsub()
            self._expire_unsub = None

    async def _async_expire(self, _now: Any) -> None:
        """唤醒窗口结束：按钮自动禁用。"""
        self._expire_unsub = None
        self._set_in_window(False)

    async def async_press(self, **kwargs: Any) -> None:
        # 防御性二次校验：窗口可能在按下瞬间刚结束
        if not self.coordinator.wake_triggered_within(WAKE_OPERABLE_WINDOW):
            raise HomeAssistantError(
                "门锁休眠中，无法网络开锁。请先按门铃唤醒，"
                f"然后在 {WAKE_OPERABLE_WINDOW} 秒内再按此按钮。"
            )
        await self.coordinator.async_send_command(
            {self._command_dp: self._command_unlock_value}
        )
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_expire()
        await super().async_will_remove_from_hass()
