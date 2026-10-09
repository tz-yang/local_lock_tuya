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
    CONF_TCP_PUSH_PROBE,
    CONF_UNLOCK_DP_LIST,
    CONF_UNLOCK_USER_MAP,
    DOMAIN,
    TCP_PUSH_FRAMES_MAX,
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
        # 注册给 coordinator，门磁 FSM 确认机械开门时写入同一份记录
        coordinator.set_unlock_record(record)
        signal = TuyaLockUnlockSignal(
            coordinator, entry, unlock_dps, user_map, record, doorbell_dp
        )
        entities += [signal, record]
    if doorbell_dp:
        entities.append(TuyaLockDoorbell(coordinator, entry, doorbell_dp))
    debug_any = (
        coordinator._data.get(CONF_DEBUG_CAPTURE)
        or coordinator._data.get(CONF_BROADCAST_HISTORY)
        or coordinator._data.get(CONF_TCP_PUSH_PROBE)
    )
    if debug_any:
        entities.append(TuyaLockBroadcastDebug(coordinator, entry))
        entities.append(TuyaLockBroadcastPlain(coordinator, entry))
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

    触发条件：
    - DP 值变化：始终触发；
    - DP 值不变（同一人重复开锁值相同）：仅当处于【新的唤醒会话】
      （距上次广播安静 WAKE_SESSION_GAP 秒后的再次唤醒）或距上次触发后
      门磁经历过【关门边沿】时才触发。
    这样同一次开锁唤醒期内的多次 TCP 抓取只计一次，而门关后的连续
    真实开锁（即使值完全相同）不会被漏掉。
    状态自动在 3 秒后重置为"待机"，自动化可用状态触发器捕获每次开锁。
    """

    _attr_has_entity_name = True
    _attr_name = "开锁信号"
    _attr_icon = "mdi:gesture-tap-button"
    SIGNAL_RESET_DELAY = 3  # 秒

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
        # 上次触发时的唤醒会话 ID；None = 尚未触发过
        self._last_fire_session_id: int | None = None
        # 上次触发时已知的最后关门时间，用于识别之后是否新关过门
        self._last_seen_closed_at: datetime | None = None
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
            value_changed = current != self._last_value
            in_wake = self.coordinator.wake_triggered_within(15)
            session_id = self.coordinator.wake_session_id
            new_session = (
                self._last_fire_session_id is not None
                and session_id != self._last_fire_session_id
            )
            last_closed = self.coordinator.last_door_closed_at
            door_closed_since = last_closed is not None and (
                self._last_seen_closed_at is None
                or last_closed > self._last_seen_closed_at
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
            if value_changed or (in_wake and (new_session or door_closed_since)):
                self._last_value = current
                self._last_fire_session_id = session_id
                self._last_seen_closed_at = last_closed
                raw_str = str(current)
                display = self._user_map.get(raw_str, raw_str)
                self._signal = display
                self.async_write_ha_state()
                self._schedule_reset()
                # 写入 B（开锁记录）
                self._record.record(dp=dp, raw_value=current, display=display)
                # 通知门磁 FSM：这是一次电子开锁（指纹/密码/App/室内按钮）
                self.coordinator.notify_electronic_unlock()
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
        if self.coordinator.tcp_push_probe_enabled:
            return len(self.coordinator.tcp_push_frames)
        return len(self.coordinator.raw_captures)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any]
        if self.coordinator.broadcast_history_enabled:
            attrs = {
                # 存储为 append 正序，显示时反转 → 最新记录在上
                "history": list(reversed(self.coordinator.broadcast_history)),
                "max": BROADCAST_HISTORY_MAX,
                "persisted": True,
            }
        else:
            attrs = {
                "captures": self.coordinator.raw_captures,
                "max": 50,
                "persisted": False,
            }
        if self.coordinator.tcp_push_probe_enabled:
            attrs["tcp_push_frames"] = list(
                reversed(self.coordinator.tcp_push_frames)
            )
            attrs["tcp_push_max"] = TCP_PUSH_FRAMES_MAX
            attrs["tcp_probe_running"] = self.coordinator.tcp_probe_running
        return attrs

    @property
    def should_poll(self) -> bool:
        # 数据由 UDP 监听被动接收，coordinator 不感知，需要 HA 周期性拉取属性
        return True

    async def async_update(self) -> None:
        """空操作：覆写 CoordinatorEntity.async_update。

        父类默认实现会触发 coordinator 发起 TCP 状态刷新，
        门锁休眠时每次轮询都白等 10 秒超时并刷警告日志。
        调试数据由 UDP 被动接收，轮询只需重读内存属性即可。
        """

    @property
    def available(self) -> bool:
        # 即使 coordinator 处于失败状态（门锁休眠），调试数据仍可访问
        return True


def _summarize_payload(payload: dict[str, Any]) -> str:
    """把解码后的广播内容压缩成一条短状态（HA 状态值上限 255 字符）。"""
    if not isinstance(payload, dict):
        return "无法摘要"
    dps = payload.get("dps")
    if isinstance(dps, dict) and dps:
        parts = [f"{k}={v}" for k, v in dps.items()]
        return "dps: " + ", ".join(parts)
    active = payload.get("active")
    version = payload.get("version")
    if active is not None:
        return f"心跳 active={active} version={version}"
    # 其他结构（上线通告等），列出顶层键
    keys = ",".join(k for k in payload if k not in ("ip", "gwId", "productKey"))
    return f"通告 {keys}" if keys else "通告"


class TuyaLockBroadcastPlain(CoordinatorEntity[TuyaLockCoordinator], SensorEntity):
    """广播明文传感器：只存放解密后的内容，不含 hex。"""

    _attr_has_entity_name = True
    _attr_name = "广播明文"
    _attr_icon = "mdi:text-box-check-outline"

    def __init__(self, coordinator: TuyaLockCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_broadcast_plain"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
        )

    def _decoded_rows(self) -> list[dict[str, Any]]:
        """从历史或内存缓冲中提取【本设备且解码成功】的记录（最新在前）。"""
        if self.coordinator.broadcast_history_enabled:
            source = self.coordinator.broadcast_history  # append 正序
        else:
            # raw_captures 为 appendleft 倒序（最新在前）
            source = self.coordinator.raw_captures
        rows: list[dict[str, Any]] = []
        for item in source:
            decoded = item.get("decoded")
            if item.get("is_mine") and isinstance(decoded, dict):
                rows.append(
                    {
                        "ts": item.get("ts"),
                        "port": item.get("port"),
                        "payload": decoded,
                    }
                )
        if self.coordinator.broadcast_history_enabled:
            rows.reverse()  # 存储正序 → 显示最新在前
        return rows

    @property
    def native_value(self) -> str:
        rows = self._decoded_rows()
        if rows:
            return _summarize_payload(rows[0]["payload"])
        push = self.coordinator.tcp_push_frames
        if push:
            frame = push[-1].get("frame")
            if isinstance(frame, dict) and "dps" in frame:
                return _summarize_payload(frame)
        return "暂无解密数据"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rows = self._decoded_rows()
        attrs: dict[str, Any] = {
            "decoded": rows,
            "decoded_count": len(rows),
            "persisted": self.coordinator.broadcast_history_enabled,
        }
        if self.coordinator.tcp_push_probe_enabled:
            # TCP 推送帧已由 tinytuya 解密，同样属于明文数据
            attrs["tcp_push_frames"] = self.coordinator.tcp_push_frames
            attrs["tcp_probe_running"] = self.coordinator.tcp_probe_running
        return attrs

    @property
    def should_poll(self) -> bool:
        return True

    async def async_update(self) -> None:
        """空操作：同广播调试，避免轮询触发 coordinator 的 TCP 刷新。"""

    @property
    def available(self) -> bool:
        return True
