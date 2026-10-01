"""涂鸦本地门锁 - 电量 + 开锁信号/开锁记录传感器。"""

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BROADCAST_HISTORY_MAX,
    CONF_BATTERY_DP,
    CONF_BROADCAST_HISTORY,
    CONF_DEBUG_CAPTURE,
    CONF_DEVICE_ID,
    CONF_DOORBELL_DP,
    CONF_UNLOCK_DP_LIST,
    CONF_UNLOCK_USER_MAP,
    DOMAIN,
)
from .coordinator import TuyaLockCoordinator


def _parse_user_map(raw: str) -> dict[str, str]:
    """解析用户映射配置。支持每行一条或英文逗号/分号分隔，格式：编号=显示名。

    例："1=爸爸（指纹）\n2=爸爸（人脸）" 或 "1=爸爸（指纹）, 2=爸爸（人脸）"
    """
    result: dict[str, str] = {}
    if not raw:
        return result
    # 统一分隔符：换行 / 英文逗号 / 英文分号 都视为条目分隔
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    for chunk in normalized.replace(",", "\n").replace(";", "\n").split("\n"):
        line = chunk.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    battery_dp = coordinator._data.get(CONF_BATTERY_DP, 0)
    if battery_dp:
        entities.append(TuyaLockBattery(coordinator, entry, str(battery_dp)))
    unlock_dps_raw = coordinator._data.get(CONF_UNLOCK_DP_LIST, "")
    unlock_dps = [s.strip() for s in str(unlock_dps_raw).split(",") if s.strip()]
    doorbell_dp = str(coordinator._data.get(CONF_DOORBELL_DP, 0) or 0)
    if doorbell_dp == "0":
        doorbell_dp = ""
    if unlock_dps:
        user_map = _parse_user_map(coordinator._data.get(CONF_UNLOCK_USER_MAP, ""))
        record = TuyaLockLastUnlock(coordinator, entry, unlock_dps, user_map)
        signal = TuyaLockUnlockSignal(
            coordinator, entry, unlock_dps, user_map, record, doorbell_dp
        )
        entities += [signal, record]
    if doorbell_dp:
        entities.append(TuyaLockDoorbell(coordinator, entry, doorbell_dp))
    if coordinator._data.get(CONF_DEBUG_CAPTURE) or coordinator._data.get(
        CONF_BROADCAST_HISTORY
    ):
        entities.append(TuyaLockBroadcastDebug(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaLockBattery(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "电量"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self, coordinator: TuyaLockCoordinator, entry: ConfigEntry, dp: str
    ) -> None:
        super().__init__(coordinator)
        self._dp = dp
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_battery"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._dp)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class TuyaLockUnlockSignal(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """开锁信号（A）：收到解锁 DP 值 → 显示 → 写入 B（开锁记录）→ 重置为待机。

    接收条件：值变化，或门铃唤醒窗口内的抓取（值不变也算——同一人
    重复开锁 DP 值相同）。同一唤醒窗口内 30 秒去重，避免多次刷新重复计数。
    状态自动在 3 秒后重置为"待机"，因此相同值也能产生状态变化，
    自动化可用状态触发器捕获每一次开锁。
    """

    _attr_has_entity_name = True
    _attr_name = "开锁信号"
    _attr_icon = "mdi:gesture-tap-button"
    SIGNAL_RESET_DELAY = 3  # 秒
    WAKE_DEDUP = 4  # 秒，连续开锁间隔 5 秒左右，4 秒可全部计数

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
        unlock_dps: list[str],
        user_map: dict[str, str],
        record: "TuyaLockLastUnlock",
        doorbell_dp: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._unlock_dps = unlock_dps
        self._user_map = user_map
        self._record = record
        self._doorbell_dp = doorbell_dp
        self._signal = "待机"
        self._last_value: Any = None
        self._last_fire_at: datetime | None = None
        self._reset_unsub: Any = None
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_unlock_signal"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> str:
        return self._signal

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        for dp in self._unlock_dps:
            current = data.get(dp)
            if current is None:
                continue
            now = datetime.now()
            value_changed = current != self._last_value
            in_wake = self.coordinator.wake_triggered_within(15)
            dedup_ok = self._last_fire_at is None or (
                now - self._last_fire_at >= timedelta(seconds=self.WAKE_DEDUP)
            )
            # 门铃排除：唤醒窗口内值未变、且门铃 DP 快照为 True →
            # 这次唤醒是门铃造成的，不算开锁
            if (
                in_wake
                and not value_changed
                and self._doorbell_dp
                and (data.get(self._doorbell_dp) is True)
            ):
                break
            if value_changed or (in_wake and dedup_ok):
                self._last_value = current
                self._last_fire_at = now
                raw_str = str(current)
                display = self._user_map.get(raw_str, raw_str)
                self._signal = display
                self.async_write_ha_state()
                self._schedule_reset()
                # 写入 B（开锁记录）
                self._record.record(dp=dp, raw_value=current, display=display)
                # 广播事件：自动化可用事件触发器精确捕获每次开锁
                self.hass.bus.async_fire(
                    f"{DOMAIN}_unlock_event",
                    {"dp": dp, "raw_value": current, "display": display},
                )
            break  # 只取第一个有值的解锁 DP
        super()._handle_coordinator_update()

    @callback
    def _schedule_reset(self) -> None:
        self._cancel_reset()
        self._reset_unsub = async_call_later(
            self.hass, self.SIGNAL_RESET_DELAY, self._async_reset
        )

    @callback
    def _cancel_reset(self) -> None:
        if self._reset_unsub is not None:
            self._reset_unsub()
            self._reset_unsub = None

    @callback
    def _async_reset(self, _now: Any) -> None:
        self._reset_unsub = None
        if self._signal != "待机":
            self._signal = "待机"
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_reset()


class TuyaLockLastUnlock(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """开锁记录（B）：由 A（开锁信号）驱动，每次接收即记录。

    状态 = 最近一次开锁的友好显示（如 "爸爸（指纹）"）
    属性 = dp、raw_value、display、changed_at、count（累计次数）
    """

    _attr_has_entity_name = True
    _attr_name = "最近开锁"
    _attr_icon = "mdi:lock-open-variant"

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
        unlock_dps: list[str],
        user_map: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._unlock_dps = unlock_dps
        self._user_map = user_map
        self._last_event: dict[str, Any] | None = None
        self._count = 0
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_last_unlock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    @callback
    def record(self, dp: str, raw_value: Any, display: str) -> None:
        self._count += 1
        self._last_event = {
            "dp": dp,
            "raw_value": raw_value,
            "display": display,
            "changed_at": datetime.now().isoformat(timespec="seconds"),
            "count": self._count,
        }
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        if self._last_event is None:
            return None
        return str(self._last_event["display"])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._last_event is None:
            return None
        return dict(self._last_event)


class TuyaLockDoorbell(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """门铃信号：门铃 DP 为 True（唤醒窗口内）或值变化时触发，3 秒后重置待机。

    状态 = "待机" ⇄ "门铃"；同时 fire 事件 tuya_local_lock_doorbell_event。
    """

    _attr_has_entity_name = True
    _attr_name = "门铃"
    _attr_icon = "mdi:bell-ring"
    SIGNAL_RESET_DELAY = 3  # 秒
    WAKE_DEDUP = 4  # 秒去重

    def __init__(
        self,
        coordinator: TuyaLockCoordinator,
        entry: ConfigEntry,
        dp: str,
    ) -> None:
        super().__init__(coordinator)
        self._dp = dp
        self._signal = "待机"
        self._last_value: Any = None
        self._last_fire_at: datetime | None = None
        self._reset_unsub: Any = None
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_doorbell"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> str:
        return self._signal

    @callback
    def _handle_coordinator_update(self) -> None:
        current = (self.coordinator.data or {}).get(self._dp)
        if current is not None:
            now = datetime.now()
            value_changed = current != self._last_value
            in_wake = self.coordinator.wake_triggered_within(15)
            dedup_ok = self._last_fire_at is None or (
                now - self._last_fire_at >= timedelta(seconds=self.WAKE_DEDUP)
            )
            is_true = bool(current)
            # 触发：值变为 True，或唤醒窗口内仍为 True（去重后）
            if is_true and ((value_changed and current is True) or (in_wake and dedup_ok)):
                self._last_value = current
                self._last_fire_at = now
                self._signal = "门铃"
                self.async_write_ha_state()
                self._schedule_reset()
                self.hass.bus.async_fire(f"{DOMAIN}_doorbell_event", {"dp": self._dp})
            else:
                self._last_value = current
        super()._handle_coordinator_update()

    @callback
    def _schedule_reset(self) -> None:
        self._cancel_reset()
        self._reset_unsub = async_call_later(
            self.hass, self.SIGNAL_RESET_DELAY, self._async_reset
        )

    @callback
    def _cancel_reset(self) -> None:
        if self._reset_unsub is not None:
            self._reset_unsub()
            self._reset_unsub = None

    @callback
    def _async_reset(self, _now: Any) -> None:
        self._reset_unsub = None
        if self._signal != "待机":
            self._signal = "待机"
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_reset()


class TuyaLockBroadcastDebug(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """广播调试传感器：状态=已捕获条数；属性=最近 N 条广播报文（含解码失败的）。"""

    _attr_has_entity_name = True
    _attr_name = "广播调试"
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator: TuyaLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_broadcast_debug"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    @property
    def native_value(self) -> int:
        if self.coordinator.broadcast_history_enabled:
            return len(self.coordinator.broadcast_history)
        return len(self.coordinator.raw_captures)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.broadcast_history_enabled:
            return {
                "history": self.coordinator.broadcast_history,
                "max": BROADCAST_HISTORY_MAX,
                "persisted": True,
            }
        return {
            "captures": self.coordinator.raw_captures,
            "max": 50,
            "persisted": False,
        }

    @property
    def should_poll(self) -> bool:
        # 数据由 UDP 监听被动接收，coordinator 不感知，需要 HA 周期性拉取属性
        return True

    @property
    def available(self) -> bool:
        # 即使 coordinator 处于失败状态（门锁休眠），调试数据仍可访问
        return True
