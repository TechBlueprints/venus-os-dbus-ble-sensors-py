# The BLE connection layer (bcmv2)

This service touches Bluetooth in two completely different ways, and they
are built on completely different stacks on purpose.

| | Advertisements (inbound) | Connections (outbound) |
|---|---|---|
| What | Every sensor reading this service publishes | Charger setpoints, key provisioning, VREG probes |
| Stack | Raw HCI monitor socket | bleak, routed through **bcmv2** |
| Scanning | Controller-driven, passive by default | Active, but only to resolve a device BlueZ has never seen |
| Code | `hci_scan_control.py`, `hci_advertisement_tap.py`, `scan_claims.py` | `ble_catcher.py`, `ble_async_loop.py`, `ble_gatt_link.py`, `orion_tr_gatt.py` |

## Why the advertisement path is not routed through bcmv2

There is no bleak scanner in that path to route.  The tap configures the
controller itself over a raw HCI socket and reads results off the monitor
channel, which is what keeps BlueZ from materialising a `Device1` object
per advertiser — the growth that used to march dbus-daemon toward OOM at
roughly 95 MB/hr.  See `hci_scan_control.py` for the full measurement.

That scan is **passive by default**: it listens, and never transmits the
SCAN_REQ an active scanner sends to every advertiser in range.
`/Settings/BleSensors/ActiveScan` switches it, for devices whose payload
only arrives in the SCAN_RSP — some Victron firmwares moved the encrypted
instant-readout record there, and a passive scanner sees only the short
product-id beacon, so the unit reads as off.

### But the catcher *does* wrap the scanner

`wrap_scanner=True`.  The process has exactly one bleak scanner — the
device-resolution fallback below — and it is unambiguously an active scan.
An active scan nobody else can see is precisely what this project has spent
effort not inflicting on other services.  Wrapped, it takes the adapter's hard `hciN.scan` claim while it
runs, ranks away from cards another process is already scanning on, and
releases when it stops.

`scan_to_score=False`, though.  That option buys RSSI-based placement by
running short active sweeps *of its own* on a 10s-every-300s cadence,
forever — recurring active scanning is exactly what the tap's passive
default exists to avoid, and it is a different proposition from one bounded
discovery for a device we cannot otherwise reach.  So placement stays bcmv2's least-used mode:
occupancy and failure history, no RSSI base.

## What bcmv2 does for the connection path

`ble_catcher.install()` rebinds `bleak.BleakClient` process-wide, so every
link this service opens gets:

- **Claim-aware placement** over `/run/bt-claims`, the same file convention
  dbus-serialbattery and the other services here follow.  A card another
  process is scanning on, or has filled to its link capacity, ranks last.
- **Failure-driven adapter rotation** for pinned devices, and connect
  scoring for unpinned ones.
- **Link slots** on adapters given a capacity in config, so a CSR dongle
  is not asked for a sixth link it cannot open.
- **Connection-parameter tuning** (habluetooth's fast-then-medium
  supervision timeouts) on the adapter the link actually uses.

Retries are *not* bcmv2's job — it routes, it never retries — so
connections go through `bleak-retry-connector`'s `establish_connection`
(`ble_gatt_link.connect`).

## Device resolution, and the discovery we avoid

Handed a bare address, bleak's BlueZ backend calls
`BleakScanner.find_device_by_address` — an active discovery.  Doing that on
every setpoint write would undo the whole point of the HCI tap, so
`ble_gatt_link.resolve()` goes in order:

1. **Ask BlueZ what it already knows** (`ble_gatt_dbus.lookup_device`).
   Our chargers are bonded, and a bonded device keeps its `Device1` object
   on its adapter across reboots.  Costs no radio time.  The resulting
   `BLEDevice` carries its D-Bus path, which bcmv2 reads as an explicit
   adapter choice — correct, because a bond *is* an adapter choice — and
   still lands claims and tuning on the card the link will use.
2. **Only if BlueZ has never seen it** — first provisioning, or a removed
   bond — fall back to a bounded discovery.  That goes through the wrapped
   scanner, so it holds the adapter's hard `hciN.scan` claim for its
   duration; we pick no adapter and hold no claim by hand.

## Claims we publish

Connections and the resolution scan claim through bcmv2 itself.  The
advertisement path is not routed through bcmv2, but it is not invisible to
it either: `scan_claims.py` holds a **soft** claim
(`hciN.use.dbus-ble-sensors-py-<pid>.scan`) for every adapter we have
scanning enabled on, released when the adapter disappears or the load
throttle stops scanning.

Claim files are keyed by the adapter's MAC, not by `hciN` — same reason the
scan path is (see [`hci-tap-architecture.md`](hci-tap-architecture.md)): the
number is not an identity, and a claim that names a number stops describing
the card the moment it renumbers.  bcmv2 accepts `hciN` everywhere as a
convenience spelling that resolves to one.

**The kind follows the scan type**, because that is what makes the claim
true:

| Scan type | Claim | Why |
|---|---|---|
| Passive (default) | soft, `<MAC>.use.<owner>.scan` | Listens, transmits nothing, genuinely shares the card — a fact to rank on, not a reservation. Ours is a permanent listen rather than a short scan activity, so a hard claim would push everyone off that radio forever. |
| Active (`ActiveScan=1`) | hard, `<MAC>.scan` | Transmits a SCAN_REQ at every advertiser and holds the channel for the reply. That is exactly what the hard claim announces; calling it soft would let a second scanner land on the same radio believing it free. |

If the hard claim is already held by another live process we fall back to a
soft one rather than going silent — we are on that radio either way, and
everyone else should still see it. We never yield a card because of someone
else's claim: one-directional by design. Our own hard claim does not push
our own connections away, since bcmv2 compares the claim's pid to its own.

The useful consequence: our own GATT writes go through bcmv2, which ranks
by occupancy, so a charger write now naturally prefers a card we are *not*
scanning on.  That is the coordination `adapter-allowlist.conf` previously
had to be hand-tuned to achieve.

## Threading

The service's main loop is GLib; bleak's is asyncio.  Rather than pump one
from the other, `ble_async_loop.py` owns **one** long-lived event loop on a
daemon thread, and results return to the GLib thread via `GLib.idle_add`.

One loop, not one per operation: bleak keeps its `BlueZManager` — and under
it a `dbus_fast` MessageBus — as a per-event-loop singleton, so a loop per
GATT write leaks a system-bus connection each time.

The split is strict:

- **GLib thread** — all dbus-python: BlueZ device lookup, the pairing agent.
- **BLE loop thread** — all bleak: resolve, connect, write.

The pairing agent is why the split matters rather than merely being tidy.
BlueZ needs an `org.bluez.Agent1` to answer Victron's passkey and bleak
registers none, so `ble_gatt_dbus.PairingAgent` supplies one over
dbus-python — and the BLE thread can block awaiting `Device1.Pair()`
precisely because a *different* thread is dispatching the passkey request
back to BlueZ.  The standalone tools have no GLib loop of their own, so
they run `ble_gatt_dbus.pump_default_context()` as an asyncio task instead.

The catcher itself is installed lazily, on the first GATT operation, so an
installation with only tank and temperature sensors never pays bleak's
import cost.  The claims layer is stdlib-only and loads at startup.

## Configuration

Optional, at `/data/apps/dbus-ble-sensors-py/ble-connect.conf`:

```ini
# Adapters usable for GATT.  Empty (or no file) means every adapter the
# kernel exposes is a candidate, ranked by live claims.
#   hci1                      pool entry
#   AA:BB:CC:DD:EE:FF@hci1    pin that device to that adapter
#                             (repeat the MAC for a preference list)
adapters = hci1 hci2

# Established-link capacity, for dongles with an undocumented limit
# (CSR ~5, Broadcom ~7 are the field starting points).  Opt-in; uncapped
# adapters are never slot-gated.
link_caps = hci1:5
```

This is **not** `adapter-allowlist.conf`.  That file reserves adapters away
from the advertisement scanner; this one bounds where GATT links may be
placed.
They are separate on purpose: a card reserved from our scanning is usually
the best card to connect on.

## Dependencies

On a Venus device the stack comes from **`/data/bcm`**, a single
checkout of `bleak-connection-manager` shared by every BLE consumer on
the box (this service, `dbus-shyion-switch`, `dbus-power-watchdog`,
`dbus-easytouchrv`, `serialbattery`).  Sharing it is what makes the
claims in `/run/bt-claims` mean the same thing to all of them: adapter
placement and drain cooperation are a protocol *between* services, so a
fix has to land for all of them at once rather than per-repo as each
bumps a submodule to a different sha.

`install.sh` converges that checkout — fast-forward only, so a stale
installer can never move the fleet backwards — and then runs BCM's own
installer, which smoke-imports the stack before writing the interpreter
shim `/data/bcm/python3`.  Our run scripts exec through that shim and
fall back to plain `python3` when it is absent.

This repo carries **no copy of its own**.  It used to, as a fallback for
a bare clone, and the fallback was removed for two reasons that only
became clear once it existed:

* Adapter placement and drain cooperation are a protocol between
  services.  A private copy of that protocol is a private opinion about
  it — the claims in `/run/bt-claims` only mean something if everyone
  reading and writing them agrees.
* The fallback was reached *precisely when* converging `/data/bcm` had
  just failed, so a stale copy meant the box silently dropped to an
  older stack at the exact moment it had reported being unhealthy.  Prod
  was found in that state on 2026-08-25 — running BCM `a96aef1` from the
  shim while this repo pinned `32197b1`, five commits ahead, with
  nothing announcing the gap.

`install.sh` therefore treats the stack as required, but distinguishes
two failures that a single "did it converge?" check would conflate:

| Situation | Install behaviour |
|---|---|
| Fetch failed, `/data/bcm/python3` already present | Warn, continue on what is there — it may be behind |
| No shim at all | **Fatal.** There is nothing to run GATT on |

That split is what keeps a transient failure on an RV uplink (Starlink,
LTE, sometimes neither) from failing an install of a service that would
otherwise run perfectly, without pretending an absent stack is
survivable.

BCM's `CONSUMER_MIGRATION.md` offers two acceptable resolutions to a
fallback that can go stale: keep both paths current, or make convergence
fatal so a broken one is loud.  We first chose the former and enforced
it with a sha comparison in `install.sh`.  That check was itself an
adjacent predicate — it answered "is the fallback stale relative to the
shared checkout", standing in for "is this box running the stack we
think it is".  The two questions coincided while the vendored copy was
the live path and stopped coinciding the moment the shim became it, so
when prod ran an *older* `/data/bcm` than the pin, the check reported
nothing: it only looked for the fallback being behind, never ahead.

Removing the copy resolves it by deletion rather than by a better check.
There is one stack, the shim provides it, and there is no second sha for
anything to be silently behind.

Advertisement-driven sensors keep working regardless — a
missing stack is reported once and degrades to "no GATT", never to a crash.
