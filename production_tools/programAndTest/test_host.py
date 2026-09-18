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
Host-side test support script for programAndTest firmware.

This script communicates with the ESP32 test fixture over serial and handles
firmware upload requests by invoking PlatformIO commands.
"""

import argparse
import csv
import json
import logging
import os
import re
import secrets
import serial
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "firmware" / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools" / "report"))
from fb_image import BL_APPEND, BL_BOOTEND, BOOTLOADER_ENV, BOOTLOADER_HEX  # noqa: E402

# Commands from ESP32
CMD_PING = ">>PING<<"
CMD_START_UPLOAD = ">>START_FIRMWARE_UPLOAD<<"
CMD_SERIAL_PREFIX = ">>SERIAL:"  # Serial number format: >>SERIAL:AABBCCDDEEFF00112233<<
CMD_FW_VERSION_PREFIX = ">>FW_VERSION:"  # Installed app version, e.g. >>FW_VERSION:1.4<<
CMD_PROTOCOL_VERSION_PREFIX = ">>PROTOCOL_VERSION:"  # I2C protocol version, e.g. >>PROTOCOL_VERSION:5<<
CMD_TEST_START = ">>TEST_START<<"  # Sent once when the full test sequence begins
CMD_TEST_RESULT_PREFIX = ">>TEST_RESULT:"  # Followed by PASS<< or FAIL:<reason><<

# Responses to ESP32
# Datapoint reporting emitted by the jig (see the reporting block in
# src/main.cpp). Values never contain a colon, so the fields split cleanly.
PHASE_START_RE = re.compile(r">>PHASE_START:([A-Z0-9_]+)<<")
PHASE_END_RE = re.compile(r">>PHASE_END:([A-Z0-9_]+):(PASS|FAIL):(\d+)<<")
DATA_RE = re.compile(
    r">>DATA:([A-Z0-9_]+):([A-Za-z0-9_]+)=([^:<]*):([^:<]*):([^:<]*):([^:<]*):([^:<]*):([^:<]*)<<")

RESP_ACK = ">>ACK<<"
RESP_SUCCESS = ">>SUCCESS<<"
RESP_FAILURE = ">>FAILURE<<"

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

# Where the QR code on a DUT label points (see docs/HOSTING_TEST_REPORTS.md).
# The report URL is a capability URL: the token in it is not a salt (nothing is
# hashed with it) but the credential itself, so it has to be unguessable rather
# than merely non-obvious.
#
# 128 bits of token is free here. The /faderbuddy/ path already forces a
# version-4 QR (140 dots on the label) whatever the token length, and a v4
# symbol has room to spare - but only if the token is UPPERCASE hex, which QR
# encodes in its denser alphanumeric mode rather than byte mode. Lowercase would
# tip the same 16 bytes to a version-5 symbol and cost ~16 dots of the label.
# Hence .upper() in report_token(); don't "tidy" it away.
REPORT_URL_TEMPLATE = "https://qc.bezeklabs.com/faderbuddy/{serial}-{token}"
REPORT_TOKEN_BYTES = 16

CSV_COLUMNS = [
    "Batch ID",
    "Serial",
    "Start Time (epoch ms)",
    "End Time (epoch ms)",
    "End Time (ISO 8601)",
    "Duration (ms)",
    "Result",
    "Failure Details",
    "Test Host Git Commit",
    "Test Host Startup Time (epoch ms)",
    # Appended last so rows written before this column existed stay readable.
    "Firmware Version",
    "Report Token",
]


class TestHost:
    def __init__(self, port: str, updi_port: str = None, baud: int = 115200, dummy: bool = False,
                 batch_id: str = None, print_labels: bool = True,
                 upload_reports: bool = True):
        self.port = port
        self.updi_port = updi_port
        self.baud = baud
        self.dummy = dummy
        self.serial = None
        self.serial_number = None  # Store the last read serial number
        self.fw_version = None  # Firmware version the DUT reported after the I2C update
        self.protocol_version = None
        self.phases = []  # Datapoints for the current run, in the order reported

        # Determine paths relative to this script
        self.script_dir = Path(__file__).parent.absolute()
        self.repo_root = self.script_dir.parent.parent
        self.logs_dir = self.script_dir / "logs"

        # CSV result logging
        self.batch_id = batch_id or None
        self.print_labels = print_labels
        self.upload_reports = upload_reports
        self.csv_path = self.logs_dir / "results.csv"
        self._report_tokens = None  # serial -> report token, loaded from the CSV on first use
        self.startup_time_ms = int(time.time() * 1000)
        self.git_commit = self._get_git_commit()
        self.current_test_start_time_ms = None  # Set when >>TEST_START<< is received

        if self.batch_id:
            self._setup_batch_log_file()

        logging.info(f"Script directory: {self.script_dir}")
        logging.info(f"Repository root: {self.repo_root}")
        logging.info(f"Test host git commit: {self.git_commit}")
        if self.batch_id:
            logging.info(f"Batch ID: {self.batch_id} - results will be logged to {self.csv_path}")
        else:
            logging.warning("No Batch ID set - test results will NOT be logged to CSV")
        if self.dummy:
            logging.warning("DUMMY MODE ENABLED - Firmware uploads will be simulated")
        if not self.print_labels:
            logging.info("Label printing disabled for this run")
        if not self.upload_reports:
            logging.info("Report upload disabled for this run - reports are still "
                         "written locally and can be backfilled later")

    def _setup_batch_log_file(self):
        """Append all INFO-level (and above) log output for this run to a per-batch raw log file."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        safe_batch_id = re.sub(r'[^A-Za-z0-9_-]', '_', self.batch_id)
        log_path = self.logs_dir / f"batch_{safe_batch_id}_raw.txt"

        handler = logging.FileHandler(log_path)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)

        logging.info(f"Appending log output to {log_path}")

    def _get_git_commit(self) -> str:
        """Get the current git commit hash, with a '-dirty' suffix if this file has local changes."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.script_dir, capture_output=True, text=True, timeout=5,
            )
            commit = result.stdout.strip()
            if result.returncode != 0 or not commit:
                return "unknown"

            dirty = subprocess.run(
                ["git", "diff", "--quiet", "--", "test_host.py"],
                cwd=self.script_dir, timeout=5,
            ).returncode != 0
            return f"{commit}-dirty" if dirty else commit
        except Exception as e:
            logging.warning(f"Failed to determine git commit: {e}")
            return "unknown"

    def handle_report_line(self, line: str) -> bool:
        """Consume a PHASE_START / DATA / PHASE_END line. True if it was one."""
        match = PHASE_START_RE.search(line)
        if match:
            from report_render import Phase
            self.phases.append(Phase(key=match.group(1)))
            return True

        match = DATA_RE.search(line)
        if match:
            from report_render import Datapoint, Phase
            phase_key, key, value, unit, lo, hi, axis_lo, axis_hi = match.groups()
            # A datapoint should always fall inside its phase, but don't drop it
            # if the PHASE_START was garbled on the wire.
            if not self.phases or self.phases[-1].key != phase_key:
                self.phases.append(Phase(key=phase_key))
            self.phases[-1].data.append(Datapoint(
                key=key, value=value, unit=unit,
                limit_lo=lo, limit_hi=hi, axis_lo=axis_lo, axis_hi=axis_hi))
            return True

        match = PHASE_END_RE.search(line)
        if match:
            phase_key, result, elapsed = match.groups()
            for phase in reversed(self.phases):
                if phase.key == phase_key:
                    phase.result = result
                    phase.elapsed_ms = int(elapsed)
                    break
            return True

        return False

    def write_report(self, serial_number: str, token: str, end_time_ms: int,
                     duration_ms: int):
        """Write the per-DUT report as JSON plus rendered HTML.

        The JSON is the record: it holds everything the page is built from, so a
        later template change can be re-applied to old reports (see
        `report_render.py <file.json>`). Never fatal - a report problem must not
        fail a board that passed.
        """
        try:
            from report_render import Report, render
        except ImportError as e:
            logging.error(f"Report rendering unavailable ({e})")
            return None

        report = Report(
            serial=serial_number,
            token=token,
            result="PASS",
            firmware=self.fw_version or "",
            protocol=str(self.protocol_version) if self.protocol_version else "",
            tested_at=datetime.fromtimestamp(end_time_ms / 1000).astimezone(),
            tzname=datetime.fromtimestamp(end_time_ms / 1000).astimezone().tzname() or "",
            duration_ms=duration_ms,
            phases=self.phases,
        )
        try:
            reports_dir = self.logs_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            # Named by the object key they will be uploaded to, so the upload
            # step is a straight copy and a missing report is obvious.
            stem = f"{serial_number}-{token}"
            json_path = reports_dir / f"{stem}.json"
            html_path = reports_dir / f"{stem}.html"
            json_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
            html_path.write_text(render(report))
            logging.info(f"Wrote test report to {html_path.name} and "
                         f"{json_path.name} ({len(self.phases)} phases)")
        except Exception as e:
            logging.error(f"Failed to write test report for {serial_number}: {e}")
            return None

        if self.upload_reports:
            self.upload_report(html_path)
        return html_path

    def upload_report(self, html_path: Path):
        """Publish a report to qc.bezeklabs.com. Never fatal: the local copy is
        kept regardless, and `tools/report/upload_report.py --all` backfills."""
        try:
            from upload_report import UploadError, upload_report
        except ImportError as e:
            logging.error(f"Report upload unavailable ({e})")
            return
        try:
            upload_report(html_path)
        except UploadError as e:
            logging.error(f"{e} - report kept locally; re-run "
                          "tools/report/upload_report.py --all to retry")

    def _load_report_tokens(self) -> dict:
        """Read the serial -> report token mapping out of the existing CSV."""
        tokens = {}
        if not self.csv_path.exists():
            return tokens
        try:
            with open(self.csv_path, newline='') as f:
                for row in csv.DictReader(f):
                    # Rows written before the column existed simply have no token.
                    serial_number, token = row.get("Serial"), row.get("Report Token")
                    if serial_number and token:
                        tokens[serial_number] = token
        except Exception as e:
            logging.warning(f"Failed to read report tokens from {self.csv_path}: {e}")
        return tokens

    def report_token(self, serial_number: str) -> str:
        """The report token for a serial, generated once and stable across re-tests."""
        if self._report_tokens is None:
            self._report_tokens = self._load_report_tokens()
        token = self._report_tokens.get(serial_number)
        if token is None:
            token = secrets.token_hex(REPORT_TOKEN_BYTES).upper()
            self._report_tokens[serial_number] = token
            logging.info(f"Generated report token for {serial_number}")
        return token

    def print_dut_label(self, serial_number: str, token: str, end_time_ms: int, duration_ms: int):
        """Print the pass label for a DUT. Never fatal: a jammed printer shouldn't stop testing."""
        try:
            from dut_label import DutLabel, print_label
        except ImportError as e:
            logging.error(f"Label printing unavailable ({e}). Run test_host.py from the "
                          "programAndTest venv (.venv/bin/python) to enable it.")
            return

        label = DutLabel(
            serial=serial_number,
            firmware=f"FW {self.fw_version}" if self.fw_version else "",
            tested_at=datetime.fromtimestamp(end_time_ms / 1000).strftime("%Y-%m-%d %H:%M"),
            duration=f"{duration_ms / 1000:.1f} s",
            result="PASS",
            qr_data=REPORT_URL_TEMPLATE.format(serial=serial_number, token=token),
        )
        try:
            print_label(label)
            logging.info(f"Printed label for {serial_number}")
        except Exception as e:
            logging.error(f"Failed to print label for {serial_number}: {e}")

    def record_test_result(self, end_time_ms: int, result: str, failure_details: str):
        """Append a completed test's result to the CSV, if a batch ID and serial number are known."""
        if not self.batch_id:
            logging.debug("Not logging test result to CSV: no Batch ID set")
            return
        if not self.serial_number:
            logging.warning("Not logging test result to CSV: serial number is not known for this test")
            return
        if self.current_test_start_time_ms is None:
            logging.warning("Not logging test result to CSV: no recorded start time for this test")
            return

        start_time_ms = self.current_test_start_time_ms
        duration_ms = end_time_ms - start_time_ms
        report_token = self.report_token(self.serial_number)
        end_time_iso = datetime.fromtimestamp(end_time_ms / 1000).astimezone().isoformat()

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        write_header = not self.csv_path.exists()
        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_COLUMNS)
            writer.writerow([
                self.batch_id,
                self.serial_number,
                start_time_ms,
                end_time_ms,
                end_time_iso,
                duration_ms,
                result,
                failure_details,
                self.git_commit,
                self.startup_time_ms,
                self.fw_version or "",
                report_token,
            ])

        logging.info(f"Logged test result to {self.csv_path}: {result} (serial={self.serial_number})")

        # Reports and labels are both for shippable boards only; a failure is
        # captured by the CSV row and the raw batch log.
        if result == "PASS":
            self.write_report(self.serial_number, report_token, end_time_ms, duration_ms)
            if self.print_labels:
                self.print_dut_label(self.serial_number, report_token,
                                     end_time_ms, duration_ms)

        self.current_test_start_time_ms = None

    def connect(self):
        """Open serial connection to ESP32."""
        try:
            self.serial = serial.Serial(self.port, self.baud, timeout=1)
            logging.info(f"Connected to {self.port} at {self.baud} baud")
        except serial.SerialException as e:
            logging.error(f"Failed to open serial port {self.port}: {e}")
            raise

    def disconnect(self):
        """Close serial connection."""
        if self.serial and self.serial.is_open:
            self.serial.close()
            logging.info("Serial connection closed")

    def send_response(self, response: str):
        """Send a response to the ESP32."""
        if self.serial and self.serial.is_open:
            msg = f"{response}\n"
            self.serial.write(msg.encode('utf-8'))
            self.serial.flush()
            logging.info(f"Sent: {response}")

    def upload_firmware(self) -> bool:
        """
        UPDI-flash the DUT's starting state: the current bootloader plus a fixed
        FW_VERSION=0 application, via firmware/tools/flash_with_fuses.py.

        That makes the DUT "a board that already has the bootloader installed and
        is running an old application" -- the precondition the jig firmware needs
        in order to enter the I2C bootloader from a *running app* and update it to
        the current application in-band. The current application is deliberately
        not flashed here; it arrives over I2C, driven by the jig itself.

        The bootloader half is built fresh from source on every run, so a
        production board can never be shipped with a stale bootloader. Only the
        application half is a fixed checked-in image, because the test needs a
        genuinely old application to update away from.

        Returns:
            True if upload succeeded, False otherwise
        """
        logging.info("Starting firmware upload (current bootloader + fixed old app via UPDI)...")

        # Clear serial number and firmware version from previous upload
        self.serial_number = None
        self.fw_version = None

        # Dummy mode: simulate upload without actually running it
        if self.dummy:
            logging.info("DUMMY MODE: Simulating firmware upload (4 second delay)")
            time.sleep(4)
            logging.info("DUMMY MODE: Simulated upload complete - SUCCESS")
            return True

        try:
            # PlatformIO Core uses a fixed virtual environment location
            # See: https://docs.platformio.org/en/latest/core/installation/methods/installer-script.html
            home_dir = os.path.expanduser("~")
            pio_venv = os.path.join(home_dir, ".platformio", "penv")
            venv_python = os.path.join(pio_venv, "bin", "python")

            # Check if PlatformIO venv exists (flash_with_fuses.py needs pymcuprog,
            # which is installed into this venv by the firmware's PlatformIO envs)
            if not os.path.exists(venv_python):
                logging.error(f"PlatformIO virtual environment not found at {pio_venv}")
                logging.error("Please ensure PlatformIO is installed correctly")
                return False

            logging.info(f"Using PlatformIO venv: {pio_venv}")

            flash_script = self.repo_root / "firmware" / "tools" / "flash_with_fuses.py"
            old_app_hex = (self.script_dir / "factory_test_images" / "old_app_fw0.hex")
            if not old_app_hex.exists():
                logging.error(f"Fixed old-application image not found: {old_app_hex}")
                return False

            # Build the bootloader from source so every board gets the current one.
            logging.info(f"Building {BOOTLOADER_ENV}...")
            build = subprocess.run(
                [venv_python, "-m", "platformio", "run", "-e", BOOTLOADER_ENV],
                cwd=self.repo_root, capture_output=True, text=True, timeout=300,
            )
            if build.returncode != 0:
                logging.error(f"Bootloader build failed:\n{build.stdout}\n{build.stderr}")
                return False

            port = self.updi_port or "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
            if self.updi_port:
                logging.info(f"Using UPDI port override: {self.updi_port}")

            cmd = [
                venv_python, str(flash_script),
                "--port", port,
                "--hex", str(BOOTLOADER_HEX),
                "--hex", str(old_app_hex),
                "--erase",
                "--bootend", hex(BL_BOOTEND),
                "--append", hex(BL_APPEND),
            ]

            logging.info(f"Running: {' '.join(cmd)}")

            # Run the command
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=60,  # 60 second timeout for upload
            )

            # Log output
            if result.stdout:
                for line in result.stdout.splitlines():
                    logging.debug(f"flash_with_fuses stdout: {line}")
            if result.stderr:
                for line in result.stderr.splitlines():
                    logging.debug(f"flash_with_fuses stderr: {line}")

            if result.returncode == 0:
                logging.info("Firmware upload succeeded!")
                return True
            else:
                logging.error(f"Firmware upload failed with return code {result.returncode}")
                return False

        except subprocess.TimeoutExpired:
            logging.error("Firmware upload timed out")
            return False
        except Exception as e:
            logging.error(f"Exception during firmware upload: {e}")
            return False

    def process_command(self, line: str):
        """Process a command received from the ESP32."""
        line = line.strip()

        # Datapoint lines are frequent and carry no commands; take them first.
        if self.handle_report_line(line):
            return

        if CMD_PING in line:
            logging.info(f"Received: {CMD_PING}")
            self.send_response(RESP_ACK)

        elif CMD_START_UPLOAD in line:
            logging.info(f"Received: {CMD_START_UPLOAD}")
            success = self.upload_firmware()
            if success:
                self.send_response(RESP_SUCCESS)
            else:
                self.send_response(RESP_FAILURE)

        elif CMD_SERIAL_PREFIX in line:
            # Parse serial number from message: >>SERIAL:AABBCCDDEEFF00112233<<
            try:
                start_idx = line.index(CMD_SERIAL_PREFIX) + len(CMD_SERIAL_PREFIX)
                end_idx = line.index("<<", start_idx)
                serial_hex = line[start_idx:end_idx]

                # Validate it's 20 hex characters (10 bytes)
                if len(serial_hex) == 20 and all(c in '0123456789ABCDEFabcdef' for c in serial_hex):
                    self.serial_number = serial_hex.upper()
                    logging.info(f"Received serial number: {self.serial_number}")
                    print(f"\n*** SERIAL NUMBER: {self.serial_number} ***\n")
                else:
                    logging.warning(f"Invalid serial number format: {serial_hex}")
            except (ValueError, IndexError) as e:
                logging.error(f"Failed to parse serial number from: {line} - {e}")

        elif CMD_PROTOCOL_VERSION_PREFIX in line:
            # Parse the I2C protocol version: >>PROTOCOL_VERSION:5<<
            try:
                start_idx = (line.index(CMD_PROTOCOL_VERSION_PREFIX)
                             + len(CMD_PROTOCOL_VERSION_PREFIX))
                version = line[start_idx:line.index("<<", start_idx)]
                if re.fullmatch(r'\d+', version):
                    self.protocol_version = version
                    logging.info(f"Received protocol version: {version}")
                else:
                    logging.warning(f"Ignoring malformed protocol version: {version!r}")
            except (ValueError, IndexError) as e:
                logging.error(f"Failed to parse protocol version from: {line} - {e}")

        elif CMD_FW_VERSION_PREFIX in line:
            # Parse the installed firmware version: >>FW_VERSION:1.4<<
            try:
                start_idx = line.index(CMD_FW_VERSION_PREFIX) + len(CMD_FW_VERSION_PREFIX)
                end_idx = line.index("<<", start_idx)
                version = line[start_idx:end_idx]

                if re.fullmatch(r'\d+\.\d+', version):
                    self.fw_version = version
                    logging.info(f"Received firmware version: {self.fw_version}")
                else:
                    logging.warning(f"Invalid firmware version format: {version}")
            except (ValueError, IndexError) as e:
                logging.error(f"Failed to parse firmware version from: {line} - {e}")

        elif CMD_TEST_START in line:
            logging.info(f"Received: {CMD_TEST_START}")
            # Reset per-test state so a stale serial number from a prior unit can't
            # be attributed to this one if this test fails before re-reading it.
            self.serial_number = None
            self.fw_version = None
            self.protocol_version = None
            self.phases = []
            self.current_test_start_time_ms = int(time.time() * 1000)

        elif CMD_TEST_RESULT_PREFIX in line:
            # Parse overall test result: >>TEST_RESULT:PASS<<, >>TEST_RESULT:FAIL:<reason><<,
            # or >>TEST_RESULT:CANCELLED:<step><<
            end_time_ms = int(time.time() * 1000)
            try:
                start_idx = line.index(CMD_TEST_RESULT_PREFIX) + len(CMD_TEST_RESULT_PREFIX)
                end_idx = line.index("<<", start_idx)
                payload = line[start_idx:end_idx]
            except (ValueError, IndexError) as e:
                logging.error(f"Failed to parse test result from: {line} - {e}")
                return

            if payload == "PASS":
                logging.info("Received test result: PASS")
                self.record_test_result(end_time_ms, "PASS", "")
            elif payload.startswith("FAIL:"):
                failure_details = payload[len("FAIL:"):]
                logging.info(f"Received test result: FAIL ({failure_details})")
                self.record_test_result(end_time_ms, "FAIL", failure_details)
            elif payload.startswith("CANCELLED:"):
                cancelled_during = payload[len("CANCELLED:"):]
                logging.info(f"Received test result: CANCELLED (during {cancelled_during})")
                self.record_test_result(end_time_ms, "CANCELLED", cancelled_during)
            else:
                logging.warning(f"Unrecognized test result payload: {payload}")

    def run(self):
        """Main loop - listen for commands and respond."""
        logging.info("Test host started. Listening for commands...")

        try:
            while True:
                if self.serial and self.serial.is_open:
                    try:
                        # Read line with timeout (blocking call with 1 second timeout)
                        line = self.serial.readline().decode('utf-8', errors='ignore')
                        if line:
                            # Echo all serial output for debugging
                            logging.debug(f"Serial: {line.rstrip()}")

                            # Check if it's a command
                            if ">>" in line and "<<" in line:
                                self.process_command(line)
                    except serial.SerialException as e:
                        logging.error(f"Serial error: {e}")
                        break
                    except UnicodeDecodeError:
                        # Ignore decode errors, just continue
                        pass

        except KeyboardInterrupt:
            logging.info("Received keyboard interrupt, shutting down...")
        finally:
            self.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Host-side test support for programAndTest firmware"
    )
    parser.add_argument(
        "--port",
        required=True,
        help="Serial port for ESP32 communication (e.g., /dev/ttyUSB0)"
    )
    parser.add_argument(
        "--updi-port",
        help="UPDI programming port override (defaults to platformio.ini setting)"
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="Serial baud rate (default: 115200)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging"
    )
    parser.add_argument(
        "--dummy",
        action="store_true",
        help="Dummy mode: simulate firmware uploads without actually running them"
    )
    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="Don't print a label for passing boards"
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Don't publish reports to qc.bezeklabs.com (still written locally)"
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT
    )

    # Ask for a Batch ID up front; leave empty to disable CSV result logging for this run.
    batch_id = input("Batch ID (leave empty to disable CSV result logging): ").strip()

    # Create and run test host
    test_host = TestHost(args.port, args.updi_port, args.baud, args.dummy, batch_id,
                         print_labels=not args.no_labels,
                         upload_reports=not args.no_upload)

    try:
        test_host.connect()
        test_host.run()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
