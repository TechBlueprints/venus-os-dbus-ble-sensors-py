# Handing the Power Watchdog onto the shared scanner (the EasyStart pattern)

**Status:** proposal, 2026-09-09. Cross-repo (dbus-ble-sensors-py owns the
router contract; dbus-power-watchdog owns its consumer side). Nothing built.

## What "the same thing the EasyStarts do" means here
The EasyStart driver does NOT run its own BLE scan. It rides sensors-py's
single passive HCI tap: the tap hears the unit's name-prefixed
advertisement, the driver connects over the shared bcmv2 layer, polls, and
lets go — one scanner, advert-driven connect, no independent discovery. The
Power Watchdog service today does the opposite: its own
`BleakScanner.discover()` active scan (a BlueZ-driven discovery that creates
a Device1 object per advertiser and drives the discovery-window load bursts
we see on foreign cards). The handoff moves it to the EasyStart pattern.

(The Power Watchdog is always-on shore power, so the EasyStart's
*silent-when-idle* half does not transfer — only the shared-scanner and
connect-via-BCM half does. Its device stays always reachable.)

## The one real gap: the router routes by mfg-id, the Watchdog is name-routed
`ble_advertisement_router` lets an external D-Bus service register interest
and receive `Advertisement` signals, but only by manufacturer id, product,
product range, or address:
```
/ble_advertisements/{service}/mfgr/{id}
/ble_advertisements/{service}/mfgr_product/{mfg}_{pid}
/ble_advertisements/{service}/mfgr_product_range/{mfg}_{min}_{max}
/ble_advertisements/{service}/addr/{mac}
```
The Power Watchdog carries no distinctive manufacturer id; it is identified
by advertised NAME prefix — `PM` (gen1) and `WD_` (gen2) — exactly the way
the EasyStart is (`EasyStart_`). sensors-py's tap already parses local names
and matches prefixes internally (that is how the EasyStart works), but the
router does not expose name-prefix registration to external services. That
is the piece to add.

## sensors-py side (this repo — the enabling half)
1. **Router: a fifth registration type**
   `/ble_advertisements/{service}/name_prefix/{prefix}`. Same `Advertisement`
   signal (`sqaynss`: mac, mfg_id, data, rssi, interface, name) — the `name`
   field already exists and carries the local name.
2. **Feed the router from the tap's name path.** The tap already delivers
   name-carrying advertisements (`name_prefixes`, `_process_name_advertisement`).
   Add the union of externally-registered prefixes to the tap's
   `name_prefixes`, and route name matches through
   `router.process_advertisement` the way manufacturer matches already are.
3. **Address learning for accept-list mode.** Under `ContinuousScan OFF` the
   controller only delivers accept-listed MACs. Reuse the existing
   name-device address machinery (the same that keeps a silent EasyStart
   hearable) to learn each Watchdog's address from its first name advert and
   inject it, so accept-list mode keeps delivering it.
4. Tests: a name_prefix registration delivers the right adverts, updates the
   tap prefix set, and injects the learned address; mfg/addr routing
   unchanged.

## dbus-power-watchdog side (that repo — the consumer half)
1. Register `PM` and `WD_` at
   `/ble_advertisements/dbus-power-watchdog/name_prefix/{prefix}` and handle
   the `Advertisement` signal.
2. Delete `BleakScanner.discover()` discovery. On an `Advertisement` for a
   Watchdog, resolve+connect via the bcmv2 layer it already uses
   (`install_ble_connection_manager`, `tolerate_late_gatt`) — the mac and
   adapter come in the signal, so the connect skips its own scan.
3. Keep `find_device_by_address` only as the direct-connect fallback bcmv2
   allows; drop the recurring discovery scan entirely.
4. Discovery toggle in its GUI becomes "register the prefixes" vs
   "unregister", not "start/stop my own scan".

## Why
- Removes a second BLE scanner from the box — the Watchdog's
  `BleakScanner.discover` is a BlueZ active scan, the exact Device1-churn +
  discovery-window-load source sensors-py moved off of in May
  (`d13891a`). One scanner, many consumers.
- The Watchdog rides the same passive tap, claims, and adapter coordination
  as everything else, instead of contending for the radios.

## Interactions / caveats
- **Accept-list capacity.** hci1's 25-slot list is already full (the
  EasyStart 0x07). Adding Watchdog addresses compounds it — another reason
  to land PR #15 Phase 2 (retire the accept list), after which name-routed
  external devices need no slot at all.
- **Only-one-active.** The Watchdog service already enforces one active
  device; nothing here changes that — the router just feeds it sightings.
- Sequencing: sensors-py ships the name_prefix router type first (behind no
  flag — it is inert until something registers), then dbus-power-watchdog
  switches over and deletes its scanner. Restart-class on both; each on
  Clint's word.
