"""Receipt rendering helpers for the Core Innovations CTP500.

The CTP500 has no on-board fonts or symbologies, so every receipt element is
rendered to a 384px-wide monochrome PIL image here and then streamed to the
printer as bitmap rows.  These helpers give the integration its ha-escpos-style
ergonomics (``print_text``, ``print_qr``, ``print_table`` ...) while remaining
pure image generators with no Home Assistant or BLE coupling.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import urllib.parse
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

PRINTER_WIDTH = 384

# Bundled fonts (shipped alongside this module).
DEFAULT_FONT = "ppb.ttf"
_FONT_DIR = os.path.dirname(__file__)

WHITE = 255
BLACK = 0

# Horizontal anchors for PIL text placement, keyed by alignment.
_ALIGN_ANCHOR = {"left": "la", "center": "ma", "right": "ra"}


@lru_cache(maxsize=32)
def _load_font(font: str, size: int) -> ImageFont.FreeTypeFont:
    path = font if os.path.isabs(font) else os.path.join(_FONT_DIR, font)
    if not os.path.exists(path):
        _LOGGER.warning("Font %s not found, falling back to %s", font, DEFAULT_FONT)
        path = os.path.join(_FONT_DIR, DEFAULT_FONT)
    return ImageFont.truetype(path, size)


def _align_x(align: str, width: int) -> int:
    if align == "center":
        return width // 2
    if align == "right":
        return width - 1
    return 0


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap ``text`` to ``max_width`` pixels, honouring explicit newlines."""
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if current and measure.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def _blank(height: int, width: int = PRINTER_WIDTH) -> Image.Image:
    return Image.new("L", (width, max(1, height)), WHITE)


def render_text(
    text: str,
    *,
    size: int = 28,
    align: str = "left",
    bold: bool = False,
    underline: str = "none",
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    line_spacing: int = 4,
    padding: int = 4,
) -> Image.Image:
    """Render wrapped text. ``underline`` is one of none/single/double."""
    ttf = _load_font(font, size)
    max_text_width = width - 2 * padding
    lines = _wrap(text, ttf, max_text_width)

    ascent, descent = ttf.getmetrics()
    line_height = ascent + descent + line_spacing
    height = padding * 2 + line_height * len(lines)

    image = _blank(height, width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # disable anti-aliasing for crisp 1bpp output
    stroke = 1 if bold else 0
    anchor = _ALIGN_ANCHOR.get(align, "la")
    x = _align_x(align, width) if align != "left" else padding

    y = padding
    for line in lines:
        draw.text((x, y), line, fill=BLACK, font=ttf, anchor=anchor, stroke_width=stroke)
        if underline in ("single", "double") and line:
            text_w = draw.textlength(line, font=ttf)
            if align == "center":
                lx = (width - text_w) / 2
            elif align == "right":
                lx = width - padding - text_w
            else:
                lx = padding
            uy = y + ascent + 1
            draw.line([(lx, uy), (lx + text_w, uy)], fill=BLACK, width=1)
            if underline == "double":
                draw.line([(lx, uy + 2), (lx + text_w, uy + 2)], fill=BLACK, width=1)
        y += line_height
    return image


def render_separator(
    *, char: str = "-", size: int = 28, font: str = DEFAULT_FONT, width: int = PRINTER_WIDTH
) -> Image.Image:
    """Draw a full-width rule made of repeated ``char``."""
    if not char:
        char = "-"
    ttf = _load_font(font, size)
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    unit = max(1, measure.textlength(char, font=ttf))
    count = max(1, int(width / unit))
    return render_text(char * count, size=size, align="center", font=font, width=width)


def render_qr(
    data: str,
    *,
    scale: int = 6,
    ec: str = "M",
    align: str = "center",
    border: int = 2,
    width: int = PRINTER_WIDTH,
) -> Image.Image:
    """Render a QR code, scaled by ``scale`` and placed per ``align``."""
    ec_map = {
        "L": qrcode.constants.ERROR_CORRECT_L,
        "M": qrcode.constants.ERROR_CORRECT_M,
        "Q": qrcode.constants.ERROR_CORRECT_Q,
        "H": qrcode.constants.ERROR_CORRECT_H,
    }
    qr = qrcode.QRCode(
        error_correction=ec_map.get(ec.upper(), qrcode.constants.ERROR_CORRECT_M),
        box_size=scale,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    code = qr.make_image(fill_color="black", back_color="white").convert("L")
    if code.width > width:
        code = code.resize((width, round(code.height * width / code.width)))
    return _place(code, align, width)


def render_barcode(
    data: str,
    *,
    code: str = "code128",
    align: str = "center",
    module_height: float = 15.0,
    write_text: bool = True,
    width: int = PRINTER_WIDTH,
) -> Image.Image:
    """Render a 1D barcode (e.g. code128, ean13) using python-barcode."""
    import barcode
    from barcode.writer import ImageWriter

    symbology = barcode.get_barcode_class(code)
    options = {
        "module_height": float(module_height),
        "quiet_zone": 2.0,
        "font_size": 10,
        "text_distance": 3.0,
        "write_text": bool(write_text),
        "background": "white",
        "foreground": "black",
    }
    buffer = io.BytesIO()
    symbology(data, writer=ImageWriter()).write(buffer, options=options)
    buffer.seek(0)
    image = Image.open(buffer).convert("L")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    return _place(image, align, width)


def render_table(
    rows: list[list[str]],
    *,
    aligns: list[str] | None = None,
    size: int = 24,
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    line_spacing: int = 6,
    padding: int = 2,
) -> Image.Image:
    """Render evenly-spaced columns; ``aligns`` sets per-column alignment."""
    rows = [[str(cell) for cell in row] for row in rows if row]
    if not rows:
        return _blank(1, width)
    columns = max(len(row) for row in rows)
    col_width = (width - 2 * padding) // columns
    ttf = _load_font(font, size)
    ascent, descent = ttf.getmetrics()
    line_height = ascent + descent + line_spacing

    image = _blank(padding * 2 + line_height * len(rows), width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    y = padding
    for row in rows:
        for col in range(columns):
            cell = row[col] if col < len(row) else ""
            align = (aligns[col] if aligns and col < len(aligns) else "left")
            cell_x = padding + col * col_width
            if align == "center":
                x, anchor = cell_x + col_width // 2, "ma"
            elif align == "right":
                x, anchor = cell_x + col_width - 1, "ra"
            else:
                x, anchor = cell_x, "la"
            draw.text((x, y), _ellipsize(draw, cell, ttf, col_width), fill=BLACK, font=ttf, anchor=anchor)
        y += line_height
    return image


def render_kvtable(
    pairs: list[list[str]],
    *,
    size: int = 24,
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    line_spacing: int = 6,
    padding: int = 2,
) -> Image.Image:
    """Render label/value pairs: label left, value right, on one line each."""
    ttf = _load_font(font, size)
    ascent, descent = ttf.getmetrics()
    line_height = ascent + descent + line_spacing
    pairs = [p for p in pairs if p]

    image = _blank(padding * 2 + line_height * max(1, len(pairs)), width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    y = padding
    for pair in pairs:
        key = str(pair[0]) if len(pair) > 0 else ""
        value = str(pair[1]) if len(pair) > 1 else ""
        draw.text((padding, y), key, fill=BLACK, font=ttf, anchor="la")
        draw.text((width - padding, y), value, fill=BLACK, font=ttf, anchor="ra")
        y += line_height
    return image


def render_box(
    text: str,
    *,
    style: str = "line",
    size: int = 28,
    align: str = "left",
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    padding: int = 8,
) -> Image.Image:
    """Wrap ``text`` in a border. ``style``: line/asterisk/hash."""
    char = {"asterisk": "*", "hash": "#"}.get(style)
    inner = render_text(
        text, size=size, align=align, font=font, width=width - 2 * padding - 4, padding=4
    )
    height = inner.height + 2 * padding
    image = _blank(height, width)
    image.paste(inner, (padding + 2, padding))
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    if char:
        ttf = _load_font(font, max(12, size // 2))
        unit = max(1, draw.textlength(char, font=ttf))
        count = max(1, int(width / unit))
        draw.text((width // 2, 0), char * count, fill=BLACK, font=ttf, anchor="ma")
        draw.text((width // 2, height - 1), char * count, fill=BLACK, font=ttf, anchor="md")
    else:
        draw.rectangle([(1, 1), (width - 2, height - 2)], outline=BLACK, width=2)
    return image


# --- image source loading & processing -----------------------------------


def decode_image_bytes(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw))


def load_data_uri(source: str) -> Image.Image:
    payload = source[5:]
    if "," not in payload:
        raise ValueError("invalid data URI")
    meta, _, encoded = payload.partition(",")
    if meta.endswith(";base64"):
        encoded += "=" * (-len(encoded) % 4)
        return decode_image_bytes(base64.b64decode(encoded))
    return decode_image_bytes(urllib.parse.unquote_to_bytes(encoded))


def process_image(
    image: Image.Image,
    *,
    image_width: int = PRINTER_WIDTH,
    rotation: int = 0,
    mirror: bool = False,
    invert: bool = False,
    dither: str = "floyd-steinberg",
    threshold: int = 128,
    align: str = "left",
    width: int = PRINTER_WIDTH,
) -> Image.Image:
    """Normalise an arbitrary image into a placed, 1bpp-ready ``L`` image."""
    image = image.convert("RGBA")
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    image = Image.alpha_composite(background, image).convert("L")

    if rotation:
        image = image.rotate(-rotation, expand=True)
    if mirror:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    if invert:
        from PIL import ImageOps

        image = ImageOps.invert(image)

    target = min(max(16, image_width), width)
    if image.width != target:
        image = image.resize((target, max(1, round(image.height * target / image.width))))

    if dither == "none":
        image = image.point(lambda p: WHITE if p >= 128 else BLACK)
    elif dither == "threshold":
        image = image.point(lambda p: WHITE if p >= threshold else BLACK)
    else:  # floyd-steinberg
        image = image.convert("1").convert("L")

    return _place(image, align, width)


def _place(image: Image.Image, align: str, width: int) -> Image.Image:
    """Paste a sub-width image onto a full-width white canvas per ``align``."""
    if image.mode != "L":
        image = image.convert("L")
    if image.width >= width:
        return image
    canvas = _blank(image.height, width)
    if align == "center":
        x = (width - image.width) // 2
    elif align == "right":
        x = width - image.width
    else:
        x = 0
    canvas.paste(image, (x, 0))
    return canvas


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"
