# V1.0 Application Layers

V1.0 keeps the FaderBuddy motor-control firmware unchanged and adds an application transport on top of the eight-fader bench configuration.

## Architecture

```text
8 x FaderBuddy (0x20-0x27)
          |
          | I2C
          v
      ESP32-S3
          |
          | MQTT JSON
          v
      PC bridge
       /  |  |  \
      /   |  |   \
 Home   Win OBS  MIDI
Assistant Audio
```

The ESP32-S3 publishes user actions and accepts target-position commands. The bridge maps each physical `(layer, channel)` pair to an application control.

## Layer plan

| Layer | Default role | Initial adapter |
|---:|---|---|
| 0 | Home Assistant | light brightness |
| 1 | Windows audio | master/session volume |
| 2 | OBS | input volume/mute |
| 3 | MIDI | MIDI CC output |

These mappings are only defaults; edit `bridge/config.yaml` to change them.

## MQTT protocol

Topic prefix defaults to `faderbuddy/console`.

### ESP32 -> bridge

Topic:

```text
faderbuddy/console/event
```

Manual move:

```json
{"event":"move","channel":1,"layer":1,"position":160}
```

Touch change:

```json
{"event":"touch","channel":1,"layer":1,"touched":true}
```

Double tap:

```json
{"event":"double_tap","channel":1,"layer":1}
```

Layer selection from the ESPHome entity:

```json
{"event":"layer","layer":2}
```

### Bridge -> ESP32

Topic:

```text
faderbuddy/console/command
```

Move a physical fader or update an inactive layer's stored target:

```json
{"op":"move","channel":1,"layer":1,"position":160,"speed":100}
```

Switch the full bank to a layer:

```json
{"op":"layer","layer":2}
```

Because layer targets are stored by FaderBuddy firmware, the bridge can keep inactive layers synchronized. When a layer is later activated, the fader moves to the most recently supplied target for that layer.

## ESPHome setup

Start from:

```text
v1.0-eight-fader-app.yaml
```

Add the MQTT values to your local `secrets.yaml` using `secrets.yaml.example` as the template.

V1.0 keeps the native ESPHome API enabled while disabling MQTT discovery. This avoids duplicate Home Assistant entities while still using MQTT as the application transport.

## Windows bridge setup

The current bridge is intended to run on Windows because Layer 1 uses Windows Core Audio through pycaw.

From PowerShell:

```powershell
cd projects\eight-channel-console\bridge
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml` before starting the bridge.

For Home Assistant, create a long-lived access token and expose it to the bridge process:

```powershell
$env:HA_TOKEN = "YOUR_LONG_LIVED_ACCESS_TOKEN"
```

For OBS WebSocket authentication:

```powershell
$env:OBS_PASSWORD = "YOUR_OBS_WEBSOCKET_PASSWORD"
```

Run:

```powershell
python .\fader_bridge.py -c .\config.yaml
```

For additional diagnostics:

```powershell
python .\fader_bridge.py -c .\config.yaml --debug
```

## Layer behavior

### Layer 0 - Home Assistant

`homeassistant_light` maps fader position `0-255` directly to Home Assistant light brightness. Position `0` turns the light off. Double tap toggles the light.

The bridge polls Home Assistant and sends external brightness changes back to the inactive/active FaderBuddy layer, keeping the motorized position synchronized.

### Layer 1 - Windows Audio

Supported mappings:

```yaml
{type: windows_master}
```

and:

```yaml
{type: windows_session, process: chrome.exe}
```

Position maps to Windows volume `0.0-1.0`. Double tap toggles mute.

A process must have an active Windows audio session before a `windows_session` mapping can be controlled. If an application has not produced audio yet, the bridge leaves that fader's stored position unchanged until the session becomes available.

### Layer 2 - OBS

Example:

```yaml
{type: obs_input, input: "Mic/Aux"}
```

Position maps to OBS input volume multiplier `0.0-1.0`. Double tap toggles input mute. OBS Studio 28+ includes obs-websocket; the default WebSocket port is `4455`.

### Layer 3 - MIDI

Example:

```yaml
{type: midi_cc, port: "FaderBuddy MIDI", midi_channel: 0, cc: 1}
```

The bridge converts fader range `0-255` to MIDI CC range `0-127`.

The initial MIDI adapter is output-only. It does not yet listen for incoming MIDI CC feedback, so external DAW changes do not move the physical fader back. Bidirectional MIDI feedback is a later enhancement.

## Sync loop

The bridge periodically reads application state for mappings that support feedback and sends motor targets back to FaderBuddy. Defaults:

```yaml
sync_interval: 0.75
sync_deadband: 2
```

`sync_deadband` prevents tiny state changes from repeatedly commanding the motor.

## Bring-up order

Do not start with all application layers enabled on untested hardware.

1. Prove V0.1 single-fader calibration and movement.
2. Prove V0.2 four-fader shared-bus operation.
3. Prove V0.3 eight-fader power and I2C stability.
4. Flash V1.0 and confirm MQTT event/command transport.
5. Enable only Layer 0 mappings first.
6. Add Windows Audio.
7. Add OBS.
8. Add MIDI last.

Keep PR #1 in draft until the physical eight-fader console has passed these tests.
