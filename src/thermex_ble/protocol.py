"""Wire protocol for Thermex range hoods with a 0xFF00 BLE module.

Pure functions only - no I/O, no async, no bleak. Everything here can be unit
tested against captured frames, which is what tests/test_protocol.py does.

Reverse engineered from Android btsnoop HCI captures of the Thermex Remote app
talking to a Design Line 8002 (TDL8002W80BK). See PROTOCOL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# --- GATT ------------------------------------------------------------------

SERVICE_UUID: Final = "000000ff-0000-1000-8000-00805f9b34fb"
CHAR_UUID: Final = "0000ff01-0000-1000-8000-00805f9b34fb"

# --- Commands --------------------------------------------------------------

CMD_FAN: Final = 0x80
CMD_LIGHT: Final = 0x81

#: Sent once after connecting. Until it arrives the hood answers with a frame
#: that is zeroed except for its tail, so every field reads as 0 and the CRC
#: field is 0x0000. Tested in isolation: the app also sends
#: ``9c00000000000000``, which has no observable effect and is not replayed.
#: The hood validates the whole payload - ``a300000000000000`` is rejected - so
#: this is either a fixed magic value or a very long-lived token. If frames
#: ever come back locked again, suspect this constant first.
UNLOCK: Final = bytes.fromhex("a30cd753f5000000")

MAX_SPEED: Final = 4
MAX_BRIGHTNESS: Final = 100

#: Duty cycle the hood reports for each speed step. Informational: the hood
#: derives this itself, we never send it.
SPEED_DUTY: Final = {0: 0, 1: 25, 2: 45, 3: 60, 4: 99}

STATUS_LENGTH: Final = 50

# --- Frame layout ----------------------------------------------------------

_OFF_FLAG: Final = 0
_OFF_FAN_DUTY: Final = 2
_OFF_BRIGHTNESS: Final = 4
_OFF_MAX_BRIGHTNESS: Final = 5
_OFF_MAX_DUTY: Final = 10
_OFF_SPEED: Final = 11
_OFF_MEASURED: Final = 25
_OFF_CRC: Final = 30
_OFF_AUX: Final = 35
_OFF_ADDRESS: Final = 42


class ThermexProtocolError(Exception):
    """Raised when a frame cannot be parsed."""


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True, slots=True)
class HoodState:
    """Decoded status frame."""

    speed: int
    """Fan step, 0-4. This is the authoritative fan state."""

    fan_duty: int
    """Duty cycle in percent the hood derived from the speed step."""

    brightness: int
    """Light level, 0-100. This is the authoritative light state."""

    measured: int
    """Tacho or airflow feedback. Lags the setpoint by ~15s on spin-down, so
    never derive on/off from this - use ``speed``."""

    flag: int
    """Byte 0. Normally 0; observed as 1 on the frame acknowledging fan-off.
    Meaning not established."""

    aux: int
    """Bytes 35-36 as uint16le. Drifts slowly (~930-945 observed) and is
    populated even in locked frames. Probably an ADC reading. Not exposed as an
    entity until it is understood."""

    unlocked: bool
    """False for the placeholder frame sent before :data:`UNLOCK` is written."""

    raw: bytes

    @property
    def fan_on(self) -> bool:
        return self.speed > 0

    @property
    def light_on(self) -> bool:
        return self.brightness > 0

    @property
    def percentage(self) -> int:
        """Fan speed as a percentage, for Home Assistant's fan entity."""
        return round(self.speed * 100 / MAX_SPEED)


def parse_status(payload: bytes) -> HoodState:
    """Decode a status notification.

    Raises:
        ThermexProtocolError: on a short frame or a CRC mismatch in an
            otherwise unlocked frame.
    """
    if len(payload) < STATUS_LENGTH:
        raise ThermexProtocolError(
            f"expected {STATUS_LENGTH} bytes, got {len(payload)}: {payload.hex()}"
        )

    crc_rx = int.from_bytes(payload[_OFF_CRC : _OFF_CRC + 2], "little")
    crc_calc = crc16_ccitt_false(payload[:_OFF_CRC])

    # The locked placeholder has a zeroed body and a zeroed CRC field. Treat it
    # as a valid frame carrying no state rather than an error - it is the
    # normal first frame on every connection.
    unlocked = crc_rx != 0 or any(payload[:_OFF_CRC])

    if unlocked and crc_rx != crc_calc:
        raise ThermexProtocolError(
            f"CRC mismatch: frame says {crc_rx:04x}, computed {crc_calc:04x} "
            f"({payload.hex()})"
        )

    return HoodState(
        speed=payload[_OFF_SPEED],
        fan_duty=payload[_OFF_FAN_DUTY],
        brightness=payload[_OFF_BRIGHTNESS],
        measured=payload[_OFF_MEASURED],
        flag=payload[_OFF_FLAG],
        aux=int.from_bytes(payload[_OFF_AUX : _OFF_AUX + 2], "little"),
        unlocked=unlocked,
        raw=bytes(payload),
    )


def encode_fan(speed: int) -> bytes:
    """Build a fan command. ``speed`` is 0-4, where 0 is off."""
    if not 0 <= speed <= MAX_SPEED:
        raise ValueError(f"speed must be 0-{MAX_SPEED}, got {speed}")
    return bytes([CMD_FAN, speed])


def encode_light(brightness: int) -> bytes:
    """Build a light command. ``brightness`` is 0-100, where 0 is off.

    The last two bytes are always zero in every capture; purpose unknown.
    """
    if not 0 <= brightness <= MAX_BRIGHTNESS:
        raise ValueError(f"brightness must be 0-{MAX_BRIGHTNESS}, got {brightness}")
    return bytes([CMD_LIGHT, brightness, 0x00, 0x00])


def percentage_to_speed(percentage: int) -> int:
    """Map Home Assistant's 0-100 fan percentage onto a 0-4 step."""
    if percentage <= 0:
        return 0
    return max(1, min(MAX_SPEED, round(percentage * MAX_SPEED / 100)))
