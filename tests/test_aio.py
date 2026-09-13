from __future__ import annotations

import asyncio

import pytest

from ddmp.aio import AsyncClient
from ddmp.errors import NotWritable, WriteRejected, WriteTimeout
from ddmp.protocol import Action, Address, publish_frame
from ddmp.session import Publish

from .conftest import RECORDED, capture_lines
from .fake_cooler import FakeCooler


@pytest.fixture
def cooler():
    with FakeCooler(capture_lines()) as c:
        yield c


async def _collect(client: AsyncClient, n: int, timeout: float = 3.0) -> list:
    out: list = []

    async def run() -> None:
        async for event in client.events():
            out.append(event)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_connect_and_state(cooler):
    client = await AsyncClient.connect(cooler.host, cooler.port)
    try:
        events = await _collect(client, RECORDED)
        assert all(isinstance(e, Publish) for e in events)
        assert client.state().product_name == "CFX525"
        assert client.value("v") == 13.5
    finally:
        await client.close()
    assert not client.connected


async def test_set_returns_echo(cooler):
    async with await AsyncClient.connect(cooler.host, cooler.port) as client:
        await _collect(client, RECORDED)
        assert await client.set("csettemp", 2.0) == [2.0]
        assert client.state().compartment(0).setpoint_c == 2.0
        assert await client.set("coolerpow", False) is False
        sets = [f for f in cooler.received if f.action == Action.SET]
        assert sets[0].encode() == bytes.fromhex("11 05 00 00 1a d0 07 00 00")


async def test_set_rejected_and_timeout(cooler):
    cooler.refuse.add(Address(0x0D, 0, 0, 0x1A))
    async with await AsyncClient.connect(cooler.host, cooler.port) as client:
        await _collect(client, RECORDED)
        with pytest.raises(WriteRejected):
            await client.set("batprotlvl", "HIGH")
        with pytest.raises(NotWritable):
            await client.set("sn", "x")
        # a SET the fake cooler never answers: make it drop SETs for cpow by refusing nothing
        # but pointing at an address it does not know -> emulate silence with a tiny timeout
        cooler.refuse.discard(Address(0x0D, 0, 0, 0x1A))
        original = cooler._reply

        def silent(frame):
            return None if frame.action == Action.SET else original(frame)

        cooler._reply = silent  # type: ignore[method-assign]
        with pytest.raises(WriteTimeout):
            await client.set("coolerpow", True, timeout=0.3)


async def test_pushed_update_stream(cooler):
    async with await AsyncClient.connect(cooler.host, cooler.port) as client:
        await _collect(client, RECORDED)
        cooler.push(publish_frame("cdoor", [True]))
        events = await _collect(client, 1)
        assert events[0].name == "cdoor" and events[0].value == [True]
        assert client.state().compartment(0).door_open is True


async def test_server_close_ends_stream(cooler):
    client = await AsyncClient.connect(cooler.host, cooler.port)
    await _collect(client, RECORDED)
    cooler.close()
    events = [e async for e in client.events()]
    assert events == []
    assert not client.connected
    await client.close()
