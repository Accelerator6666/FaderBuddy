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

## Files

- `v0.1-single-fader.yaml` - single-channel electrical and ESPHome bring-up
- `v0.2-four-fader.yaml` - four-channel shared-I2C and layer validation
- `v0.3-eight-fader.yaml` - complete eight-address console bench configuration
- `POWER_AND_BUS.md` - eight-channel power distribution, I2C topology and staged bring-up
- `secrets.yaml.example` - ESPHome secrets template

## Current status

The project currently stops at the **bench-validation layer**. V0.3 is designed to prove eight physical faders, unique I2C addresses, calibration, touch reporting, synchronized layer switching and safe staged motor movement before Windows/OBS/MIDI bridges are added.

Keep the pull request in draft until the single-fader and eight-fader hardware tests are completed.

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

Use `v0.3-eight-fader.yaml` and read `POWER_AND_BUS.md` before powering the full motor bank.

Addresses:

| Channel | Address | A2 | A1 | A0 |
|---|---:|---|---|---|
| 1 | `0x20` | open | open | open |
| 2 | `0x21` | open | open | bridged |
| 3 | `0x22` | open | bridged | open |
| 4 | `0x23` | open | bridged | bridged |
| 5 | `0x24` | bridged | open | open |
| 6 | `0x25` | bridged | open | bridged |
| 7 | `0x26` | bridged | bridged | open |
| 8 | `0x27` | bridged | bridged | bridged |

V0.3 adds:

- all eight FaderBuddy addresses on one bus
- `CH1` through `CH8` position, target and touch entities
- synchronized global layer switching across all eight faders
- `Layer Sync Count` diagnostic; normal settled value is `8`
- a staggered center test that deliberately avoids a first-run simultaneous motor transient
- a conservative `20ms` polling interval per fader for initial full-bus validation

The four test layers retain distinct haptic behavior so layer changes are easy to verify physically:

| Layer | Haptic mode | Purpose |
|---|---|---|
| 0 | smooth | baseline continuous control |
| 1 | smooth with magnetic endpoints | layer-change tactile check |
| 2 | 5 detents | stepped-control test |
| 3 | 9 detents | finer stepped-control test |

A requested layer change can be deferred on a fader that is currently touched or manually moving. During that interval `Layer Sync Count` may temporarily be below `8`; after release it should return to `8`.

### V1.0 - application layers

Planned host roles:

- Home Assistant / ESPHome
- Windows audio bridge
- OBS WebSocket bridge
- USB MIDI / custom HID on ESP32-S3

The V0.3 configuration deliberately keeps these application mappings out of the motor-control test. First prove eight-channel electrical, I2C, calibration and layer behavior; then add PC/HA application bridges without changing the FaderBuddy real-time motor firmware.

## Important design rule

Do not modify the FaderBuddy motor-control firmware for console-specific behavior unless necessary. Keep real-time motor control, touch sensing, calibration and layer state inside FaderBuddy; implement application mapping on the ESP32-S3 host.

FaderBuddy already stores eight independent layers per physical fader. Each layer remembers its target position and haptic configuration, and switching layers automatically restores the stored position.
