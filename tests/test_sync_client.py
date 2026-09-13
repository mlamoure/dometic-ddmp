from __future__ import annotations

import threading
import time

import pytest

from ddmp.errors import ConnectionClosed, NotWritable
from ddmp.models import BatteryProtection
from ddmp.protocol import Action, Address, publish_frame
from ddmp.session import Nak, Publish
from ddmp.sync_client import SyncClient

from .conftest import RECORDED, capture_lines
from .fake_cooler import FakeCooler


def _wait(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def cooler():
    with FakeCooler(capture_lines()) as c:
        yield c


def test_connect_subscribes_and_populates_state(cooler):
    events: list = []
    client = SyncClient(cooler.host, cooler.port, on_event=events.append, subscribe_spacing=0)
    client.connect()
    try:
        assert _wait(lambda: len(events) >= RECORDED)
        subs = [f for f in cooler.received if f.action == Action.SUBSCRIBE]
        assert len(subs) == 23 and subs[0].address == Address(2, 0, 0, 0)
        st = client.state()
        assert st.product_name == "CFX525"
        assert st.compartment(0).temperature_c == 2.0
        assert client.value("v") == 13.5
        assert client.connected
    finally:
        client.close()
    assert not client.connected


def test_pushed_updates_arrive(cooler):
    seen = threading.Event()
    got: list = []

    def on_event(e):
        if isinstance(e, Publish) and e.name == "ctemp":
            got.append(e.value)
            if e.value == [-5.0]:
                seen.set()

    with SyncClient(cooler.host, cooler.port, on_event=on_event, subscribe_spacing=0) as client:
        assert _wait(lambda: len(got) >= 1)
        cooler.push(publish_frame("ctemp", [-5.0]))
        assert seen.wait(3)
        assert client.state().compartment(0).temperature_c == -5.0


def test_set_is_echoed_and_matches_expectation(cooler):
    events: list = []
    with SyncClient(cooler.host, cooler.port, on_event=events.append, subscribe_spacing=0) as c:
        assert _wait(lambda: any(isinstance(e, Publish) and e.name == "csettemp" for e in events))
        exp = c.set("csettemp", 2.0)
        assert _wait(lambda: any(exp.matches(e) for e in events))
        assert c.state().compartment(0).setpoint_c == 2.0
        sets = [f for f in cooler.received if f.action == Action.SET]
        assert sets[-1].encode() == bytes.fromhex("11 05 00 00 1a d0 07 00 00")


def test_nak_is_reported(cooler):
    cooler.refuse.add(Address(0x0D, 0, 0, 0x1A))
    events: list = []
    with SyncClient(cooler.host, cooler.port, on_event=events.append, subscribe_spacing=0) as c:
        assert _wait(lambda: len(events) >= RECORDED)
        exp = c.set("batprotlvl", BatteryProtection.HIGH)
        assert _wait(lambda: any(isinstance(e, Nak) for e in events))
        nak = next(e for e in events if isinstance(e, Nak))
        assert exp.matches(nak) is False


def test_default_deny_before_anything_is_sent(cooler):
    with SyncClient(cooler.host, cooler.port, subscribe_spacing=0) as c:
        with pytest.raises(NotWritable):
            c.set("sn", "x")
        assert not any(f.action == Action.SET for f in cooler.received)


def test_close_releases_slot_and_client_is_single_use(cooler):
    closes: list = []
    c = SyncClient(cooler.host, cooler.port, on_close=closes.append, subscribe_spacing=0)
    c.connect()
    c.close()
    assert _wait(lambda: len(closes) == 1)
    assert closes[0] is None  # closed by us, not an error
    with pytest.raises(ConnectionClosed):
        c.connect()
    with pytest.raises(ConnectionClosed):
        c.send(publish_frame("v", 1.0))
    for _ in range(5):
        with SyncClient(cooler.host, cooler.port, subscribe_spacing=0):
            pass
    assert cooler.connections == 6


def test_server_going_away_triggers_on_close(cooler):
    closes: list = []
    c = SyncClient(cooler.host, cooler.port, on_close=closes.append, subscribe_spacing=0)
    c.connect()
    cooler.close()
    assert _wait(lambda: len(closes) == 1)
    assert isinstance(closes[0], ConnectionClosed | OSError)
    assert not c.connected
    c.close()


def test_unreachable_host_raises_oserror():
    with pytest.raises(OSError):
        SyncClient("127.0.0.1", 1, connect_timeout=1.0).connect()
