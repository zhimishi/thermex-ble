# Thermex BLE protocol

Reverse engineered from Android `btsnoop_hci.log` captures of the Thermex
Remote app talking to a **Design Line 8002 (TDL8002W80BK)**. Other Thermex
models with the same BLE module are likely to speak the same protocol, but this
is untested.

Nothing here is encrypted or authenticated. There is no nonce and no
challenge/response, so commands can be replayed freely.

## GATT

| | |
|---|---|
| Service | `0xFF00` |
| Characteristic | `0xFF01`, properties read / write / notify |
| CCCD | write `0100` to enable notifications |

A single characteristic carries everything: commands in, status out.

The hood **does not advertise its service UUID**, and stops advertising
entirely while a client is connected. It accepts only one connection at a time
— a phone with the app open will lock everything else out. This rules out
passive discovery; connect by address instead.

## Unlock

Immediately after subscribing, write:

```
a3 0c d7 53 f5 00 00 00
```

Until this lands the hood answers with a placeholder frame that is zeroed
except for its tail, so every field reads as 0 and the CRC field is `0x0000`.

Tested in isolation:

| Payload | Result |
|---|---|
| `a30cd753f5000000` | unlocks |
| `a300000000000000` | no effect |
| `0000000000000000` | no effect |
| `9c00000000000000` | no effect |

The app also sends `9c00000000000000` right after the unlock. It is not
replayed by this library because it changes nothing.

Because the hood validates the whole payload, this is either a fixed magic
value or a token with a very long life. It was still accepted days after the
capture it came from. If frames ever come back locked, suspect this constant.

## Commands

All commands are **Write Request** (opcode `0x12`), not Write Command.

### Fan

```
80 <step>        step = 0x00-0x04, 0 = off
```

The hood derives its own duty cycle from the step:

| Step | Duty |
|---|---|
| 0 | 0% |
| 1 | 25% |
| 2 | 45% |
| 3 | 60% |
| 4 | 99% |

### Light

```
81 <level> 00 00     level = 0x00-0x64 (0-100), 0 = off
```

The trailing two bytes were zero in every capture. Purpose unknown.

## Status notifications

50 bytes, pushed roughly once per second, and also whenever state changes —
including changes made on the hood's own control panel. That makes the
integration genuinely event-driven; polling is unnecessary.

| Byte | Meaning |
|---|---|
| 0 | Flag. Normally 0; observed as 1 on the frame acknowledging fan-off. Not understood. |
| 2 | Fan duty cycle, percent |
| 4 | **Light level, 0-100** |
| 5 | `0x64` — looks like max light level |
| 10 | `0x64` — looks like max fan duty |
| 11 | **Fan step, 0-4** |
| 25 | Measured airflow or tacho feedback |
| 30-31 | CRC-16/CCITT-FALSE over bytes 0-29, little-endian |
| 35-36 | uint16le, drifts slowly (930-945 observed). Probably an ADC reading |
| 42-48 | Device address, two below the address it answers on |

Everything else was constant across every frame captured.

### Use byte 11, not byte 25

Byte 25 is a *measurement*, not a setpoint. It ramps up over several seconds
after a speed change and decays toward zero over roughly 15 seconds after the
fan is switched off, even overshooting slightly just after. Its peak tracks the
duty cycle closely — 127 at step 4 against 33 at step 1, so about 26% versus the
25% duty.

Deriving on/off from it would leave Home Assistant showing the fan as running
for a quarter of a minute after it stopped.

### CRC

`CRC-16/CCITT-FALSE`: polynomial `0x1021`, init `0xFFFF`, no reflection, no
final xor. Computed over bytes 0-29, stored little-endian at bytes 30-31.

Verified against every captured frame. The check value for `123456789` is
`0x29B1`.

The locked placeholder frame has a zero CRC field, which is how this library
distinguishes it from a corrupt frame.

## How this was captured

1. Enable **Bluetooth HCI snoop log** in Android developer options, then
   restart Bluetooth (airplane mode off/on). Logging does not start until
   Bluetooth restarts.
2. Use the app, one action at a time with a few seconds between, writing down
   the order.
3. `adb bugreport bug.zip`, then extract `FS/data/log/bt/btsnoop_hci.log`.
   The exact path varies by vendor — `unzip -l bug.zip | grep -i btsnoop`.
4. Open in Wireshark, filter `btatt`, add `btatt.handle` and `btatt.value` as
   columns.

The frame layout was worked out by lining up equal-length notifications and
diffing them column by column: constant columns are configuration or padding,
columns with exactly as many distinct values as there are states are the state
fields, and continuously changing columns are sensors or checksums.

The first and last frame of a capture were byte-identical, which proved bytes
30-31 were deterministic and therefore a checksum rather than a nonce — the
single most important thing to establish, since a nonce would have meant
decompiling the APK.

## Open questions

- What byte 0 means.
- What bytes 35-36 measure.
- Whether the unlock value is constant across units and over time.
- Whether other Thermex models with a 0xFF00 module use the same layout.
