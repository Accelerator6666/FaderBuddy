#!/usr/bin/env python3
# Copyright 2026 Scott Bezek
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Minimal TSPL label printer driver over USB bulk transfers.

Reimplements just the parts of the "ORGSTA Printer" Chrome extension that we
need: the extension pairs over WebUSB, claims interface 0 of a USB
printer-class device, and bulk-writes a TSPL job to the OUT endpoint. There is
no handshake and nothing is read back, so a job is just a byte stream.

A job looks like:

    SIZE 50.8 mm,25.4 mm
    GAP 2 mm,0
    DIRECTION 1,0
    SPEED 4
    DENSITY 8
    CLS
    BITMAP 0,0,<row_bytes>,<height>,0,<packed rows>
    PRINT 1,1

Bitmap bits are 1 = white, 0 = black (TSPL OVERWRITE mode), rows padded to a
whole number of bytes.
"""

import logging
import time

import usb.core
import usb.util

# Zhuhai Poskey T001. Other models in the extension's printer-mapping.json
# (T003, T001Plus) speak the same TSPL dialect and differ only in paper width.
DEFAULT_VENDOR_ID = 0x2D84
DEFAULT_PRODUCT_ID = 0x2786

USB_CLASS_PRINTER = 0x07

DPI = 203
MM_PER_INCH = 25.4

# Chunk bulk writes so a large bitmap can't blow a single transfer's timeout.
_CHUNK_BYTES = 4096
_WRITE_TIMEOUT_MS = 5000


def mm_to_dots(mm: float, dpi: int = DPI) -> int:
    """Convert millimetres to printer dots, rounded to the nearest dot."""
    return int(round(mm / MM_PER_INCH * dpi))


class PrinterNotFoundError(Exception):
    pass


class TsplPrinter:
    """A TSPL printer on the USB bus, opened lazily and closed on exit."""

    def __init__(self, vendor_id: int = DEFAULT_VENDOR_ID, product_id: int = DEFAULT_PRODUCT_ID,
                 dpi: int = DPI):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.dpi = dpi
        self._device = None
        self._out_ep = None
        self._in_ep = None
        self._detached_interface = None

    # -- connection ------------------------------------------------------

    def open(self):
        if self._device is not None:
            return

        device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
        if device is None:
            raise PrinterNotFoundError(
                f"No USB device {self.vendor_id:04x}:{self.product_id:04x} found. "
                "Is the printer plugged in and powered on?")

        # usblp (or anything else) holding the interface would make claiming it
        # fail; take it back the way the WebUSB stack does.
        config = device.get_active_configuration()
        interface = self._find_printer_interface(config)
        if device.is_kernel_driver_active(interface.bInterfaceNumber):
            logging.debug("Detaching kernel driver from interface %d", interface.bInterfaceNumber)
            device.detach_kernel_driver(interface.bInterfaceNumber)
            self._detached_interface = interface.bInterfaceNumber

        usb.util.claim_interface(device, interface.bInterfaceNumber)

        self._out_ep = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self._in_ep = usb.util.find_descriptor(
            interface,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if self._out_ep is None:
            raise PrinterNotFoundError("Printer interface has no bulk OUT endpoint")

        self._device = device
        self._interface = interface
        logging.debug("Opened %04x:%04x, OUT endpoint 0x%02x",
                      self.vendor_id, self.product_id, self._out_ep.bEndpointAddress)

    @staticmethod
    def _find_printer_interface(config):
        interface = usb.util.find_descriptor(config, bInterfaceClass=USB_CLASS_PRINTER)
        if interface is not None:
            return interface
        # Some clones don't declare the printer class; the extension just takes
        # the first interface of the first configuration, so fall back to that.
        return config[(0, 0)]

    def close(self):
        if self._device is None:
            return
        usb.util.release_interface(self._device, self._interface.bInterfaceNumber)
        if self._detached_interface is not None:
            try:
                self._device.attach_kernel_driver(self._detached_interface)
            except usb.core.USBError:
                pass  # Nothing was bound to it before us, or it's already back.
            self._detached_interface = None
        usb.util.dispose_resources(self._device)
        self._device = None
        self._out_ep = None
        self._in_ep = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- raw transport ---------------------------------------------------

    def write(self, data: bytes):
        """Bulk-write raw bytes to the printer."""
        self.open()
        for offset in range(0, len(data), _CHUNK_BYTES):
            chunk = data[offset:offset + _CHUNK_BYTES]
            written = self._out_ep.write(chunk, _WRITE_TIMEOUT_MS)
            if written != len(chunk):
                raise IOError(f"Short USB write: {written} of {len(chunk)} bytes")

    def command(self, text: str):
        """Send one TSPL command line (CR/LF terminated, as the extension does)."""
        self.write(text.encode("ascii", errors="replace") + b"\r\n")

    # -- TSPL jobs -------------------------------------------------------

    def self_test(self):
        """Print the printer's own configuration/test page."""
        self.command("SELFTEST")

    def form_feed(self):
        self.command("FORMFEED")

    def gap_detect(self):
        """Re-measure the label gap. Feeds a few labels."""
        self.command("GAPDETECT")

    def print_image(self, image, width_mm: float, height_mm: float, gap_mm=2.0,
                    gap_offset_mm: float = 0.0, direction: int = 1, speed: int = 4,
                    density: int = 8, copies: int = 1, x_dots: int = 0, y_dots: int = 0,
                    threshold: int = 128):
        """Print a PIL image as a full-label bitmap.

        `gap_mm` may be None to leave the printer's stored media setting alone.
        `threshold` is the 0-255 grey level at or above which a pixel is white.
        """
        job = bytearray()
        job += f"SIZE {width_mm} mm,{height_mm} mm\r\n".encode("ascii")
        if gap_mm is not None:
            job += f"GAP {gap_mm} mm,{gap_offset_mm} mm\r\n".encode("ascii")
        job += f"DIRECTION {direction},0\r\n".encode("ascii")
        job += f"SPEED {speed}\r\n".encode("ascii")
        job += f"DENSITY {density}\r\n".encode("ascii")
        job += b"CLS\r\n"
        job += bitmap_command(image, x_dots, y_dots, threshold)
        job += b"\r\n"
        job += f"PRINT {copies},1\r\n".encode("ascii")

        logging.debug("Sending %d byte TSPL job", len(job))
        self.write(bytes(job))


def bitmap_command(image, x_dots: int = 0, y_dots: int = 0, threshold: int = 128) -> bytes:
    """Pack a PIL image into a TSPL `BITMAP` command in OVERWRITE mode.

    A set bit is a white dot, so the image is thresholded with light pixels
    becoming 1s. Rows are padded out to whole bytes, matching the extension's
    align-width-to-8 behaviour.
    """
    grey = image.convert("L")
    width, height = grey.size
    row_bytes = (width + 7) // 8
    pixels = grey.load()

    data = bytearray(row_bytes * height)
    for y in range(height):
        row_start = y * row_bytes
        for x in range(width):
            if pixels[x, y] >= threshold:
                data[row_start + (x >> 3)] |= 0x80 >> (x & 7)

    header = f"BITMAP {x_dots},{y_dots},{row_bytes},{height},0,".encode("ascii")
    return bytes(header + data)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Low-level TSPL label printer utility")
    parser.add_argument("--vid", type=lambda v: int(v, 0), default=DEFAULT_VENDOR_ID)
    parser.add_argument("--pid", type=lambda v: int(v, 0), default=DEFAULT_PRODUCT_ID)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="Open the printer and report what was found")
    sub.add_parser("selftest", help="Print the printer's own test page")
    sub.add_parser("formfeed", help="Feed one label")
    sub.add_parser("gapdetect", help="Re-measure the label gap (feeds several labels)")

    raw = sub.add_parser("raw", help="Send raw TSPL command lines from stdin or --text")
    raw.add_argument("--text", help="Command text; newlines separate commands")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    with TsplPrinter(args.vid, args.pid) as printer:
        if args.cmd == "info":
            device = printer._device
            print(f"Device:       {device.idVendor:04x}:{device.idProduct:04x}")
            print(f"Manufacturer: {usb.util.get_string(device, device.iManufacturer)}")
            print(f"Product:      {usb.util.get_string(device, device.iProduct)}")
            print(f"Interface:    {printer._interface.bInterfaceNumber} "
                  f"(class {printer._interface.bInterfaceClass})")
            print(f"OUT endpoint: 0x{printer._out_ep.bEndpointAddress:02x}")
            if printer._in_ep is not None:
                print(f"IN endpoint:  0x{printer._in_ep.bEndpointAddress:02x}")
        elif args.cmd == "selftest":
            printer.self_test()
        elif args.cmd == "formfeed":
            printer.form_feed()
        elif args.cmd == "gapdetect":
            printer.gap_detect()
        elif args.cmd == "raw":
            import sys
            text = args.text if args.text is not None else sys.stdin.read()
            for line in text.splitlines():
                if line.strip():
                    printer.command(line.strip())
        # The printer needs a moment to drain before the interface is released.
        time.sleep(0.5)


if __name__ == "__main__":
    main()
