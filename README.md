# thermex-ble

Unofficial, reverse engineered library for controlling **Thermex range hoods**
over Bluetooth LE. No cloud, no account, no VoiceLink firmware requirement.

Developed and tested against a **Design Line 8002 (TDL8002W80BK)** on firmware
1.28. If you have another model, please open an issue with a capture — see
[PROTOCOL.md](PROTOCOL.md) for how to make one.

## Install

```bash
pip install thermex-ble
```

## Use

```python
import asyncio
from thermex_ble import ThermexHood

async def main():
    hood = ThermexHood("AA:BB:CC:DD:EE:FF")
    hood.register_callback(lambda state: print(state.speed, state.brightness))

    await hood.connect()
    await hood.set_fan(2)      # steps 0-4
    await hood.set_light(75)   # 0-100
    await asyncio.sleep(30)    # status frames arrive about once a second
    await hood.disconnect()

asyncio.run(main())
```

## Notes

The hood accepts **one connection at a time** and stops advertising while
connected. Disconnect the phone app before connecting from elsewhere.

State changes made on the hood's own control panel arrive as notifications, so
a persistent connection gives you true two-way sync.

`state.measured` is an airflow measurement that lags the setpoint by roughly 15
seconds. Use `state.speed` for on/off logic.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The protocol layer is pure functions with no I/O, tested against frames
captured from a real hood. No hardware is needed to run the tests.

## Licence

MIT. Not affiliated with or endorsed by Thermex.

## Connection lifecycle

`connect()` only succeeds after an unlocked status frame is received. Connection
setup and writes have timeouts, and command writes are serialized with link
cleanup. Late notifications and disconnect callbacks from an older connection
are ignored.

Use `register_disconnect_callback(callback)` to invalidate cached state as soon
as the current link drops; it returns an unsubscribe function. The library does
not run a reconnect loop itself. A long-running consumer, such as the Home
Assistant integration, must supervise the connection and re-resolve the current
Bluetooth device/proxy before recovery.

Cleanup accepts an already-disconnected client even when its proxy omitted the
disconnect callback. If the client still reports a live link after cleanup times
out, it is retained so another connection is not opened on top of it.
