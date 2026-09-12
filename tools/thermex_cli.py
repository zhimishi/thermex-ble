#!/usr/bin/env python3
"""
Thermex Design Line 8002 - BLE test tool

Protocol reverse engineered from a btsnoop HCI capture:
  service        0xFF00
  characteristic 0xFF01  (read / write / notify)

  Unlock (Write Request, 8 bytes) - must be sent once after connecting or the
  hood answers with an all-zero placeholder frame:
      a3 0c d7 53 f5 00 00 00

  Fan (Write Request, 2 bytes):
      80 00   off
      80 01 .. 80 04   speed 1-4

  Light (Write Request, 4 bytes):
      81 <level> 00 00     level = 0-100 (0x00-0x64), 0 = off

  Status notifications: 50 bytes, roughly once per second.
      byte  0   flag, normally 0 (seen as 1 on the off-ack) - meaning unclear
      byte  2   fan duty cycle in percent (0/25/45/60/99)
      byte  4   light level 0-100        <- authoritative light state
      byte  5   0x64, looks like max light level
      byte 10   0x64, looks like max fan duty
      byte 11   fan speed level 0-4      <- authoritative fan state
      byte 25   measured RPM/airflow, lags the setpoint
      byte 30-31 CRC-16/CCITT-FALSE over bytes 0..29, little-endian
      byte 35-36 slow-moving 16-bit LE value, purpose unknown (ADC?)

Usage:
    python3 thermex_ble.py scan            list everything nearby
    python3 thermex_ble.py find            autodetect the hood, print its address
    python3 thermex_ble.py monitor [addr]
    python3 thermex_ble.py set     [addr] <0-4>
    python3 thermex_ble.py light   [addr] <0-100>
    python3 thermex_ble.py sweep   [addr]
    python3 thermex_ble.py lightsweep [addr]
    python3 thermex_ble.py raw     [addr] <hexbytes>

The address is optional. Leave it out and the hood is found by scanning for
service 0xFF00, the 44:17:93 OUI, or a matching name; every candidate is then
verified by checking for characteristic 0xFF01 before use. The result is cached
in ~/.thermex_ble_address, so only the first run pays for a scan.

The unlock write is sent automatically by every command. Pass --no-init to
skip it, e.g. to confirm that the hood really does stay locked without it.

On macOS the address is a CoreBluetooth UUID, not a MAC, so OUI matching does
not work there - autodetection falls back to the service UUID and name.
Close the Thermex app and disable Bluetooth on the phone first - these modules
usually accept only one connection at a time.

Requires: pip install bleak
"""

import asyncio
import os
import sys

from bleak import BleakClient, BleakScanner

CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"

SPEED_NAMES = {0: "off", 1: "speed 1", 2: "speed 2", 3: "speed 3", 4: "speed 4"}

# Cache the discovered address so later runs skip the scan entirely.
CACHE_PATH = os.path.expanduser("~/.thermex_ble_address")

# The hood's BLE address sits two above the address reported inside its own
# status frames (frame says ...AD:94, it answers on ...AD:96). Match on the OUI
# only - the last three octets vary per unit.
ADDR_PREFIX = "44:17:93"

NAME_HINTS = ("thermex", "hood", "emhaet", "ventilator")


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def decode_status(payload: bytes) -> dict:
    """Decode a 50-byte status notification."""
    if len(payload) < 32:
        return {"error": f"short packet ({len(payload)} bytes)", "raw": payload.hex()}

    crc_rx = int.from_bytes(payload[30:32], "little")
    crc_calc = crc16_ccitt_false(payload[0:30])

    return {
        "speed": payload[11],
        "duty_pct": payload[2],
        "light": payload[4],
        "measured": payload[25],
        "flag0": payload[0],
        "aux": int.from_bytes(payload[35:37], "little") if len(payload) >= 37 else None,
        "crc_ok": crc_rx == crc_calc,
        "crc_rx": crc_rx,
        "crc_calc": crc_calc,
        "raw": payload.hex(),
    }


def format_status(st: dict) -> str:
    if "error" in st:
        return f"!! {st['error']}  {st['raw']}"
    speed = SPEED_NAMES.get(st["speed"], f"speed {st['speed']}?")
    crc = "ok" if st["crc_ok"] else f"BAD (rx {st['crc_rx']:04x} calc {st['crc_calc']:04x})"
    flag = "" if st["flag0"] == 0 else f"  flag0={st['flag0']}"
    light = f"light {st['light']:3d}%" if st["light"] else "light off "
    line = (
        f"{speed:<8} duty {st['duty_pct']:3d}%  {light}  "
        f"measured {st['measured']:3d}  crc {crc}{flag}"
    )
    # Always show the raw bytes when something looks off - that is what you
    # actually need to debug an unexpected packet.
    if not st["crc_ok"]:
        line += f"\n         raw {st['raw']}"
    return line


def score_candidate(device, adv) -> int:
    """Higher is more likely to be the hood. 0 means no evidence at all."""
    score = 0
    uuids = " ".join(u.lower() for u in (adv.service_uuids or []))
    if SERVICE_UUID in uuids or "ff00" in uuids:
        score += 10
    if device.address.upper().startswith(ADDR_PREFIX):
        score += 5
    name = (device.name or "").lower()
    if any(hint in name for hint in NAME_HINTS):
        score += 3
    return score


def read_cached_address():
    try:
        with open(CACHE_PATH) as fh:
            addr = fh.read().strip()
            return addr or None
    except OSError:
        return None


def write_cached_address(address: str):
    try:
        with open(CACHE_PATH, "w") as fh:
            fh.write(address + "\n")
    except OSError:
        pass  # cache is a convenience, never fatal


async def verify(address: str) -> bool:
    """Connect briefly and check the hood's characteristic is really there."""
    try:
        async with BleakClient(address, timeout=10.0) as client:
            return any(
                char.uuid.lower() == CHAR_UUID
                for service in client.services
                for char in service.characteristics
            )
    except Exception:
        return False


async def find_hood(timeout: float = 8.0, use_cache: bool = True) -> str:
    """Return the hood's address, scanning only when necessary."""
    if use_cache:
        cached = read_cached_address()
        if cached:
            print(f"Trying cached address {cached} ...")
            if await verify(cached):
                return cached
            print("Cached address did not respond, scanning instead.\n")

    print(f"Scanning for {timeout:.0f} seconds ...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    scored = sorted(
        ((score_candidate(d, a), d, a) for d, a in found.values()),
        key=lambda t: (-t[0], -t[2].rssi),
    )
    candidates = [(s, d, a) for s, d, a in scored if s > 0]

    if not candidates:
        sys.exit(
            "No likely candidate found.\n"
            "Run `scan` to list everything nearby, then pass the address "
            "explicitly. The hood does not always advertise its service UUID."
        )

    for score, device, adv in candidates:
        label = device.name or "(no name)"
        print(f"Candidate {device.address}  rssi {adv.rssi}  {label}  [score {score}]")
        if await verify(device.address):
            print(f"Verified {device.address}\n")
            write_cached_address(device.address)
            return device.address
        print("  ... does not expose characteristic 0xFF01, skipping")

    sys.exit("Found candidates, but none exposed the hood's characteristic.")


def resolve(args, index: int = 0):
    """Pull the address out of argv, or fall back to autodetection."""
    if len(args) > index and args[index].lower() not in ("auto", "-"):
        return args[index], args[index + 1:]
    return None, args[index + 1:] if len(args) > index else args[index:]


async def cmd_scan():
    print("Scanning for 10 seconds...\n")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)

    hits, others = [], []
    for dev, adv in devices.values():
        # 0xFF00 shows up as a short-form UUID in advertising data
        advertised = [u.lower() for u in adv.service_uuids]
        if SERVICE_UUID in advertised or "ff00" in " ".join(advertised):
            hits.append((dev, adv))
        else:
            others.append((dev, adv))

    if hits:
        print("Devices advertising service 0xFF00 - try these first:")
        for dev, adv in hits:
            print(f"  {dev.address}  rssi {adv.rssi:4d}  {dev.name or '(no name)'}")
        print()

    print("All other devices seen:")
    for dev, adv in sorted(others, key=lambda x: -x[1].rssi):
        print(f"  {dev.address}  rssi {adv.rssi:4d}  {dev.name or '(no name)'}")

    print(
        "\nNo 0xFF00 hit? The hood may not advertise the service UUID. Look for an "
        "unnamed device with strong RSSI while standing next to it, then run "
        "`monitor` against it - the script prints the full GATT table on connect."
    )


# Unlock write. Tested in isolation: this one alone unlocks status reporting.
# The app also sends 9c00000000000000, but that has no observable effect and is
# not replayed here. The hood validates the whole payload - a300000000000000
# does NOT work - so this is either a fixed magic value or a very long-lived
# token. If status frames ever come back all-zero again, suspect this first.
UNLOCK = bytes.fromhex("a30cd753f5000000")


async def connect(address) -> BleakClient:
    if address is None:
        address = await find_hood()
    client = BleakClient(address, timeout=20.0)
    await client.connect()
    print(f"Connected to {address}\n")
    write_cached_address(address)
    return client


async def send_init(client: BleakClient):
    print(f"--> unlock {UNLOCK.hex()}")
    await client.write_gatt_char(CHAR_UUID, UNLOCK, response=True)
    await asyncio.sleep(0.5)
    print()


def dump_services(client: BleakClient):
    print("GATT table:")
    for service in client.services:
        print(f"  service {service.uuid}")
        for char in service.characteristics:
            props = ",".join(char.properties)
            print(f"    char {char.uuid}  handle 0x{char.handle:04x}  [{props}]")
    print()


async def cmd_monitor(address=None, do_init: bool = True):
    client = await connect(address)
    try:
        dump_services(client)

        def on_notify(_, data: bytearray):
            print(format_status(decode_status(bytes(data))))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        print("Listening. Change the speed on the hood's own panel to see it update.")
        print("Ctrl-C to stop.\n")
        await asyncio.Event().wait()
    finally:
        await client.disconnect()


async def cmd_set(address, speed: int, do_init: bool = True):
    if not 0 <= speed <= 4:
        sys.exit("Speed must be 0-4")

    client = await connect(address)
    try:
        latest = []

        def on_notify(_, data: bytearray):
            latest.append(decode_status(bytes(data)))
            print(format_status(latest[-1]))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        await asyncio.sleep(1.5)

        payload = bytes([0x80, speed])
        print(f"\n--> writing {payload.hex()}  ({SPEED_NAMES[speed]})\n")
        await client.write_gatt_char(CHAR_UUID, payload, response=True)

        await asyncio.sleep(6.0)

        if latest and latest[-1].get("speed") == speed:
            print(f"\nConfirmed: hood reports {SPEED_NAMES[speed]}")
        else:
            print("\nHood did not report the requested speed. Check the notifications above.")
    finally:
        await client.disconnect()


async def cmd_sweep(address=None, do_init: bool = True):
    """Walk through every speed, then off. Verifies the whole command set."""
    client = await connect(address)
    try:
        state = {}

        def on_notify(_, data: bytearray):
            st = decode_status(bytes(data))
            state.update(st)
            print("   " + format_status(st))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        await asyncio.sleep(1.5)

        results = []
        for speed in [1, 2, 3, 4, 0]:
            payload = bytes([0x80, speed])
            print(f"\n--> {payload.hex()}  ({SPEED_NAMES[speed]})")
            await client.write_gatt_char(CHAR_UUID, payload, response=True)
            await asyncio.sleep(6.0)
            ok = state.get("speed") == speed
            results.append((speed, ok, state.get("duty_pct")))

        print("\n" + "=" * 46)
        print("Summary")
        for speed, ok, duty in results:
            mark = "ok  " if ok else "FAIL"
            print(f"  {mark}  {SPEED_NAMES[speed]:<8} duty reported: {duty}%")
    finally:
        await client.disconnect()


async def cmd_light(address, level: int, do_init: bool = True):
    if not 0 <= level <= 100:
        sys.exit("Light level must be 0-100")

    client = await connect(address)
    try:
        latest = []

        def on_notify(_, data: bytearray):
            latest.append(decode_status(bytes(data)))
            print(format_status(latest[-1]))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        await asyncio.sleep(1.5)

        payload = bytes([0x81, level, 0x00, 0x00])
        label = "off" if level == 0 else f"{level}%"
        print(f"\n--> writing {payload.hex()}  (light {label})\n")
        await client.write_gatt_char(CHAR_UUID, payload, response=True)

        await asyncio.sleep(4.0)

        if latest and latest[-1].get("light") == level:
            print(f"\nConfirmed: hood reports light {label}")
        else:
            reported = latest[-1].get("light") if latest else "nothing"
            print(f"\nRequested {level}, hood reports {reported}.")
            print("If it clamped the value, you have found the real maximum.")
    finally:
        await client.disconnect()


async def cmd_lightsweep(address=None, do_init: bool = True):
    """Step the light through its range to find the real limits and granularity."""
    client = await connect(address)
    try:
        state = {}

        def on_notify(_, data: bytearray):
            state.update(decode_status(bytes(data)))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        await asyncio.sleep(1.5)

        results = []
        for level in [0, 1, 4, 18, 50, 75, 100]:
            payload = bytes([0x81, level, 0x00, 0x00])
            print(f"--> {payload.hex()}  (light {level})")
            await client.write_gatt_char(CHAR_UUID, payload, response=True)
            await asyncio.sleep(3.0)
            reported = state.get("light")
            print(f"    hood reports {reported}")
            results.append((level, reported))

        print("\n" + "=" * 46)
        print("Summary - requested vs reported")
        for level, reported in results:
            mark = "ok  " if level == reported else "DIFF"
            print(f"  {mark}  sent {level:3d}  ->  reported {reported}")
        print("\nWatch the lamp itself: reported values can track a setpoint the")
        print("hardware does not actually reach at the low end.")
    finally:
        await client.disconnect()


async def cmd_raw(address, hexbytes: str, do_init: bool = True):
    """Send an arbitrary payload. Use this to hunt for the light command."""
    payload = bytes.fromhex(hexbytes.replace(" ", ""))
    client = await connect(address)
    try:
        def on_notify(_, data: bytearray):
            print(format_status(decode_status(bytes(data))))

        await client.start_notify(CHAR_UUID, on_notify)
        if do_init:
            await asyncio.sleep(0.5)
            await send_init(client)
        await asyncio.sleep(1.5)
        print(f"\n--> writing {payload.hex()}\n")
        await client.write_gatt_char(CHAR_UUID, payload, response=True)
        await asyncio.sleep(6.0)
    finally:
        await client.disconnect()


def looks_like_address(token: str) -> bool:
    """A MAC (aa:bb:...) or a CoreBluetooth UUID, but not a speed or hex payload."""
    return ":" in token or (len(token) == 36 and token.count("-") == 4)


def main():
    args = sys.argv[1:]
    do_init = "--no-init" not in args
    args = [a for a in args if a != "--no-init"]

    if not args:
        print(__doc__)
        sys.exit(1)

    cmd, rest = args[0], args[1:]

    # The address is optional. If the first argument after the command does not
    # look like one, autodetection kicks in and everything shifts left.
    address = None
    if rest and looks_like_address(rest[0]):
        address, rest = rest[0], rest[1:]
    elif rest and rest[0].lower() in ("auto", "-"):
        rest = rest[1:]

    try:
        if cmd == "scan":
            asyncio.run(cmd_scan())
        elif cmd == "find":
            print("\nHood address:", asyncio.run(find_hood(use_cache=False)))
        elif cmd == "monitor":
            asyncio.run(cmd_monitor(address, do_init))
        elif cmd == "set":
            asyncio.run(cmd_set(address, int(rest[0]), do_init))
        elif cmd == "light":
            asyncio.run(cmd_light(address, int(rest[0]), do_init))
        elif cmd == "sweep":
            asyncio.run(cmd_sweep(address, do_init))
        elif cmd == "lightsweep":
            asyncio.run(cmd_lightsweep(address, do_init))
        elif cmd == "raw":
            asyncio.run(cmd_raw(address, "".join(rest), do_init))
        else:
            print(__doc__)
            sys.exit(1)
    except (IndexError, ValueError) as exc:
        print(f"Bad arguments: {exc}\n")
        print(__doc__)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
