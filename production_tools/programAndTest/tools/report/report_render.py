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
Renders a per-DUT test report to a self-contained HTML page.

Input is whatever the jig reported over serial (see the datapoint reporting
block in production_tools/programAndTest/src/main.cpp), parsed by test_host.py.
Nothing here knows what any measurement means:
values, units, pass limits and the full plotted range all arrive from the
firmware, so adding a datapoint is a firmware-only change.

The presentation map below is the one thing that is host-side: phase titles and
human labels. An unmapped key still renders, with a label derived from the key,
so a new datapoint shows up without touching this file.
"""

import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# This record is served publicly alongside the page, so it holds only what the
# report itself is built from. Internal process metadata (batch ID, test-host
# commit) belongs in logs/results.csv, not here.
#
# Bumped when the JSON record's shape changes, so an old file stays readable.
REPORT_FORMAT_VERSION = 1

TEMPLATE_PATH = Path(__file__).parent / "template.html"

# Power-rail phases, measured on the fixture before the DUT's own behaviour is
# exercised. A failure here implicates the board's rails or the jig, not the
# device's logic.
JIG_PHASES = ("LOGIC_POWER", "MOTOR_POWER")

PHASE_TITLES = {
    "LOGIC_POWER": "Logic power",
    "MOTOR_POWER": "Motor power",
    "POWER_LED": "Power LED",
    "FW_BOOTSTRAP": "Firmware bootstrap (UPDI)",
    "FW_I2C_UPDATE": "Firmware update over I2C",
    "DEBUG_LED": "Debug LED",
    "SELF_CALIBRATION": "Self-calibration",
    "MOVEMENT": "Movement",
    "TOUCH_SENSOR": "Touch sensor",
    "MOTOR_CAL": "Motor characterization",
}

# Within a phase, datapoints render in the order the firmware reported them.
# These are the exceptions, where the reading that matters should lead.
DATA_ORDER = {
    "LOGIC_POWER": ("logic_current", "logic_voltage"),
    "MOTOR_POWER": ("motor_idle_current", "motor_voltage"),
}

# What each phase actually checks, shown under its title. These describe the
# measurement rather than the outcome, so they read the same on a pass or fail.
#
# The two power phases share one note above the section instead (POWER_NOTE),
# since the thing being checked is the same on both rails.
POWER_NOTE = ("This checks for any major faults on the PCB, like short circuits or "
              "high idle current that might indicate a failed component. Voltage is "
              "a sanity check of the jig\u2019s power supplies during the current "
              "draw test.")

DEVICE_NOTE = ("This checks that every major feature functions, with real motor "
               "fader hardware attached.")

PHASE_NOTES = {
    "POWER_LED": "Ensures power LED is present and lit, using a photodiode.",
    "FW_BOOTSTRAP": "Installs the bootloader over UPDI to support firmware updates, "
                    "then loads a known older firmware so the next step can test a "
                    "real update.",
    "FW_I2C_UPDATE": "Ensures the bootloader functions and allows for firmware "
                     "updates over I2C, for in-place firmware updates without a "
                     "dedicated UPDI programmer.",
    "DEBUG_LED": "Ensures debug LED is present and blinks at the expected heartbeat "
                 "rate of the base firmware.",
    "SELF_CALIBRATION": "Ensures motor control and potentiometer feedback work for "
                        "closed-loop control, and tests motor characterization.",
    "MOVEMENT": "Ensures fader can move to precise locations and settles quickly.",
    "TOUCH_SENSOR": "Ensures capacitive touch detection on fader cap works.",
}

# key -> (label, optional note shown under the value)
LABELS = {
    "logic_voltage": ("3V3 rail voltage", None),
    "logic_current": ("Quiescent current", None),
    "motor_voltage": ("5V rail voltage", None),
    "motor_idle_current": ("Idle current", None),
    "power_led_adc": ("Photodiode reading", None),
    "pages_written": ("Pages written", None),
    "image_crc16": ("Image CRC16", "Read back from flash and matched against the packaged image."),
    "version_after": ("Version after update", None),
    "led_transitions": ("Blink transitions in 2.5 s", None),
    "travel_min_adc": ("Travel minimum", None),
    "travel_max_adc": ("Travel maximum", None),
    "travel_span_adc": ("Usable span", None),
    "settle_ms_1": ("Position A settle time", None),
    "settle_ms_2": ("Position B settle time", None),
    "settle_ms_3": ("Position C settle time", None),
    "worst_settle_error": ("Worst settled position error", "Informational only."),
    "peak_motor_current": ("Peak motor current", "Informational only."),
    "touch_reference": ("Untouched reference", None),
    "touch_delta": ("Delta when touched", None),

    "touch_detect_ms": ("Detect latency", None),
    "touch_release_ms": ("Release latency", None),
    "touch_recal_count": ("Auto-recalibrations", None),
}

# The motor characterization renders as a rising/falling table rather than a
# list of rows: (row label, rising key, falling key, unit).
CAL_ROWS = (
    ("Breakaway duty", "breakaway_rising", "breakaway_falling", "/255"),
    ("Speed per duty", "k_rising", "k_falling", "ADC/s"),
    ("Stribeck jump", "vjump_rising", "vjump_falling", "ADC/s"),
)


# key -> symbol shown immediately before the value.
VALUE_PREFIXES = {
    "touch_delta": "\u0394",
}


@dataclass
class Datapoint:
    key: str
    value: str
    unit: str = ""
    limit_lo: str = ""
    limit_hi: str = ""
    axis_lo: str = ""
    axis_hi: str = ""

    @staticmethod
    def _num(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    @property
    def numeric(self):
        return self._num(self.value)

    def to_dict(self):
        """Typed for querying: numbers where the wire value is numeric, null for
        an absent limit. The wire form is all strings, so a value that isn't a
        number (a version, a hex CRC) is kept as the string it was."""
        def maybe(text):
            if text == "" or text is None:
                return None
            value = self._num(text)
            return value if value is not None else text

        return {
            "key": self.key,
            "value": maybe(self.value),
            "unit": self.unit or None,
            "limit_lo": maybe(self.limit_lo),
            "limit_hi": maybe(self.limit_hi),
            "axis_lo": maybe(self.axis_lo),
            "axis_hi": maybe(self.axis_hi),
        }

    @classmethod
    def from_dict(cls, raw):
        def text(value):
            if value is None:
                return ""
            if isinstance(value, float) and value.is_integer():
                return f"{value:g}"
            return str(value)

        return cls(
            key=raw["key"],
            value=text(raw.get("value")),
            unit=raw.get("unit") or "",
            limit_lo=text(raw.get("limit_lo")),
            limit_hi=text(raw.get("limit_hi")),
            axis_lo=text(raw.get("axis_lo")),
            axis_hi=text(raw.get("axis_hi")),
        )

    def track(self):
        """(axis_lo, axis_hi, lo, hi, value) if this can be plotted, else None."""
        value = self.numeric
        lo, hi = self._num(self.limit_lo), self._num(self.limit_hi)
        axis_lo, axis_hi = self._num(self.axis_lo), self._num(self.axis_hi)
        if value is None or (lo is None and hi is None):
            return None
        # Fall back to the limits when the firmware reported no axis.
        if axis_lo is None:
            axis_lo = lo if lo is not None else 0.0
        if axis_hi is None:
            axis_hi = hi if hi is not None else max(value, axis_lo + 1.0)
        if axis_hi <= axis_lo:
            return None
        return (axis_lo, axis_hi, lo, hi, value)


@dataclass
class Phase:
    key: str
    result: str = ""          # PASS / FAIL, or "" if it never closed
    elapsed_ms: int = 0
    data: list = field(default_factory=list)

    @property
    def passed(self):
        return self.result == "PASS"

    def to_dict(self):
        return {
            "phase": self.key,
            "result": self.result or None,
            "elapsed_ms": self.elapsed_ms,
            "data": [p.to_dict() for p in self.data],
        }

    @classmethod
    def from_dict(cls, raw):
        return cls(
            key=raw["phase"],
            result=raw.get("result") or "",
            elapsed_ms=raw.get("elapsed_ms") or 0,
            data=[Datapoint.from_dict(d) for d in raw.get("data", [])],
        )


@dataclass
class Report:
    serial: str = ""
    token: str = ""
    result: str = ""
    failure: str = ""
    firmware: str = ""
    protocol: str = ""
    tested_at: datetime = None
    duration_ms: int = 0
    phases: list = field(default_factory=list)
    # Zone abbreviation as it was at test time. ISO 8601 carries the offset but
    # not the name, so without this a re-render says "UTC-07:00" where the
    # original said "PDT".
    tzname: str = ""

    def to_dict(self):
        return {
            "format_version": REPORT_FORMAT_VERSION,
            "serial": self.serial,
            "report_token": self.token,
            "result": self.result,
            "failure": self.failure or None,
            "firmware_version": self.firmware or None,
            "protocol_version": self.protocol or None,
            "tested_at": self.tested_at.isoformat() if self.tested_at else None,
            "tested_at_tzname": (self.tzname or
                                 (self.tested_at.tzname() if self.tested_at else None)),
            "duration_ms": self.duration_ms,
            "phases": [p.to_dict() for p in self.phases],
        }

    @classmethod
    def from_dict(cls, raw):
        version = raw.get("format_version")
        if version != REPORT_FORMAT_VERSION:
            raise ValueError(
                f"Unsupported report format_version {version!r} "
                f"(this renderer writes {REPORT_FORMAT_VERSION})")
        tested_at = raw.get("tested_at")
        return cls(
            serial=raw.get("serial", ""),
            token=raw.get("report_token", ""),
            result=raw.get("result", ""),
            failure=raw.get("failure") or "",
            firmware=raw.get("firmware_version") or "",
            protocol=str(raw["protocol_version"]) if raw.get("protocol_version") else "",
            tested_at=datetime.fromisoformat(tested_at) if tested_at else None,
            tzname=raw.get("tested_at_tzname") or "",
            duration_ms=raw.get("duration_ms") or 0,
            phases=[Phase.from_dict(p) for p in raw.get("phases", [])],
        )


def _label(key):
    mapped = LABELS.get(key)
    if mapped:
        return mapped
    # Unmapped keys still render: a new firmware datapoint appears without
    # needing a change here.
    return (key.replace("_", " ").capitalize(), None)


def _fmt(text):
    """Trim a trailing .0 off whole numbers so limits read as 3000, not 3000.0."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return str(text)
    return f"{value:g}"


def _render_track(spec):
    axis_lo, axis_hi, lo, hi, value = spec
    span = axis_hi - axis_lo

    def pct(v):
        return max(0.0, min(100.0, (v - axis_lo) / span * 100.0))

    # One rail across the whole axis, with the passing window laid over it. An
    # open-ended limit runs the window out to that end of the axis.
    left = pct(lo) if lo is not None else 0.0
    right = 100.0 - pct(hi) if hi is not None else 0.0

    out = ((lo is not None and value < lo) or (hi is not None and value > hi))
    at_class = "at out" if out else "at"

    if lo is not None and hi is not None:
        rng = f"pass {_fmt(lo)}&ndash;{_fmt(hi)}"
    elif hi is not None:
        rng = f"pass &le; {_fmt(hi)}"
    else:
        rng = f"pass &ge; {_fmt(lo)}"

    return (
        '<div class="track"><div class="rail"></div>'
        f'<div class="ok" style="left:{left:.4g}%;right:{right:.4g}%"></div>'
        f'<div class="{at_class}" style="left:{pct(value):.4g}%"></div></div>\n'
        f'        <div class="limits"><span>{_fmt(axis_lo)}</span>'
        f'<span class="pass-range">{rng}</span><span>{_fmt(axis_hi)}</span></div>'
    )


def _render_row(point):
    label, note = _label(point.key)
    unit = (f'<span class="unit">{html.escape(point.unit)}</span>' if point.unit else "")
    value = html.escape(_fmt(point.value))
    prefix = VALUE_PREFIXES.get(point.key, "")
    mono = " mono" if point.numeric is None else ""

    out = ['      <div class="row">',
           '        <div class="row-top">'
           f'<span class="label">{html.escape(label)}</span>'
           f'<span class="val{mono}">{prefix}{value}{unit}</span></div>']
    track = point.track()
    if track:
        out.append("        " + _render_track(track))
    if note:
        out.append(f'        <div class="note">{html.escape(note)}</div>')
    out.append("      </div>")
    return "\n".join(out)


def _render_phase(phase):
    title = PHASE_TITLES.get(phase.key, phase.key.replace("_", " ").capitalize())
    tick = "&#10003;" if phase.passed else "&#10007;"
    tick_class = "tick" if phase.passed else "tick fail"
    dur = f"{phase.elapsed_ms / 1000:.1f} s"
    order = DATA_ORDER.get(phase.key, ())
    data = sorted(phase.data,
                  key=lambda p: order.index(p.key) if p.key in order else len(order))
    rows = "\n".join(_render_row(p) for p in data)
    note = PHASE_NOTES.get(phase.key)
    blurb = (f'\n    <div class="phase-note">{html.escape(note)}</div>' if note else "")
    # A phase can legitimately report no datapoints (the firmware bootstrap
    # either worked or it didn't); don't leave an empty rows container behind.
    body = f'\n    <div class="rows">\n{rows}\n    </div>' if phase.data else ""
    if not phase.data and blurb:
        blurb = blurb.replace('class="phase-note"',
                              'class="phase-note" style="padding-bottom:14px"')
    return f'''  <div class="phase">
    <div class="phase-head"><span class="{tick_class}">{tick}</span>\
<span class="name">{html.escape(title)}</span><span class="dur">{dur}</span></div>{blurb}{body}
  </div>'''


def _render_motor_cal(phase):
    """The characterization as a rising/falling table, plus the derived gains."""
    values = {p.key: _fmt(p.value) for p in phase.data}
    rows = []
    for label, rising_key, falling_key, unit in CAL_ROWS:
        if rising_key not in values and falling_key not in values:
            continue
        rows.append(f'        <tr><td>{html.escape(label)}</td>'
                    f'<td>{values.get(rising_key, "&mdash;")}</td>'
                    f'<td>{values.get(falling_key, "&mdash;")}</td>'
                    f'<td>{html.escape(unit)}</td></tr>')
    if not rows:
        return ""

    note = ""
    if "vel_min" in values and "deadband" in values:
        note = (f'    <div class="cal-note">Derived gains now running on this unit: '
                f'velocity floor {values["vel_min"]}&nbsp;ADC/s, '
                f'deadband {values["deadband"]}&nbsp;ADC.</div>')
    if values.get("cal_valid") == "0":
        note += ('\n    <div class="cal-note">This unit is running the compiled-in '
                 'default gains: self-calibration did not produce a usable '
                 'measurement of its motor.</div>')

    return f'''  <h2>Motor characterization</h2>
  <div class="section-note">Note: characterization parameters are informational
  only and derived for the test jig motor fader only; self-calibration should be
  re-run with your motor fader once installed.</div>
  <div class="cal-wrap">
    <table class="cal">
      <thead>
        <tr><th>Measured</th><th>Toward motor</th><th>Away</th><th>Unit</th></tr>
      </thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
{note}
  </div>'''


def render(report):
    """Render a Report to a complete self-contained HTML page."""
    template = TEMPLATE_PATH.read_text()

    jig = [p for p in report.phases if p.key in JIG_PHASES]
    cal = next((p for p in report.phases if p.key == "MOTOR_CAL"), None)
    device = [p for p in report.phases
              if p.key not in JIG_PHASES and p.key != "MOTOR_CAL"]

    checks = [p for p in report.phases if p.key != "MOTOR_CAL"]
    passed = sum(1 for p in checks if p.passed)
    verdict_class = "pass" if report.result == "PASS" else "fail"

    when = ""
    if report.tested_at:
        zone = report.tzname or report.tested_at.tzname() or ""
        when = f"{report.tested_at.strftime('%d %b %Y, %H:%M')} {zone}".strip()

    meta = [("Serial", f'<span class="mono">{html.escape(report.serial)}</span>')]
    if when:
        meta.append(("Tested", html.escape(when)))
    meta.append(("Duration", f"{report.duration_ms / 1000:.1f} s"))
    if report.firmware:
        fw = html.escape(report.firmware)
        if report.protocol:
            fw += (f' <span style="color:var(--ink-faint)">'
                   f'(protocol v{html.escape(report.protocol)})</span>')
        meta.append(("Firmware", fw))
    meta_html = "\n".join(
        f"      <div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in meta)

    failure = ""
    if report.result != "PASS" and report.failure:
        failure = (f'\n      <div class="note" style="color:var(--fail);padding-top:10px">'
                   f'Failed at: {html.escape(report.failure)}</div>')

    parts = [f'''  <header>
    <div class="mark">FaderBuddy<span> QC</span></div>
    <div class="tagline">Factory test report</div>
  </header>

  <section class="intro">
    <p>Your FaderBuddy PCB was fully tested, with real motor fader hardware
    attached, to make sure it meets my high quality bar. Every measurement below
    was taken on your device, and is tied to the microcontroller\u2019s unique serial
    number (<span class="mono">{html.escape(report.serial)}</span>).</p>
    <p>Learn more about the testing process
    <a href="https://qc.bezeklabs.com/faderbuddy/index.html">here</a>. If you
    have any questions please don\u2019t hesitate to reach out!</p>
    <p class="sig">\u2014Scott<br>
    <a href="mailto:scott@bezeklabs.com">scott@bezeklabs.com</a></p>
  </section>

  <section class="verdict">
    <div class="verdict-top">
      <span class="badge {verdict_class}">{html.escape(report.result or "UNKNOWN")}</span>
      <span class="of">{passed} of {len(checks)} checks passed</span>
    </div>{failure}
    <dl class="meta">
{meta_html}
    </dl>
  </section>''']

    if jig:
        parts.append("  <h2>Basic power draw</h2>")
        parts.append(f'  <div class="section-note">{html.escape(POWER_NOTE)}</div>')
        parts.extend(_render_phase(p) for p in jig)

    if device:
        parts.append("  <h2>Device functionality</h2>")
        parts.append(f'  <div class="section-note">{html.escape(DEVICE_NOTE)}</div>')
        parts.extend(_render_phase(p) for p in device)

    if cal:
        cal_html = _render_motor_cal(cal)
        if cal_html:
            parts.append(cal_html)

    # Relative, so it resolves next to this page wherever it is served from.
    raw_link = ""
    if report.serial and report.token:
        raw_link = (f'\n    <div><a class="raw" href="'
                    f'{html.escape(report.serial)}-{html.escape(report.token)}.json">'
                    f'Raw measurements (JSON)</a></div>')
    parts.append(f'''  <footer>{raw_link}
    <div class="who">Bezek Labs</div>
    <div>designed / tested in oakland, ca</div>
  </footer>''')

    title = f"FaderBuddy {report.serial} — Test Report"
    return (template
            .replace("{{TITLE}}", html.escape(title))
            .replace("{{CONTENT}}", "\n\n".join(parts)))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Re-render a stored test report JSON to HTML. The JSON is the "
                    "record of truth, so a template change can be applied to "
                    "reports that were generated long ago.")
    parser.add_argument("json_path", type=Path, help="A .json written alongside a report")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output .html (default: alongside the JSON)")
    args = parser.parse_args()

    report = Report.from_dict(json.loads(args.json_path.read_text()))
    out = args.output or args.json_path.with_suffix(".html")
    out.write_text(render(report))
    print(f"Rendered {out} from {args.json_path} "
          f"({len(report.phases)} phases, {report.result})")


if __name__ == "__main__":
    main()
