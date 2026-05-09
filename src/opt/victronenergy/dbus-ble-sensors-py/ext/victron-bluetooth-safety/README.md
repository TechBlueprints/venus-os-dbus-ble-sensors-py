# victron-bluetooth-safety

Prevent Victron's `vesmart-server` on Venus OS from disconnecting
**all** BLE devices on **all** adapters every 60 seconds.

Upstream issue:
[victronenergy/venus#1587](https://github.com/victronenergy/venus/issues/1587)

## The problem

When any BLE device connects to the Cerbo (even on a different adapter),
`vesmart-server` starts a hardcoded 60-second keep-alive timer. When
that timer fires (because the connected device isn't a VictronConnect
client and never sends a keep-alive), it disconnects **every** connected
BLE device it can find — batteries, sensors, everything.

This makes it impossible to maintain stable BLE connections for
third-party services (battery monitors, temperature sensors, relay
switches, etc.) while `vesmart-server` is running.

## Two approaches

This repo provides two approaches. Choose whichever fits your setup.

### 1. Inline snippet (recommended)

**[`vesmart-safety.sh`](vesmart-safety.sh)** is a small, self-contained
shell function that any BLE service can source or copy into its startup
script. It uses a Python patcher to find and neutralize the disconnect
behavior **by method name**, making it version-agnostic across Venus OS
releases.

**How to use it:**

Source the file and call the function from your service's `run` or start
script:

```sh
. /data/vesmart-safety/vesmart-safety.sh
ensure_vesmart_safe
```

Or copy the `ensure_vesmart_safe` function body directly into your
script. The Apache 2.0 license header in the file permits this.

**How it works:**

- Detects whether `gattserver.py` needs patching (idempotent)
- Uses a Python regex patcher to replace the `_keep_alive_timer_timeout`
  method body with a no-op, regardless of what that body contains
- Disables the hardcoded 60-second timer in `connected()`
- Preserves VictronConnect's dynamic keepalive (separate code path)
- Uses a lock directory to prevent races when multiple services start
  simultaneously
- After a firmware update reverts the change, the first service to
  start re-applies it automatically — no `rc.local` hook needed

**Deploy to Cerbo:**

```bash
ssh root@cerbo 'mkdir -p /data/vesmart-safety'
scp vesmart-safety.sh root@cerbo:/data/vesmart-safety/
```

Then add the two-line source + call to any service startup script.

### 2. Full patch (standalone installer)

**[`victron-bluetooth-safety.sh`](victron-bluetooth-safety.sh)** is a
standalone installer that applies unified diff patches to
`gattserver.py` and `vesmart_server.py`. It provides more surgical
behavior: tracking which devices are actual VictronConnect GATT clients
and only disconnecting those.

**Note:** The patch files in `patches/` are version-specific and may
need regeneration when Victron updates `vesmart-server`. The inline
snippet above is preferred for most use cases because it is
version-agnostic.

```bash
scp -r patches victron-bluetooth-safety.sh root@cerbo:/data/victron-bluetooth-safety/
ssh root@cerbo 'sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh install'
```

## Compatibility

| Venus OS | Inline snippet | Full patch |
|----------|---------------|------------|
| v3.67    | Yes           | Needs regeneration |
| v3.72    | Yes           | Yes (with updated patch) |
| Future   | Expected yes  | May need regeneration |

## Development

A non-production Cerbo GX is available for testing at `root@dev-cerbo`.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
