import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import tinytuya

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BROADCAST_HISTORY_MAX,
    CONF_BROADCAST_HISTORY,
    CONF_DEBUG_CAPTURE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IP,
    CONF_LOCAL_KEY,
    CONF_POLL_INTERVAL,
    CONF_PROTOCOL,
    DEBUG_CAPTURE_MAX,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROTOCOL,
    DOMAIN,
)
from .udp import TuyaUdpListener

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """无法连接设备。"""


class NoDeviceFound(Exception):
    """局域网内未发现设备。"""


def build_device(data: dict[str, Any], ip: str) -> tinytuya.Device:
    return tinytuya.Device(
        data[CONF_DEVICE_ID],
        address=ip,
        local_key=data[CONF_LOCAL_KEY],
        version=float(data.get(CONF_PROTOCOL, DEFAULT_PROTOCOL)),
        connection_timeout=5,
    )


def _scan_for_ip(device_id: str) -> str | None:
    # tinytuya >= 1.20: maxretry 为扫描秒数, 广播字典中设备 ID 键为 gwId(旧版为 id)
    result = tinytuya.deviceScan(poll=False, maxretry=5)
    for ip, val in result.items():
        if not isinstance(val, dict):
            continue
        found_id = val.get("id") or val.get("gwId")
        if found_id and str(found_id) == device_id:
            return val.get("ip") or ip
    return None


async def discover_device_ip(hass: HomeAssistant, device_id: str) -> str:
    try:
        ip = await asyncio.wait_for(
            hass.async_add_executor_job(_scan_for_ip, device_id), timeout=15
        )
    except asyncio.TimeoutError as err:
        raise NoDeviceFound("扫描超时") from err
    if not ip:
        raise NoDeviceFound
    return ip


async def validate_connection(hass: HomeAssistant, data: dict[str, Any]) -> str:
    ip = data.get(CONF_DEVICE_IP) or ""
    if not ip:
        ip = await discover_device_ip(hass, data[CONF_DEVICE_ID])
    try:
        device = build_device(data, ip)
        result = await asyncio.wait_for(
            hass.async_add_executor_job(device.status), timeout=10
        )
    except asyncio.TimeoutError:
        _LOGGER.warning("门锁连接超时 (ip=%s)，按休眠设备处理并允许保存", ip)
        return ip
    except CannotConnect:
        raise
    except Exception as err:
        # 电池门锁休眠时 TCP 不可达：允许保存，唤醒后自动验证密钥/协议
        _LOGGER.warning(
            "门锁暂时无法连接 (ip=%s)，按休眠设备处理并允许保存: %s", ip, err
        )
        return ip
    if not isinstance(result, dict):
        _LOGGER.warning("门锁返回异常数据: %r", result)
        raise CannotConnect
    if "Error" in result and "dps" not in result:
        if result.get("Err") == "905":
            # Err 905 = Device Unreachable，休眠设备，允许保存
            _LOGGER.warning(
                "门锁暂时不可达 (ip=%s)，按休眠设备处理并允许保存: %r", ip, result
            )
            return ip
        # 已连上但报错（如密钥/协议错误）
        _LOGGER.warning("门锁返回异常数据: %r", result)
        raise CannotConnect
    return ip


class TuyaLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    # 收到广播后延迟抓取的秒数（短防抖：连续广播只触发最早的一次）
    WAKE_REFRESH_DELAY = 1.0

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        merged = {**entry.data, **entry.options}
        self._entry = entry
        self._data = merged
        self._device_id: str = merged[CONF_DEVICE_ID]
        self._ip: str | None = merged.get(CONF_DEVICE_IP) or None
        self._device: tinytuya.Device | None = None
        self._listener: TuyaUdpListener | None = None
        self._has_connected = False
        self._wake_refresh_unsub: Callable[[], None] | None = None
        self._debug_capture: bool = merged.get(CONF_DEBUG_CAPTURE, False)
        # 环形缓冲区：每个元素 = {"ts": ISO 时间, ...}，上限 DEBUG_CAPTURE_MAX
        self._raw_captures: deque[dict[str, Any]] = deque(maxlen=DEBUG_CAPTURE_MAX)
        # 广播历史持久化（开关 + 环形缓冲区 + 落盘防抖）
        self._history_enabled: bool = merged.get(CONF_BROADCAST_HISTORY, False)
        self._history: deque[dict[str, Any]] = deque(maxlen=BROADCAST_HISTORY_MAX)
        self._history_save_unsub: Callable[[], None] | None = None
        self._history_loaded = False
        self._history_file = hass.config.path(
            ".storage", f"tuya_local_lock_history_{self._device_id}.json"
        )
        # 最近一次"唤醒广播触发抓取"的时间，用于识别同值重复开锁事件
        self._wake_triggered_at: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=merged.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            ),
        )

    async def _async_ensure_device(self) -> None:
        if self._device is None:
            if not self._ip:
                self._ip = await discover_device_ip(self.hass, self._device_id)
            self._device = build_device(self._data, self._ip)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await asyncio.wait_for(self._async_ensure_device(), timeout=15)
        except asyncio.TimeoutError:
            if not self._has_connected:
                raise UpdateFailed("发现门锁超时")
            _LOGGER.debug("发现门锁超时，跳过本次轮询")
            return self.data or {}
        except Exception as err:
            if not self._has_connected:
                raise UpdateFailed(f"无法发现门锁: {err}") from err
            _LOGGER.debug("门锁休眠中，跳过本次轮询: %s", err)
            return self.data or {}
        try:
            result = await asyncio.wait_for(
                self.hass.async_add_executor_job(self._device.status),
                timeout=10,
            )
        except asyncio.TimeoutError:
            self._device = None
            if not self._has_connected:
                raise UpdateFailed("连接门锁超时")
            _LOGGER.debug("门锁响应超时，保留上次状态")
            return self.data or {}
        except Exception as err:
            self._device = None
            if not self._has_connected:
                raise UpdateFailed(f"无法连接门锁: {err}") from err
            _LOGGER.debug("门锁暂时不可达，保留上次状态: %s", err)
            return self.data or {}
        if isinstance(result, dict) and "dps" in result:
            self._has_connected = True
            _LOGGER.info("轮询获取到门锁 DPS: %r", result["dps"])
            return result["dps"]
        self._device = None
        if not self._has_connected:
            raise UpdateFailed(f"门锁返回异常数据: {result}")
        _LOGGER.debug("门锁返回异常数据（可能休眠）: %r", result)
        return self.data or {}

    async def _async_send_once(self, dps: dict[str, Any]) -> dict[str, Any] | None:
        """单次尝试发送指令，超时/不可达时抛原始异常。"""
        await self._async_ensure_device()
        return await asyncio.wait_for(
            self.hass.async_add_executor_job(self._device.set_multiple_values, dps),
            timeout=10,
        )

    async def async_send_command(self, dps: dict[str, Any]) -> None:
        # 门锁平时休眠，只有门铃唤醒窗口内才接受指令（由 lock 实体校验窗口，
        # 这里兜底处理窗口外的超时）
        try:
            result = await self._async_send_once(dps)
        except asyncio.TimeoutError:
            self._device = None
            raise HomeAssistantError("发送指令超时，门锁可能已回睡。请按门铃后重试。")
        except (OSError, NoDeviceFound) as err:
            self._device = None
            raise HomeAssistantError(
                f"门锁不可达，请先按门铃唤醒后再操作: {err}"
            ) from err
        if isinstance(result, dict) and result.get("Err"):
            self._device = None
            raise HomeAssistantError(f"门锁拒绝指令: {result.get('Err')}")

    async def async_start_listener(self) -> None:
        """启动 UDP 状态推送监听（纯被动接收，不占用门锁连接）。"""
        if self._listener is not None:
            return
        listener = TuyaUdpListener(
            device_id=self._device_id,
            local_key=self._data[CONF_LOCAL_KEY],
            callback=self._async_handle_udp,
            raw_callback=self._async_handle_udp_raw,
        )
        await listener.async_start()
        self._listener = listener

    @callback
    def _async_handle_udp_raw(self, packet: dict[str, Any]) -> None:
        """调试捕获：把每个收到的广播报文（含解码失败的）存入缓冲区/历史。"""
        entry = dict(packet)
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        if self._debug_capture:
            self._raw_captures.appendleft(entry)
        if self._history_enabled and entry.get("is_mine"):
            # 历史按时间正序：新的放最右；deque 满了自动淘汰最旧。
            # 只记录本设备广播，避免其他涂鸦设备刷屏
            self._history.append(entry)
            self._schedule_history_save()

    @property
    def debug_capture_enabled(self) -> bool:
        return self._debug_capture

    @property
    def raw_captures(self) -> list[dict[str, Any]]:
        return list(self._raw_captures)

    @property
    def broadcast_history_enabled(self) -> bool:
        return self._history_enabled

    @property
    def broadcast_history(self) -> list[dict[str, Any]]:
        return list(self._history)

    async def async_load_history(self) -> None:
        """启动时从磁盘恢复广播历史。"""
        if self._history_loaded:
            return
        self._history_loaded = True
        if not self._history_enabled:
            return

        def _read() -> list[dict[str, Any]] | None:
            try:
                with open(self._history_file, encoding="utf-8") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                return []
            except (OSError, json.JSONDecodeError):
                _LOGGER.exception("广播历史文件读取失败: %s", self._history_file)
                return None
            if isinstance(data, list):
                return data
            return None

        rows = await self.hass.async_add_executor_job(_read)
        if rows:
            self._history.extend(rows[-BROADCAST_HISTORY_MAX:])
            _LOGGER.info("已恢复广播历史 %d 条", len(self._history))

    @callback
    def _schedule_history_save(self) -> None:
        """落盘防抖：连续广播合并为一次写盘（5 秒后）。"""
        if self._history_save_unsub is not None:
            return
        self._history_save_unsub = async_call_later(
            self.hass, 5, self._async_save_history
        )

    async def _async_save_history(self, _now: Any) -> None:
        self._history_save_unsub = None
        rows = list(self._history)

        def _write() -> None:
            tmp = f"{self._history_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            os.replace(tmp, self._history_file)

        try:
            await self.hass.async_add_executor_job(_write)
        except OSError:
            _LOGGER.exception("广播历史写入失败: %s", self._history_file)

    @callback
    def _async_handle_udp(self, payload: dict[str, Any], ip: str) -> None:
        if ip and ip != self._ip:
            _LOGGER.info("门锁 IP 变更: %s -> %s", self._ip, ip)
            self._ip = ip
            self._device = None
        if ip and ip != self._entry.data.get(CONF_DEVICE_IP):
            # 广播纠正后的 IP 持久化到配置条目，避免 HA 重启后退回旧地址
            # （会触发 entry 更新监听器 → 重载集成，新实例直接带上正确 IP）
            _LOGGER.info("持久化门锁 IP: %s", ip)
            self.hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, CONF_DEVICE_IP: ip}
            )
        dps = payload.get("dps")
        if isinstance(dps, dict) and dps:
            _LOGGER.info("收到门锁 UDP 推送 DPS: %r", dps)
            merged = dict(self.data or {})
            merged.update(dps)
            self.async_set_updated_data(merged)
        # 能收到广播说明门锁此刻已唤醒，立即抓取一次完整状态，抓住唤醒窗口
        self._async_schedule_wake_refresh()

    @callback
    def _async_schedule_wake_refresh(self) -> None:
        if self._wake_refresh_unsub is not None:
            # 已有待执行的唤醒刷新，保持最早的一次，尽快进入抓取
            return
        self._wake_refresh_unsub = async_call_later(
            self.hass, self.WAKE_REFRESH_DELAY, self._async_wake_refresh
        )

    async def _async_wake_refresh(self, _now: Any) -> None:
        self._wake_refresh_unsub = None
        _LOGGER.debug("门锁唤醒广播触发立即抓取状态")
        self._wake_triggered_at = dt_util.utcnow()
        await self.async_refresh()

    def wake_triggered_within(self, seconds: float) -> bool:
        """当前数据是否来自「唤醒广播触发的抓取」（seconds 秒窗口内）。"""
        if self._wake_triggered_at is None:
            return False
        return dt_util.utcnow() - self._wake_triggered_at <= timedelta(seconds=seconds)

    async def async_shutdown(self) -> None:
        if self._wake_refresh_unsub is not None:
            self._wake_refresh_unsub()
            self._wake_refresh_unsub = None
        # 取消待执行的防抖保存，立即同步落盘，避免最后几条广播丢失
        if self._history_save_unsub is not None:
            self._history_save_unsub()
            self._history_save_unsub = None
            if self._history_enabled and self._history:
                rows = list(self._history)

                def _write() -> None:
                    tmp = f"{self._history_file}.tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(rows, fh, ensure_ascii=False)
                    os.replace(tmp, self._history_file)

                try:
                    await self.hass.async_add_executor_job(_write)
                except OSError:
                    _LOGGER.exception("广播历史退出时写入失败")
        if self._listener is not None:
            self._listener.async_stop()
            self._listener = None
        await super().async_shutdown()
