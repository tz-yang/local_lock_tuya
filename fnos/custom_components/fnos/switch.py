"""fnOS 电源开关实体：WOL 唤醒 + API 关机。"""
from __future__ import annotations

import logging
import socket
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_HOSTNAME, DATA_ONLINE, DOMAIN
from .coordinator import FnosCoordinator

_LOGGER = logging.getLogger(__name__)


def _send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """发送 WOL 魔术包。"""
    mac_clean = mac.replace(":", "").replace("-", "").replace(".", "")
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
    _LOGGER.info("已发送 WOL 魔术包到 %s", mac)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置开关实体。"""
    coordinator: FnosCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FnosPowerSwitch(coordinator)])


class FnosPowerSwitch(CoordinatorEntity[FnosCoordinator], SwitchEntity):
    """fnOS 电源开关。"""

    _attr_name = "电源"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: FnosCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator._host}_power"
        self._mac = coordinator.get_mac()

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get(DATA_ONLINE))

    @property
    def available(self) -> bool:
        # 开关始终可用（即使 NAS 离线也要能开机）
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """开机：发送 WOL。"""
        if not self._mac:
            _LOGGER.error("未配置 MAC 地址，无法唤醒")
            return
        await self.hass.async_add_executor_job(_send_magic_packet, self._mac)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """关机：调用 fnOS API。"""
        if not self.is_on:
            return
        try:
            await self.coordinator.async_poweroff_nas()
        except Exception as err:
            _LOGGER.error("关机失败: %s", err)

    @property
    def device_info(self) -> DeviceInfo:
        hostname = (self.coordinator.data or {}).get(DATA_HOSTNAME, "fnOS")
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator._host)},
            name=f"飞牛NAS ({hostname})",
            manufacturer="飞牛",
            model="fnOS",
        )
