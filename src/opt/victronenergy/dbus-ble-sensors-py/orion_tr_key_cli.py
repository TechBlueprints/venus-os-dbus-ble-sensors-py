#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Standalone Victron advertisement-key fetcher (Orion-TR, Blue Smart IP22).

Short-lived helper the main service shells out to whenever it needs to
read VREG 0xEC65 from a paired charger.  Running as a subprocess isolates
provisioning from any long-running process state — bleak's BlueZManager,
the dbus-python proxy cache — that we have seen corrupt CCCD writes after
repeated connect/disconnect cycles within one service lifetime.

Usage::

    python3 orion_tr_key_cli.py MAC [--passkey N] [--timeout S]
                                    [--preferred-adapter hciX]

On success prints a JSON object to stdout and exits 0; on failure prints
diagnostics to stderr and exits non-zero.  Callers depend on that
contract the drivers used before provisioning moved in-process
(AsyncGATTWriter.provision_key); the CLI remains for provisioning and
diagnosis by hand.

Run by hand while the service is running, nothing referees a collision
on the same device except BlueZ itself — the service serialises its own
GATT work through its writer slot, but it cannot see this process.  If
a session fails with "Operation already in progress", that is you
racing the service; stop it or wait out its poll.

The connection runs through bcmv2 like every other link this project
opens, so a provisioning attempt is visible to — and placed around — the
other BLE services sharing these radios.  ``--preferred-adapter`` becomes
a bcmv2 pin: try the card that worked last time first, then walk.

Protocol notes, all of them hard-won on real hardware:

* **The notify path is fleet policy, not a local choice.**  The shared BLE
  stack forces StartNotify (``BCM_FORCE_START_NOTIFY`` via the
  ``/data/bcm`` shim) because AcquireNotify is the BlueZ 5.72
  use-after-free path.  This tool used to insist on AcquireNotify because
  StartNotify once delivered *empty* payloads for the 306b characteristics
  after SMP pairing; that is unverified under the current stack and the
  wrapper overrides the request regardless.
* **The CTRL read is load-bearing.**  Reading the control characteristic
  is what puts the device into CBOR mode; skip it and DATA_LAST never
  fires.
* **Prime before asking.**  A GetValue sent as the very first CBOR request
  is sometimes swallowed before the device has established its outgoing
  stream, so a plain subscribe to a chatty public register goes first.
* **Official key fetch is GetDevices, then instance-0 GetValues.**  The
  HEX client asks for the device list (opcode ``0x01``), subscribes
  instance ``0``, then GetValues ``0x05`` for ``[0xEC66, 0xEC65]`` in
  one request.  It never sends opcode ``0x25``.
* **0x25 is an Orion fallback only.**  Some charger firmwares ACK plain
  GetValue with RequestedEncryptionNotSupported; ``0x05 | 0x20`` still
  works there.  After PUK+PIN we retry the official batch, then ``0x25``,
  then a lone ``0x05``, then the ASCII HEX Get.
"""
from __future__ import annotations

import argparse
import asyncio
import binascii
import json
import logging
import struct
import sys
import time

import dbus
import dbus.mainloop.glib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

import ble_catcher  # noqa: E402
import ble_gatt_dbus  # noqa: E402
import ble_gatt_link  # noqa: E402
import victron_vreg as vreg  # noqa: E402

from hex_key_session import (  # noqa: E402
    _Collector,
    _credits,
    _fetch_vreg,
    _handshake,
    _prime,
    _start_notify,
    _stop_notify_all,
    provision_session,
)



def _err(*a) -> None:
    print(*a, file=sys.stderr, flush=True)



def _pre_disconnect(bus, mac: str) -> None:
    """Tear down any stale session on every adapter that knows this MAC.

    After a previous successful provisioning the device-side session
    lingers, and a re-PUK can be rejected until the link is torn down and
    rebuilt.  dbus-python, on the main thread, before any async work.
    """
    suffix = "/dev_" + mac.upper().replace(":", "_")
    try:
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        paths = sorted(str(p) for p in om.GetManagedObjects())
    except dbus.DBusException as exc:
        _err(f"object lookup failed: {exc}")
        return
    for path in paths:
        if not path.endswith(suffix):
            continue
        try:
            dbus.Interface(bus.get_object("org.bluez", path),
                           "org.bluez.Device1").Disconnect()
            _err(f"Pre-disconnected {path}")
        except dbus.DBusException:
            pass


async def provision(mac: str, passkey: int, timeout_s: float,
                    preferred_adapter: str | None = None) -> dict:
    bus = dbus.SystemBus()
    _pre_disconnect(bus, mac)

    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=preferred_adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    adapter = ble_gatt_dbus.adapter_from_path(
        (getattr(device, "details", None) or {}).get("path"))
    _err(f"Using {adapter or 'bcmv2-selected adapter'} for {mac}")

    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    agent = None
    if not (props or {}).get("Paired"):
        agent = ble_gatt_dbus.PairingAgent(bus, passkey, mac)
        agent.register()

    client = None
    try:
        client = await ble_gatt_link.connect(device, mac)
        payload = await provision_session(client, passkey, timeout_s,
                                          pair=agent is not None)
        payload["adapter"] = adapter
        return payload
    finally:
        if client is not None:
            try:
                await ble_gatt_link.disconnect(client)
            finally:
                # Synchronous, so it still runs if the await above is
                # cut short by cancellation — that is when the socket
                # is most likely to be stranded.
                ble_gatt_link.force_close(client)
        if agent is not None:
            agent.unregister()
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


async def telemetry(mac: str, passkey: int, timeout_s: float,
                    preferred_adapter: str | None = None) -> dict:
    """Short live-VREG session: voltage + state, then enable Instant Readout."""
    bus = dbus.SystemBus()
    _pre_disconnect(bus, mac)
    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=preferred_adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    adapter = ble_gatt_dbus.adapter_from_path(
        (getattr(device, "details", None) or {}).get("path"))
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = None
    try:
        client = await ble_gatt_link.connect(device, mac)
        collector = _Collector()
        acquired: list = []
        ok = False
        await _start_notify(client, vreg.CHAR_CONTROL, collector.on_ctrl, acquired)
        await _start_notify(client, vreg.CHAR_DATA_LAST, collector.on_last, acquired)
        await _start_notify(client, vreg.CHAR_DATA_BULK, collector.on_bulk, acquired)
        await asyncio.sleep(0.4)
        await _handshake(client)
        await _prime(client, collector)
        voltage = await _fetch_vreg(client, collector, 0xED8D, "voltage")
        current = await _fetch_vreg(client, collector, 0xED8F, "current")
        state = await _fetch_vreg(client, collector, 0x0201, "state")
        try:
            await client.write_gatt_char(
                vreg.CHAR_DATA_LAST,
                vreg.encode_write_command(
                    vreg.VREG_BLE_ADVERTISEMENT_MODE, b"\x01"),
                response=False)
            _err("Set 0xEC7D = 01")
            await asyncio.sleep(0.5)
            await _credits(client)
        except Exception as exc:
            _err(f"0xEC7D write failed (non-fatal): {exc}")
        ok = True
        return {
            "voltage": voltage,
            "current": current,
            "device_state": state,
            "adapter": adapter,
        }
    finally:
        if client is not None:
            # Before the link drops, not after — see _stop_notify_all.
            # DISABLED pending diagnosis.  Releasing notifies was added to
            # stop stranded acquires planting the BlueZ 5.72 UAF; prod's
            # crash rate then went from ~5-9/hr to 30-50/hr, with every
            # SIGSEGV landing within 0-1 s of one of our session teardowns.
            #
            # Two narrowing attempts both failed to move the rate: skipping
            # the release on a dead link (is_connected lies on a phantom
            # connection, which is exactly the failing case) and skipping it
            # on a failed session (rate got worse still).  So the mechanism
            # is not understood, and the release is off entirely until it
            # is — this restores the behaviour prod ran at its lower rate.
            #
            # Cost of being here: acquires are stranded again, which is the
            # leak this was meant to fix.  That is the lesser harm at
            # 50 crashes/hr.
            acquired.clear()
            try:
                await ble_gatt_link.disconnect(client)
            finally:
                # Synchronous, so it still runs if the await above is
                # cut short by cancellation — that is when the socket
                # is most likely to be stranded.
                ble_gatt_link.force_close(client)
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


def main() -> int:
    # The session helpers (hex_key_session) narrate through logging; the
    # drivers capture this process's stderr, and a person provisioning
    # by hand reads it live — so mirror the old _err behaviour exactly:
    # bare messages, stderr, no timestamps.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(message)s")

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("mac")
    ap.add_argument("--passkey", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--preferred-adapter", default=None,
                    help="Try this adapter first (e.g. hci1)")
    ap.add_argument("--telemetry", action="store_true",
                    help="Read live voltage/current/state and enable Instant Readout")
    args = ap.parse_args()

    mac = args.mac.upper()
    # A pin, not a restriction: bcmv2 walks a pinned device's preference
    # list failure-driven, so a dead preferred card costs one attempt.
    pins = [f"{mac}@{args.preferred_adapter}"] if args.preferred_adapter else []
    if not ble_catcher.install(owner="dbus-ble-sensors-py.keycli",
                               extra_adapters=pins):
        _err("BLE connection stack unavailable — cannot provision "
             "(run install.sh, or /data/bcm/install.sh directly)")
        return 1

    work = telemetry if args.telemetry else provision
    label = "telemetry" if args.telemetry else "key provisioning"
    try:
        result = asyncio.run(
            asyncio.wait_for(
                work(mac, args.passkey, args.timeout,
                     preferred_adapter=args.preferred_adapter),
                timeout=args.timeout + 30.0))
    except Exception as exc:
        _err(f"{label} failed: {exc}")
        return 1

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
