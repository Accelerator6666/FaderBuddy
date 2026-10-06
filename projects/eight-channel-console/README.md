# FaderBuddy 8-Channel Console

This folder contains a staged reference project for building a desktop motor-fader console around FaderBuddy and an ESP32-S3.

## Goal

Final target:

- 8 x FaderBuddy / 60 mm motorized faders
- ESP32-S3 host
- Shared I2C bus, addresses `0x20` through `0x27`
- Four initial control layers:
  - Layer 0: Home Assistant
  - Layer 1: Windows audio
  - Layer 2: OBS
  - Layer 3: MIDI / custom control
- Later expansion for display, per-channel buttons and LEDs

## Development stages

### V0.1 - single-fader bring-up

Use `v0.1-single-fader.yaml`.

Purpose:

- verify I2C detection at `0x20`
- run self-calibration
- verify manual position reporting
- verify motorized remote movement
- verify touch detection
- verify double-tap event logging

Recommended wiring for an ESP32-S3 DevKitC-1:

| ESP32-S3 / PSU | FaderBuddy |
|---|---|
| 3.3 V | Vio |
| GND | GND |
| GPIO12 | SDA |
| GPIO13 | SCL |
| external 5 V | Vmot |
| external PSU GND | GND |

The ESP32 and motor power supply must share ground.

### V0.2 - four-fader bus validation

Use `v0.2-four-fader.yaml`.

Addresses:

| Channel | Address | A2 | A1 | A0 |
|---|---:|---|---|---|
| 1 | `0x20` | open | open | open |
| 2 | `0x21` | open | open | bridged |
| 3 | `0x22` | open | bridged | open |
| 4 | `0x23` | open | bridged | bridged |

This stage validates shared-bus operation and synchronized layer switching before scaling to eight faders.

### V0.3 - eight-fader console

Planned addresses are `0x20` through `0x27`. At this stage the motor 5 V rail should be treated as a separate power domain from the ESP32 logic supply, with common ground and adequate current capacity for simultaneous motion.

### V1.0 - application layers

Planned host roles:

- Home Assistant / ESPHome
- Windows audio bridge
- OBS WebSocket bridge
- USB MIDI / custom HID on ESP32-S3

## Important design rule

Do not modify the FaderBuddy motor-control firmware for console-specific behavior unless necessary. Keep real-time motor control, touch sensing, calibration and layer state inside FaderBuddy; implement application mapping on the ESP32-S3 host.

FaderBuddy already stores eight independent layers per physical fader. Each layer remembers its target position and haptic configuration, and switching layers automatically restores the stored position.
