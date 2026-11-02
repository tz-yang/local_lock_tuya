"""fnOS 传感器实体：CPU 使用率、CPU 温度、内存占用。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_CPU_TEMP,
    DATA_CPU_USAGE,
    DATA_GPU_USAGE,
    DATA_HOSTNAME,
    DATA_MEM_PERCENT,
    DATA_MEM_TOTAL,
    DATA_MEM_USED,
    DATA_NET_RECEIVE,
    DATA_NET_TRANSMIT,
    DOMAIN,
)
from .coordinator import FnosCoordinator


@dataclass(frozen=True)
class FnosSensorDescription(SensorEntityDescription):
    """fnOS 传感器描述。"""

    value_fn: Callable[[dict[str, Any]], Any] | None = None


SENSORS: tuple[FnosSensorDescription, ...] = (
    FnosSensorDescription(
        key="cpu_usage",
        name="CPU 使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.get(DATA_CPU_USAGE, 0.0), 1),
    ),
    FnosSensorDescription(
        key="cpu_temp",
        name="CPU 温度",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_CPU_TEMP),
    ),
    FnosSensorDescription(
        key="mem_percent",
        name="内存占用",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_MEM_PERCENT, 0.0),
    ),
    FnosSensorDescription(
        key="mem_used",
        name="内存已用",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_MEM_USED, 0),
    ),
    FnosSensorDescription(
        key="mem_total",
        name="内存总量",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_MEM_TOTAL, 0),
    ),
    FnosSensorDescription(
        key="net_receive",
        name="下载速率",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_NET_RECEIVE, 0),
    ),
    FnosSensorDescription(
        key="net_transmit",
        name="上传速率",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_NET_TRANSMIT, 0),
    ),
    FnosSensorDescription(
        key="gpu_usage",
        name="GPU 使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get(DATA_GPU_USAGE),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置传感器实体。"""
    coordinator: FnosCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [FnosSensor(coordinator, desc) for desc in SENSORS]
    async_add_entities(entities)


class FnosSensor(CoordinatorEntity[FnosCoordinator], SensorEntity):
    """fnOS 传感器。"""

    entity_description: FnosSensorDescription

    def __init__(
        self, coordinator: FnosCoordinator, description: FnosSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator._host}_{description.key}"

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data or not self.coordinator.data.get("online"):
            return None
        fn = self.entity_description.value_fn
        return fn(self.coordinator.data) if fn else None

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("online"))

    @property
    def device_info(self) -> DeviceInfo:
        hostname = (self.coordinator.data or {}).get(DATA_HOSTNAME, "fnOS")
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator._host)},
            name=f"飞牛NAS ({hostname})",
            manufacturer="飞牛",
            model="fnOS",
        )
