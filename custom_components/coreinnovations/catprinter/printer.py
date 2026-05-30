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

# Flow-control frames the printer pushes on the notify characteristic: it asks
# us to pause when its buffer is filling and to resume once it has drained.
# Ignoring these overflows the buffer and silently drops rows on long jobs.
DATA_FLOW_PAUSE = b"\x51\x78\xae\x01\x01\x00\x10\x70\xff"
DATA_FLOW_RESUME = b"\x51\x78\xae\x01\x01\x00\x00\x00\xff"
# Safety cap so a missed "resume" can't wedge a print forever.
FLOW_PAUSE_TIMEOUT = 10.0

# Default tuning, matched to NaitLee/Cat-Printer. Speed is lower = faster
# (values < 4 may stall the feed motor); Cat-Printer uses 32 (quality 3).
# Energy controls darkness: Cat-Printer burns text at 0x6000 and images at
# 0x4000 — 0x3000 under-burns thin strokes, so default to the text value.
DEFAULT_SPEED = 32
DEFAULT_ENERGY = 0x6000
DEFAULT_IMAGE_ENERGY = 0x4000
# Cat-Printer drops to this slow speed before the post-lattice feed.
FEED_SPEED = 8
# Cat-Printer's finish feed (pixels), advancing the last line past the tear bar.
DEFAULT_FEED = 128

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
        self._write_response: bool | None = None
        self._paused = False

    def _use_response(self) -> bool:
        """Prefer write-with-response when AE01 supports it (Bleak's own rule).

        This is what Cat-Printer effectively does by leaving the mode to Bleak's
        default, and what makes feed/print reliable over a proxy. Falls back to
        write-without-response only if the characteristic lacks the property.
        """
        if self._write_response is None:
            char = self._client.services.get_characteristic(WRITE_UUID)
            self._write_response = bool(char and "write" in char.properties)
        return self._write_response

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
        frame = bytes(data)
        if frame == DATA_FLOW_PAUSE:
            self._paused = True
        elif frame == DATA_FLOW_RESUME:
            self._paused = False
        self._notifications.extend(data)
        _LOGGER.debug("Notify (%d bytes): %s", len(data), frame.hex())

    async def _write_chunked(
        self, data: bytes, chunk_size: int, packet_delay: float
    ) -> None:
        """Stream ``data`` to AE01 in MTU-sized chunks, paced like Cat-Printer.

        Uses write-*with-response* (``response=True``): each chunk is
        acknowledged before the next, so packets aren't dropped over a proxy and
        the final writes (the feed) survive the subsequent disconnect. This
        matches both Cat-Printer and the Niimbot integration.
        """
        response = self._use_response()
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            # Respect the printer's flow control: while it has asked us to pause
            # (buffer filling) hold off, or it will drop the rows we send.
            waited = 0.0
            while self._paused:
                if waited >= FLOW_PAUSE_TIMEOUT:
                    _LOGGER.debug("Flow-control pause timed out; resuming")
                    self._paused = False
                    break
                await sleep(0.05)
                waited += 0.05
            await self._client.write_gatt_char(WRITE_UUID, chunk, response=response)
            if packet_delay > 0:
                await sleep(packet_delay)

    @staticmethod
    def _blank_rows(count: int) -> bytes:
        """`count` blank bitmap rows (all white) as draw_bitmap commands."""
        if count <= 0:
            return b""
        return cmd.draw_bitmap(bytes(BYTES_PER_ROW)) * count

    async def print_image(
        self,
        image: Image.Image,
        *,
        speed: int = DEFAULT_SPEED,
        energy: int = DEFAULT_ENERGY,
        feed: int = DEFAULT_FEED,
        problem_feeding: bool = True,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> int:
        """Rasterise and print ``image``.

        Prepare/draw mirror Cat-Printer exactly. For the feed, the reliable
        method on the CTP500 is ``problem_feeding`` (default): draw ``feed`` blank
        rows as the last content *inside* the lattice — the same draw_bitmap that
        reliably advances paper for the image, and protected by flow control.
        ``feed_paper`` (0xA1) after end_lattice (problem_feeding=False) matches
        Cat-Printer's bytes but is unreliable here in a connect-per-job model.
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

        # Content rows, plus the blank-row feed when problem_feeding (still inside
        # the lattice, so it's paced by flow control and actually advances paper).
        body = bytearray()
        for row in rows:
            body += cmd.draw_bitmap(row)
        if problem_feeding:
            body += self._blank_rows(feed)
        await self._write_chunked(bytes(body), chunk_size, packet_delay)

        # _finish: leave draw mode. feed_paper path only for non-problem_feeding.
        finish = bytearray()
        finish += cmd.end_lattice()
        if not problem_feeding and feed > 0:
            finish += cmd.set_speed(FEED_SPEED)
            finish += cmd.feed_paper(feed)
        finish += cmd.get_device_state()
        await self._write_chunked(bytes(finish), chunk_size, packet_delay)

        # write_gatt_char returns on command receipt, not physical completion, so
        # give the motor time to finish before the caller disconnects.
        await sleep(min(3.0, 0.5 + feed / 200.0))
        return len(rows)

    async def feed_paper(
        self,
        pixels: int,
        *,
        problem_feeding: bool = True,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        packet_delay: float = DEFAULT_PACKET_DELAY,
    ) -> None:
        """Advance the paper by ``pixels`` without printing anything."""
        buffer = bytearray()
        buffer += cmd.get_device_state()
        if problem_feeding:
            buffer += cmd.start_printing()
            buffer += cmd.set_dpi_as_200()
            buffer += cmd.set_speed(DEFAULT_SPEED)
            buffer += cmd.start_lattice()
            buffer += self._blank_rows(pixels)
            buffer += cmd.end_lattice()
        else:
            buffer += cmd.set_speed(FEED_SPEED)
            buffer += cmd.feed_paper(pixels)
        buffer += cmd.get_device_state()
        await self._write_chunked(bytes(buffer), chunk_size, packet_delay)
        await sleep(min(3.0, 0.5 + pixels / 200.0))
