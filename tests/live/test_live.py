"""Read-only checks against a real cooler. Opt in with ``DDMP_LIVE_HOST=<ip>``.

These never send a SET frame.
"""

from __future__ import annotations

import os
import time

import pytest

from ddmp import SyncClient, discover, probe_ddmd

HOST = os.environ.get("DDMP_LIVE_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="set DDMP_LIVE_HOST to run live tests")


def test_probe_answers_with_version_2():
    (reply,) = probe_ddmd(HOST, timeout=2.0, first_only=True)
    assert reply.version == 2
    assert len(reply.cooler_id) == 12
    assert reply.name.startswith("MC1_")


def test_subscribe_populates_full_state():
    with SyncClient(HOST) as client:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and client.value("errst") is None:
            time.sleep(0.2)
        st = client.state()
    assert st.firmware_id == "MC1"
    assert st.product_name and st.product_name.startswith("CFX")
    assert st.mac and len(st.mac) == 17
    assert st.compartment(0) is not None
    assert st.compartment(0).temperature_c is not None
    assert st.compartment(0).setpoint_c is not None
    assert st.voltage_v and st.voltage_v > 5
    assert st.battery_protection is not None


def test_discovery_finds_the_cooler():
    found = discover(timeout=3.0, hosts=[HOST])
    assert any(d.ip == HOST for d in found)
