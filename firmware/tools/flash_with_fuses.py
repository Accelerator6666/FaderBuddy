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
"""Flash one or more Intel-hex images (and optionally the BOOTEND/APPEND fuses)
to an ATtiny1616 over serial UPDI using pymcuprog.

Used by the bootloader-related PlatformIO environments (see platformio.ini) to
install the boot section, the offset application, and the correct fuses in a
single UPDI upload. With --erase the first --hex is preceded by a full chip
erase (used when installing the bootloader / doing a factory flash); additional
--hex files are always written without a chip erase (disjoint flash regions,
page erases happen per write). Without --erase nothing is chip-erased, so you
can re-flash just the offset application on top of an existing bootloader.

Everything runs in ONE pymcuprog session. The serial UPDI link is bound by USB
round-trip latency (~2.4ms per transaction on the CH340 adapter, whatever the
baud), so re-opening the port and re-entering programming mode once per image
and once per fuse - as separate pymcuprog CLI calls did - was a large share of
the flash time.
"""

import argparse
import sys

from pymcuprog.backend import Backend, SessionConfig
from pymcuprog.deviceinfo.deviceinfokeys import DeviceMemoryInfoKeys
from pymcuprog.deviceinfo.memorynames import MemoryNameAliases, MemoryNames
from pymcuprog.hexfileutils import read_memories_from_hex
from pymcuprog.toolconnection import ToolSerialConnection

DEVICE = "attiny1616"

# ATtiny1616 fuse byte numbers (see ATtiny1614/16/17 datasheet + megaTinyCore).
FUSE_APPEND = 7
FUSE_BOOTEND = 8

# The UPDI's baud is recovered from each SYNCH character, but on the default
# 4 MHz UPDI clock the datasheet's recommended maximum is 225 kbps (Table 33-1)
# and pymcuprog never raises UPDICLKSEL. 230400 is the conventional ceiling for
# that clock (megaTinyCore's SerialUPDI default); go no higher.
MAX_BAUD = 230400


def parse_int(value):
    return int(value, 0)


def write_and_verify(backend, data, memory_name, offset):
    backend.write_memory(data, memory_name, offset)
    if not backend.verify_memory(data, memory_name, offset):
        raise SystemExit("verify failed: %s @ 0x%X" % (memory_name, offset))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True, help="serial UPDI port")
    p.add_argument(
        "--hex",
        action="append",
        default=[],
        dest="hexes",
        help="Intel-hex image to write (repeatable; --erase chip-erases before the first)",
    )
    p.add_argument("--bootend", type=parse_int, default=None,
                   help="BOOTEND fuse value (256-byte units), e.g. 0x08")
    p.add_argument("--append", type=parse_int, default=None,
                   help="APPEND fuse value (256-byte units), e.g. 0x00")
    p.add_argument("--erase", action="store_true",
                   help="chip-erase before writing the first hex (bootloader install / factory)")
    p.add_argument("--erase-only", action="store_true",
                   help="just chip-erase and exit (no --hex required, fuses untouched)")
    p.add_argument("--baud", type=int, default=None,
                   help="UPDI baud (default: pymcuprog's 115200; max %d)" % MAX_BAUD)
    args = p.parse_args()

    if not args.erase_only and not args.hexes:
        p.error("--hex is required unless --erase-only is given")
    if args.baud is not None and args.baud > MAX_BAUD:
        p.error("--baud above %d is out of spec on the default UPDI clock" % MAX_BAUD)

    backend = Backend()
    backend.connect_to_tool(ToolSerialConnection(serialport=args.port, baudrate=args.baud))
    try:
        backend.start_session(SessionConfig(DEVICE))
        try:
            device_id = backend.read_device_id()
            print("Device ID: " + "".join("%02X" % b for b in reversed(device_id)), flush=True)

            if args.erase or args.erase_only:
                print("Chip erase...", flush=True)
                backend.erase(MemoryNameAliases.ALL, address=None)
            if args.erase_only:
                print("Erase complete.", flush=True)
                return

            for hexfile in args.hexes:
                for segment in read_memories_from_hex(hexfile, backend.device_memory_info):
                    name = segment.memory_info[DeviceMemoryInfoKeys.NAME]
                    print("Writing %s: %u bytes @ 0x%X from %s"
                          % (name, len(segment.data), segment.offset, hexfile), flush=True)
                    write_and_verify(backend, segment.data, name, segment.offset)

            if args.append is not None:
                write_and_verify(backend, bytearray([args.append]), MemoryNames.FUSES, FUSE_APPEND)
            if args.bootend is not None:
                write_and_verify(backend, bytearray([args.bootend]), MemoryNames.FUSES, FUSE_BOOTEND)
        finally:
            backend.end_session()
    finally:
        backend.disconnect_from_tool()

    print("Flash complete.", flush=True)


if __name__ == "__main__":
    main()
