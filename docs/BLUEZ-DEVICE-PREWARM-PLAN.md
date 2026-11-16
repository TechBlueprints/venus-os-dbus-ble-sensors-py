# Plan: pre-seeding BlueZ with MACs the passive tap has heard

> **Status (2026-11-16): reference, not scheduled.** Written 2026-09-05.
> None of the experiments in section 3 have been run and the code change in
> section 4 has not been made. Two things have changed since:
>
> - The 2026-09-15 radio layout gave links their own GATT pool
>   (`ble-connect.conf`). Our scan holds no claim on those cards (read on
>   prod 2026-11-16), so the discovery fallback has a free card and the
>   scan-claim deadlock that prompted this plan has lost its precondition.
>   No first-time provision has exercised that since. The plan matters again
>   if a layout ever puts our scan on every GATT-eligible card.
> - Links no longer go on the card that heard the advertisement. A
>   connection on a listening card turns its scan off until the next
>   rotation tick, so EasyStart's `ConnectDevice` now targets a pool card
>   (`link_adapter_key` in `ble_device_easystart.py`). Wherever this
>   document says "the adapter that heard it", read "a GATT-pool card", and
>   take the blind-window measurements of 3.3 and 3.6 there. Section 0 is
>   the 09-05 snapshot; the card at `00:01:95:C9:B0:AF` has been removed.

**Question.** While the raw HCI tap is passively scanning, can we push the
MAC addresses it hears into the BlueZ/bleak device registry, so that when a
driver later asks "is this device available on that card?" the answer is
already yes, with no discovery scan?

**Short answer from the source and the box.** There is no BlueZ API that
creates a `Device1` object from a bare address without either a
BlueZ-driven scan or a successful connection. The one create-on-demand
primitive we have, `Adapter1.ConnectDevice`, is a *connect*, and it only
materialises the object after the link comes up. But once it has succeeded
once, BlueZ marks that device non-temporary and stores it under
`/data/var/lib/bluetooth/<adapter MAC>/<device MAC>/info`, so it is
"already there" on that card for every later attempt, across bluetoothd
restarts and reboots. So the achievable form of the idea is a **one-time
bootstrap connection on the adapter that heard the advertisement**, after
which the zero-scan resolution path (`ble_gatt_link.resolve` step 1) takes
over for good. The EasyStart driver already does exactly this for itself
(`_resolve_without_scanning` in `ble_device_easystart.py`); the work is to
verify its lifecycle claims, measure the side effects, and generalise it.

Everything below is a plan to test that, plus the dead ends and why they
are dead, so nobody re-walks them.

## 0. What is on the box (prod, 2026-09-05, read-only probe)

| Item | Value |
|---|---|
| Kernel | 6.12.90-venus-4 |
| bluetoothd | 5.72, started `-E -f /etc/bluetooth/ble.conf` (ControllerMode=le, Privacy=device) |
| `Adapter1.ConnectDevice` | present (experimental method, needs `-E`, which we have) |
| `AdvertisementMonitorManager1` | present; `SupportedMonitorTypes=[or_patterns]`, `SupportedFeatures=[]` (no controller-side patterns, bluetoothd filters in software) |
| bleak | 3.0.2 vendored at `/data/bcm/ext/upstream/bleak` |
| bcmv2 | `/data/bcm/src/bleak_connection_manager/catcher.py` |
| dbus-fast | 2.21.1 |
| habluetooth / bleak-retry-connector | not installed as packages (brc's `establish_connection` is what `ble_gatt_link.connect` imports, so it exists somewhere on the path; confirm with the service's `sys.path`) |
| Storage | `/data/var/lib/bluetooth/<adapter MAC>/` (13 adapter dirs, 3 of them for cards no longer present) |
| Tools | `btmgmt`, `hcitool`, `btmon`, `bluetoothctl`, `dbus-send`. **No** `busctl`, `gdbus`, `paste`. |
| `TemporaryTimeout` | not set in ble.conf, so BlueZ default of 30 s |

Devices BlueZ holds today are exactly the ones that were once connected
(chargers, thermostats, BMSes, the SmartSolar pair on `00:01:95:C9:B0:AF`).
Nothing the tap hears but has never been connected has an object.

## 1. Why the tap-heard MACs are invisible to BlueZ (mechanism)

Two gates, one in the kernel and one in bluetoothd, and our passive scan
fails both by design:

1. **Kernel (`net/bluetooth/hci_event.c`, `process_adv_report`, v6.12).**
   During a passive scan the kernel forwards a report to userspace as a
   mgmt `Device Found` only if the address is in `pend_le_reports` (a
   device added with mgmt `Add Device`, action 0) or an advertisement
   monitor is registered. Otherwise `return;`. Our raw-socket scan is
   passive, so nothing reaches bluetoothd.
2. **bluetoothd (`src/adapter.c`, `update_found_devices`, 5.72).** Even
   when a `Device Found` arrives, a *new* object is only created when
   `adapter->discovering || monitoring` is true; otherwise the report is
   dropped before the create. So switching our raw scan to active (which
   makes the kernel forward everything) still creates nothing, because
   BlueZ is not the one discovering.

Both gates are the reason the tap keeps dbus-daemon flat. Any scheme that
opens them reopens the ~95 MB/h growth path documented in
`hci_scan_control.py`, scaled by how many devices get through.

## 2. Candidate mechanisms, ranked

### A. `Adapter1.ConnectDevice` at resolve time, fed by the tap (recommended)

What the EasyStart driver does. `device_connect()` opens an L2CAP ATT
link to the address; on success `device_connect_cb()` calls
`btd_adapter_get_device()` (creates the object if absent), replies with
the path, and calls `btd_device_set_temporary(device, false)`, which
stores the `info` file. That last call is what makes this an *injection*
and not just a connect: the object outlives the session and
`load_devices()` recreates it at the next bluetoothd start.

Costs and unknowns to measure (section 3):

- The kernel connects via scan (`hci_connect_le_scan`): it puts the
  address in the controller accept list and runs *its own* passive scan
  until the device advertises, then issues LE Create Connection. That
  toggles LE scan on the card underneath our raw scan. `enable_scan`'s
  recovery model already re-issues our parameters, but the length of the
  blind window has never been measured for this path.
- On failure no object is created (`goto failed` → `org.bluez.Failed`),
  and there is no BlueZ-side timeout in `device_connect()`. A
  ConnectDevice to a device that stops advertising pends in the kernel
  and in bluetoothd with no cancel API (no object exists yet, so
  `RemoveDevice` cannot target it). What that leaves behind, and whether
  a retry gets `InProgress`, is unknown.
- The EasyStart code tolerates `org.bluez.Error.AlreadyExists` from
  ConnectDevice. Nothing in 5.72's `connect_device`/`device_connect`
  returns that name, so it comes from a lower layer or was observed on an
  older build. Record the real error names during the experiment.

### B. Proactive pre-warm (bootstrap connect at first sighting)

Same primitive as A, but triggered by the router when a GATT-capable
driver's device is first heard, not when GATT is first needed. Buys
nothing over A except moving the first connection earlier, at the cost of
a connection nobody asked for. Decide after A's measurements; the default
recommendation is *lazy* (A).

### C. Write the `info` file ourselves and restart bluetoothd

`load_devices()` creates an object for every `<adapter>/<dev>/info` it
finds. Writing `[General]\nAddressType=random\nSupportedTechnologies=LE;`
would inject without any radio time, but only at bluetoothd start, and a
bluetoothd restart takes `org.bluez` off the bus for every consumer on
the box (documented in `service/run`). Usable once, at install or during
a planned outage, never live. Keep as a fallback for a device that
refuses to connect but must be pinned to a card.

### D. `AdvertisementMonitor1` with a narrow `or_patterns` filter

What habluetooth does for passive mode. Makes BlueZ own the passive scan
on that adapter (conflicts with `hci_scan_control`) and creates objects
plus a `PropertiesChanged` per advertisement for every matching device.
A pattern on Victron's manufacturer ID would bound it to Victron devices,
but that is still the growth path, and the scan-ownership conflict is
structural. Rejected unless A fails.

### E. mgmt `Add Device` action 0 (`btmgmt add-device -a 0`)

Opens the kernel gate for one address (`pend_le_reports`) but not the
bluetoothd gate, so the report is dropped. Also triggers
`hci_update_passive_scan`, toggling our scan. Dead on the source reading;
one cheap live test in section 3 confirms it so the reading is not the
adjacent predicate.

### F. mgmt `Add Device` action 2 (kernel auto-connect)

The kernel connects whenever it sees the device; the resulting
`Device Connected` event makes bluetoothd create the object. Strictly
worse than A: same connection, plus BlueZ's auto-connect bookkeeping
gets an entry it did not make.

### G. `SetDiscoveryFilter({Pattern: MAC, Transport: le})` + `StartDiscovery`, called directly

The one live create-without-connect we do have. `device_is_discoverable()`
prefix-matches the pattern against the address, and a non-matching report
is dropped *before* `adapter_create_device`, so a pattern equal to the full
MAC creates exactly that one object and nothing else. A Victron beacon at
about 1 Hz means the object appears within a second or two, then
`StopDiscovery`.

Two limits, both verified in 5.72:

- The object is **temporary**. `Trusted=true` does not clear that
  (`btd_device_set_trusted` only stores and emits), so 30 s after
  discovery ends the temporary timer removes it. It persists only if a
  connection follows, which clears temporary the same way A does. So G is a
  resolve-time create, not a standing pre-seed: create, then connect within
  30 s. (The timer is restarted on a connect attempt, so a slow connect is
  fine.)
- It is a BlueZ discovery, which the kernel runs as an **active** scan on
  that card, toggling our raw scan and sending SCAN_REQs for its 1 to 3 s.
  The deadlock it hits today is bcmv2's wrapped scanner refusing a card our
  own pid holds the scan claim on. That is our policy, not BlueZ's: calling
  `Adapter1.StartDiscovery` directly on the card that heard the device
  sidesteps it, and the card is ours to blind briefly.

Its advantage over A is cancellation: `StopDiscovery` is a real cancel, and
a device that stopped advertising leaves nothing pending in the kernel.
Measure G alongside A in section 3; if the blind window is comparable, G
is the safer primitive for the create step, with `Device1.Connect` on the
resulting path for the connect step.

## 3. Experiments

Run on dev-cerbo first; prod only for the read-only steps and for step
3.3 once dev has shown the blind window is short. Every command on the
Cerbo is BusyBox: `head -n`, `tail -n`, no `timeout` (use `cmd & sleep N;
kill`). Standalone scripts that pair need `DBusGMainLoop`; these do not
pair, and use dbus-fast, so that rule does not apply here.

### 3.1 Confirm the two gates are real (E, ~10 min, dev-cerbo)

Pick an adapter we passive-scan and a MAC the tap hears often.

```bash
btmgmt -i hciN add-device -a 0 -t 2 <MAC>
```

then poll for 60 s:

```bash
dbus-send --system --print-reply --dest=org.bluez / org.freedesktop.DBus.ObjectManager.GetManagedObjects | grep -c "dev_<MAC_>"
```

Expected: `btmon -i hciN` shows the kernel forwarding `Device Found` for
that address; the object count stays 0. Remove with `btmgmt del-device`.
Also record what the add did to our scan (tap advertisement rate on that
card before/after, and any `enable_scan` recovery lines in the service
log).

### 3.2 Object lifecycle after one ConnectDevice (A, ~30 min, dev-cerbo)

Choose a GATT-connectable device the tap hears that has no object and no
`info` dir on any adapter. Standalone script (dbus-fast, system bus):

1. Resolve the hearing adapter MAC to its current `hciN` immediately
   before the call (never carry the number).
2. `ConnectDevice({Address, AddressType})` with the address type from the
   tap, wrapped in a 30 s `asyncio.wait_for`.
3. Record: reply path or error name; wall time to reply; whether
   `/data/var/lib/bluetooth/<adapter>/<MAC>/info` appeared and its
   contents.
4. `Device1.Disconnect()`, then poll the object for 120 s (well past the
   30 s `TemporaryTimeout`). Expected: it stays, because
   `device_connect_cb` cleared temporary.
5. Confirm `ble_gatt_dbus.lookup_device` now returns the path, and that
   `ble_gatt_link.resolve` therefore takes step 1 with no scan.
6. Restart bluetoothd on dev only, confirm `load_devices` brings it back.

### 3.3 The blind window on our passive scan (A, ~30 min, dev then prod)

Run `btmon -i hciN` to a file in the background during 3.2 and count the
LE Set Scan Enable / LE Set Scan Parameters / LE Add Device To Accept
List / LE Create Connection commands and who issued them. From the tap,
measure how long that card reported no advertisements. Compare with the
existing cost of a `Device1.Connect` to a bonded charger on the same card,
which is the same kernel path; if they match, A adds nothing new.

### 3.4 Abandoned ConnectDevice (A, ~20 min, dev-cerbo)

Power off or move the device out of range, call ConnectDevice, let the
30 s wrapper expire. Then: does bluetoothd ever reply (and after how
long)? Is the address still in the controller accept list (`btmon`, or
`hcitool` LE read accept list)? Does a second ConnectDevice for the same
MAC succeed once the device is back, or return `InProgress`? Does our
scan on that card recover on its own? This decides whether A needs a
"only if heard within the last N s" guard and a cleanup step.

### 3.5 dbus-daemon memory (A, 1 h, prod, read-only)

The whole reason the tap exists. Objects added by A are bounded: one per
GATT device per adapter that has heard it, with no per-advertisement
signals because BlueZ still is not scanning. Confirm by sampling
dbus-daemon RSS every 5 min for an hour after 3.2, against the flat
baseline in `hci_scan_control.py`.

### 3.6 Filtered discovery as the create step (G, ~30 min, dev-cerbo)

Same device as 3.2, before it has been connected on this card. Directly on
`org.bluez.Adapter1` for the hearing card: `SetDiscoveryFilter({Pattern:
<MAC>, Transport: "le"})`, `StartDiscovery`, poll `GetManagedObjects` for
the object, `StopDiscovery`. Record: time to object; that no other object
appeared on that card; the `btmon` scan-parameter sequence and the tap's
blind window, side by side with 3.3; and that the object disappears about
30 s after `StopDiscovery` when nothing connects. Then repeat and call
`Device1.Connect` on the path inside the window, confirm the object is
still there 120 s after disconnect, and that the `info` file now exists.

## 4. If the experiments pass: the code change

1. Move `_resolve_without_scanning` out of the EasyStart driver into
   `ble_gatt_link.resolve()` as step 2, between the BlueZ lookup and the
   discovery fallback, taking the hearing adapter MAC and address type as
   parameters.
2. Have the advertisement router keep, per device MAC, the adapter MAC
   and address type of the most recent advertisement and its timestamp;
   `resolve()` uses it only when fresh (N chosen from 3.4).
3. Keep the discovery fallback as step 3 for a device the tap has never
   heard (a device only another process's card can see).
4. Because step 2 persists the device on that card, `get_preferred_adapter`
   can be seeded from it, which closes the loop with the existing
   "PreferredAdapter stored" behaviour for the SmartSolar.

## 4b. What newer BlueZ adds (checked against master, 5.87)

Nothing creates a `Device1` from a bare address without a scan or a
connection, in any version. `ConnectDevice` is still experimental and
unchanged since 5.48; its documented errors include `AlreadyExists`, which
settles the flag in section 2A. Two things do matter:

- **`TemporaryTimeout = 0`** (`[General]`, main.conf/ble.conf): "temporary
  devices stay around forever". Already parsed by 5.72, so we have it.
  With it, the object G creates would persist without a connection, which
  is the true pre-seed. It is global: any discovery by any process
  (shyion-switch's active scans included) would leave its connectable
  finds behind for good, though non-connectable ones are still swept by
  `discovery_cleanup` 3 min after discovery stops. It is also an edit to
  the box's `/etc/bluetooth/ble.conf`, a Victron-owned file. Worth a
  dev-cerbo trial: set it, count objects after 24 h of normal traffic.
- **`AutoConnect` discovery filter** (absent in 5.82, present by 5.84):
  `SetDiscoveryFilter({Pattern: MAC, Transport: le, AutoConnect: true})`
  makes bluetoothd connect the matching device as soon as it is found
  (`btd_device_connect_services`), which is G's create plus A's connect
  in one call, with `StopDiscovery` as the cancel. Not on the box; arrives
  only when Venus OS ships BlueZ 5.84 or later.

## 5. Prior art checked

- **habluetooth** does not inject into BlueZ. Its passive mode registers an
  `AdvertisementMonitor` with `or_patterns` on the AD Flags byte
  (`0x02, 0x04, 0x05, 0x06, 0x18, 0x1A`), i.e. it asks BlueZ to create an
  object for everything, which is mechanism D and the growth path this
  project measured and left. Its `restore_discoveries` goes the other way:
  it re-seeds bleak's *scanner cache* from BlueZ's existing objects after a
  scanner restart.
- **bleak-retry-connector** `get_bluez_device` searches `/org/bluez/hci0..8`
  for an existing object for the address and prefers a connected one, then
  best RSSI. Note the hard-coded `range(0, 9)`: with `hci9` live on prod,
  a device known only on `hci9` would be invisible to that helper.
  `ble_gatt_dbus.lookup_device` walks the object manager instead and does
  not have that bug.
- **bleak 3.0.2** never calls ConnectDevice (it appears only in a
  docstring). With a path it goes straight to `Device1.Connect`; without
  one it runs `find_device_by_address`. There is no bleak-side registry to
  inject into separately from BlueZ.
- There is a `/data/habluetooth-switch.txt` on prod dated 2026-08-17
  01:39 whose meaning is not recorded in this repo; worth a line in the
  docs when someone remembers what was switched.

Sources: BlueZ 5.72 `src/adapter.c`, `src/device.c`, `src/main.c`; Linux
v6.12 `net/bluetooth/hci_event.c`; habluetooth `scanner_bleak.py`;
bleak-retry-connector `bluez.py` and `__init__.py`; bleak 3.0.2
`backends/bluezdbus/client.py` as vendored on the Cerbo.
