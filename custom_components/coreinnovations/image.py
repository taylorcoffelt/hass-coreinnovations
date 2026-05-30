"""Image platform: shows the last receipt printed or previewed."""

from __future__ import annotations

import logging

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util
from propcache.api import cached_property

from .const import DOMAIN, ImageAndBLEData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the image platform."""
    assert config_entry.unique_id
    image_coordinator = hass.data[DOMAIN][config_entry.entry_id]["image_coordinator"]
    desc = ImageEntityDescription(key="last_receipt", name="Last Receipt")
    async_add_entities(
        [CTP500ImageEntity(hass, image_coordinator, desc, config_entry.unique_id)]
    )


class CTP500ImageEntity(
    CoordinatorEntity[DataUpdateCoordinator[ImageAndBLEData]], ImageEntity
):
    """Shows the most recent receipt rendered by a print/preview service."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator[ImageAndBLEData],
        entity_description: ImageEntityDescription,
        unique_id: str,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self.entity_description = entity_description
        self._attr_unique_id = f"{unique_id}_{entity_description.key}"
        ble_data = coordinator.data[1]
        name = f"{ble_data.name} {ble_data.identifier}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, ble_data.address)},
            name=name,
            manufacturer="Core Innovations",
            model=ble_data.model,
            hw_version=ble_data.hw_version,
            sw_version=ble_data.sw_version,
            serial_number=ble_data.serial_number,
        )
        self._cached_image = coordinator.data[0]

    @cached_property
    def available(self) -> bool:
        return True

    @property
    def data(self) -> ImageAndBLEData:
        return self.coordinator.data

    def image(self) -> bytes | None:
        return self._cached_image.content

    @callback
    def _handle_coordinator_update(self) -> None:
        self._cached_image = self.data[0]
        self._attr_image_last_updated = dt_util.now()
        super()._handle_coordinator_update()
