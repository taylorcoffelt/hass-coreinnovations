"""Button platform: a Test Print button on the CTP500 device page."""

from __future__ import annotations

import logging

from homeassistant import config_entries
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .catprinter import BLEData
from .const import DOMAIN
from .services import async_test_print

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the CTP500 buttons."""
    coordinator: DataUpdateCoordinator[BLEData] = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([TestPrintButton(coordinator.data, entry.entry_id)])


class TestPrintButton(ButtonEntity):
    """Prints the calibration strip when pressed."""

    _attr_has_entity_name = True
    _attr_name = "Test Print"
    _attr_icon = "mdi:printer-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, ble_data: BLEData, entry_id: str) -> None:
        self._entry_id = entry_id
        name = f"{ble_data.name} {ble_data.identifier}"
        self._attr_unique_id = f"{name}_test_print"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, ble_data.address)},
            name=name,
            manufacturer="Core Innovations",
            model=ble_data.model,
            hw_version=ble_data.hw_version,
            sw_version=ble_data.sw_version,
            serial_number=ble_data.serial_number,
        )

    async def async_press(self) -> None:
        """Print the calibration strip."""
        await async_test_print(self.hass, self._entry_id)
