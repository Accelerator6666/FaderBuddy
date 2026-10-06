# Eight-Channel Power and I2C Bring-Up

This note is for the V0.3 eight-fader bench build.

## Power domains

Treat the console as three connected electrical domains:

1. **Motor rail**: regulated 5 V to every FaderBuddy `Vmot`
2. **Logic rail**: regulated 3.3 V to every FaderBuddy `Vio` and the ESP32-S3 logic domain
3. **Ground**: one shared ground reference for the ESP32-S3, the 3.3 V logic rail and the 5 V motor rail

Do not power eight motor faders from the ESP32-S3 USB connector or by routing the motor current through the ESP32 development board.

For the final console, size the 5 V supply from measured real hardware current, including simultaneous motor motion and margin. The upstream project documentation does not specify an eight-fader worst-case current, so this project intentionally does not hard-code a PSU amp rating before measurement.

Likewise, do not assume the ESP32-S3 development board's onboard 3.3 V regulator can supply eight FaderBuddy logic boards. Check the regulator rating and measured load, or use a dedicated regulated 3.3 V logic rail.

When using 3.3 V logic, leave the FaderBuddy `Vmot` ↔ `Vio` solder bridge open. That bridge is only appropriate when the design intentionally uses a common 5 V logic/motor domain.

## Recommended distribution topology

Use a low-impedance power distribution point rather than daisy-chaining motor current through thin signal cabling:

```text
                  +---------------- ESP32-S3 power input
                  |
5 V PSU ----------+---------------- motor distribution bus
                                   |  |  |  |  |  |  |  |
                                   M1 M2 M3 M4 M5 M6 M7 M8

3.3 V regulated logic rail --------+--+--+--+--+--+--+--+
                                   |  |  |  |  |  |  |  |
                                  Vio Vio ...              Vio

GND ------------------------------- common ground distribution
```

`M1` through `M8` are the eight FaderBuddy `Vmot` connections.

Keep motor-current wiring physically separate from SDA/SCL where practical. Add local bulk decoupling at the motor distribution point if testing shows rail sag during movement; choose the capacitance after observing the actual rail behavior rather than guessing a value.

## I2C topology

Use one shared SDA/SCL bus:

```text
ESP32-S3 GPIO12 SDA -----+---- FB1 0x20
                         +---- FB2 0x21
                         +---- FB3 0x22
                         +---- FB4 0x23
                         +---- FB5 0x24
                         +---- FB6 0x25
                         +---- FB7 0x26
                         +---- FB8 0x27

ESP32-S3 GPIO13 SCL -----+---- same eight boards
```

Keep the physical bus short and orderly. Avoid long star branches during bench testing.

The FaderBuddy hardware includes I2C pull-up resistance on the bus. With eight boards connected in parallel, verify bus levels and signal integrity rather than assuming the aggregate pull-up is automatically ideal. If the full bus is unstable while smaller groups are stable, investigate total pull-up strength, wiring capacitance, branch length and grounding before changing application logic.

The V0.3 ESPHome configuration uses a 20 ms polling interval per fader as a conservative first eight-channel setting. V0.1/V0.2 use 10 ms. Reduce V0.3 to 10 ms only after the complete bus is proven stable.

## Address map

| Channel | Address | A2 | A1 | A0 |
|---|---:|---|---|---|
| CH1 | `0x20` | open | open | open |
| CH2 | `0x21` | open | open | bridged |
| CH3 | `0x22` | open | bridged | open |
| CH4 | `0x23` | open | bridged | bridged |
| CH5 | `0x24` | bridged | open | open |
| CH6 | `0x25` | bridged | open | bridged |
| CH7 | `0x26` | bridged | bridged | open |
| CH8 | `0x27` | bridged | bridged | bridged |

## Safe bring-up sequence

1. Power the ESP32-S3 and logic rail with no motor motion command.
2. Connect one FaderBuddy at `0x20`; confirm it appears in the ESPHome I2C scan.
3. Add boards one at a time in address order through `0x27`, verifying there are no duplicate addresses.
4. Confirm all eight status entities report normally before running motors.
5. Run self-calibration on each fader individually.
6. Use the V0.3 **Staggered Center Test** first. It moves one channel every 150 ms to avoid deliberately creating the maximum simultaneous motor transient during first power-up.
7. Verify `Layer Sync Count` returns `8` after a layer change when no fader is being touched.
8. Only after measuring motor-rail behavior should a simultaneous eight-channel move test be added.

## Expected layer behavior

FaderBuddy intentionally defers a requested layer change on a channel while that fader is being touched or manually moved. Therefore `Layer Sync Count` may briefly fall below `8` if a layer switch is requested while a user is holding a fader. After release, the pending layer change should apply and the count should return to `8`.

That behavior is desirable: it prevents the motor from fighting the user's hand during a global layer switch.
