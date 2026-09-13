# CLAUDE.md — dometic-ddmp

Repo-specific rules. Global rules in `~/.claude/CLAUDE.md` apply.

## Commands

```bash
source .venv/bin/activate.fish && pip install -e ".[dev]"
ruff format . && ruff check . && pytest            # run before every commit
DDMP_LIVE_HOST=10.66.40.129 pytest tests/live       # opt-in, read-only, needs the cooler on
python -m ddmp watch 10.66.40.129 --raw             # live read-only stream
```

## Architecture

- `src/ddmp/protocol.py` — sans-I/O: actions, `Address`, `Frame`, `LineDecoder`, codecs,
  the topic table (`TOPICS`, `SUBSCRIBE_TOPICS`), the write allow-list (`WRITABLE`) and
  `assert_writable()`. No sockets, no logging.
- `src/ddmp/session.py` — pure state machine: `feed(bytes) -> events`, last values,
  `subscribe_frames()`, `set_frame()` + `WriteExpectation` (the cooler confirms a SET only
  by echoing a PUBLISH; there is no ACK on this transport).
- `src/ddmp/sync_client.py` — blocking client for hosts without an event loop (Indigo):
  socket + daemon reader thread + callbacks. **Single-use**: one object per connection;
  reconnect policy belongs to the caller.
- `src/ddmp/aio.py` — asyncio client with the same semantics (for Home Assistant later).
- `src/ddmp/discovery.py` — `DDMD` UDP probe and a stdlib one-shot mDNS browse (multicast
  query so an mDNS repeater can reflect the answer across VLANs). The browse is two-stage
  DNS-SD: PTR first, then SRV/TXT/A follow-ups, because the CFX5 answers a browse with the
  PTR record alone once it has been up for a while. `discover()` merges mDNS and probe hits
  by the MAC tail.
- `src/ddmp/models.py` — enums, error texts, `CoolerState`.
- `src/ddmp/__main__.py` — CLI. Write commands are dry runs unless `--yes`.

## Invariants

- **Default deny on writes.** Only `csettemp`, `cpow`, `coolerpow`, `batprotlvl`, `icepow`
  are writable; the guard is the allow-list, `NEVER_WRITE` is documentation. Never add the
  gateway command word (`14 00 00 00`, Restart / Factory reset), OTA, certificate, Wi-Fi or
  factory-data topics to the allow-list.
- Library values are °C / V / A. Unit conversion is the caller's job.
- Per-compartment writes send the whole array so the other zone keeps its value; the
  current value must have been published first (`StateUnknown` otherwise).
- Tests never touch a real cooler except `tests/live/` (read-only, opt-in). The fake cooler
  in `tests/fake_cooler.py` replays `tests/fixtures/capture-2026-09-13.b64`.

## Known device facts (CFX5 25, firmware MC1_1.0.2)

- Pool of 4 TCP clients (the 5th is closed within seconds); idle connections never time out;
  input is read at ~2.5 KB/s; unparseable input is silently discarded.
- Publishes arrive once per subscribe and then on every change. No keepalive either way, so
  `SyncClient` enables TCP keepalive (30 s idle / 10 s / 3 probes).
- A SET is answered by a re-publish of the OLD value first, then the NEW value ~3 s later
  (each usually twice). Only a NAK means refusal; wait up to 10 s for the matching publish.
  Set-points are rounded by the cooler to 0.1 °C. Verified live 2026-09-13 for `csettemp`
  and `batprotlvl` (values restored afterwards).
- The cooler is often powered off: a connect timeout means "off", not "broken".

## Release

Static version in `pyproject.toml` and `src/ddmp/__init__.py` (keep them equal). Tag
`vX.Y.Z` on Gitea (`mike/dometic-ddmp`). GitHub / PyPI publishing only after Mike's explicit
approval.
