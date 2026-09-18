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
Renders and prints the per-DUT label for a tested FaderBuddy board.

The label is a 2x1 inch die-cut label on the ORGSTA T001 (203 dpi), rendered
here as a 1-bit image and sent as a single TSPL BITMAP by `label_printer`.
Rendering in PIL rather than using the printer's built-in TEXT command is what
lets the lettermark use the Righteous typeface.

The printer's origin sits a little inside the physical label, so everything is
kept within SAFE_MARGIN_DOTS of the edge; a 1mm inset was measured to be fully
visible on all four edges.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

from label_printer import DPI, TsplPrinter, mm_to_dots

LABEL_WIDTH_MM = 50.8
LABEL_HEIGHT_MM = 25.4

LABEL_WIDTH_DOTS = mm_to_dots(LABEL_WIDTH_MM)    # 406
LABEL_HEIGHT_DOTS = mm_to_dots(LABEL_HEIGHT_MM)  # 203

# Measured: a box inset 8 dots prints fully on all four edges, one inset 0 dots
# loses its right and bottom lines. Sit well inside that so label-to-label
# registration drift can't eat the content.
SAFE_MARGIN_DOTS = 16

LETTERMARK_FONT = Path("/home/scott/.local/share/fonts/Righteous-Regular.ttf")
TEXT_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
BOLD_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MONO_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
# Terminus is a real pixel font shipped as an OpenType bitmap, so at its native
# 8x16 strike it renders with no antialiasing at all - nothing for the 1-bit
# threshold to chew up, which a small outline face would suffer from.
FOOTER_FONT = Path("/usr/share/fonts/opentype/terminus/terminus-normal.otb")
FOOTER_SIZE = 16
# Clearance between the QR block and the footer band below it.
QR_FOOTER_GAP = 6
# The QR sits closer to the top edge than the text does: it carries its own
# one-module white quiet zone (4 dots), so its ink never reaches this margin.
QR_TOP_MARGIN = 8

LETTERMARK = "FaderBuddy"


FOOTER = "designed/tested/packed in oakland, ca"
QR_CAPTION = "TEST REPORT"
QR_CAPTION_SIZE = 14
# Gap between the QR and its caption, and between the caption and the footer.
QR_CAPTION_GAP = 2


@dataclass
class DutLabel:
    """Everything that goes on one DUT label."""
    serial: str
    firmware: str = ""
    tested_at: str = ""      # Already-formatted date/time string
    duration: str = ""       # Already-formatted duration, e.g. "42.1 s"
    result: str = ""         # "PASS" / "FAIL"
    qr_data: str = ""        # Usually the results URL; falls back to the serial


def _font(path: Path, size: int):
    return ImageFont.truetype(str(path), size)


def _render_qr(data: str, target_dots: int) -> Image.Image:
    """Render `data` as a QR whose module size divides evenly into the target.

    Scaling a QR by a non-integer factor smears module edges at 203 dpi, so the
    box size is floored and the resulting image is a little under the target.
    """
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=1, border=1)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.modules_count + 2 * qr.border
    box_size = max(1, target_dots // modules)
    image = qr.make_image(fill_color="black", back_color="white").convert("L")
    return image.resize((modules * box_size, modules * box_size), Image.NEAREST)


def render(label: DutLabel) -> Image.Image:
    """Render a DUT label to a white-background greyscale image."""
    image = Image.new("L", (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS), 255)
    draw = ImageDraw.Draw(image)

    top = SAFE_MARGIN_DOTS
    bottom = LABEL_HEIGHT_DOTS - SAFE_MARGIN_DOTS
    left = SAFE_MARGIN_DOTS
    right = LABEL_WIDTH_DOTS - SAFE_MARGIN_DOTS

    # Footer first: it owns the bottom band, and the QR is then sized to fit
    # what's left. Sizing the QR first (and hoping the rest fits) is what let a
    # longer report URL grow the QR into the footer.
    footer_font = _font(FOOTER_FONT, FOOTER_SIZE)
    footer_top = bottom - FOOTER_SIZE

    # QR block, top-right, as large as the band above the footer allows once its
    # caption is accounted for.
    caption_font = _font(TEXT_FONT, QR_CAPTION_SIZE)
    caption_band = QR_CAPTION_SIZE + QR_CAPTION_GAP
    qr_image = _render_qr(label.qr_data or label.serial,
                          footer_top - QR_FOOTER_GAP - caption_band - QR_TOP_MARGIN)
    qr_x = right - qr_image.width
    image.paste(qr_image, (qr_x, QR_TOP_MARGIN))

    # Caption tucked directly under the QR, centred on it.
    while draw.textlength(QR_CAPTION, font=caption_font) > qr_image.width and caption_font.size > 8:
        caption_font = _font(TEXT_FONT, caption_font.size - 1)
    caption_x = qr_x + (qr_image.width - draw.textlength(QR_CAPTION, font=caption_font)) / 2
    caption_y = QR_TOP_MARGIN + qr_image.height + QR_CAPTION_GAP
    draw.text((caption_x, caption_y), QR_CAPTION, font=caption_font, fill=0)

    text_right = qr_x - 10
    text_width = text_right - left

    def fitted(path, size, text, min_size=8):
        """Largest font <= `size` at which `text` fits the text column."""
        font = _font(path, size)
        while draw.textlength(text, font=font) > text_width and size > min_size:
            size -= 1
            font = _font(path, size)
        return font

    # Lettermark.
    mark_font = fitted(LETTERMARK_FONT, 32, LETTERMARK, min_size=12)
    draw.text((left, top - 3), LETTERMARK, font=mark_font, fill=0)

    y = top - 3 + mark_font.size + 6
    draw.line([(left, y), (text_right, y)], fill=0, width=2)
    y += 8

    # Identity block: what this board is. Serial in mono so the hex stays readable.
    serial_font = fitted(MONO_FONT, 20, label.serial)
    draw.text((left, y), label.serial, font=serial_font, fill=0)
    y += serial_font.size + 5

    if label.firmware:
        fw_font = fitted(TEXT_FONT, 19, label.firmware)
        draw.text((left, y), label.firmware, font=fw_font, fill=0)
        y += fw_font.size + 4

    # Test block: what happened to it, set off from the identity block above.
    y += 8

    if label.tested_at:
        tested = f"TESTED {label.tested_at}"
        tested_font = fitted(TEXT_FONT, 19, tested)
        draw.text((left, y), tested, font=tested_font, fill=0)
        y += tested_font.size + 4

    if label.result:
        # Same size as the Tested line above it so the two read as a pair of
        # fields; only the verdict itself is bolded.
        runs = [("RESULT: ", TEXT_FONT), (label.result, BOLD_FONT)]
        if label.duration:
            runs.append((f" ({label.duration})", TEXT_FONT))
        size = 19
        while size > 8:
            fonts = [_font(path, size) for _, path in runs]
            if sum(draw.textlength(text, font=font)
                   for (text, _), font in zip(runs, fonts)) <= text_width:
                break
            size -= 1
        x = left
        for (text, _), font in zip(runs, fonts):
            draw.text((x, y), text, font=font, fill=0)
            x += draw.textlength(text, font=font)
        y += size + 4

    # Footer, centred on the full label width along the bottom edge.
    footer_width = draw.textlength(FOOTER, font=footer_font)
    draw.text(((LABEL_WIDTH_DOTS - footer_width) / 2, footer_top), FOOTER,
              font=footer_font, fill=0)

    if y > footer_top - 2:
        logging.warning("Label text column overruns the footer by %d dots", y - (footer_top - 2))
    qr_block_bottom = caption_y + caption_font.size
    if qr_block_bottom > footer_top - 2:
        logging.warning("QR block overruns the footer by %d dots",
                        qr_block_bottom - (footer_top - 2))

    return image


def print_label(label: DutLabel, printer: TsplPrinter = None, copies: int = 1, **print_kwargs):
    """Render and print one DUT label, opening the printer if one isn't given."""
    image = render(label)
    if printer is not None:
        printer.print_image(image, LABEL_WIDTH_MM, LABEL_HEIGHT_MM, copies=copies, **print_kwargs)
        return
    with TsplPrinter() as owned:
        owned.print_image(image, LABEL_WIDTH_MM, LABEL_HEIGHT_MM, copies=copies, **print_kwargs)


def main():
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Render and print a FaderBuddy DUT label")
    parser.add_argument("serial", help="DUT serial number")
    parser.add_argument("--firmware", default="", help='e.g. "FW 1.4"')
    parser.add_argument("--tested-at", default="", help="Formatted test date/time")
    parser.add_argument("--duration", default="", help='Formatted duration, e.g. "42.1 s"')
    parser.add_argument("--result", default="", help='"PASS" or "FAIL"')
    parser.add_argument("--qr-data", default="", help="QR payload; defaults to the serial")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--density", type=int, default=8)
    parser.add_argument("--speed", type=int, default=4)
    parser.add_argument("--preview", metavar="PNG",
                        help="Write the rendered label here instead of printing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    label = DutLabel(serial=args.serial, firmware=args.firmware, tested_at=args.tested_at,
                     duration=args.duration, result=args.result, qr_data=args.qr_data)

    if args.preview:
        render(label).save(args.preview)
        print(f"Wrote {args.preview} ({LABEL_WIDTH_DOTS}x{LABEL_HEIGHT_DOTS} dots @ {DPI} dpi)")
        return

    print_label(label, copies=args.copies, density=args.density, speed=args.speed)
    time.sleep(0.5)


if __name__ == "__main__":
    main()
