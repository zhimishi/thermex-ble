"""Protocol tests.

Every frame here came off the wire - captured with btsnoop on Android or with
the CLI against the hood. Keeping real frames as fixtures means the decoder is
verified against reality rather than against my own encoder.
"""

import pytest

from thermex_ble.protocol import (
    UNLOCK,
    ThermexProtocolError,
    crc16_ccitt_false,
    encode_fan,
    encode_light,
    parse_status,
    percentage_to_speed,
)

# --- Captured frames -------------------------------------------------------

FRAME_IDLE = bytes.fromhex(
    "0004000000640000000064000006a0000000000000000000780000000000275e"
    "0b0300b0030000000000014417935cad9400"
)
FRAME_SPEED_1 = bytes.fromhex(
    "0004190000640000000064010006a00000000000000000007800000000002ff9"
    "0b0300b0030000000000014417935cad9400"
)
FRAME_SPEED_2 = bytes.fromhex(
    "00042d0000640000000064020006a00000000000000000007821000000009c00"
    "0b0300b0030000000000014417935cad9400"
)
FRAME_SPEED_3 = bytes.fromhex(
    "00043c0000640000000064030006a0000000000000000000783900000000 6295".replace(" ", "")
    + "0b0300b0030000000000014417935cad9400"
)
FRAME_SPEED_4 = bytes.fromhex(
    "0004630000640000000064040006a0000000000000000000785600000000 1ec5".replace(" ", "")
    + "0b0300b0030000000000014417935cad9400"
)
FRAME_LIGHT_4 = bytes.fromhex(
    "0004000004640000000064000006a0000000000000000000780000000000e1cf"
    "0b0300a0030000000000014417935cad9400"
)
FRAME_OFF_ACK = bytes.fromhex(
    "0104630000640000000064000006a0000000000000000000787f00000000d624"
    "0b0300b0030000000000014417935cad9400"
)
FRAME_LOCKED = bytes.fromhex(
    "00000000000000000000000000000000000000000000000000000000000000000"
    "b0300a5030000000000004417935cad9400"
)

ALL_UNLOCKED = [
    FRAME_IDLE,
    FRAME_SPEED_1,
    FRAME_SPEED_2,
    FRAME_SPEED_3,
    FRAME_SPEED_4,
    FRAME_LIGHT_4,
    FRAME_OFF_ACK,
]


# --- CRC -------------------------------------------------------------------


def test_crc_known_vector():
    # CRC-16/CCITT-FALSE check value for b"123456789"
    assert crc16_ccitt_false(b"123456789") == 0x29B1


@pytest.mark.parametrize("frame", ALL_UNLOCKED)
def test_crc_matches_on_captured_frames(frame):
    assert crc16_ccitt_false(frame[:30]) == int.from_bytes(frame[30:32], "little")


# --- Decoding --------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "speed", "duty"),
    [
        (FRAME_IDLE, 0, 0),
        (FRAME_SPEED_1, 1, 25),
        (FRAME_SPEED_2, 2, 45),
        (FRAME_SPEED_3, 3, 60),
        (FRAME_SPEED_4, 4, 99),
    ],
)
def test_fan_decoding(frame, speed, duty):
    state = parse_status(frame)
    assert state.speed == speed
    assert state.fan_duty == duty
    assert state.fan_on == (speed > 0)
    assert state.unlocked


def test_light_decoding():
    assert parse_status(FRAME_LIGHT_4).brightness == 4
    assert parse_status(FRAME_LIGHT_4).light_on is True
    assert parse_status(FRAME_IDLE).brightness == 0
    assert parse_status(FRAME_IDLE).light_on is False


def test_measured_lags_and_is_not_used_for_state():
    """Byte 25 keeps reporting airflow after the fan is switched off."""
    state = parse_status(FRAME_OFF_ACK)
    assert state.speed == 0
    assert state.fan_on is False
    assert state.measured == 0x7F  # still spinning down
    assert state.flag == 1  # the off-acknowledgement flag


def test_locked_frame_is_not_an_error():
    state = parse_status(FRAME_LOCKED)
    assert state.unlocked is False
    assert state.speed == 0
    assert state.brightness == 0


def test_short_frame_raises():
    with pytest.raises(ThermexProtocolError):
        parse_status(b"\x00\x04\x00")


def test_corrupt_frame_raises():
    corrupt = bytearray(FRAME_SPEED_2)
    corrupt[11] ^= 0xFF  # change the payload, leave the CRC
    with pytest.raises(ThermexProtocolError):
        parse_status(bytes(corrupt))


# --- Encoding --------------------------------------------------------------


@pytest.mark.parametrize(
    ("speed", "expected"),
    [(0, "8000"), (1, "8001"), (2, "8002"), (3, "8003"), (4, "8004")],
)
def test_encode_fan(speed, expected):
    assert encode_fan(speed).hex() == expected


@pytest.mark.parametrize(
    ("brightness", "expected"),
    [(0, "81000000"), (4, "81040000"), (18, "81120000"), (75, "814b0000"), (100, "81640000")],
)
def test_encode_light(brightness, expected):
    """Values 0/18/75/100 are exactly what the phone app sent while dimming."""
    assert encode_light(brightness).hex() == expected


@pytest.mark.parametrize("bad", [-1, 5, 99])
def test_encode_fan_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        encode_fan(bad)


@pytest.mark.parametrize("bad", [-1, 101, 255])
def test_encode_light_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        encode_light(bad)


def test_unlock_constant_unchanged():
    """Guards against an accidental edit of the magic value."""
    assert UNLOCK.hex() == "a30cd753f5000000"


# --- Percentage mapping ----------------------------------------------------


@pytest.mark.parametrize(
    ("percentage", "speed"),
    [(0, 0), (1, 1), (25, 1), (30, 1), (50, 2), (75, 3), (99, 4), (100, 4)],
)
def test_percentage_to_speed(percentage, speed):
    assert percentage_to_speed(percentage) == speed


def test_percentage_round_trip():
    for speed in range(5):
        assert percentage_to_speed(round(speed * 100 / 4)) == speed
