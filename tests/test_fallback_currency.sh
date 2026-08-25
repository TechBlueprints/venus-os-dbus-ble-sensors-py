#!/bin/sh
# Exercise install.sh's check_fallback_currency against real git repos.
#
# Worth testing rather than eyeballing: the check cannot be provoked on
# a box, because step 3b resets the submodule to its pinned sha before
# step 3c compares.  That is correct in production — the comparison is
# "does the repo's pin lag the shared checkout", not "did someone poke
# the working tree" — but it means the only way to see the warning fire
# is to build the situation directly.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Two commits in one repo; clone it twice so shared and vendored can sit
# at different points of the same history.
SRC="$TMP/src"
mkdir -p "$SRC"
git -C "$SRC" init -q
git -C "$SRC" config user.email t@t; git -C "$SRC" config user.name t
echo one > "$SRC/f"; git -C "$SRC" add f; git -C "$SRC" commit -qm one
OLD=$(git -C "$SRC" rev-parse HEAD)
echo two > "$SRC/f"; git -C "$SRC" commit -qam two
NEW=$(git -C "$SRC" rev-parse HEAD)

export BCM_DIR="$TMP/bcm"
export INSTALL_DIR="$TMP/install"
export APP_DIR="app"
VENDORED="$INSTALL_DIR/$APP_DIR/ext/bleak-connection-manager"
git clone -q "$SRC" "$BCM_DIR"
mkdir -p "$(dirname "$VENDORED")"
git clone -q "$SRC" "$VENDORED"

sed -n '/^check_fallback_currency() {/,/^}/p' "$HERE/../install.sh" > "$TMP/fn.sh"

run() { ( . "$TMP/fn.sh"; check_fallback_currency ); }
fail() { echo "FAIL: $1"; exit 1; }

# 1. In step -> silent.
git -C "$BCM_DIR" checkout -q "$NEW"; git -C "$VENDORED" checkout -q "$NEW"
[ -z "$(run)" ] || fail "must say nothing when the shas match"

# 2. Vendored behind -> names the distance and the consequence.
git -C "$VENDORED" checkout -q "$OLD"
out=$(run)
echo "$out" | grep -q "1 commit(s) behind" || fail "must say how far behind: $out"
echo "$out" | grep -q "runs the older stack" || fail "must say what it costs: $out"

# 3. Vendored AHEAD (a deliberate pin) -> not this check's business.
git -C "$BCM_DIR" checkout -q "$OLD"; git -C "$VENDORED" checkout -q "$NEW"
[ -z "$(run)" ] || fail "ahead is a pin, not a downgrade"

# 4. No vendored checkout at all -> silent, not an error.
rm -rf "$VENDORED"
git -C "$BCM_DIR" checkout -q "$NEW"
[ -z "$(run)" ] || fail "must tolerate a missing vendored tree"

echo "check_fallback_currency: all checks passed"
