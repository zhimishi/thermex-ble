"""Connection handling for a Thermex hood over BLE."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .protocol import (
    CHAR_UUID,
    MAX_BRIGHTNESS,
    MAX_SPEED,
    UNLOCK,
    HoodState,
    ThermexProtocolError,
    encode_fan,
    encode_light,
    parse_status,
)

_LOGGER = logging.getLogger(__name__)

#: The hood stops advertising while something is connected, and it accepts only
#: one connection at a time. Hold the link open rather than reconnecting per
#: command - that also keeps notifications flowing so panel changes are seen.
DISCONNECT_GRACE = 0.5

#: How long to wait for the hood to confirm a command in a status frame.
CONFIRM_TIMEOUT = 3.0


class ThermexHood:
    """A single Thermex range hood.

    Usage::

        hood = ThermexHood(ble_device)
        hood.register_callback(lambda state: print(state))
        await hood.connect()
        await hood.set_fan(2)
        await hood.set_light(75)
    """

    def __init__(self, ble_device: BLEDevice | str) -> None:
        self._ble_device = ble_device
        self._client: BleakClient | None = None
        self._state: HoodState | None = None
        self._callbacks: list[Callable[[HoodState], None]] = []
        self._lock = asyncio.Lock()
        self._connected_event = asyncio.Event()

    # --- properties --------------------------------------------------------

    @property
    def address(self) -> str:
        if isinstance(self._ble_device, str):
            return self._ble_device
        return self._ble_device.address

    @property
    def state(self) -> HoodState | None:
        """Last decoded state, or None if nothing has arrived yet."""
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # --- callbacks ---------------------------------------------------------

    def register_callback(self, callback: Callable[[HoodState], None]) -> Callable[[], None]:
        """Subscribe to state updates. Returns an unsubscribe function."""
        self._callbacks.append(callback)

        def unregister() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unregister

    def _publish(self, state: HoodState) -> None:
        self._state = state
        for callback in self._callbacks:
            try:
                callback(state)
            except Exception:  # noqa: BLE001 - a bad subscriber must not kill the link
                _LOGGER.exception("Error in state callback")

    # --- connection --------------------------------------------------------

    async def connect(self) -> None:
        """Connect, subscribe to notifications, and unlock status reporting."""
        async with self._lock:
            if self.is_connected:
                return

            self._connected_event.clear()
            self._client = await establish_connection(
                BleakClient,
                self._ble_device,
                self.address,
                self._on_disconnect,
            )
            await self._client.start_notify(CHAR_UUID, self._on_notify)
            await self._client.write_gatt_char(CHAR_UUID, UNLOCK, response=True)
            _LOGGER.debug("Connected and unlocked %s", self.address)

        # Wait for the first real frame so callers see a populated state.
        try:
            await asyncio.wait_for(self._connected_event.wait(), CONFIRM_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning("No unlocked status frame from %s after unlock", self.address)

    async def disconnect(self) -> None:
        async with self._lock:
            if self._client is None:
                return
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Error while disconnecting %s", self.address, exc_info=True)
            await asyncio.sleep(DISCONNECT_GRACE)

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("Disconnected from %s", self.address)
        self._client = None
        self._connected_event.clear()

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        try:
            state = parse_status(bytes(data))
        except ThermexProtocolError as err:
            _LOGGER.warning("Discarding bad frame from %s: %s", self.address, err)
            return

        if state.unlocked:
            self._connected_event.set()
        self._publish(state)

    # --- commands ----------------------------------------------------------

    async def _write(self, payload: bytes) -> None:
        if not self.is_connected:
            await self.connect()
        assert self._client is not None
        _LOGGER.debug("Writing %s to %s", payload.hex(), self.address)
        await self._client.write_gatt_char(CHAR_UUID, payload, response=True)

    async def set_fan(self, speed: int) -> None:
        """Set the fan to a step between 0 (off) and 4."""
        await self._write(encode_fan(speed))

    async def set_light(self, brightness: int) -> None:
        """Set the light to a level between 0 (off) and 100."""
        await self._write(encode_light(brightness))

    async def turn_fan_off(self) -> None:
        await self.set_fan(0)

    async def turn_light_off(self) -> None:
        await self.set_light(0)

    async def update(self) -> HoodState | None:
        """Ensure a live connection and return the latest state.

        The hood pushes a frame every second, so this exists for callers that
        want to force a reconnect rather than to poll.
        """
        if not self.is_connected:
            await self.connect()
        return self._state


__all__ = ["ThermexHood", "MAX_SPEED", "MAX_BRIGHTNESS"]
