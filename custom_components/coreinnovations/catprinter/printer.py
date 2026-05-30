"""Cat-printer print client: rasterisation, sequencing and paced BLE writes."""

from __future__ import annotations

import logging
from asyncio import sleep
from typing import Any

from bleak import BleakClient
from PIL import Image

from . import commander as cmd

_LOGGER = logging.getLogger(__name__)

# AE00 cat-printer GATT profile.
SERVICE_UUID = "0000ae00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"

# CTP500: 384 dots across = 48 bytes per row, 1 bit per pixel.
PRINTER_WIDTH = 384
BYTES_PER_ROW = PRINTER_WIDTH // 8

# Default tuning, matched to NaitLee/Cat-Printer. Speed is lower = faster
# (values < 4 may stall the feed motor); Cat-Printer uses 32 (quality 3).
# Energy controls darkness: Cat-Printer burns text at 0x6000 and images at
# 0x4000 — 0x3000 under-burns thin strokes, so default to the text value.
DEFAULT_SPEED = 32
DEFAULT_ENERGY = 0x6000
DEFAULT_IMAGE_ENERGY = 0x4000
# Clears the print head past the tear bar; matches Cat-Printer's finish feed.
DEFAULT_FEED = 128
# Cat-Printer drops to this slow speed before feeding, for a clean advance.
FEED_SPEED = 8

# How many bytes to push per BLE write and how long to pause between writes.
# Going over a proxy adds round-trip latency, so we batch into MTU-sized chunks
# and pace them rather than awaiting an ack per row.
DEFAULT_CHUNK_SIZE = 200
DEFAULT_PACKET_DELAY = 0.02


def image_to_rows(image: Image.Image) -> list[bytes]:
    """Convert a PIL image to a list of 48-byte 1bpp rows (MSB = leftmost dot).

    The image is scaled to exactly ``PRINTER_WIDTH`` dots wide, preserving the
    aspect ratio.  A pixel darker than mid-grey becomes a burned (black) dot.
    """
    if image.width != PRINTER_WIDTH:
        new_height = max(1, round(image.height * PRINTER_WIDTH / image.width))
        image = image.resize((PRINTER_WIDTH, new_height))

    mono = image.convert("L")
    pixels = mono.load()
    rows: list[bytes] = []
    for y in range(mono.height):
        row = bytearray(BYTES_PER_ROW)
        for x in range(PRINTER_WIDTH):
            if pixels[x, y] < 128:  # dark pixel -> burn
                row[x >> 3] |= 0x80 >> (x & 7)
        rows.append(bytes(row))
    return rows


class CatPrinterClient:
    """Wraps a connected :class:`BleakClient` and speaks the cat-printer protocol."""

    def __init__(self, client: BleakClient) -> None:
        self._client = client
        self._notifications = bytearray()

    async def start_notify(self) -> None:
        try:
            await self._client.start_notify(NOTIFY_UUID, self._on_notify)
            await sleep(0.2)
        except Exception as err:  # noqa: BLE001 - notifications are best-effort
            _LOGGER.debug("Could not subscribe to %s: %s", NOTIFY_UUID, err)

    async def stop_notify(self) -> None:
        try:
            await self._client.stop_notify(NOTIFY_UUID)
        except Exception:  # noqa: BLE001 - already gone / disconnected
            pass

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        self._notifications.extend(data)
        _LOGGER.debug("Notify (%d bytes): %s", len(data), data.hex())

    async def _write_chunked(
        self, data: bytes, chunk_size: int, packet_delay: float
    ) -> None:
        """Stream ``data`` to AE01 in chunks, pacing to avoid proxy congestion."""
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            await self._client.write_gatt_char(WRITE_UUID, chunk, response=False)
            if packet_delay > 0:
                await sleep(packet_delay)

    def _feed(self, pixels: int, problem_feeding: bool) -> bytes:
        """Build the post-print feed, matching Cat-Printer's _finish().

        Normal printers advance with the FEED_PAPER (0xA1) command; printers
        flagged "problem_feeding" ignore it and must be advanced by drawing
        blank rows instead.  Either way this runs *after* end_lattice at the
        slow feed speed.
        """
        if pixels <= 0:
            return b""
        if problem_feeding:
            blank = cmd.draw_bitmap(bytes(BYTES_PER_ROW))
            return blank * pixels
        return cmd.feed_paper(pixels)

    async def print_image(
        self,
        image: Image.Image,
        *,
        speed: int = DEFAULT_SPEED,
        energy: int = DEFAULT_ENERGY,
        feed: int = DEFAULT_FEED,
        problem_feeding: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> int:
        """Rasterise and print ``image``. Returns the number of rows printed.

        The command order mirrors NaitLee/Cat-Printer's _prepare()/_finish():
        energy is applied *before* drawing, and the paper is fed *after*
        end_lattice at a slow speed (feeding inside the lattice is ignored).
        """
        rows = image_to_rows(image)
        _LOGGER.debug(
            "Printing %d rows (speed=%s energy=%s feed=%s problem_feeding=%s)",
            len(rows), speed, energy, feed, problem_feeding,
        )

        # _prepare: state, begin run, DPI, speed, energy, apply, commit, lattice.
        prepare = bytearray()
        prepare += cmd.get_device_state()
        prepare += cmd.start_printing()
        prepare += cmd.set_dpi_as_200()
        prepare += cmd.set_speed(speed)
        prepare += cmd.set_energy(energy)
        prepare += cmd.apply_energy()
        prepare += cmd.update_device()
        prepare += cmd.start_lattice()
        await self._write_chunked(bytes(prepare), chunk_size, packet_delay)

        # Bitmap rows.
        body = bytearray()
        for row in rows:
            body += cmd.draw_bitmap(row)
        await self._write_chunked(bytes(body), chunk_size, packet_delay)

        # _finish: leave draw mode, slow down, feed past the tear bar.
        finish = bytearray()
        finish += cmd.end_lattice()
        finish += cmd.set_speed(FEED_SPEED)
        finish += self._feed(feed, problem_feeding)
        finish += cmd.get_device_state()
        await self._write_chunked(bytes(finish), chunk_size, packet_delay)

        return len(rows)

    async def feed_paper(
        self,
        pixels: int,
        *,
        problem_feeding: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> None:
        """Advance the paper by ``pixels`` without printing anything."""
        buffer = bytearray()
        buffer += cmd.get_device_state()
        buffer += cmd.set_speed(FEED_SPEED)
        buffer += self._feed(pixels, problem_feeding)
        buffer += cmd.get_device_state()
        await self._write_chunked(bytes(buffer), chunk_size, packet_delay)
