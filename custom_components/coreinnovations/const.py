"""Constants for the Core Innovations CTP500 integration."""

import base64

from homeassistant.components.image import Image

from .catprinter import BLEData

DOMAIN = "coreinnovations"

# AE00 cat-printer GATT service advertised by the CTP500.
SERVICE_UUID = "0000ae00-0000-1000-8000-00805f9b34fb"

# The CTP500 advertises a BLE local name like "Mini Printer-0825". Many cat
# printers expose the name but not the AE00 service UUID in the advertisement,
# so discovery matches on either.
LOCAL_NAME_PREFIX = "Mini Printer"

# Options / config keys.
CONF_SPEED = "speed"
CONF_ENERGY = "energy"
CONF_FEED = "feed"
CONF_CHUNK_SIZE = "chunk_size"
CONF_PACKET_DELAY = "packet_delay"  # milliseconds between BLE write chunks
CONF_KEEP_CONNECTION = "keep_connection"

# Defaults, matched to NaitLee/Cat-Printer (see catprinter.printer for rationale).
DEFAULT_SCAN_INTERVAL = 3600
DEFAULT_SPEED = 32
DEFAULT_ENERGY = 0x6000  # Cat-Printer "text" energy; thin strokes need this
DEFAULT_IMAGE_ENERGY = 0x4000  # Cat-Printer "image" energy (photos/dithered)
DEFAULT_FEED = 160
DEFAULT_CHUNK_SIZE = 200
DEFAULT_PACKET_DELAY = 20
DEFAULT_KEEP_CONNECTION = False

# 1x1 white PNG placeholder for the "last receipt" image entity.
EMPTY_PNG: bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

ImageAndBLEData = tuple[Image, BLEData]
