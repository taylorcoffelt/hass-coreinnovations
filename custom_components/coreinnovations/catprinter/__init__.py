"""Cat-printer protocol package for the Core Innovations CTP500."""

from .parser import BLEData, CatPrinterDevice
from .printer import PRINTER_WIDTH

__all__ = ["BLEData", "CatPrinterDevice", "PRINTER_WIDTH"]
