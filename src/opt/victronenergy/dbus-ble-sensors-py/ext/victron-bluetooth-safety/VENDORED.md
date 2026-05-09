# Vendored: victron-bluetooth-safety

## Source

- Repository: https://github.com/TechBlueprints/victron-bluetooth-safety
- Commit: `97c6fae2ebd7b90c92e7ea778c98cbbdbb1cb9b8`
- License: Apache 2.0 (see `LICENSE`)

## What this is

A standalone fix for the `vesmart-server` mass-disconnect bug on Venus OS
documented in
[victronenergy/venus#1587](https://github.com/victronenergy/venus/issues/1587).

When *any* BLE device connects to the Cerbo, `vesmart-server` starts a
hardcoded 60-second timer.  When that timer fires, it iterates **every
connected BLE device on every adapter** and disconnects them — batteries,
tank sensors, relay switches, everything.  This makes stable BLE
connections impossible for any third-party service while
`vesmart-server` is running.

The vendored code patches `vesmart-server`'s Python source to:

- Replace `_keep_alive_timer_timeout`'s body with a no-op that only logs.
- Disable the 60-second `GObject.timeout_add` that arms the timer in
  the first place.
- Preserve VictronConnect's dynamic per-client keep-alive (separate
  code path).

`dbus-ble-sensors-py` itself maintains long-lived BLE *advertisement*
subscriptions through `bluetoothd` and is **directly affected** by the
mass-disconnect: every 60s, all BLE adapters get a flood of
disconnect events that disrupt scanning and re-trigger BlueZ state
machines.  Vendoring this fix is a hard requirement for the fork to
work reliably alongside other BLE services on the Cerbo.

## How it gets applied

`install.sh` (Step 5.5) copies this directory to
`/data/victron-bluetooth-safety/` and then sources the inline snippet:

    . /data/victron-bluetooth-safety/vesmart-safety.sh
    ensure_vesmart_safe

This is the **version-agnostic** path: it patches `gattserver.py` by
method name using a Python regex, so it works across Venus OS releases.
It is idempotent (a fast `grep` no-op on an already-patched system).

`service/run` also sources the same snippet on every (re)start of
`dbus-ble-sensors-py`, so a Venus OS firmware update that reverts the
patch is fixed up automatically the next time the service runs.

### Optional: full installer with per-client tracking

The full installer `victron-bluetooth-safety.sh` is also vendored.
It applies the unified diffs in `patches/` to give per-GATT-client
tracking (only disconnects clients that actually used the
VictronConnect GATT service, instead of disabling disconnects
entirely).  However, the patches are **version-pinned** and may
fail to apply on Venus OS releases other than the one they were
generated against.  Power users can run:

    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install

If the patch hunks fail to apply, regenerate them upstream against
the new Venus OS source and update both the patches in the upstream
repo and this vendored copy.  See "Updating" below.

### Status / uninstall

    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh status
    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh uninstall

## Why we vendor instead of fetching

- Air-gapped / poor-connectivity Cerbo installs (RVs, boats, remote
  monitoring sites) can't necessarily reach GitHub at install time.
- `install.sh` already fetches the dbus-ble-sensors-py source via git;
  we don't want to add a second remote dependency to a security-relevant
  patch.
- The patch files (`patches/*.patch`) are version-pinned to specific
  upstream Venus OS releases; pinning them in tree means we can verify
  exactly which patch shipped with which release of this fork.

## Updating

To pick up a new upstream commit:

    cd /tmp
    git clone --depth=1 https://github.com/TechBlueprints/victron-bluetooth-safety.git
    cd victron-bluetooth-safety
    git rev-parse HEAD                    # note the new SHA

Then in this fork:

    DEST=src/opt/victronenergy/dbus-ble-sensors-py/ext/victron-bluetooth-safety
    cp /tmp/victron-bluetooth-safety/victron-bluetooth-safety.sh "$DEST/"
    cp /tmp/victron-bluetooth-safety/vesmart-safety.sh "$DEST/"
    cp /tmp/victron-bluetooth-safety/LICENSE "$DEST/"
    cp /tmp/victron-bluetooth-safety/README.md "$DEST/"
    cp /tmp/victron-bluetooth-safety/patches/*.patch "$DEST/patches/"
    # Update the SHA in this file.

## Local modifications

None.  Files are byte-identical to upstream commit
`f0c95eacc060a8f0e2087275932b7afc942e06d5`.
