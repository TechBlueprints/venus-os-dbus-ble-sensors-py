# Vendored: victron-bluetooth-safety

## Source

- Repository: https://github.com/TechBlueprints/victron-bluetooth-safety
- Commit: `f0c95eacc060a8f0e2087275932b7afc942e06d5`
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

`install.sh` (Step 6.5) copies this directory to
`/data/victron-bluetooth-safety/` and runs:

    sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install

That call:

1. Remounts root rw.
2. Applies `patches/gattserver.py.patch` and
   `patches/vesmart_server.py.patch` to `/opt/victronenergy/vesmart-server/`.
3. Adds an idempotent boot hook to `/data/rc.local` so the patch is
   re-applied automatically after a Venus OS firmware update reverts
   the changes.
4. Restarts `/service/vesmart-server`.

The installer is idempotent: re-running it on an already-patched system
is a no-op.

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
