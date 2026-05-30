"""Constants for the Core Innovations CTP500 integration."""

import base64

from homeassistant.components.image import Image

from .catprinter import BLEData

DOMAIN = "coreinnovations"

# AE00 cat-printer GATT service advertised by the CTP500.
SERVICE_UUID = "0000ae00-0000-1000-8000-00805f9b34fb"

# Options / config keys.
CONF_SPEED = "speed"
CONF_ENERGY = "energy"
CONF_FEED = "feed"
CONF_CHUNK_SIZE = "chunk_size"
CONF_PACKET_DELAY = "packet_delay"  # milliseconds between BLE write chunks
CONF_KEEP_CONNECTION = "keep_connection"

# Defaults (see catprinter.printer for the rationale behind these values).
DEFAULT_SCAN_INTERVAL = 3600
DEFAULT_SPEED = 32
DEFAULT_ENERGY = 0x3000
DEFAULT_FEED = 80
DEFAULT_CHUNK_SIZE = 200
DEFAULT_PACKET_DELAY = 20
DEFAULT_KEEP_CONNECTION = False

# 1x1 white PNG placeholder for the "last receipt" image entity.
EMPTY_PNG: bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

ImageAndBLEData = tuple[Image, BLEData]
