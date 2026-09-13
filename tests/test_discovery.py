from __future__ import annotations

import json
import socket
import struct
import threading

from ddmp import discovery
from ddmp.discovery import DdmdReply, Discovered, _MdnsCollector, build_query, normalize_id

LIVE_REPLY = (
    b'{"version":2,"pid":4,"id":"14335c34f12c","name":"MC1_34f12c","f":0,"sku":"\\r\\n97000050753"}'
)


def test_ddmd_reply_parse_strips_and_normalizes():
    r = DdmdReply.parse(LIVE_REPLY, "10.66.40.129", 13143)
    assert r.cooler_id == "14335c34f12c"
    assert r.name == "MC1_34f12c"
    assert r.sku == "97000050753"
    assert r.version == 2 and r.pid == 4 and r.f == 0
    assert DdmdReply.parse(b"DDMD", "x", 1) is None
    assert DdmdReply.parse(b"{}", "x", 1) is None
    assert normalize_id("14:33:5C:34:F1:2C") == "14335c34f12c"


def test_probe_ddmd_unicast_roundtrip():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2)
    port = server.getsockname()[1]

    def answer() -> None:
        data, addr = server.recvfrom(64)
        if data == b"DDMD":
            server.sendto(LIVE_REPLY, addr)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    try:
        replies = discovery.probe_ddmd("127.0.0.1", port, timeout=1.0, first_only=True)
    finally:
        t.join(2)
        server.close()
    assert len(replies) == 1 and replies[0].cooler_id == "14335c34f12c"
    assert replies[0].host == "127.0.0.1" and replies[0].port == port


def _name(n: str) -> bytes:
    out = b""
    for label in n.strip(".").split("."):
        out += bytes([len(label)]) + label.encode()
    return out + b"\x00"


def _record(name: str, rtype: int, rdata: bytes) -> bytes:
    return _name(name) + struct.pack(">HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def _mdns_response() -> bytes:
    """A response shaped like the real cooler's (PTR + SRV + TXT + A, no compression)."""
    instance = "Dometic CFX5._ddmp._tcp.local"
    hostname = "MC1_34f12c.local"
    txt = b"".join(
        bytes([len(s)]) + s for s in (b"fwversion=MC1_1.0.2", b"sku=97000050753", b"board=MC1 #1")
    )
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    return (
        header
        + _record("_ddmp._tcp.local", 12, _name(instance))
        + _record(instance, 33, struct.pack(">HHH", 0, 0, 13143) + _name(hostname))
        + _record(instance, 16, txt)
        + _record(hostname, 1, socket.inet_aton("10.66.40.129"))
    )


def test_mdns_collector_assembles_services():
    c = _MdnsCollector("_ddmp._tcp.local")
    c.add(_mdns_response())
    (svc,) = c.services()
    assert svc.instance == "Dometic CFX5._ddmp._tcp.local"
    assert svc.hostname == "MC1_34f12c.local"
    assert svc.port == 13143
    assert svc.addresses == ("10.66.40.129",)
    assert svc.txt == {"fwversion": "MC1_1.0.2", "sku": "97000050753", "board": "MC1 #1"}


def test_mdns_collector_handles_compression_pointers():
    # Build a message where the SRV target and PTR use a pointer back to the question name.
    service = "_ddmp._tcp.local"
    header = struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 1)
    msg = header + _name(service) + struct.pack(">HH", 12, 1)
    q_off = 12
    instance_bytes = b"\x0cDometic CFX5" + struct.pack(">H", 0xC000 | q_off)  # "Dometic CFX5" + ptr
    ptr_rdata = instance_bytes
    msg += _name(service) + struct.pack(">HHIH", 12, 1, 120, len(ptr_rdata)) + ptr_rdata
    inst_off = len(msg) - len(ptr_rdata)
    srv_rdata = struct.pack(">HHH", 0, 0, 13143) + _name("MC1_34f12c.local")
    msg += struct.pack(">H", 0xC000 | inst_off) + struct.pack(">HHIH", 33, 1, 120, len(srv_rdata))
    msg += srv_rdata
    c = _MdnsCollector(service)
    c.add(msg)
    (svc,) = c.services()
    assert svc.instance == "Dometic CFX5._ddmp._tcp.local"
    assert svc.port == 13143 and svc.hostname == "MC1_34f12c.local"


def test_build_query_shape():
    q = build_query()
    assert q[:12] == struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    assert q.endswith(struct.pack(">HH", 12, 1))
    assert build_query(unicast_response=True).endswith(struct.pack(">HH", 12, 0x8001))


def test_discovered_matching_by_full_and_short_id():
    d = Discovered("14335c34f12c", "MC1_34f12c", "MC1_34f12c.local", "10.66.40.129", 13143)
    assert d.matches("14:33:5C:34:F1:2C")
    assert not d.matches("14335c000000")
    partial = Discovered("", "MC1_34f12c", "MC1_34f12c.local", "10.66.40.129", 13143)
    assert partial.short_id == "34f12c"
    assert partial.matches("14335c34f12c")
    assert not partial.matches("")


def test_discover_merges_mdns_and_probe(monkeypatch):
    svc = discovery.MdnsService(
        "Dometic CFX5._ddmp._tcp.local", "MC1_34f12c.local", 13143, ("10.66.40.129",),
        {"fwversion": "MC1_1.0.2", "sku": "97000050753"},
    )  # fmt: skip
    monkeypatch.setattr(discovery, "query_mdns", lambda **kw: [svc])
    reply = DdmdReply.parse(LIVE_REPLY, "10.66.40.129", 13143)

    def fake_probe(host="255.255.255.255", port=13143, **kw):
        return [reply] if host == "10.66.40.129" else []

    monkeypatch.setattr(discovery, "probe_ddmd", fake_probe)
    found = discovery.discover(timeout=0.1)
    assert len(found) == 1
    d = found[0]
    assert d.cooler_id == "14335c34f12c"
    assert d.hostname == "MC1_34f12c.local" and d.ip == "10.66.40.129"
    assert d.firmware == "MC1_1.0.2" and d.sku == "97000050753"
    assert d.sources == {"mdns", "probe"}
    assert discovery.find_cooler("14:33:5c:34:f1:2c", timeout=0.1) == d
    assert discovery.find_cooler("000000000000", timeout=0.1) is None


def test_json_reply_survives_repr():
    r = DdmdReply.parse(LIVE_REPLY, "h", 1)
    assert json.loads(json.dumps(r.__dict__ if hasattr(r, "__dict__") else {"id": r.cooler_id}))


def test_collector_reports_missing_records_for_ptr_only_answer():
    c = _MdnsCollector("_ddmp._tcp.local")
    header = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    instance = "Dometic CFX5._ddmp._tcp.local"
    c.add(header + _record("_ddmp._tcp.local", 12, _name(instance)))
    assert c.services() == []  # unresolved: no SRV yet
    assert c.missing() == [(instance, 33), (instance, 16)]
    hostname = "MC1_34f12c.local"
    c.add(
        struct.pack(">HHHHHH", 0, 0x8400, 0, 2, 0, 0)
        + _record(instance, 33, struct.pack(">HHH", 0, 0, 13143) + _name(hostname))
        + _record(instance, 16, b"\x13fwversion=MC1_1.0.2")
    )
    assert c.missing() == [(hostname, 1)]
    c.add(
        struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        + _record(hostname, 1, socket.inet_aton("10.66.40.129"))
    )
    assert c.missing() == []
    (svc,) = c.services()
    assert svc.addresses == ("10.66.40.129",) and svc.txt == {"fwversion": "MC1_1.0.2"}


def test_build_query_with_follow_up_questions():
    q = build_query(questions=[("Dometic CFX5._ddmp._tcp.local", 33), ("MC1_34f12c.local", 1)])
    assert struct.unpack(">HHHHHH", q[:12]) == (0, 0, 2, 0, 0, 0)
    assert b"\x0cDometic CFX5" in q and q.endswith(struct.pack(">HH", 1, 1))
