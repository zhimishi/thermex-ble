"""Connection lifecycle regressions using a proxy with delayed callbacks."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from thermex_ble import hood as hood_module
from thermex_ble.hood import ThermexHood


class Client:
    def __init__(self, callback):
        self.callback = callback
        self.is_connected = True
        self.start_notify = AsyncMock()
        self.write_gatt_char = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self.report_disconnect)

    def report_disconnect(self):
        self.is_connected = False
        self.callback(self)


@pytest.fixture
def connection(monkeypatch):
    hood = ThermexHood("AA:BB:CC:DD:EE:FF")
    clients = []

    async def establish(_type, _device, _name, callback):
        client = Client(callback)
        client.write_gatt_char.side_effect = lambda *a, **kw: hood._connected_event.set()
        clients.append(client)
        return client

    factory = AsyncMock(side_effect=establish)
    monkeypatch.setattr(hood_module, "establish_connection", factory)
    monkeypatch.setattr(hood_module, "DISCONNECT_GRACE", 0)
    monkeypatch.setattr(hood_module, "DISCONNECT_TIMEOUT", 0.1)
    return hood, clients, factory


@pytest.mark.parametrize("stage", ["start_notify", "write_gatt_char"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_setup_releases_client(connection, stage, cancelled):
    hood, clients, factory = connection
    establish = factory.side_effect
    error = asyncio.CancelledError() if cancelled else RuntimeError("setup failed")

    async def fail(*args):
        client = await establish(*args)
        getattr(client, stage).side_effect = error
        return client

    factory.side_effect = fail
    with pytest.raises(type(error)) as raised:
        await hood.connect()
    assert raised.value is error
    clients[0].disconnect.assert_awaited_once()
    assert not hood.is_connected
    assert hood._client is None
    factory.side_effect = establish
    await hood.connect()
    assert hood.is_connected


async def test_reconnect_waits_for_disconnect_callback(connection):
    hood, clients, factory = connection
    await hood.connect()
    old = clients[0]
    requested = asyncio.Event()
    old.disconnect.side_effect = requested.set
    disconnect = asyncio.create_task(hood.disconnect())
    await requested.wait()
    reconnect = asyncio.create_task(hood.connect())
    await asyncio.sleep(0)
    assert not disconnect.done()
    assert factory.await_count == 1
    old.report_disconnect()
    await disconnect
    await reconnect
    assert factory.await_count == 2
    assert hood.is_connected


async def test_old_callback_does_not_clear_new_client(connection):
    hood, clients, _ = connection
    await hood.connect()
    old = clients[0]
    await hood.disconnect()
    await hood.connect()
    old.report_disconnect()
    assert hood._client is clients[1]
    assert hood.is_connected
    assert hood._connected_event.is_set()
    assert not hood._disconnected_event.is_set()


async def test_disconnect_timeout_blocks_new_connection(connection):
    hood, clients, factory = connection
    await hood.connect()
    old = clients[0]
    old.disconnect.side_effect = None
    with pytest.raises(TimeoutError):
        await hood.disconnect()
    assert hood._client is old
    assert not hood.is_connected
    with pytest.raises(TimeoutError):
        await hood.connect()
    assert factory.await_count == 1
    old.disconnect.side_effect = old.report_disconnect
    await hood.connect()
    assert hood.is_connected
    assert factory.await_count == 2


async def test_cleanup_failure_preserves_setup_error(connection):
    hood, clients, factory = connection
    establish = factory.side_effect
    error = RuntimeError("notify failed")

    async def fail(*args):
        client = await establish(*args)
        client.start_notify.side_effect = error
        client.disconnect.side_effect = RuntimeError("disconnect failed")
        return client

    factory.side_effect = fail
    with pytest.raises(RuntimeError) as raised:
        await hood.connect()
    assert raised.value is error
    assert hood._client is clients[0]
    assert not hood.is_connected


async def test_disconnect_is_idempotent(connection):
    hood, clients, _ = connection
    await hood.disconnect()
    await hood.connect()
    await hood.disconnect()
    await hood.disconnect()
    clients[0].disconnect.assert_awaited_once()


async def test_unexpected_disconnect_allows_reconnect(connection):
    hood, clients, factory = connection
    await hood.connect()
    clients[0].report_disconnect()
    assert not hood.is_connected
    assert not hood._connected_event.is_set()
    await hood.connect()
    assert factory.await_count == 2
    assert hood.is_connected


async def test_disconnect_without_callback_releases_disconnected_client(connection):
    hood, clients, factory = connection
    await hood.connect()
    clients[0].is_connected = False
    clients[0].disconnect.side_effect = None
    await hood.disconnect()
    assert hood._client is None
    await hood.connect()
    assert factory.await_count == 2
    assert hood.is_connected


async def test_disconnect_flag_cleared_while_waiting_without_callback(connection):
    hood, clients, _ = connection
    await hood.connect()
    old = clients[0]
    requested = asyncio.Event()
    old.disconnect.side_effect = requested.set
    task = asyncio.create_task(hood.disconnect())
    await requested.wait()
    old.is_connected = False
    await task
    assert hood._client is None


async def test_disconnect_subscribers_are_notified_and_can_unsubscribe(connection):
    from unittest.mock import Mock

    hood, clients, _ = connection
    callback = Mock()
    unregister = hood.register_disconnect_callback(callback)
    await hood.connect()
    clients[0].report_disconnect()
    callback.assert_called_once_with()
    unregister()
    unregister()
    await hood.connect()
    clients[1].report_disconnect()
    callback.assert_called_once_with()


async def test_old_disconnect_does_not_notify_subscribers_again(connection):
    from unittest.mock import Mock

    hood, clients, _ = connection
    callback = Mock()
    hood.register_disconnect_callback(callback)
    await hood.connect()
    old = clients[0]
    old.report_disconnect()
    await hood.connect()
    old.report_disconnect()
    callback.assert_called_once_with()
    assert hood.is_connected


async def test_no_initial_status_fails_and_releases_connection(connection, monkeypatch):
    hood, clients, factory = connection
    monkeypatch.setattr(hood_module, "CONFIRM_TIMEOUT", 0.01)
    establish = factory.side_effect

    async def no_status(*args):
        client = await establish(*args)
        client.write_gatt_char.side_effect = None
        return client

    factory.side_effect = no_status
    with pytest.raises(TimeoutError):
        await hood.connect()
    assert not hood.is_connected
    assert hood._client is None
    clients[0].disconnect.assert_awaited_once()


async def test_hung_setup_is_bounded_and_releases_lock(connection, monkeypatch):
    hood, clients, factory = connection
    monkeypatch.setattr(hood_module, "CONNECT_TIMEOUT", 0.01)
    establish = factory.side_effect

    async def hang():
        await asyncio.Event().wait()

    # Accept the normal characteristic and callback arguments.
    async def hang_notify(*args):
        await hang()

    async def bad_notify(*args):
        client = await establish(*args)
        client.start_notify.side_effect = hang_notify
        return client

    factory.side_effect = bad_notify
    with pytest.raises(TimeoutError):
        await hood.connect()
    clients[0].disconnect.assert_awaited_once()
    assert not hood._lock.locked()
    factory.side_effect = establish
    await hood.connect()
    assert hood.is_connected


async def test_hung_write_is_bounded(connection, monkeypatch):
    hood, clients, _ = connection
    monkeypatch.setattr(hood_module, "WRITE_TIMEOUT", 0.01)
    await hood.connect()

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    clients[0].write_gatt_char.side_effect = hang
    with pytest.raises(TimeoutError):
        await hood.set_fan(2)
    assert not hood._lock.locked()


async def test_late_notification_from_retired_client_is_ignored(connection):
    from unittest.mock import Mock

    hood, clients, _ = connection
    await hood.connect()
    old_notify = clients[0].start_notify.call_args.args[1]
    await hood.disconnect()
    await hood.connect()
    hood._on_notify = Mock()
    old_notify(None, bytearray(50))
    hood._on_notify.assert_not_called()


async def test_connect_calls_share_one_setup(connection):
    hood, clients, factory = connection
    await asyncio.gather(hood.connect(), hood.connect())
    assert factory.await_count == 1
    assert hood.is_connected
