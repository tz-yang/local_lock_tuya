"""涂鸦本地门锁 - 锁实体。"""

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_AUTO_RELOCK_DELAY,
    CONF_COMMAND_DP,
    CONF_COMMAND_LOCK_VALUE,
    CONF_COMMAND_UNLOCK_VALUE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IP,
    CONF_OPEN_DP,
    CONF_OPEN_VALUE,
    CONF_PRODUCT_ID,
    CONF_PROTOCOL,
    CONF_STATE_DP,
    CONF_STATE_TRUE_IS_LOCKED,
    CONF_UNLOCK_DP_LIST,
    DOMAIN,
    WAKE_OPERABLE_WINDOW,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TuyaLocalLock(coordinator, entry)])


class TuyaLocalLock(CoordinatorEntity[TuyaLockCoordinator], LockEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: TuyaLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        opts = coordinator._data
        self._entry = entry
        self._state_dp = str(opts.get(CONF_STATE_DP, 0))
        self._state_true_is_locked = opts.get(CONF_STATE_TRUE_IS_LOCKED, True)
        self._command_dp = str(opts.get(CONF_COMMAND_DP, 1))
        self._command_lock_value = opts.get(CONF_COMMAND_LOCK_VALUE, True)
        self._command_unlock_value = opts.get(CONF_COMMAND_UNLOCK_VALUE, False)
        self._open_dp = opts.get(CONF_OPEN_DP, 0)
        self._open_value = opts.get(CONF_OPEN_VALUE, True)
        unlock_dps = opts.get(CONF_UNLOCK_DP_LIST, "")
        self._unlock_dps: list[str] = (
            [s.strip() for s in str(unlock_dps).split(",") if s.strip()]
            if unlock_dps
            else []
        )
        self._auto_relock_delay = opts.get(CONF_AUTO_RELOCK_DELAY, 0)
        self._unlock_relock_timer = None
        self._last_seen_unlock_dps: dict[str, Any] = {}
        self._attr_unique_id = entry.data[CONF_DEVICE_ID]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
            model=entry.data.get(CONF_PRODUCT_ID) or None,
        )
        if self._open_dp:
            self._attr_supported_features = LockEntityFeature.OPEN

    @property
    def is_locked(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        # 优先使用显式状态 DP（如反锁状态）
        if self._state_dp and self._state_dp != "0":
            value = self.coordinator.data.get(self._state_dp)
            if value is not None:
                return bool(value) == self._state_true_is_locked
        # 事件型门锁：通过解锁 DP 的值变化推断状态
        # （注意：不能用唤醒窗口判断，否则门铃响也会误报"已解锁"）
        if self._unlock_dps:
            for dp in self._unlock_dps:
                current = self.coordinator.data.get(dp)
                if current is None:
                    continue
                last = self._last_seen_unlock_dps.get(dp)
                if last is not None and current != last:
                    # 解锁事件发生 → 门已开
                    self._last_seen_unlock_dps[dp] = current
                    self._schedule_auto_relock()
                    # 主动通知 HA 状态已更新
                    self.async_write_ha_state()
                    return False
                self._last_seen_unlock_dps[dp] = current
        # 无明确状态 → 保持上次状态（默认认为已锁）
        return True

    def _schedule_auto_relock(self) -> None:
        if self._unlock_relock_timer is not None:
            self._unlock_relock_timer()
            self._unlock_relock_timer = None
        if self._auto_relock_delay and self._auto_relock_delay > 0:
            self._unlock_relock_timer = self.hass.helpers.event.async_call_later(
                self._auto_relock_delay, self._auto_relock
            )

    async def _auto_relock(self, _now: Any) -> None:
        self._unlock_relock_timer = None
        self.async_write_ha_state()

    @property
    def operable(self) -> bool:
        """门锁是否处于可操作窗口（门铃唤醒后 WAKE_OPERABLE_WINDOW 秒内）。"""
        return self.coordinator.wake_triggered_within(WAKE_OPERABLE_WINDOW)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {
            "dps": self.coordinator.data,
            "ip": self._entry.data.get(CONF_DEVICE_IP),
            "protocol": self._entry.data.get(CONF_PROTOCOL),
            "operable": self.operable,
        }
        if self._unlock_dps and self.coordinator.data:
            for dp in self._unlock_dps:
                val = self.coordinator.data.get(dp)
                if val is not None:
                    attrs[f"unlock_dp_{dp}"] = val
        return attrs

    def _ensure_operable(self) -> None:
        """门锁平时休眠，只有门铃唤醒窗口内才接受网络指令。"""
        if not self.operable:
            raise HomeAssistantError(
                "门锁休眠中，无法网络操作。请先按门铃唤醒，"
                f"然后在 {WAKE_OPERABLE_WINDOW} 秒内执行开锁。"
            )

    async def async_lock(self, **kwargs: Any) -> None:
        self._ensure_operable()
        await self.coordinator.async_send_command(
            {self._command_dp: self._command_lock_value}
        )
        await self.coordinator.async_request_refresh()

    async def async_unlock(self, **kwargs: Any) -> None:
        self._ensure_operable()
        await self.coordinator.async_send_command(
            {self._command_dp: self._command_unlock_value}
        )
        await self.coordinator.async_request_refresh()

    async def async_open_door(self, **kwargs: Any) -> None:
        self._ensure_operable()
        await self.coordinator.async_send_command(
            {str(self._open_dp): self._open_value}
        )
        await self.coordinator.async_request_refresh()
