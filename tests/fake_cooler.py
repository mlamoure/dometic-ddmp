"""A tiny TCP server that behaves like the CFX5's port 13143 for tests.

* On SUBSCRIBE it replies with the recorded PUBLISH for that address (if any).
* On SET it echoes a PUBLISH with the same payload, or a NAK when told to refuse.
* Anything else is silently ignored, like the real firmware.
"""

from __future__ import annotations

import base64
import contextlib
import socket
import threading
from collections.abc import Iterable

from ddmp.protocol import Action, Address, Frame, LineDecoder


class FakeCooler:
    def __init__(self, recorded_lines: Iterable[bytes], *, refuse: set[Address] | None = None):
        self.publishes: dict[Address, Frame] = {}
        for line in recorded_lines:
            frame = Frame.decode(base64.b64decode(line))
            self.publishes[frame.address] = frame  # last value wins, as on the wire
        self.refuse: set[Address] = set(refuse or ())
        self.received: list[Frame] = []
        self.connections = 0
        self.max_clients = 4
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def host(self) -> str:
        return self._server.getsockname()[0]

    @property
    def port(self) -> int:
        return self._server.getsockname()[1]

    def push(self, frame: Frame) -> None:
        """Send an unsolicited PUBLISH to every connected client (value changed)."""
        self.publishes[frame.address] = frame
        with self._lock:
            for conn in list(self._clients):
                with contextlib.suppress(OSError):
                    conn.sendall(frame.encode_line())

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for conn in self._clients:
                with contextlib.suppress(OSError):
                    conn.close()
        self._server.close()
        self._thread.join(timeout=2)

    def __enter__(self) -> FakeCooler:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- internals

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._clients) >= self.max_clients:
                    conn.close()
                    continue
                self._clients.append(conn)
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        decoder = LineDecoder()
        conn.settimeout(0.2)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                for frame in decoder.feed(data):
                    self.received.append(frame)
                    reply = self._reply(frame)
                    if reply is not None:
                        with contextlib.suppress(OSError):
                            conn.sendall(reply.encode_line())
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            with contextlib.suppress(OSError):
                conn.close()

    def _reply(self, frame: Frame) -> Frame | None:
        if frame.action == Action.SUBSCRIBE:
            return self.publishes.get(frame.address)
        if frame.action == Action.SET:
            if frame.address in self.refuse:
                return Frame(Action.NAK, frame.address, b"")
            echo = Frame(Action.PUBLISH, frame.address, frame.payload)
            self.publishes[frame.address] = echo
            return echo
        return None
