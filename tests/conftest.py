from __future__ import annotations

import base64
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def capture_lines() -> list[bytes]:
    """The base64 lines from the 2026-09-13 live session (without terminators)."""
    lines = []
    for line in (FIXTURES / "capture-2026-09-13.b64").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line.encode())
    return lines


def capture_stream() -> bytes:
    """The same session as it arrived on the wire: every line terminated with CR."""
    return b"".join(line + b"\r" for line in capture_lines())


def capture_frames_raw() -> list[bytes]:
    return [base64.b64decode(line) for line in capture_lines()]


@pytest.fixture
def stream() -> bytes:
    return capture_stream()


@pytest.fixture
def raw_frames() -> list[bytes]:
    return capture_frames_raw()


#: Distinct topics present in the capture that the client subscribes to (the fake cooler can
#: only answer those): 21 of the 23 default subscriptions (acpt and icepow were never published).
RECORDED = 21
