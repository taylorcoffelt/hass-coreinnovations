"""Receipt-oriented services for the Core Innovations CTP500.

The public API mirrors the ergonomics of ha-escpos-thermal-printer: discrete
services (``print_text``, ``print_qr``, ``print_table`` ...) each take simple
parameters and an optional ``device_id`` target (broadcasting to every
configured printer when omitted).  Every print service also accepts
``preview: true`` to render to the "last receipt" image entity without using
paper.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image as PILImage

from . import render
from .catprinter import CatPrinterDevice
from .const import DEFAULT_IMAGE_ENERGY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Services registered against the domain (used for setup/teardown bookkeeping).
_PRINT_SERVICES = (
    "print_text",
    "print_image",
    "print_qr",
    "print_barcode",
    "print_separator",
    "print_table",
    "print_kvtable",
    "print_box",
    "print_test",
)
_ALL_SERVICES = _PRINT_SERVICES + ("feed",)

# Shared optional fields: targeting, preview, and per-call tuning overrides
# (any omitted value falls back to the printer's configured option).
_TARGET = {
    vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("preview", default=False): cv.boolean,
    vol.Optional("feed"): vol.All(vol.Coerce(int), vol.Range(min=0, max=500)),
    vol.Optional("energy"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    vol.Optional("speed"): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
}
_ALIGN = vol.In(["left", "center", "right"])

_SCHEMAS: dict[str, vol.Schema] = {
    "print_text": vol.Schema(
        {
            **_TARGET,
            vol.Required("text"): cv.string,
            vol.Optional("size", default=28): vol.All(vol.Coerce(int), vol.Range(min=8, max=200)),
            vol.Optional("align", default="left"): _ALIGN,
            vol.Optional("bold", default=False): cv.boolean,
            vol.Optional("underline", default="none"): vol.In(["none", "single", "double"]),
            vol.Optional("font", default=render.DEFAULT_FONT): cv.string,
        }
    ),
    "print_image": vol.Schema(
        {
            **_TARGET,
            vol.Required("image"): cv.template,
            vol.Optional("image_width"): vol.All(vol.Coerce(int), vol.Range(min=16, max=render.PRINTER_WIDTH)),
            vol.Optional("rotation", default=0): vol.In([0, 90, 180, 270]),
            vol.Optional("dither", default="floyd-steinberg"): vol.In(["floyd-steinberg", "none", "threshold"]),
            vol.Optional("threshold", default=128): vol.All(vol.Coerce(int), vol.Range(min=1, max=254)),
            vol.Optional("mirror", default=False): cv.boolean,
            vol.Optional("invert", default=False): cv.boolean,
            vol.Optional("align", default="left"): _ALIGN,
        }
    ),
    "print_qr": vol.Schema(
        {
            **_TARGET,
            vol.Required("data"): cv.string,
            vol.Optional("scale", default=6): vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
            vol.Optional("ec", default="M"): vol.In(["L", "M", "Q", "H"]),
            vol.Optional("align", default="center"): _ALIGN,
        }
    ),
    "print_barcode": vol.Schema(
        {
            **_TARGET,
            vol.Required("data"): cv.string,
            vol.Optional("code", default="code128"): cv.string,
            vol.Optional("align", default="center"): _ALIGN,
            vol.Optional("write_text", default=True): cv.boolean,
        }
    ),
    "print_separator": vol.Schema(
        {**_TARGET, vol.Optional("char", default="-"): cv.string}
    ),
    "print_table": vol.Schema(
        {
            **_TARGET,
            vol.Required("rows"): vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [cv.string])]),
            vol.Optional("aligns"): vol.All(cv.ensure_list, [_ALIGN]),
            vol.Optional("size", default=24): vol.All(vol.Coerce(int), vol.Range(min=8, max=120)),
        }
    ),
    "print_kvtable": vol.Schema(
        {
            **_TARGET,
            vol.Required("rows"): vol.Any(dict, vol.All(cv.ensure_list, [vol.All(cv.ensure_list, [cv.string])])),
            vol.Optional("size", default=24): vol.All(vol.Coerce(int), vol.Range(min=8, max=120)),
        }
    ),
    "print_box": vol.Schema(
        {
            **_TARGET,
            vol.Required("text"): cv.string,
            vol.Optional("style", default="line"): vol.In(["line", "asterisk", "hash"]),
            vol.Optional("size", default=28): vol.All(vol.Coerce(int), vol.Range(min=8, max=120)),
            vol.Optional("align", default="left"): _ALIGN,
        }
    ),
    "print_test": vol.Schema({**_TARGET}),
    "feed": vol.Schema(
        {
            vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("pixels", default=80): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
        }
    ),
}


def async_register_services(hass: HomeAssistant) -> None:
    """Register the shared services once for the domain."""
    if hass.services.has_service(DOMAIN, "print_text"):
        return

    handlers = {
        "print_text": _handle_print_text,
        "print_image": _handle_print_image,
        "print_qr": _handle_print_qr,
        "print_barcode": _handle_print_barcode,
        "print_separator": _handle_print_separator,
        "print_table": _handle_print_table,
        "print_kvtable": _handle_print_kvtable,
        "print_box": _handle_print_box,
        "print_test": _handle_print_test,
        "feed": _handle_feed,
    }
    for name, handler in handlers.items():
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=_SCHEMAS[name],
            supports_response=SupportsResponse.OPTIONAL,
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    for name in _ALL_SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)


# --- target resolution & delivery -----------------------------------------


def _targets(hass: HomeAssistant, call: ServiceCall) -> list[dict[str, Any]]:
    """Resolve the call's ``device_id`` selection to configured printers."""
    entries: dict[str, dict[str, Any]] = {
        k: v for k, v in hass.data.get(DOMAIN, {}).items() if isinstance(v, dict)
    }
    device_ids = call.data.get("device_id")
    if not device_ids:
        return list(entries.values())

    registry = dr.async_get(hass)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            continue
        for entry_id in device.config_entries:
            if entry_id in entries and entry_id not in seen:
                seen.add(entry_id)
                selected.append(entries[entry_id])
    if not selected:
        raise ServiceValidationError("No CTP500 printer matched the selected device(s)")
    return selected


async def _deliver(
    hass: HomeAssistant,
    call: ServiceCall,
    image: PILImage.Image,
    *,
    energy_default: int | None = None,
) -> ServiceResponse:
    """Push ``image`` to the image entity and (unless previewing) print it.

    ``energy_default`` lets a service pick a different default darkness than the
    printer's configured value (e.g. images burn lighter than text).
    """
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    png = buffer.getvalue()
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    targets = _targets(hass, call)
    for entry in targets:
        entry["image_coordinator"].async_set_updated_data(
            (Image(content_type="image/png", content=png), entry["coordinator"].data)
        )

    if call.data.get("preview"):
        return {"image": data_uri, "previewed": True}

    # Per-call tuning overrides win over configured options; the service-level
    # energy default applies only when neither a call value nor override exists.
    overrides: dict[str, int] = {}
    for key in ("feed", "energy", "speed"):
        value = call.data.get(key)
        if value is not None:
            overrides[key] = int(value)
    if "energy" not in overrides and energy_default is not None:
        overrides["energy"] = energy_default

    results: list[dict[str, Any]] = []
    for entry in targets:
        device: CatPrinterDevice = entry["device"]
        address: str = entry["address"]
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            raise HomeAssistantError(
                f"Could not reach CTP500 {address} over Bluetooth"
            )
        options = {**entry["options"], **overrides}
        try:
            result = await device.print_image(ble_device, image, **options)
        except Exception as err:  # noqa: BLE001 - surface a clean error to HA
            raise HomeAssistantError(f"Failed to print on {address}: {err}") from err
        results.append({"address": address, **result})

    return {"image": data_uri, "results": results}


async def _render(hass: HomeAssistant, func, *args, **kwargs) -> PILImage.Image:
    """Run a (CPU-bound) renderer in the executor."""
    return await hass.async_add_executor_job(lambda: func(*args, **kwargs))


# --- handlers ---------------------------------------------------------------


async def _handle_print_text(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    image = await _render(
        hass,
        render.render_text,
        call.data["text"],
        size=call.data["size"],
        align=call.data["align"],
        bold=call.data["bold"],
        underline=call.data["underline"],
        font=call.data["font"],
    )
    return await _deliver(hass, call, image)


async def _handle_print_qr(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    image = await _render(
        hass,
        render.render_qr,
        call.data["data"],
        scale=call.data["scale"],
        ec=call.data["ec"],
        align=call.data["align"],
    )
    return await _deliver(hass, call, image)


async def _handle_print_barcode(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    try:
        image = await _render(
            hass,
            render.render_barcode,
            call.data["data"],
            code=call.data["code"],
            align=call.data["align"],
            write_text=call.data["write_text"],
        )
    except Exception as err:  # noqa: BLE001
        raise ServiceValidationError(f"Could not render barcode: {err}") from err
    return await _deliver(hass, call, image)


async def _handle_print_separator(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    image = await _render(hass, render.render_separator, char=call.data["char"])
    return await _deliver(hass, call, image)


async def _handle_print_table(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    image = await _render(
        hass,
        render.render_table,
        call.data["rows"],
        aligns=call.data.get("aligns"),
        size=call.data["size"],
    )
    return await _deliver(hass, call, image)


async def _handle_print_kvtable(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    rows = call.data["rows"]
    if isinstance(rows, dict):
        rows = [[str(k), str(v)] for k, v in rows.items()]
    image = await _render(hass, render.render_kvtable, rows, size=call.data["size"])
    return await _deliver(hass, call, image)


async def _handle_print_box(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    image = await _render(
        hass,
        render.render_box,
        call.data["text"],
        style=call.data["style"],
        size=call.data["size"],
        align=call.data["align"],
    )
    return await _deliver(hass, call, image)


async def _handle_print_test(call: ServiceCall) -> ServiceResponse:
    """Render a labelled all-black test strip to validate the protocol."""
    hass = call.hass

    def _build() -> PILImage.Image:
        strip = PILImage.new("L", (render.PRINTER_WIDTH, 120), 0)  # all black
        label = render.render_text("CTP500 TEST", size=32, align="center", bold=True)
        out = PILImage.new("L", (render.PRINTER_WIDTH, strip.height + label.height), 255)
        out.paste(strip, (0, 0))
        out.paste(label, (0, strip.height))
        return out

    image = await hass.async_add_executor_job(_build)
    return await _deliver(hass, call, image)


async def _handle_print_image(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    source = call.data["image"]
    if hasattr(source, "async_render"):
        source.hass = hass
        source = source.async_render(parse_result=False)
    else:
        source = str(source)

    raw = await _load_image_bytes(hass, source)
    pil = await hass.async_add_executor_job(render.decode_image_bytes, raw)
    image = await _render(
        hass,
        render.process_image,
        pil,
        image_width=call.data.get("image_width", render.PRINTER_WIDTH),
        rotation=call.data["rotation"],
        mirror=call.data["mirror"],
        invert=call.data["invert"],
        dither=call.data["dither"],
        threshold=call.data["threshold"],
        align=call.data["align"],
    )
    return await _deliver(hass, call, image, energy_default=DEFAULT_IMAGE_ENERGY)


async def _handle_feed(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    pixels = call.data["pixels"]
    results = []
    for entry in _targets(hass, call):
        device: CatPrinterDevice = entry["device"]
        address: str = entry["address"]
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            raise HomeAssistantError(f"Could not reach CTP500 {address} over Bluetooth")
        options = entry["options"]
        result = await device.feed_paper(
            ble_device,
            pixels,
            problem_feeding=options["problem_feeding"],
            chunk_size=options["chunk_size"],
            packet_delay=options["packet_delay"],
        )
        results.append({"address": address, **result})
    return {"results": results}


# --- image source loading ---------------------------------------------------


async def _load_image_bytes(hass: HomeAssistant, source: str) -> bytes:
    """Resolve an image source string to raw bytes.

    Supported: ``data:`` URIs, ``http(s)`` URLs, ``camera.<id>`` entities and
    allowlisted local file paths.
    """
    source = source.strip()
    if source.startswith("data:"):
        return await hass.async_add_executor_job(_data_uri_bytes, source)

    if source.startswith("camera."):
        from homeassistant.components.camera import async_get_image

        image = await async_get_image(hass, source)
        return image.content

    if source.startswith(("http://", "https://")):
        session = async_get_clientsession(hass)
        async with session.get(source) as response:
            response.raise_for_status()
            return await response.read()

    # Local file path (must be inside an allowlisted directory).
    if not hass.config.is_allowed_path(source):
        raise ServiceValidationError(f"Path is not allowed: {source}")
    return await hass.async_add_executor_job(_read_file, source)


def _data_uri_bytes(source: str) -> bytes:
    image = render.load_data_uri(source)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
