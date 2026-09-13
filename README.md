# dometic-ddmp

Local Wi-Fi client for **Dometic CFX5** coolers. Speaks Dometic's DDMP v2 ("DDM2") protocol
directly to the cooler on the LAN: no cloud, no account, no Bluetooth. Pure Python 3.12+,
zero dependencies.

The protocol was decoded from Dometic's Mobile Cooling app (2.0.30, the last build with
Wi-Fi) and verified against a CFX5 25 running firmware MC1_1.0.2. Dometic removed Wi-Fi
control from its apps in November 2025; the cooler firmware still serves it.

## Install

```bash
pip install git+https://github.com/mlamoure/dometic-ddmp.git@v0.1.2   # not on PyPI yet
```

## Command line

```bash
ddmp discover                     # mDNS + DDMD broadcast, then a unicast probe per hit
ddmp discover --host 10.66.40.129 # probe one address
ddmp state 10.66.40.129           # subscribe, wait, print the decoded state as JSON
ddmp watch 10.66.40.129 --raw     # stream every publish (read-only)
ddmp set-temp 10.66.40.129 2.0    # dry run: prints the frame, sends nothing
ddmp set-temp 10.66.40.129 2.0 --yes
ddmp set 10.66.40.129 batprotlvl MEDIUM --yes
```

Write commands print the exact frame first and wait for the cooler's echo (exit 0 confirmed,
3 refused, 4 no echo).

## Library

```python
from ddmp import SyncClient, Publish


def on_event(event):
    if isinstance(event, Publish):
        print(event.name, event.value)


client = SyncClient("10.66.40.129", on_event=on_event)
client.connect()  # subscribes to everything; publishes arrive as they change
state = client.state()  # ddmp.CoolerState, all values in °C / V / A
expect = client.set("csettemp", 2.0)  # returns a WriteExpectation; the echo confirms it
client.close()
```

```python
from ddmp import AsyncClient

async with await AsyncClient.connect("10.66.40.129") as client:
    async for event in client.events():
        ...
    await client.set("coolerpow", False)  # waits for the echo; WriteRejected / WriteTimeout
```

Only five topics can be written (`csettemp`, `cpow`, `coolerpow`, `batprotlvl`, `icepow`);
everything else raises `NotWritable`. The cooler holds at most four connections and never
closes idle ones, so keep one client per process and close it when done.

### How the cooler answers a write (verified 2026-09-13 on a CFX5 25, firmware MC1_1.0.2)

* A SET is not acknowledged. The cooler first re-publishes the **old** value (usually twice),
  then publishes the **new** value about 3 s later, again twice. `WriteExpectation` therefore
  treats a non-matching publish as "not yet" and only a NAK as a refusal; wait up to 10 s.
* Set-points are stored with 0.1 °C granularity: `2.2222` is stored and published as `2.2`.
  Non-integer values are fine (2.0 °C = 35.6 °F was accepted on a unit displaying °F).
* Verified frames: set-point `11 05 00 00 1A D0 07 00 00` (2.0 °C) and `… E8 03 00 00`
  (1.0 °C); battery protection `11 0D 00 00 1A 00 00 00 00` (Low) and `… 01 00 00 00`
  (Medium). Cooler power (`coolerpow`) and compartment power (`cpow`) use the same layout
  and are verified by the community over Bluetooth.

## Protocol summary

| | |
|---|---|
| Discovery | mDNS `_ddmp._tcp.local` (instance `Dometic CFX5`, host `MC1_<mac tail>.local`), or send `DDMD` to UDP 13143 and read the JSON reply |
| Transport | TCP 13143; each frame is `base64(bytes) + "\r"` |
| Frame | `action, param, instance, subclass, class, payload…` |
| Actions | `0x12` SUBSCRIBE (5 bytes), `0x10` PUBLISH (cooler → client, on subscribe and on change), `0x11` SET, `0x05` NAK |
| Values | int32 little-endian; °C, V and A scaled by 1000; one int32 per compartment; UTF-8 strings NUL-padded; errors uint16[] |
| Cooler class `0x1A` | 04 measured temp · 05 set-point · 03 compartment power · 0B cooler power · 0D battery protection (0 Low, 1 Medium, 2 High) · 07 door · 0E compressor · 0C voltage · 0F current · 10 power source (0 AC, 1 DC) · 12 errors · 08 range · 13 serial · 14 article |
| Product class `0x1C` | 01 name · 03 SKU |
| Gateway | `02 00 00 00` firmware · `11 00 00 00` MAC · `07 00 01 00` firmware id (`MC1`) |

There is no authentication on this channel: anyone who can reach port 13143 can change the
set-point or switch the cooler off. Keep the cooler on an isolated network.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate.fish   # fish; use activate for bash
pip install -e ".[dev]"
ruff format . && ruff check . && pytest
DDMP_LIVE_HOST=10.66.40.129 pytest tests/live   # read-only checks against a real cooler
```
