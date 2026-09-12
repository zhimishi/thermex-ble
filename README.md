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
    hood = ThermexHood("44:17:93:5C:AD:96")
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
