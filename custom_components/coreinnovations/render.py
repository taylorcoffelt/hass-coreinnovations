"""Receipt rendering helpers for the Core Innovations CTP500.

The CTP500 has no on-board fonts or symbologies, so every receipt element is
rendered to a 384px-wide monochrome PIL image here and then streamed to the
printer as bitmap rows.  These helpers give the integration its ha-escpos-style
ergonomics (``print_text``, ``print_qr``, ``print_table`` ...) while remaining
pure image generators with no Home Assistant or BLE coupling.

Two cross-cutting features live here:

* **Glyph fallback** (the tofu fix).  The bundled Ubuntu Nerd Fonts cover Latin,
  Cyrillic and Greek plus the whole Nerd Font private-use icon range, but *not*
  the common Unicode symbol blocks (arrows, box-drawing, ballot/check marks,
  dingbats).  Drawn naively those render as ".notdef" boxes ("tofu").  ``FontStack``
  resolves every character against a chain of fonts (via the real cmap), remaps
  well-known Unicode symbols onto equivalent Nerd Font icons, expands ``:icon-name:``
  tokens into Material Design Icon glyphs, and finally substitutes a visible
  placeholder so nothing ever prints as tofu.

* **Compositor** (``render_document``).  Lays out a header, rules, checkbox items,
  tables and mixed font sizes into a *single* image so an entire receipt prints in
  one BLE transmission instead of one job (and one paper-wasting feed) per line.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import urllib.parse
from functools import lru_cache

import qrcode
from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

PRINTER_WIDTH = 384
_FONT_DIR = os.path.dirname(__file__)

WHITE = 255
BLACK = 0

# Friendly font aliases -> bundled filename.  Callers may also pass a bare
# filename (resolved against this directory) or an absolute path.
FONT_ALIASES = {
    "ubuntu": "UbuntuNerdFont-Regular.ttf",
    "ubuntu-regular": "UbuntuNerdFont-Regular.ttf",
    "ubuntu-light": "UbuntuNerdFont-Light.ttf",
    "ubuntu-light-italic": "UbuntuNerdFont-LightItalic.ttf",
    "ubuntu-bold": "UbuntuNerdFontPropo-Bold.ttf",
    # Legacy bundled faces, kept selectable for backwards compatibility.
    "ppb": "ppb.ttf",
    "rbm": "rbm.ttf",
    "mdi": "materialdesignicons-webfont.ttf",
}

# Default face: the Ubuntu Nerd Font, which carries the icon glyphs the fallback
# layer leans on.
DEFAULT_FONT = "ubuntu"

# Icon font used to resolve ``:name:`` tokens and as the final fallback for the
# full Material Design Icon set (the Ubuntu Nerd Font carries most, but not all).
ICON_FONT = "materialdesignicons-webfont.ttf"

# Real bold faces, preferred over synthetic stroke-bolding when available.
_BOLD_SIBLING = {
    "UbuntuNerdFont-Regular.ttf": "UbuntuNerdFontPropo-Bold.ttf",
    "UbuntuNerdFont-Light.ttf": "UbuntuNerdFontPropo-Bold.ttf",
    "UbuntuNerdFontPropo-Bold.ttf": "UbuntuNerdFontPropo-Bold.ttf",
}

# Fonts appended after the chosen face to widen glyph coverage.  Order matters:
# Ubuntu first (carries the Nerd Font icon ranges), the MDI webfont last (covers
# every Material Design Icon, including the few Ubuntu omits).
_FALLBACK_FONTS = (
    "UbuntuNerdFont-Regular.ttf",
    ICON_FONT,
)

# Substituted for any codepoint no font in the stack can render and that has no
# symbol remapping -- guarantees we never emit a ".notdef" tofu box.
MISSING_GLYPH = "?"

# Common Unicode symbols that the bundled fonts lack at their native codepoint,
# remapped onto an equivalent Nerd Font / Material Design Icon that they *do*
# carry.  This is what actually kills tofu for everyday symbols.
_SYMBOL_FALLBACK = {
    0x2610: 0xF0131,  # ☐ ballot box -> checkbox-blank-outline
    0x2611: 0xF0132,  # ☑ ballot box with check -> checkbox-marked
    0x2612: 0xF0135,  # ☒ ballot box with x -> checkbox-marked-outline
    0x2713: 0xF012C,  # ✓ check mark -> check
    0x2714: 0xF0E1E,  # ✔ heavy check mark -> check-bold
    0x2705: 0xF012C,  # ✅ white heavy check -> check
    0x2717: 0xF0156,  # ✗ ballot x -> close
    0x2718: 0xF1398,  # ✘ heavy ballot x -> close-thick
    0x274C: 0xF0156,  # ❌ cross mark -> close
    0x2605: 0xF04CE,  # ★ black star -> star
    0x2606: 0xF04D2,  # ☆ white star -> star-outline
    0x2B50: 0xF04CE,  # ⭐ star -> star
    0x2190: 0xF004D,  # ← -> arrow-left
    0x2191: 0xF005D,  # ↑ -> arrow-up
    0x2192: 0xF0054,  # → -> arrow-right
    0x2193: 0xF0045,  # ↓ -> arrow-down
    0x21D2: 0xF0055,  # ⇒ -> arrow-right-thick
    0x25B6: 0xF0142,  # ▶ -> chevron-right
    0x25C0: 0xF0141,  # ◀ -> chevron-left
    0x26A0: 0xF0026,  # ⚠ warning -> alert
    0x2139: 0xF02FC,  # ℹ information -> information
    0x2753: 0xF02D7,  # ❓ -> help-circle
    0x2744: 0xF0717,  # ❄ snowflake -> snowflake
    0x2665: 0xF02D1,  # ♥ -> heart
    0x2764: 0xF02D1,  # ❤ -> heart
    0x25CF: 0xF0765,  # ● black circle -> circle
    0x25CB: 0xF0766,  # ○ white circle -> circle-outline
    0x25A0: 0xF0764,  # ■ black square -> square
    0x25A1: 0xF0763,  # □ white square -> square-outline
    0x2600: 0xF0599,  # ☀ -> weather-sunny
    0x2601: 0xF0590,  # ☁ -> weather-cloudy
    0x2602: 0xF0597,  # ☂ umbrella ~ -> weather-rainy
    0x260E: 0xF03F2,  # ☎ phone -> phone
    0x2709: 0xF01EE,  # ✉ envelope -> email
    0x23F0: 0xF0020,  # ⏰ alarm clock -> alarm
    0x231A: 0xF0954,  # ⌚ watch -> clock
    0x1F514: 0xF009A,  # 🔔 bell -> bell
    0x1F3B5: 0xF0387,  # 🎵 -> music-note
    0x1F4C5: 0xF00ED,  # 📅 -> calendar
    0x1F4CD: 0xF034E,  # 📍 -> map-marker
}

# Box-drawing / block characters degrade to ASCII rather than an icon.
_ASCII_FALLBACK = {
    0x2500: "-", 0x2501: "-", 0x2504: "-", 0x2505: "-", 0x2508: "-", 0x2509: "-",
    0x2502: "|", 0x2503: "|", 0x2506: "|", 0x2507: "|",
    0x250C: "+", 0x2510: "+", 0x2514: "+", 0x2518: "+", 0x251C: "+", 0x2524: "+",
    0x252C: "+", 0x2534: "+", 0x253C: "+",
    0x2550: "=", 0x2551: "|", 0x2554: "+", 0x2557: "+", 0x255A: "+", 0x255D: "+",
    0x2588: "#", 0x2593: "#", 0x2592: "#", 0x2591: ".",
}

# ``:mdi:icon-name:`` tokens are expanded to Material Design Icon glyphs.  The
# ``mdi:`` prefix keeps everyday text (``ratio 16:9``, ``:-)``, log dumps) from
# being accidentally rewritten into icons.
_ICON_TOKEN = re.compile(r":mdi:([a-z0-9][a-z0-9-]*):")


# --- font loading & glyph coverage -----------------------------------------


def _font_path(font: str) -> str:
    """Resolve an alias / bare filename / absolute path to a filesystem path."""
    if os.path.isabs(font):
        return font
    if font in FONT_ALIASES:
        return os.path.join(_FONT_DIR, FONT_ALIASES[font])
    return os.path.join(_FONT_DIR, font)


def _existing_font_path(font: str) -> str:
    path = _font_path(font)
    if not os.path.exists(path):
        _LOGGER.warning("Font %s not found, falling back to %s", font, DEFAULT_FONT)
        path = _font_path(DEFAULT_FONT)
    return path


def _bold_path(path: str) -> str | None:
    """Return the bold sibling for a resolved font path, if one is bundled."""
    sibling = _BOLD_SIBLING.get(os.path.basename(path))
    if not sibling:
        return None
    candidate = os.path.join(_FONT_DIR, sibling)
    return candidate if os.path.exists(candidate) else None


@lru_cache(maxsize=64)
def _load_one(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=32)
def _load_font(font: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a single font by alias / filename / path (no fallback chain)."""
    return _load_one(_existing_font_path(font), size)


@lru_cache(maxsize=16)
def _coverage(path: str) -> frozenset[int] | None:
    """Set of codepoints a font can render, read from its cmap.

    Returns ``None`` when coverage can't be determined (fontTools missing or an
    unreadable font); callers then assume the font covers everything, preserving
    the pre-fallback behaviour rather than crashing.
    """
    try:
        from fontTools.ttLib import TTFont
    except Exception:  # pragma: no cover - fonttools is a declared requirement
        _LOGGER.debug("fonttools unavailable; glyph fallback disabled")
        return None
    try:
        logging.getLogger("fontTools").setLevel(logging.ERROR)
        with TTFont(path, fontNumber=0, lazy=True) as ttf:
            return frozenset(ttf.getBestCmap().keys())
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not read glyph coverage for %s: %s", path, err)
        return None


@lru_cache(maxsize=1)
def _mdi_names() -> dict[str, int]:
    """Material Design Icon ``name -> codepoint`` map from the bundled meta JSON."""
    meta_path = os.path.join(_FONT_DIR, "materialdesignicons-webfont_meta.json")
    try:
        with open(meta_path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {entry["name"]: int(entry["codepoint"], 16) for entry in data}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not load MDI icon metadata: %s", err)
        return {}


def expand_icons(text: str) -> str:
    """Replace ``:mdi:icon-name:`` tokens with the matching Material Design Icon glyph.

    Only tokens that resolve to a real icon name are substituted; anything else
    (``:mdi:not-a-real-icon:``, ``:-)``, ``12:30:00`` ...) is left untouched.
    """
    if ":mdi:" not in text:
        return text
    names = _mdi_names()
    if not names:
        return text

    def _sub(match: re.Match[str]) -> str:
        cp = names.get(match.group(1))
        return chr(cp) if cp is not None else match.group(0)

    return _ICON_TOKEN.sub(_sub, text)


class FontStack:
    """A primary font plus coverage-aware fallbacks, drawn per glyph run.

    PIL renders a string with a single face and no fallback, so out-of-coverage
    codepoints become tofu.  ``FontStack`` instead resolves each character to the
    first font in the chain that can render it (remapping common symbols and
    finally substituting a placeholder), groups consecutive same-font characters
    into runs, and draws/measures run by run on a shared baseline.
    """

    def __init__(self, paths: list[str], size: int) -> None:
        self.fonts = [_load_one(p, size) for p in paths]
        self.coverage = [_coverage(p) for p in paths]
        self.primary = self.fonts[0]

    def _font_for_cp(self, cp: int) -> ImageFont.FreeTypeFont | None:
        for font, cov in zip(self.fonts, self.coverage):
            if cov is None or cp in cov:
                return font
        return None

    def _resolve(self, ch: str) -> tuple[str, ImageFont.FreeTypeFont]:
        cp = ord(ch)
        font = self._font_for_cp(cp)
        if font is not None:
            return ch, font
        # Remap a well-known symbol onto an icon the stack carries.
        alt = _SYMBOL_FALLBACK.get(cp)
        if alt is not None:
            font = self._font_for_cp(alt)
            if font is not None:
                return chr(alt), font
        ascii_alt = _ASCII_FALLBACK.get(cp)
        if ascii_alt is not None:
            return ascii_alt, self.primary
        _LOGGER.debug(
            "No glyph for U+%04X (%r) in the font stack; substituting %r",
            cp, ch, MISSING_GLYPH,
        )
        return MISSING_GLYPH, self.primary

    def runs(self, text: str) -> list[tuple[str, ImageFont.FreeTypeFont]]:
        out: list[tuple[str, ImageFont.FreeTypeFont]] = []
        buf: list[str] = []
        cur: ImageFont.FreeTypeFont | None = None
        for ch in text:
            rep, font = self._resolve(ch)
            if font is cur:
                buf.append(rep)
            else:
                if buf:
                    out.append(("".join(buf), cur))  # type: ignore[arg-type]
                buf = [rep]
                cur = font
        if buf:
            out.append(("".join(buf), cur))  # type: ignore[arg-type]
        return out

    def getmetrics(self) -> tuple[int, int]:
        return self.primary.getmetrics()

    def textlength(self, text: str) -> float:
        """Advance width of ``text``. Uses ``font.getlength`` so no (shared,
        non-thread-safe) ``ImageDraw`` is needed for measurement."""
        return sum(font.getlength(seg) for seg, font in self.runs(text))

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        x: float,
        baseline: float,
        text: str,
        *,
        fill: int = BLACK,
        stroke_width: int = 0,
    ) -> float:
        """Draw ``text`` left-to-right from ``x`` on ``baseline``; return end x."""
        for seg, font in self.runs(text):
            draw.text(
                (x, baseline), seg, fill=fill, font=font, anchor="ls",
                stroke_width=stroke_width,
            )
            x += font.getlength(seg)
        return x


@lru_cache(maxsize=32)
def _stack(font: str, size: int, bold: bool = False) -> FontStack:
    primary = _existing_font_path(font)
    if bold:
        sibling = _bold_path(primary)
        if sibling:
            primary = sibling
    paths = [primary]
    for name in _FALLBACK_FONTS:
        path = os.path.join(_FONT_DIR, name)
        if path not in paths and os.path.exists(path):
            paths.append(path)
    return FontStack(paths, size)


def _has_real_bold(font: str) -> bool:
    return _bold_path(_existing_font_path(font)) is not None


def _line_x(align: str, line_width: float, width: int, padding: int) -> float:
    if align == "center":
        return (width - line_width) / 2
    if align == "right":
        return width - padding - line_width
    return padding


def _wrap(text: str, stack: FontStack, max_width: int) -> list[str]:
    """Word-wrap ``text`` to ``max_width`` pixels, honouring explicit newlines."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if current and stack.textlength(candidate) > max_width:
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
    """Render wrapped text with glyph fallback. ``underline``: none/single/double."""
    text = expand_icons(text)
    real_bold = bold and _has_real_bold(font)
    stack = _stack(font, size, bold=real_bold)
    stroke = 1 if (bold and not real_bold) else 0

    # getlength measures the glyph advance, not the stroke bleed; reserve a
    # little extra so synthetic-bold lines near the edge still wrap in time.
    max_text_width = width - 2 * padding - 2 * stroke
    lines = _wrap(text, stack, max_text_width)

    ascent, descent = stack.getmetrics()
    line_height = ascent + descent + line_spacing
    height = padding * 2 + line_height * len(lines)

    image = _blank(height, width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # disable anti-aliasing for crisp 1bpp output

    for i, line in enumerate(lines):
        y = padding + i * line_height
        baseline = y + ascent
        line_width = stack.textlength(line)
        x = _line_x(align, line_width, width, padding)
        stack.draw(draw, x, baseline, line, fill=BLACK, stroke_width=stroke)
        if underline in ("single", "double") and line:
            uy = baseline + 1
            draw.line([(x, uy), (x + line_width, uy)], fill=BLACK, width=1)
            if underline == "double":
                draw.line([(x, uy + 2), (x + line_width, uy + 2)], fill=BLACK, width=1)
    return image


def render_rule(
    *,
    style: str = "solid",
    thickness: int = 2,
    width: int = PRINTER_WIDTH,
    margin: int = 8,
    pad: int = 6,
) -> Image.Image:
    """Draw a real full-width horizontal rule.

    ``style``: solid / double / dashed / dotted.  ``margin`` insets the rule from
    the paper edges; ``pad`` is the blank space above and below it.
    """
    thickness = max(1, thickness)
    if style == "double":
        body = thickness * 2 + 2
    else:
        body = thickness
    height = body + 2 * pad
    image = _blank(height, width)
    draw = ImageDraw.Draw(image)
    x0, x1 = margin, width - 1 - margin
    y = pad

    if style in ("dashed", "dotted"):
        dash = 3 if style == "dotted" else 12
        gap = 4 if style == "dotted" else 8
        x = x0
        while x <= x1:
            draw.rectangle([(x, y), (min(x + dash - 1, x1), y + thickness - 1)], fill=BLACK)
            x += dash + gap
    elif style == "double":
        draw.rectangle([(x0, y), (x1, y + thickness - 1)], fill=BLACK)
        draw.rectangle([(x0, y + thickness + 2), (x1, y + 2 * thickness + 1)], fill=BLACK)
    else:  # solid
        draw.rectangle([(x0, y), (x1, y + thickness - 1)], fill=BLACK)
    return image


def render_separator(
    *, char: str = "-", size: int = 28, font: str = DEFAULT_FONT, width: int = PRINTER_WIDTH
) -> Image.Image:
    """Full-width rule. Simple line characters draw a crisp line; others tile."""
    if not char:
        char = "-"
    line_styles = {"-": "solid", "_": "solid", "=": "double", "─": "solid", "═": "double"}
    if char in line_styles:
        return render_rule(style=line_styles[char], width=width)
    stack = _stack(font, size)
    unit = max(1, stack.textlength(char))
    count = max(1, int(width / unit))
    return render_text(char * count, size=size, align="center", font=font, width=width)


def render_checkbox(
    text: str,
    *,
    checked: bool = False,
    mark: str = "check",
    size: int = 28,
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    line_spacing: int = 4,
    padding: int = 4,
    indent: int = 0,
) -> Image.Image:
    """Render a vector checkbox followed by wrapped, hanging-indented text.

    The box is drawn (not glyph'd) so it stays crisp at any size on 1bpp paper.
    ``mark``: check / x / fill / none.
    """
    text = expand_icons(text)
    stack = _stack(font, size)
    ascent, descent = stack.getmetrics()
    line_height = ascent + descent + line_spacing

    box = max(10, round(size * 0.7))
    gap = max(6, size // 4)
    text_x = padding + indent + box + gap
    max_text_width = width - text_x - padding
    lines = _wrap(text, stack, max_text_width) or [""]
    height = padding * 2 + line_height * len(lines)

    image = _blank(height, width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"

    # Box vertically centred on the first text line.
    first_baseline = padding + ascent
    by0 = first_baseline - box + max(0, (box - ascent) // 2)
    bx0 = padding + indent
    stroke = max(2, box // 12)
    draw.rectangle([(bx0, by0), (bx0 + box, by0 + box)], outline=BLACK, width=stroke)

    if checked and mark != "none":
        if mark == "fill":
            draw.rectangle(
                [(bx0 + stroke + 1, by0 + stroke + 1), (bx0 + box - stroke - 1, by0 + box - stroke - 1)],
                fill=BLACK,
            )
        elif mark == "x":
            m = stroke + 2
            draw.line([(bx0 + m, by0 + m), (bx0 + box - m, by0 + box - m)], fill=BLACK, width=stroke)
            draw.line([(bx0 + box - m, by0 + m), (bx0 + m, by0 + box - m)], fill=BLACK, width=stroke)
        else:  # check
            draw.line(
                [
                    (bx0 + box * 0.20, by0 + box * 0.55),
                    (bx0 + box * 0.42, by0 + box * 0.78),
                    (bx0 + box * 0.82, by0 + box * 0.25),
                ],
                fill=BLACK,
                width=stroke,
                joint="curve",
            )

    for i, line in enumerate(lines):
        baseline = padding + ascent + i * line_height
        stack.draw(draw, text_x, baseline, line, fill=BLACK)
    return image


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
    stack = _stack(font, size)
    ascent, descent = stack.getmetrics()
    line_height = ascent + descent + line_spacing

    image = _blank(padding * 2 + line_height * len(rows), width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    y = padding
    for row in rows:
        baseline = y + ascent
        for col in range(columns):
            cell = expand_icons(row[col]) if col < len(row) else ""
            cell = _ellipsize(stack, cell, col_width)
            align = aligns[col] if aligns and col < len(aligns) else "left"
            cell_x = padding + col * col_width
            cell_w = stack.textlength(cell)
            if align == "center":
                x = cell_x + (col_width - cell_w) / 2
            elif align == "right":
                x = cell_x + col_width - cell_w
            else:
                x = cell_x
            stack.draw(draw, x, baseline, cell, fill=BLACK)
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
    stack = _stack(font, size)
    ascent, descent = stack.getmetrics()
    line_height = ascent + descent + line_spacing
    pairs = [p for p in pairs if p]

    image = _blank(padding * 2 + line_height * max(1, len(pairs)), width)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    y = padding
    for pair in pairs:
        baseline = y + ascent
        key = expand_icons(str(pair[0])) if len(pair) > 0 else ""
        value = expand_icons(str(pair[1])) if len(pair) > 1 else ""
        stack.draw(draw, padding, baseline, key, fill=BLACK)
        value_w = stack.textlength(value)
        stack.draw(draw, width - padding - value_w, baseline, value, fill=BLACK)
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
        stack = _stack(font, max(12, size // 2))
        unit = max(1, stack.textlength(char))
        count = max(1, int(width / unit))
        border = char * count
        ascent, _ = stack.getmetrics()
        border_w = stack.textlength(border)
        bx = (width - border_w) / 2  # centre the (possibly sub-width) border row
        stack.draw(draw, bx, ascent, border, fill=BLACK)
        stack.draw(draw, bx, height - 1, border, fill=BLACK)
    else:
        draw.rectangle([(1, 1), (width - 2, height - 2)], outline=BLACK, width=2)
    return image


# --- compositor ------------------------------------------------------------


def vstack(images: list[Image.Image], *, gap: int = 0, width: int = PRINTER_WIDTH) -> Image.Image:
    """Vertically stack full-width images into one, with ``gap`` px between them."""
    images = [im for im in images if im is not None]
    if not images:
        return _blank(1, width)
    total = sum(im.height for im in images) + gap * (len(images) - 1)
    canvas = _blank(total, width)
    y = 0
    for im in images:
        if im.mode != "L":
            im = im.convert("L")
        if im.width != width:
            im = _place(im, "left", width) if im.width < width else im.resize(
                (width, max(1, round(im.height * width / im.width)))
            )
        canvas.paste(im, (0, y))
        y += im.height + gap
    return canvas


def _bint(block: dict, key: str, default: int) -> int:
    """Coerce a document block field to int, with a clear error on bad input."""
    value = block.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"document block type={block.get('type', 'text')!r}: "
            f"{key!r} must be a number, got {value!r}"
        ) from err


def _render_block(block: dict, *, doc_font: str, width: int) -> Image.Image | None:
    """Render a single document block to a full-width image."""
    btype = str(block.get("type", "text")).lower()
    font = block.get("font", doc_font)

    if btype == "space":
        return _blank(_bint(block, "height", 16), width)

    if btype in ("rule", "separator", "hr"):
        return render_rule(
            style=block.get("style", "solid"),
            thickness=_bint(block, "thickness", 2),
            margin=_bint(block, "margin", 8),
            width=width,
        )

    if btype == "header":
        return render_text(
            str(block.get("text", "")),
            size=_bint(block, "size", 48),
            align=block.get("align", "center"),
            bold=block.get("bold", True),
            underline=block.get("underline", "none"),
            font=font,
            width=width,
        )

    if btype == "text":
        return render_text(
            str(block.get("text", "")),
            size=_bint(block, "size", 28),
            align=block.get("align", "left"),
            bold=block.get("bold", False),
            underline=block.get("underline", "none"),
            font=font,
            width=width,
            line_spacing=_bint(block, "line_spacing", 4),
        )

    if btype == "checkbox":
        return render_checkbox(
            str(block.get("text", "")),
            checked=bool(block.get("checked", False)),
            mark=block.get("mark", "check"),
            size=_bint(block, "size", 28),
            font=font,
            width=width,
            indent=_bint(block, "indent", 0),
        )

    if btype == "qr":
        return render_qr(
            str(block.get("data", "")),
            scale=_bint(block, "scale", 6),
            ec=block.get("ec", "M"),
            align=block.get("align", "center"),
            width=width,
        )

    if btype == "barcode":
        return render_barcode(
            str(block.get("data", "")),
            code=block.get("code", "code128"),
            align=block.get("align", "center"),
            write_text=bool(block.get("write_text", True)),
            width=width,
        )

    if btype == "table":
        return render_table(
            block.get("rows", []),
            aligns=block.get("aligns"),
            size=_bint(block, "size", 24),
            font=font,
            width=width,
        )

    if btype == "kvtable":
        rows = block.get("rows", [])
        if isinstance(rows, dict):
            rows = [[str(k), str(v)] for k, v in rows.items()]
        return render_kvtable(rows, size=_bint(block, "size", 24), font=font, width=width)

    if btype == "box":
        return render_box(
            str(block.get("text", "")),
            style=block.get("style", "line"),
            size=_bint(block, "size", 28),
            align=block.get("align", "left"),
            font=font,
            width=width,
        )

    if btype == "image":
        source = block.get("_image")
        if source is None:
            _LOGGER.warning("Document image block has no resolved image; skipping")
            return None
        return process_image(
            source,
            image_width=_bint(block, "image_width", width),
            rotation=_bint(block, "rotation", 0),
            mirror=bool(block.get("mirror", False)),
            invert=bool(block.get("invert", False)),
            dither=block.get("dither", "floyd-steinberg"),
            threshold=_bint(block, "threshold", 128),
            align=block.get("align", "left"),
            width=width,
        )

    _LOGGER.warning("Unknown document block type %r; skipping", btype)
    return None


def render_document(
    blocks: list[dict],
    *,
    font: str = DEFAULT_FONT,
    width: int = PRINTER_WIDTH,
    gap: int = 0,
) -> Image.Image:
    """Compose a list of blocks into one image for a single print job.

    Block types: header, text, rule (a.k.a. separator/hr), checkbox, qr, barcode,
    table, kvtable, box, image, space.  Stacking them here means an entire
    receipt prints in one BLE transmission instead of one job (and feed) per
    element.
    """
    images = [_render_block(b, doc_font=font, width=width) for b in blocks if b]
    return vstack(images, gap=gap, width=width)


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


def _ellipsize(stack: FontStack, text: str, max_width: int) -> str:
    if stack.textlength(text) <= max_width:
        return text
    while text and stack.textlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"
