from __future__ import annotations

import pytest

from ddmp.errors import NotWritable, StateUnknown
from ddmp.models import BatteryProtection, PowerSource, ProductType
from ddmp.protocol import Address, Frame, publish_frame
from ddmp.session import Nak, Publish, Session, Unhandled, WriteExpectation


def _fed_session(stream: bytes) -> Session:
    s = Session()
    s.feed(stream)
    return s


class TestFeed:
    def test_capture_yields_publishes(self, stream):
        s = Session()
        events = s.feed(stream)
        assert len(events) == 28
        assert all(isinstance(e, Publish) for e in events)
        assert events[0].name == "gw_fwver" and events[0].value == "1.0.2"
        assert s.dropped == 0

    def test_state_matches_the_live_cooler(self, stream):
        st = _fed_session(stream).state()
        assert st.firmware == "1.0.2"
        assert st.firmware_id == "MC1"
        assert st.mac == "14:33:5c:34:f1:2c"
        assert st.article == "SN0756"
        assert st.serial == "52402647"
        assert st.product_name == "CFX525"
        assert st.cms_sku == "97000050753"
        assert st.product_type is ProductType.SINGLE_ZONE
        assert st.compartment_count == 1
        assert st.cooler_on is True
        assert st.compressor_on is True
        assert st.voltage_v == 13.5
        assert st.current_a == 2.1  # the last pushed value wins
        assert st.power_source is PowerSource.DC
        assert st.battery_protection is BatteryProtection.MEDIUM
        assert st.error_codes == ()
        assert st.error_text == ""
        assert len(st.compartments) == 1
        c = st.compartment(0)
        assert c.temperature_c == 2.0
        assert c.setpoint_c == 1.0
        assert c.powered is True
        assert c.door_open is False
        assert c.range_c == (-22.0, 20.0)
        assert st.ice_maker_on is None  # never published on this unit

    def test_nak_and_unknown_actions(self):
        s = Session()
        nak = Frame(0x05, Address(5, 0, 0, 0x1A), b"").encode_line()
        nop = Frame(0x06, Address(0, 0, 0, 0), b"").encode_line()
        events = s.feed(nak + nop)
        assert isinstance(events[0], Nak) and events[0].address == Address(5, 0, 0, 0x1A)
        assert isinstance(events[1], Unhandled)

    def test_unknown_address_publish_keeps_raw(self):
        s = Session()
        (event,) = s.feed(Frame(0x10, Address(0x63, 0, 0, 0x1A), b"\x01").encode_line())
        assert event.topic is None and event.value == b"\x01"
        assert event.name == "63 00 00 1A"


class TestSubscribe:
    def test_subscribe_frames_are_five_bytes_each(self):
        frames = Session().subscribe_frames()
        assert len(frames) == 23
        assert all(len(f.encode()) == 5 and f.action == 0x12 for f in frames)
        assert frames[0].encode() == bytes.fromhex("12 02 00 00 00")


class TestSetFrame:
    def test_setpoint_single_zone(self, stream):
        s = _fed_session(stream)
        frame, exp = s.set_frame("csettemp", 2.0)
        assert frame.encode() == bytes.fromhex("11 05 00 00 1a d0 07 00 00")
        assert exp.expected == [2.0]
        echo = publish_frame("csettemp", [2.0])
        (event,) = s.feed(echo.encode_line())
        assert exp.matches(event) is True

    def test_setpoint_dual_zone_preserves_other_compartment(self, stream):
        s = _fed_session(stream)
        s.feed(publish_frame("csettemp", [1.0, -18.0]).encode_line())
        s.feed(publish_frame("ctemprng", [(-22.0, 20.0), (-22.0, 20.0)]).encode_line())
        frame, exp = s.set_frame("csettemp", 3.0, compartment=1)
        assert frame.payload == bytes.fromhex("e8 03 00 00 b8 0b 00 00")
        assert exp.expected == [1.0, 3.0]

    def test_setpoint_out_of_range_rejected(self, stream):
        s = _fed_session(stream)
        with pytest.raises(ValueError):
            s.set_frame("csettemp", 25.0)
        with pytest.raises(ValueError):
            s.set_frame("csettemp", -30.0)

    def test_per_compartment_needs_state(self):
        with pytest.raises(StateUnknown):
            Session().set_frame("csettemp", 1.0)
        with pytest.raises(StateUnknown):
            Session().set_frame("cpow", True)

    def test_bool_and_enum_coercion(self, stream):
        s = _fed_session(stream)
        frame, exp = s.set_frame("coolerpow", "off")
        assert frame.encode() == bytes.fromhex("11 0b 00 00 1a 00 00 00 00")
        assert exp.expected is False
        frame, exp = s.set_frame("batprotlvl", "low")
        assert frame.payload == b"\x00\x00\x00\x00"
        assert exp.expected is BatteryProtection.LOW
        frame, _ = s.set_frame("cpow", 0)
        assert frame.payload == b"\x00\x00\x00\x00"

    def test_default_deny(self, stream):
        s = _fed_session(stream)
        for name in ("ctemp", "sn", "ptype", "gw_fwver", "14 00 00 00"):
            with pytest.raises((NotWritable, KeyError)):
                s.set_frame(name, 1)

    def test_compartment_on_scalar_topic(self, stream):
        s = _fed_session(stream)
        with pytest.raises(ValueError):
            s.set_frame("coolerpow", True, compartment=1)


class TestWriteExpectation:
    def test_nak_and_mismatch(self, stream):
        s = _fed_session(stream)
        _, exp = s.set_frame("batprotlvl", "high")
        assert exp.matches(Nak(exp.topic.address, b"")) is False
        assert exp.matches(Nak(Address(1, 1, 1, 1), b"")) is None
        (other,) = s.feed(publish_frame("v", 13.6).encode_line())
        assert exp.matches(other) is None
        # the cooler re-publishes the OLD value first: not a refusal, just "not yet"
        (old,) = s.feed(publish_frame("batprotlvl", BatteryProtection.LOW).encode_line())
        assert exp.matches(old) is None
        assert exp.observed is BatteryProtection.LOW
        (new,) = s.feed(publish_frame("batprotlvl", BatteryProtection.HIGH).encode_line())
        assert exp.matches(new) is True

    def test_tolerance(self):
        from ddmp.protocol import TOPIC_BY_NAME

        exp = WriteExpectation(TOPIC_BY_NAME["csettemp"], [2.2222], tolerance=0.051)
        # the cooler rounds to 0.1 °C: 2.2222 is stored and published as 2.2
        assert exp.matches(Publish(exp.topic.address, exp.topic, [2.2], b"")) is True
        assert exp.matches(Publish(exp.topic.address, exp.topic, [1.5], b"")) is None
