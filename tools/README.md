# tools/

`thermex_cli.py` is the standalone script this library grew out of. It talks to
the hood directly with bleak and needs nothing else installed.

It is the right tool for bringing up a new model: `monitor` prints the GATT
table and decodes every status frame, and `raw` sends arbitrary payloads so you
can hunt for commands that are not implemented yet.

```bash
pip install bleak
python3 thermex_cli.py find
python3 thermex_cli.py monitor
python3 thermex_cli.py set 2
python3 thermex_cli.py light 75
python3 thermex_cli.py raw 8104
```

The address is optional and cached in `~/.thermex_ble_address` after the first
successful connection.
