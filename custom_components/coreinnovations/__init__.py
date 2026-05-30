"""The Core Innovations CTP500 BLE thermal printer integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from bleak_retry_connector import close_stale_connections_by_address
from homeassistant.components import bluetooth
from homeassistant.components.image import Image
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .catprinter import BLEData, CatPrinterDevice
from .const import (
    CONF_CHUNK_SIZE,
    CONF_ENERGY,
    CONF_FEED,
    CONF_KEEP_CONNECTION,
    CONF_PACKET_DELAY,
    CONF_SPEED,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_ENERGY,
    DEFAULT_FEED,
    DEFAULT_KEEP_CONNECTION,
    DEFAULT_PACKET_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SPEED,
    DOMAIN,
    EMPTY_PNG,
    ImageAndBLEData,
)
from .services import async_register_services, async_unregister_services

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)


def _resolve_print_options(entry: ConfigEntry) -> dict:
    """Merge options/data/defaults into the print tuning parameters."""

    def value(key, default):
        return entry.options.get(key, entry.data.get(key, default))

    return {
        "speed": int(value(CONF_SPEED, DEFAULT_SPEED)),
        "energy": int(value(CONF_ENERGY, DEFAULT_ENERGY)),
        "feed": int(value(CONF_FEED, DEFAULT_FEED)),
        "chunk_size": int(value(CONF_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)),
        # stored in milliseconds, the printer wants seconds
        "packet_delay": int(value(CONF_PACKET_DELAY, DEFAULT_PACKET_DELAY)) / 1000.0,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a CTP500 from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    address = entry.unique_id
    assert address is not None

    keep_connection = bool(
        entry.options.get(
            CONF_KEEP_CONNECTION,
            entry.data.get(CONF_KEEP_CONNECTION, DEFAULT_KEEP_CONNECTION),
        )
    )
    scan_interval = float(
        entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
    )

    await close_stale_connections_by_address(address)

    device = CatPrinterDevice(address, keep_connection)

    async def _async_update_method() -> BLEData:
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        return await device.update_device(ble_device)

    coordinator: DataUpdateCoordinator[BLEData] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=_async_update_method,
        update_interval=timedelta(seconds=scan_interval),
    )
    coordinator.data = device.ble_data
    await coordinator.async_refresh()

    image_coordinator: DataUpdateCoordinator[ImageAndBLEData] = DataUpdateCoordinator(
        hass, _LOGGER, name=DOMAIN
    )
    image_coordinator.async_set_updated_data(
        (Image(content_type="image/png", content=EMPTY_PNG), coordinator.data)
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "address": address,
        "device": device,
        "coordinator": coordinator,
        "image_coordinator": image_coordinator,
        "options": _resolve_print_options(entry),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    device: CatPrinterDevice = hass.data[DOMAIN][entry.entry_id]["device"]
    await device.disconnect()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        # Tear down the shared services once the last printer is gone.
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)

    return unload_ok
