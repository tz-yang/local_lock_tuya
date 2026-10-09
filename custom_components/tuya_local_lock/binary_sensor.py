"""涂鸦本地门锁 - 门锁状态传感器（binary_sensor，device_class: lock）。

只负责显示/记录门锁状态，为只读实体：on = 已解锁，off = 已锁定。
开锁动作由 button.网络开锁 承担（仅门铃唤醒窗口内可用）。
门磁 FSM、外部状态同步、set_lock_state 服务均保留。
"""

import logging
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_ON
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_AUTO_RELOCK_DELAY,
    CONF_DEVICE_ID,
    CONF_DEVICE_IP,
    CONF_EXTERNAL_STATE_ENTITY,
    CONF_EXTERNAL_STATE_INVERT,
    CONF_MANUAL_UNLOCK_NAME,
    CONF_MANUAL_UNLOCK_RECORD,
    CONF_MANUAL_UNLOCK_WINDOW,
    CONF_PRODUCT_ID,
    CONF_PROTOCOL,
    CONF_SESSION_TIMEOUT,
    CONF_STATE_DP,
    CONF_STATE_TRUE_IS_LOCKED,
    CONF_UNLOCK_DP_LIST,
    DEFAULT_MANUAL_UNLOCK_NAME,
    DEFAULT_MANUAL_UNLOCK_WINDOW,
    DEFAULT_SESSION_TIMEOUT,
    DOOR_DEBOUNCE_SECONDS,
    DOMAIN,
    WAKE_OPERABLE_WINDOW,
)
from .coordinator import TuyaLockCoordinator

_LOGGER = logging.getLogger(__name__)

# 门磁 FSM 状态
FSM_ARMED = "armed"              # 门关，等待机械开门判定
FSM_PENDING = "pending"          # 门刚开，竞态窗口内等待电子开锁信号
FSM_ELECTRONIC = "electronic"    # 电子开锁会话，关门后才重新武装
FSM_MECHANICAL = "mechanical"    # 已确认室内机械开门

SERVICE_SET_LOCK_STATE = "set_lock_state"
ATTR_LOCKED = "locked"
SET_LOCK_STATE_SCHEMA = vol.Schema({vol.Required(ATTR_LOCKED): bool})

# 记录所有已注册的门锁状态实体（按 device_id 索引），供服务调用定位目标
_registered_entities: dict[str, "TuyaLockStateBinarySensor"] = {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TuyaLockCoordinator = hass.data[DOMAIN][entry.entry_id]
    entity = TuyaLockStateBinarySensor(coordinator, entry)
    async_add_entities([entity])

    # 注册服务：允许自动化手动设置门锁显示状态（不发送物理指令）
    if not hass.services.has_service(DOMAIN, SERVICE_SET_LOCK_STATE):

        def _handle_set_lock_state(call: ServiceCall) -> None:
            locked = call.data[ATTR_LOCKED]
            # 可选指定目标设备；未指定则作用于所有门锁状态实体
            target = call.data.get("device_id")
            targets = (
                [_registered_entities[target]]
                if target and target in _registered_entities
                else list(_registered_entities.values())
            )
            for ent in targets:
                ent.set_display_lock_state(locked)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_LOCK_STATE,
            _handle_set_lock_state,
            schema=SET_LOCK_STATE_SCHEMA.extend(
                {vol.Optional("device_id"): str}
            ),
        )


class TuyaLockStateBinarySensor(
    CoordinatorEntity[TuyaLockCoordinator], BinarySensorEntity
):
    """门锁状态：on = 已锁定，off = 已解锁（只读）。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(
        self, coordinator: TuyaLockCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        opts = coordinator._data
        self._entry = entry
        self._state_dp = str(opts.get(CONF_STATE_DP, 0))
        self._state_true_is_locked = opts.get(CONF_STATE_TRUE_IS_LOCKED, True)
        unlock_dps = opts.get(CONF_UNLOCK_DP_LIST, "")
        self._unlock_dps: list[str] = (
            [s.strip() for s in str(unlock_dps).split(",") if s.strip()]
            if unlock_dps
            else []
        )
        self._auto_relock_delay = opts.get(CONF_AUTO_RELOCK_DELAY, 0)
        self._unlock_relock_timer: Callable[[], None] | None = None
        self._last_seen_unlock_dps: dict[str, Any] = {}
        # 持久化解锁状态：解锁事件触发后置 True，自动回锁定时器到期后置 False
        self._unlocked = False
        # 外部状态来源（门磁等），优先级高于门锁自身推断
        self._external_state_entity: str = opts.get(
            CONF_EXTERNAL_STATE_ENTITY, ""
        )
        self._external_state_invert: bool = opts.get(
            CONF_EXTERNAL_STATE_INVERT, False
        )
        self._external_unsub: Callable[[], None] | None = None
        # 门磁 FSM：机械开门识别与记录
        self._manual_enabled: bool = opts.get(CONF_MANUAL_UNLOCK_RECORD, True)
        self._manual_name: str = (
            opts.get(CONF_MANUAL_UNLOCK_NAME) or DEFAULT_MANUAL_UNLOCK_NAME
        )
        self._race_window: float = float(
            opts.get(CONF_MANUAL_UNLOCK_WINDOW, DEFAULT_MANUAL_UNLOCK_WINDOW)
        )
        self._session_timeout: float = float(
            opts.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
        )
        self._fsm: str = FSM_ARMED
        self._door_open: bool = False
        self._debounce_unsub: Callable[[], None] | None = None
        self._race_unsub: Callable[[], None] | None = None
        self._session_unsub: Callable[[], None] | None = None
        self._remove_electronic_listener: Callable[[], None] | None = None
        self._attr_unique_id = entry.data[CONF_DEVICE_ID]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.data[CONF_NAME],
            manufacturer="Tuya",
            model=entry.data.get(CONF_PRODUCT_ID) or None,
        )
        # 注册到全局表，供服务调用定位
        _registered_entities[entry.data[CONF_DEVICE_ID]] = self

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 订阅外部门磁状态变化，强制同步门锁显示状态
        if self._external_state_entity:
            self._external_unsub = async_track_state_change(
                self.hass,
                self._external_state_entity,
                self._async_external_state_changed,
            )
            # 初始化时读取一次当前状态。
            # 先设定门状态基线，启动瞬间不产生跳变事件（防止误记机械开门）
            state = self.hass.states.get(self._external_state_entity)
            if state is not None:
                is_open = self._compute_is_open(state.state)
                if is_open is not None:
                    self._door_open = is_open
                self._apply_external_state(state.state)
            # 订阅电子开锁事件，驱动门磁 FSM
            if self._manual_enabled:
                self._remove_electronic_listener = (
                    self.coordinator.add_electronic_unlock_listener(
                        self._on_electronic_unlock
                    )
                )

    async def async_will_remove_from_hass(self) -> None:
        if self._external_unsub is not None:
            self._external_unsub()
            self._external_unsub = None
        if self._remove_electronic_listener is not None:
            self._remove_electronic_listener()
            self._remove_electronic_listener = None
        self._cancel_timer(self._debounce_unsub)
        self._debounce_unsub = None
        self._cancel_timer(self._race_unsub)
        self._race_unsub = None
        self._cancel_timer(self._session_unsub)
        self._session_unsub = None
        if self._unlock_relock_timer is not None:
            self._unlock_relock_timer()
            self._unlock_relock_timer = None
        _registered_entities.pop(self._entry.data[CONF_DEVICE_ID], None)
        await super().async_will_remove_from_hass()

    @callback
    def _async_external_state_changed(
        self, entity_id: str, old_state: Any, new_state: Any
    ) -> None:
        if new_state is None:
            return
        self._apply_external_state(new_state.state)

    def _compute_is_open(self, state_str: str | None) -> bool | None:
        """门磁状态 → 门是否开着（应用反转）。无效状态返回 None。"""
        if state_str in ("unknown", "unavailable", None):
            return None
        is_open = state_str == STATE_ON
        if self._external_state_invert:
            is_open = not is_open
        return is_open

    def _apply_external_state(self, state_str: str) -> None:
        """根据外部门磁状态强制更新门锁显示状态。

        默认：on = 门开 = 已解锁；off = 门关 = 已锁定。
        配置 invert 后反转。
        """
        is_open = self._compute_is_open(state_str)
        if is_open is None:
            return
        self._unlocked = is_open
        # 门物理上开着时，取消自动回锁（保持解锁显示）
        if is_open and self._unlock_relock_timer is not None:
            self._unlock_relock_timer()
            self._unlock_relock_timer = None
        self.async_write_ha_state()
        # FSM：门磁防抖后再提交跳变，避免抖动产生垃圾事件
        if self._manual_enabled:
            self._cancel_timer(self._debounce_unsub)
            self._debounce_unsub = async_call_later(
                self.hass,
                DOOR_DEBOUNCE_SECONDS,
                lambda _now: self._commit_door(is_open),
            )

    # ---- 门磁 FSM ----

    @staticmethod
    def _cancel_timer(unsub: Callable[[], None] | None) -> None:
        if unsub is not None:
            unsub()

    @callback
    def _commit_door(self, is_open: bool) -> None:
        """防抖后提交门状态跳变。"""
        self._debounce_unsub = None
        if is_open == self._door_open:
            return
        self._door_open = is_open
        if is_open:
            self._door_opened()
        else:
            self._door_closed()

    @callback
    def _door_opened(self) -> None:
        """门磁 off → on。"""
        if self._fsm == FSM_ARMED:
            # 竞态窗口内已有电子开锁信号 → 电子开门，门磁只是结果
            if self.coordinator.electronic_unlock_within(self._race_window):
                self._fsm = FSM_ELECTRONIC
            else:
                # 门磁先开：启动竞态窗口等待电子开锁信号
                self._fsm = FSM_PENDING
                self._race_unsub = async_call_later(
                    self.hass, self._race_window, self._race_timeout
                )
        elif self._fsm == FSM_ELECTRONIC:
            # 门锁信号先到、门磁后到：会话确认，取消兜底超时
            self._cancel_timer(self._session_unsub)
            self._session_unsub = None

    @callback
    def _door_closed(self) -> None:
        """门磁 on → off：核心规则——关门 = 会话结束，重新武装。

        门开多久都不会触发超时误报；关门后的下一次 off → on
        才可能被判定为机械开门。
        """
        self._cancel_timer(self._race_unsub)
        self._race_unsub = None
        self._cancel_timer(self._session_unsub)
        self._session_unsub = None
        self._fsm = FSM_ARMED
        # 通知 coordinator 关门边沿，供开锁信号识别"门关后的新开锁"
        self.coordinator.mark_door_closed()

    @callback
    def _on_electronic_unlock(self) -> None:
        """收到电子开锁信号（指纹/密码/App/室内按钮）。"""
        if self._fsm == FSM_PENDING:
            # 门磁先开、门锁信号在竞态窗口内到达 → 电子开门
            self._cancel_timer(self._race_unsub)
            self._race_unsub = None
            self._fsm = FSM_ELECTRONIC
        elif self._fsm == FSM_ARMED:
            # 门锁信号先到、门还没开：成立电子会话，启动兜底超时
            # 防止"验证完没推门"导致会话永久挂起
            self._fsm = FSM_ELECTRONIC
            self._session_unsub = async_call_later(
                self.hass, self._session_timeout, self._session_timeout_cb
            )
        # ELECTRONIC / MECHANICAL：忽略

    async def _race_timeout(self, _now: Any) -> None:
        """竞态窗口超时无电子开锁信号 → 室内机械开门。"""
        self._race_unsub = None
        if self._fsm != FSM_PENDING:
            return
        self._fsm = FSM_MECHANICAL
        record = self.coordinator.unlock_record
        if record is not None:
            record.record(
                dp="manual", raw_value="manual", display=self._manual_name
            )

    async def _session_timeout_cb(self, _now: Any) -> None:
        """电子会话兜底超时（门锁信号到了但门始终没开）。"""
        self._session_unsub = None
        if self._fsm == FSM_ELECTRONIC:
            self._fsm = FSM_ARMED

    def set_display_lock_state(self, locked: bool) -> None:
        """供服务调用：手动设置门锁显示状态（不发送物理指令）。"""
        self._unlocked = not locked
        if locked and self._unlock_relock_timer is not None:
            self._unlock_relock_timer()
            self._unlock_relock_timer = None
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """device_class: lock 约定：on = 已解锁，off = 已锁定。"""
        locked = self._compute_is_locked()
        return None if locked is None else not locked

    def _compute_is_locked(self) -> bool | None:
        """门锁是否已锁定（None = 未知）。"""
        # 外部状态来源（门磁）优先级最高：只要门磁可用，就强制用其状态
        if self._external_state_entity:
            state = self.hass.states.get(self._external_state_entity)
            if state is not None and state.state not in (
                "unknown",
                "unavailable",
            ):
                is_open = state.state == STATE_ON
                if self._external_state_invert:
                    is_open = not is_open
                return not is_open
        if self.coordinator.data is None:
            return None
        # 事件型门锁优先：通过解锁 DP 的值变化推断状态
        # 涂鸦门锁通常没有实时"锁舌位置"DP，DP 1 读取值恒定不变，
        # 因此解锁事件 DP 才是判断开锁的可靠依据。
        if self._unlock_dps:
            for dp in self._unlock_dps:
                current = self.coordinator.data.get(dp)
                if current is None:
                    continue
                last = self._last_seen_unlock_dps.get(dp)
                if last is not None and current != last:
                    # 解锁事件发生 → 门已开
                    self._last_seen_unlock_dps[dp] = current
                    self._unlocked = True
                    self._schedule_auto_relock()
                    self.async_write_ha_state()
                    return False
                self._last_seen_unlock_dps[dp] = current
            # 没有新的解锁事件：返回持久化的解锁状态
            return not self._unlocked
        # 无解锁 DP 时，退回到显式状态 DP（如反锁状态）
        if self._state_dp and self._state_dp != "0":
            value = self.coordinator.data.get(self._state_dp)
            if value is not None:
                return bool(value) == self._state_true_is_locked
        # 无明确状态 → 默认认为已锁
        return True

    def _schedule_auto_relock(self) -> None:
        if self._unlock_relock_timer is not None:
            self._unlock_relock_timer()
            self._unlock_relock_timer = None
        # 涂鸦门锁物理上会自动回锁，若用户未配置延时，使用 5 秒默认值
        delay = self._auto_relock_delay if self._auto_relock_delay > 0 else 5
        self._unlock_relock_timer = async_call_later(
            self.hass, delay, self._auto_relock
        )

    async def _auto_relock(self, _now: Any) -> None:
        self._unlock_relock_timer = None
        self._unlocked = False
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
        if self._external_state_entity:
            attrs["external_state_entity"] = self._external_state_entity
            state = self.hass.states.get(self._external_state_entity)
            attrs["external_state_value"] = state.state if state else None
        if self._unlock_dps and self.coordinator.data:
            for dp in self._unlock_dps:
                val = self.coordinator.data.get(dp)
                if val is not None:
                    attrs[f"unlock_dp_{dp}"] = val
        return attrs
