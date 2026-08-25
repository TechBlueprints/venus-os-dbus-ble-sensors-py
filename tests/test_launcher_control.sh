#!/bin/sh
# Exercise the launcher's control() against a fake supervise tree.
#
# The bug this covers is not hypothetical: every restart on dev-cerbo
# logged "svc: warning: unable to control ...: supervise not running",
# a stop that silently did nothing.  Cosmetic while it only happened
# during teardown; a real outage the first time it happens to a start.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# A fake svc that records its arguments instead of talking to runit.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/svc" <<'EOF'
#!/bin/sh
echo "$@" >> "$SVC_CALLS"
EOF
chmod +x "$TMP/bin/svc"
PATH="$TMP/bin:$PATH"
export SVC_CALLS="$TMP/calls"
: > "$SVC_CALLS"

# Pull control() out of the run script rather than duplicating it, so
# this test cannot drift away from what actually ships.
sed -n '/^control() {/,/^}/p' "$HERE/../service-launcher/run" > "$TMP/control.sh"

export SVC_LINK="$TMP/service"
mkdir -p "$SVC_LINK/supervise"

fail() { echo "FAIL: $1"; exit 1; }

# 1. supervise up -> svc is actually invoked
mkfifo "$SVC_LINK/supervise/control"
( . "$TMP/control.sh"; control -u 0 ) || fail "should succeed when supervise is up"
grep -q -- "-u $SVC_LINK" "$SVC_CALLS" || fail "svc was not invoked"

# 2. supervise gone -> reported, and svc is NOT invoked
rm "$SVC_LINK/supervise/control"
: > "$SVC_CALLS"
if ( . "$TMP/control.sh"; control -d 0 ); then
    fail "should report failure when supervise is gone"
fi
[ -s "$SVC_CALLS" ] && fail "svc must not be invoked with no supervise"

# 3. supervise appears during the wait -> the request still lands
( sleep 2; mkfifo "$SVC_LINK/supervise/control" ) &
( . "$TMP/control.sh"; control -u 5 ) || fail "should wait for supervise"
grep -q -- "-u $SVC_LINK" "$SVC_CALLS" || fail "svc not invoked after wait"
wait

echo "launcher control(): all checks passed"
