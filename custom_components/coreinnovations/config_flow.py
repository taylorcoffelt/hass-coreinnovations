"""Config flow for the Core Innovations CTP500 integration."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_CHUNK_SIZE,
    CONF_ENERGY,
    CONF_FEED,
    CONF_KEEP_CONNECTION,
    CONF_PACKET_DELAY,
    CONF_PROBLEM_FEEDING,
    CONF_SPEED,
    CONF_STRICT_FLOW_CONTROL,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_ENERGY,
    DEFAULT_FEED,
    DEFAULT_KEEP_CONNECTION,
    DEFAULT_PACKET_DELAY,
    DEFAULT_PROBLEM_FEEDING,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SPEED,
    DEFAULT_STRICT_FLOW_CONTROL,
    DOMAIN,
    LOCAL_NAME_PREFIX,
    SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


def _number(minimum: float, maximum: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


OPTIONS_SCHEMA = {
    vol.Required(CONF_SPEED, default=DEFAULT_SPEED): _number(1, 255, 1, "lower=faster"),
    vol.Required(CONF_ENERGY, default=DEFAULT_ENERGY): _number(0, 0xFFFF, 1, "darkness"),
    vol.Required(CONF_FEED, default=DEFAULT_FEED): _number(0, 2000, 1, "pixels"),
    vol.Required(CONF_PACKET_DELAY, default=DEFAULT_PACKET_DELAY): _number(0, 1000, 1, "milliseconds"),
    vol.Required(CONF_CHUNK_SIZE, default=DEFAULT_CHUNK_SIZE): _number(20, 512, 1, "bytes"),
    vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): _number(10, 86400, 1, "seconds"),
    vol.Required(CONF_PROBLEM_FEEDING, default=DEFAULT_PROBLEM_FEEDING): bool,
    vol.Required(CONF_KEEP_CONNECTION, default=DEFAULT_KEEP_CONNECTION): bool,
    vol.Required(CONF_STRICT_FLOW_CONTROL, default=DEFAULT_STRICT_FLOW_CONTROL): bool,
}


def _is_catprinter(info: BluetoothServiceInfoBleak) -> bool:
    """True if this advertisement looks like a CTP500 cat printer.

    Matches by local name (e.g. "Mini Printer-0825") or, as a fallback, the
    AE00 GATT service UUID when the printer happens to advertise it.
    """
    if info.name and info.name.startswith(LOCAL_NAME_PREFIX):
        return True
    return SERVICE_UUID in [uuid.lower() for uuid in info.service_uuids]


@dataclasses.dataclass
class Discovery:
    """A discovered cat printer."""

    name: str
    discovery_info: BluetoothServiceInfoBleak


class CoreInnovationsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the CTP500."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_device: Discovery | None = None
        self._discovered_devices: dict[str, Discovery] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle automatic bluetooth discovery."""
        _LOGGER.debug("Discovered CTP500 candidate: %s", discovery_info)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": name}
        self._discovered_device = Discovery(name, discovery_info)
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self.context["title_placeholders"]["name"], data=user_input
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
            data_schema=vol.Schema(OPTIONS_SCHEMA),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            discovery = self._discovered_devices[address]
            self.context["title_placeholders"] = {"name": discovery.name}
            self._discovered_device = discovery
            return self.async_create_entry(title=discovery.name, data=user_input)

        current_addresses = self._async_current_ids()
        for info in async_discovered_service_info(self.hass):
            address = info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            if not _is_catprinter(info):
                continue
            self._discovered_devices[address] = Discovery(info.name or address, info)

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles = {
            address: f"{discovery.name} ({address})"
            for address, discovery in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(titles)} | OPTIONS_SCHEMA
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        suggested = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(OPTIONS_SCHEMA), suggested
            ),
        )
