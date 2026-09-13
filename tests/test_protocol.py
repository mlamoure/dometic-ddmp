from __future__ import annotations

import base64

import pytest

from ddmp import protocol
from ddmp.errors import DecodeError, NotWritable
from ddmp.models import BatteryProtection, PowerSource, ProductType
from ddmp.protocol import (
    NEVER_WRITE,
    SUBSCRIBE_TOPICS,
    TOPIC_BY_ADDRESS,
    TOPIC_BY_NAME,
    TOPICS,
    WRITABLE,
    Action,
    Address,
    Frame,
    LineDecoder,
    assert_writable,
    publish_frame,
    subscribe_frame,
    topic,
)


class TestAddress:
    def test_roundtrip_and_str(self):
        a = Address(0x04, 0, 0, 0x1A)
        assert a.to_bytes() == b"\x04\x00\x00\x1a"
        assert Address.from_bytes(b"\x04\x00\x00\x1a") == a
        assert str(a) == "04 00 00 1A"

    def test_parse_forms(self):
        for text in ("04 00 00 1A", "0400001a", "04:00:00:1a"):
            assert Address.parse(text) == Address(4, 0, 0, 0x1A)
        with pytest.raises(ValueError):
            Address.parse("04 00 1A")

    def test_short_bytes(self):
        with pytest.raises(DecodeError):
            Address.from_bytes(b"\x04\x00")


class TestFrame:
    def test_subscribe_measured_temperature_is_the_live_line(self):
        frame = subscribe_frame("ctemp")
        assert frame.encode() == bytes.fromhex("12 04 00 00 1a")
        assert frame.encode_line() == b"EgQAABo=\r"

    def test_decode_publish_temperature(self):
        raw = base64.b64decode(b"EAQAABrQBwAA")
        frame = Frame.decode(raw)
        assert frame.action == Action.PUBLISH
        assert frame.address == Address(4, 0, 0, 0x1A)
        assert frame.payload == b"\xd0\x07\x00\x00"
        assert str(frame) == "PUBLISH 04 00 00 1A D0 07 00 00"

    def test_unknown_action_kept_as_int(self):
        frame = Frame.decode(b"\x77\x01\x02\x03\x04")
        assert frame.action == 0x77
        assert frame.action_name == "0x77"

    def test_too_short(self):
        with pytest.raises(DecodeError):
            Frame.decode(b"\x10\x04\x00")


class TestLineDecoder:
    def test_split_chunks_and_garbage(self):
        d = LineDecoder()
        assert d.feed(b"EAQAA") == []
        frames = d.feed(b"BrQBwAA\rnot base64!\rEAUAABroAwAA\rEg==\r")
        assert [str(f) for f in frames] == [
            "PUBLISH 04 00 00 1A D0 07 00 00",
            "PUBLISH 05 00 00 1A E8 03 00 00",
        ]
        assert d.dropped == 2  # garbage line + 1-byte frame
        assert d.pending == b""

    def test_blank_lines_and_crlf_are_ignored(self):
        d = LineDecoder()
        frames = d.feed(b"\r\rEAsAABoBAAAA\r\n\r")
        assert len(frames) == 1
        assert d.dropped == 0

    def test_whole_capture_decodes(self, stream, raw_frames):
        d = LineDecoder()
        frames = d.feed(stream)
        assert [f.encode() for f in frames] == raw_frames
        assert d.dropped == 0


class TestCodecs:
    def test_milli_negative_and_pairs(self):
        assert TOPIC_BY_NAME["i"].codec.decode(bytes.fromhex("34 08 00 00")) == 2.1
        assert TOPIC_BY_NAME["i"].codec.decode(bytes.fromhex("cc f7 ff ff")) == -2.1
        rng = TOPIC_BY_NAME["ctemprng"].codec.decode(bytes.fromhex("10 aa ff ff 20 4e 00 00"))
        assert rng == [(-22.0, 20.0)]

    def test_milli_array_single_and_dual_zone(self):
        codec = TOPIC_BY_NAME["ctemp"].codec
        assert codec.decode(bytes.fromhex("d0 07 00 00")) == [2.0]
        assert codec.decode(bytes.fromhex("d0 07 00 00 b0 b9 ff ff")) == [2.0, -18.0]
        assert codec.encode([1.0, -18.0]) == bytes.fromhex("e8 03 00 00 b0 b9 ff ff")

    def test_strings_strip_nul_padding_and_whitespace(self):
        codec = TOPIC_BY_NAME["sn"].codec
        assert codec.decode(b"52402647" + b"\x00" * 7) == "52402647"
        assert TOPIC_BY_NAME["cms_sku"].codec.decode(b"\r\n97000050753") == "97000050753"
        assert codec.encode("abc") == b"abc"

    def test_enum_known_and_unknown(self):
        codec = TOPIC_BY_NAME["batprotlvl"].codec
        assert codec.decode(b"\x01\x00\x00\x00") is BatteryProtection.MEDIUM
        assert codec.decode(b"\x09\x00\x00\x00") == 9
        assert codec.encode("high") == b"\x02\x00\x00\x00"
        assert codec.encode(BatteryProtection.LOW) == b"\x00\x00\x00\x00"
        assert TOPIC_BY_NAME["powsrc"].codec.decode(b"\x01\x00\x00\x00") is PowerSource.DC
        assert TOPIC_BY_NAME["ptype"].codec.decode(b"\x01\x00\x00\x00") is ProductType.SINGLE_ZONE

    def test_bool_mac_u16_empty_errors(self):
        assert TOPIC_BY_NAME["coolerpow"].codec.decode(b"\x01\x00\x00\x00") is True
        assert TOPIC_BY_NAME["cdoor"].codec.decode(b"\x00\x00\x00\x00") == [False]
        assert (
            TOPIC_BY_NAME["gw_mac"].codec.decode(bytes.fromhex("14335c34f12c"))
            == "14:33:5c:34:f1:2c"
        )
        assert TOPIC_BY_NAME["errst"].codec.decode(b"") == []
        assert TOPIC_BY_NAME["errst"].codec.decode(b"\x17\x00\x10\x00") == [23, 16]

    def test_short_payload_raises(self):
        with pytest.raises(DecodeError):
            TOPIC_BY_NAME["v"].codec.decode(b"\x01\x02")


class TestTopics:
    def test_table_is_consistent(self):
        assert len({t.name for t in TOPICS}) == len(TOPICS)
        assert len({t.address for t in TOPICS}) == len(TOPICS)
        assert set(TOPIC_BY_ADDRESS) == {t.address for t in TOPICS}
        assert all(t.name in TOPIC_BY_NAME for t in SUBSCRIBE_TOPICS)

    def test_live_addresses(self):
        assert TOPIC_BY_NAME["ctemp"].address == Address.parse("04 00 00 1A")
        assert TOPIC_BY_NAME["csettemp"].address == Address.parse("05 00 00 1A")
        assert TOPIC_BY_NAME["coolerpow"].address == Address.parse("0B 00 00 1A")
        assert TOPIC_BY_NAME["product_name"].address == Address.parse("01 00 00 1C")
        assert TOPIC_BY_NAME["cfg_fwid"].address == Address.parse("07 00 01 00")
        assert TOPIC_BY_NAME["gw_mac"].address == Address.parse("11 00 00 00")

    def test_lookup_forms(self):
        assert topic("ctemp") is TOPIC_BY_NAME["ctemp"]
        assert topic(Address(4, 0, 0, 0x1A)) is TOPIC_BY_NAME["ctemp"]
        assert topic("04 00 00 1A") is TOPIC_BY_NAME["ctemp"]
        with pytest.raises(KeyError):
            topic("nope")

    def test_default_deny_writes(self):
        assert {"csettemp", "cpow", "coolerpow", "batprotlvl", "icepow"} == WRITABLE
        for name in ("ctemp", "sn", "sku", "fwver", "ptype", "nocpt", "ctemprng", "gw_fwver"):
            with pytest.raises(NotWritable):
                assert_writable(name)
        for address, _reason in NEVER_WRITE:
            if address in TOPIC_BY_ADDRESS:
                with pytest.raises(NotWritable):
                    assert_writable(address)
        assert assert_writable("csettemp").name == "csettemp"

    def test_gateway_command_word_is_not_even_a_topic(self):
        assert Address(0x14, 0, 0, 0) not in TOPIC_BY_ADDRESS

    def test_publish_frame_helper(self):
        frame = publish_frame("csettemp", [1.0])
        assert frame.encode_line() == b"EAUAABroAwAA\r"
        assert protocol.DEFAULT_PORT == 13143
