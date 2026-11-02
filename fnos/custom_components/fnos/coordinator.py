"""fnOS 数据更新协调器。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import FnosClient
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DATA_CPU_NAME,
    DATA_CPU_TEMP,
    DATA_CPU_USAGE,
    DATA_GPU_USAGE,
    DATA_HOSTNAME,
    DATA_MEM_FREE,
    DATA_MEM_PERCENT,
    DATA_MEM_TOTAL,
    DATA_MEM_USED,
    DATA_NET_RECEIVE,
    DATA_NET_TRANSMIT,
    DATA_ONLINE,
)

_LOGGER = logging.getLogger(__name__)


class FnosCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """协调 fnOS 数据轮询。"""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self._config = config
        self._client: FnosClient | None = None
        self._host = config[CONF_HOST]
        self._mac = config.get("mac", "")
        self._consecutive_failures = 0
        interval = timedelta(seconds=config.get(CONF_SCAN_INTERVAL, 60))
        super().__init__(hass, _LOGGER, name="fnOS", update_interval=interval)

    def get_mac(self) -> str:
        return self._mac

    async def _ensure_client(self) -> FnosClient:
        """确保客户端已连接并登录。"""
        if self._client and self._client.connected:
            return self._client
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        client = FnosClient(
            host=self._config[CONF_HOST],
            port=self._config[CONF_PORT],
            username=self._config[CONF_USERNAME],
            password=self._config[CONF_PASSWORD],
        )
        await client.connect()
        await client.login()
        self._client = client
        return client

    async def _async_update_data(self) -> dict[str, Any]:
        """拉取最新数据。"""
        try:
            client = await self._ensure_client()
        except Exception as err:
            # 连接失败视为离线；仅首次告警，恢复前不重复刷屏
            self._consecutive_failures += 1
            if self._consecutive_failures == 1:
                _LOGGER.warning("无法连接 fnOS: %s（NAS 恢复在线前不再重复告警）", err)
            else:
                _LOGGER.debug(
                    "连接 fnOS 失败（第 %s 次）: %s", self._consecutive_failures, err
                )
            return {DATA_ONLINE: False}

        if self._consecutive_failures:
            _LOGGER.info("fnOS 连接已恢复")
            self._consecutive_failures = 0

        data: dict[str, Any] = {DATA_ONLINE: True}

        try:
            host_data = await client.get_host_name()
            data[DATA_HOSTNAME] = host_data.get("hostName", "fnOS")
        except Exception as err:
            _LOGGER.debug("获取主机名失败: %s", err)

        try:
            cpu_data = await client.get_cpu()
            data[DATA_CPU_NAME] = cpu_data.get("name", "CPU")
            # 真实结构：cpu.busy.{all,user,system,iowait,other}，all 已是百分数（0~100）
            data[DATA_CPU_USAGE] = cpu_data.get("busy", {}).get("all") or 0.0
            # temp 为各核心温度数组，取均值
            temp = cpu_data.get("temp")
            if isinstance(temp, list) and temp:
                data[DATA_CPU_TEMP] = sum(temp) / len(temp)
            elif isinstance(temp, (int, float)):
                data[DATA_CPU_TEMP] = float(temp)
            else:
                data[DATA_CPU_TEMP] = None
        except Exception as err:
            _LOGGER.debug("获取 CPU 数据失败: %s", err)

        try:
            mem_data = await client.get_memory()
            mem = mem_data.get("mem", {})
            total = mem.get("total", 0)
            used = mem.get("used", 0)
            free = mem.get("free", 0)
            data[DATA_MEM_TOTAL] = total
            data[DATA_MEM_USED] = used
            data[DATA_MEM_FREE] = free
            data[DATA_MEM_PERCENT] = round(used / total * 100, 1) if total else 0.0
        except Exception as err:
            _LOGGER.debug("获取内存数据失败: %s", err)

        try:
            net_data = await client.get_net()
            ifs = net_data.get("ifs", [])
            # 与前端一致：直接对各网卡速率求和（receive/transmit 已是每秒字节数）
            data[DATA_NET_RECEIVE] = sum(i.get("receive") or 0 for i in ifs)
            data[DATA_NET_TRANSMIT] = sum(i.get("transmit") or 0 for i in ifs)
        except Exception as err:
            _LOGGER.debug("获取网络数据失败: %s", err)

        try:
            gpu_data = await client.get_gpu()
            # gpu[].engine 为 {引擎名: 占用%}，取所有引擎最大值；无 GPU 时为 None
            engines = [
                v
                for gpu in gpu_data.get("gpu", [])
                for v in (gpu.get("engine") or {}).values()
                if isinstance(v, (int, float))
            ]
            data[DATA_GPU_USAGE] = round(max(engines), 1) if engines else None
        except Exception as err:
            _LOGGER.debug("获取 GPU 数据失败: %s", err)

        return data

    async def async_poweroff_nas(self) -> None:
        """执行 NAS 关机（勿命名为 async_shutdown，避免覆盖 HA 生命周期钩子）。"""
        client = await self._ensure_client()
        await client.shutdown()
        # 关机后标记离线
        self.async_set_updated_data({DATA_ONLINE: False})
