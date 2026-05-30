"""Proxy-aware connection manager for the cat-printer (CTP500)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from PIL import Image

from .printer import (
    CatPrinterClient,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_ENERGY,
    DEFAULT_FEED,
    DEFAULT_PACKET_DELAY,
    DEFAULT_SPEED,
)

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class BLEData:
    """Lightweight device record surfaced to Home Assistant entities."""

    name: str = ""
    identifier: str = ""
    address: str = ""
    model: str = "CTP500"
    hw_version: str = ""
    sw_version: str = ""
    serial_number: str = ""
    sensors: dict[str, str | float | None] = dataclasses.field(default_factory=dict)


class CatPrinterDevice:
    """Owns the BLE connection lifecycle and dispatches print jobs."""

    def __init__(self, address: str, keep_connection: bool = False) -> None:
        self.address = address
        self.keep_connection = keep_connection
        self.lock = asyncio.Lock()
        self.client: BleakClient | None = None
        self.ble_data = BLEData(
            address=address,
            name="Core Innovations CTP500",
            identifier=address.replace(":", "")[-6:],
        )
        self.callback_connection = None
        self.callback_printing = None
        self._is_printing = False
        self._print_start_time: float | None = None
        self._print_end_time: float | None = None

    # -- notifications to HA entities -------------------------------------

    def _notify_connection(self) -> None:
        if self.callback_connection:
            self.callback_connection()

    def _notify_printing(self) -> None:
        if self.callback_printing:
            self.callback_printing()

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    @property
    def is_printing(self) -> bool:
        return self._is_printing

    @property
    def print_duration(self) -> float:
        if self._print_start_time is None:
            return 0.0
        if self._is_printing:
            return time.time() - self._print_start_time
        if self._print_end_time is not None:
            return self._print_end_time - self._print_start_time
        return 0.0

    # -- connection lifecycle ---------------------------------------------

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._notify_connection()

    async def _ensure_connected(self, ble_device: BLEDevice) -> CatPrinterClient:
        if not self.is_connected:
            self.client = await establish_connection(
                BleakClient, ble_device, ble_device.address
            )
            if not self.client.is_connected:
                raise RuntimeError("could not connect to the CTP500 printer")
            self._notify_connection()
        return CatPrinterClient(self.client)

    async def update_device(self, ble_device: BLEDevice) -> BLEData:
        """Refresh cached metadata.

        Polling deliberately does NOT open a BLE connection: cat printers sleep
        aggressively and connecting wakes the motor.  Connectivity is reflected
        while a print job is in flight instead.
        """
        if ble_device is not None and not self.ble_data.name:
            self.ble_data.name = ble_device.name or "Core Innovations CTP500"
        return self.ble_data

    # -- printing ----------------------------------------------------------

    async def print_image(
        self,
        ble_device: BLEDevice,
        image: Image.Image,
        *,
        speed: int = DEFAULT_SPEED,
        energy: int = DEFAULT_ENERGY,
        feed: int = DEFAULT_FEED,
        problem_feeding: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> dict:
        async with self.lock:
            printer = await self._ensure_connected(ble_device)

            self._is_printing = True
            self._print_start_time = time.time()
            self._print_end_time = None
            self._notify_printing()

            try:
                await printer.start_notify()
                rows = await printer.print_image(
                    image,
                    speed=speed,
                    energy=energy,
                    feed=feed,
                    problem_feeding=problem_feeding,
                    chunk_size=chunk_size,
                    packet_delay=packet_delay,
                )
                await printer.stop_notify()
            finally:
                self._print_end_time = time.time()
                self._is_printing = False
                self._notify_printing()
                if not self.keep_connection:
                    await self.disconnect()

        return {
            "status": "ok",
            "rows": rows,
            "duration": self.print_duration,
        }

    async def feed_paper(
        self,
        ble_device: BLEDevice,
        pixels: int,
        *,
        problem_feeding: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> dict:
        async with self.lock:
            printer = await self._ensure_connected(ble_device)
            try:
                await printer.feed_paper(
                    pixels,
                    problem_feeding=problem_feeding,
                    chunk_size=chunk_size,
                    packet_delay=packet_delay,
                )
            finally:
                if not self.keep_connection:
                    await self.disconnect()
        return {"status": "ok", "pixels": pixels}
