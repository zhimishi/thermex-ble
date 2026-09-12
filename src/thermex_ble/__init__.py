"""Unofficial library for controlling Thermex range hoods over BLE.

Reverse engineered from Android HCI captures. No cloud, no account, no network.
"""

from .hood import ThermexHood
from .protocol import (
    CHAR_UUID,
    MAX_BRIGHTNESS,
    MAX_SPEED,
    SERVICE_UUID,
    HoodState,
    ThermexProtocolError,
    crc16_ccitt_false,
    encode_fan,
    encode_light,
    parse_status,
    percentage_to_speed,
)

__version__ = "0.1.0"

__all__ = [
    "CHAR_UUID",
    "MAX_BRIGHTNESS",
    "MAX_SPEED",
    "SERVICE_UUID",
    "HoodState",
    "ThermexHood",
    "ThermexProtocolError",
    "crc16_ccitt_false",
    "encode_fan",
    "encode_light",
    "parse_status",
    "percentage_to_speed",
]
