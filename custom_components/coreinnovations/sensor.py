"""Sensor platform: print duration for the CTP500."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant import config_entries
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .catprinter import BLEData, CatPrinterDevice
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CTP500 sensors."""
    coordinator: DataUpdateCoordinator[BLEData] = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    device: CatPrinterDevice = hass.data[DOMAIN][entry.entry_id]["device"]
    async_add_entities([PrintDurationSensor(coordinator, coordinator.data, device)])


class PrintDurationSensor(
    CoordinatorEntity[DataUpdateCoordinator[BLEData]], SensorEntity
):
    """Reports how long the most recent print took (and is taking, live)."""

    _attr_has_entity_name = True
    _attr_name = "Print Duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[BLEData],
        ble_data: BLEData,
        device: CatPrinterDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._unsub_timer = None
        name = f"{ble_data.name} {ble_data.identifier}"
        self._attr_unique_id = f"{name}_print_duration"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, ble_data.address)},
            name=name,
            manufacturer="Core Innovations",
            model=ble_data.model,
            hw_version=ble_data.hw_version,
            sw_version=ble_data.sw_version,
            serial_number=ble_data.serial_number,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._device.callback_printing = self._handle_printing_update

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._device.callback_printing = None
        self._stop_timer()

    def _start_timer(self) -> None:
        if self._unsub_timer is None:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=1)
            )

    def _stop_timer(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _tick(self, now=None) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_printing_update(self) -> None:
        if self._device.is_printing:
            self._start_timer()
        else:
            self._stop_timer()
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._device.print_duration, 1)

    @property
    def extra_state_attributes(self) -> dict:
        duration = self._device.print_duration
        return {
            "formatted": f"{int(duration // 60):02d}:{int(duration % 60):02d}",
            "is_printing": self._device.is_printing,
        }
