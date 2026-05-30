# Home Assistant — Core Innovations CTP500

A custom [Home Assistant](https://www.home-assistant.io/) integration that prints
receipts (text, images, QR codes, barcodes, tables…) to a **Core Innovations /
DP Audio Video CTP500** BLE thermal printer — including over an **ESPHome
Bluetooth proxy** when the printer is out of range of your Home Assistant host.

The CTP500 is a BLE "cat printer" (M02 / MX family). It is **not** an ESC/POS
device: it has no on-board fonts or symbologies. Everything is rendered to a
384 px (48 byte) wide 1-bit-per-pixel bitmap by this integration and streamed to
the printer one row at a time using the cat-printer protocol.

The service surface intentionally mirrors the receipt-printer ergonomics of
[`ha-escpos-thermal-printer`](https://github.com/cognitivegears/ha-escpos-thermal-printer):
small, discrete services with simple parameters.

## Features

- Auto-discovery over Bluetooth (matches the AE00 GATT service), proxy-aware
  connection management (`bleak-retry-connector` / `establish_connection`).
- Discrete receipt services: `print_text`, `print_image`, `print_qr`,
  `print_barcode`, `print_separator`, `print_table`, `print_kvtable`,
  `print_box`, `feed`, and a `print_test` calibration strip.
- **Preview** any print service with `preview: true` to render to the
  *Last Receipt* image entity without using paper.
- Per-device targeting via `device_id` (omit to broadcast to every printer).
- Tunable print **speed**, **energy** (darkness), **feed**, and proxy pacing
  (write chunk size + inter-write delay) in the options flow.

## Installation

1. Copy `custom_components/coreinnovations` into your Home Assistant
   `config/custom_components/` directory (or add this repository to HACS as a
   custom repository), then restart Home Assistant.
2. The printer should be discovered automatically under
   **Settings → Devices & Services**. If not, add **Core Innovations CTP500**
   manually; make sure the printer is powered on and advertising.

## Entities

| Entity | Description |
| --- | --- |
| `binary_sensor` *Connection* | On while a BLE connection is open (i.e. during a job). |
| `sensor` *Print Duration* | Live/last print duration in seconds. |
| `image` *Last Receipt* | The most recent rendered receipt (printed or previewed). |

## Services

All `print_*` services accept:

- `device_id` *(optional)* — one or more printers; omit to broadcast to all.
- `preview` *(optional, default `false`)* — render to the *Last Receipt* image
  entity instead of printing.
- `feed` *(optional)* — extra pixels to advance after printing.

### `coreinnovations.print_text`

```yaml
service: coreinnovations.print_text
data:
  text: |
    Order #1234
    Thanks for your purchase!
  size: 30
  align: center
  bold: true
```

### `coreinnovations.print_qr`

```yaml
service: coreinnovations.print_qr
data:
  data: "https://www.home-assistant.io"
  scale: 6
  ec: M
```

### `coreinnovations.print_barcode`

```yaml
service: coreinnovations.print_barcode
data:
  data: "012345678905"
  code: ean13
```

### `coreinnovations.print_image`

`image` may be an `http(s)` URL, an allowlisted local file path, a
`camera.<entity_id>`, or a `data:` URI.

```yaml
service: coreinnovations.print_image
data:
  image: "https://example.com/logo.png"
  dither: floyd-steinberg
  align: center
```

### `coreinnovations.print_table` / `print_kvtable`

```yaml
service: coreinnovations.print_table
data:
  rows:
    - ["Item", "Qty", "Price"]
    - ["Coffee", "2", "$6.00"]
    - ["Muffin", "1", "$3.50"]
  aligns: [left, center, right]
```

```yaml
service: coreinnovations.print_kvtable
data:
  rows:
    Subtotal: "$12.00"
    Tax: "$0.96"
    Total: "$12.96"
```

### `coreinnovations.print_box` / `print_separator`

```yaml
service: coreinnovations.print_box
data:
  text: "RECEIPT"
  style: line
  align: center
```

### `coreinnovations.feed` / `print_test`

```yaml
service: coreinnovations.print_test   # all-black calibration strip
```

```yaml
service: coreinnovations.feed
data:
  pixels: 100
```

## Tuning (options flow)

| Option | Default | Notes |
| --- | --- | --- |
| Print speed | 32 | Lower is faster; values below ~4 can stall the feed motor. |
| Darkness / energy | 24576 (`0x6000`) | Cat-Printer's "text" value; thin font strokes need it. Images default to 16384 (`0x4000`). Max 65535. |
| Feed after print | 128 px | Cat-Printer's finish feed; advances paper past the tear bar. |

Every print service also accepts per-call `energy`, `speed` and `feed` overrides,
so you can tune darkness for a single receipt without changing the defaults.
| Delay between BLE writes | 20 ms | Increase if a proxy drops data mid-print. |
| BLE write chunk size | 200 bytes | Bytes per `write_gatt_char` to the AE01 characteristic. |
| Keep BLE connection open | off | Stay connected between jobs (faster, uses more battery). |

## Protocol notes

- GATT: service `0000ae00-…`, write `0000ae01-…`, notify `0000ae02-…`.
- Frame: `0x51 0x78 <cmd> 0x00 <len_lo> <len_hi> <payload…> <crc8(payload)> 0xff`.
- Bitmap rows are 48 bytes, each byte **bit-reversed** before sending.
- Print sequence: get state → set DPI 200 → set speed → set energy →
  start lattice → draw rows → apply energy → feed → end lattice.

See `custom_components/coreinnovations/catprinter/commander.py` for the full
command set and CRC8 table.

## Credits

This integration stands on the shoulders of three excellent projects:

- **Cat-printer protocol** ported from
  [NaitLee/Cat-Printer](https://github.com/NaitLee/Cat-Printer) — the framing,
  CRC8 table, command set and default tuning (speed 32, text energy `0x6000`,
  image energy `0x4000`) for the M02/MX family.
- **Home Assistant / Bluetooth-proxy plumbing** adapted from
  [eigger/hass-niimbot](https://github.com/eigger/hass-niimbot) — discovery,
  config flow and proxy-aware connection management.
- **Receipt service ergonomics** modeled on
  [cognitivegears/ha-escpos-thermal-printer](https://github.com/cognitivegears/ha-escpos-thermal-printer)
  — the discrete `print_text` / `print_image` / `print_qr` / `print_table` /
  `print_kvtable` / `print_box` / `feed` service surface, `device_id` targeting
  and the preview workflow are all inspired by its ESC/POS integration.

## License

See [LICENSE](LICENSE).
