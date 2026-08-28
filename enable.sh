#!/bin/bash
#
# Re-enable dbus-ble-sensors-py (curl install)
# Repairs permissions, recreates service symlinks, updates rc.local.
# Useful for recovery after a firmware update or if services go missing.
#

set -e

INSTALL_DIR="/data/apps/dbus-ble-sensors-py"
SERVICE_NAME="dbus-ble-sensors-py"
LAUNCHER_NAME="dbus-ble-sensors-py-launcher"
APP_DIR="$INSTALL_DIR/src/opt/victronenergy/dbus-ble-sensors-py"

echo ""
echo "Re-enabling $SERVICE_NAME..."

if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: $INSTALL_DIR not found. Run install.sh first."
    exit 1
fi

# --- Fix permissions ---

chmod +x "$INSTALL_DIR"/service/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
echo "  Permissions fixed"

# --- Disable stock service ---

STOCK_START="/opt/victronenergy/dbus-ble-sensors/start-ble-sensors.sh"
BT_CONFIG="/lib/udev/bt-config"
BT_REMOVE="/lib/udev/bt-remove"

if [ -f "$STOCK_START" ] && grep -q "^exec " "$STOCK_START"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i 's|^exec |#exec |g' "$STOCK_START"
    sed -i '\|--banner|a\
svc -d .' "$STOCK_START"
    echo "  Stock start script disabled"
fi

if [ -f "$BT_CONFIG" ] && grep -q "/service/dbus-ble-sensors " "$BT_CONFIG"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i 's|/service/dbus-ble-sensors |/service/dbus-ble-sensors-py |g' "$BT_CONFIG"
    echo "  bt-config patched"
fi

if [ -f "$BT_REMOVE" ] && ! grep -q "dbus-ble-sensors-py" "$BT_REMOVE"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i '\|/service/dbus-ble-sensors$|a\
    svc -d /service/dbus-ble-sensors-py' "$BT_REMOVE"
    echo "  bt-remove patched"
fi

if [ -n "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
    svc -d /service/dbus-ble-sensors 2>/dev/null || true
fi

# --- Leave a healthy tree alone --------------------------------------
#
# rc.local runs this script at EVERY boot as a recovery net, but by the
# time it runs svscan has already built supervise trees from these very
# symlinks.  Tearing them down and re-linking orphans those supervisors:
# they keep running with a "(deleted)" cwd, and an orphaned multilog goes
# on holding the log directory's lock, so the live generation's logger
# can never acquire it.  The service then writes into a pipe nobody
# drains -- on dev that hid 14 minutes of startup output, and it would
# have blocked the process outright once the 64 kB pipe buffer filled.
#
# So if the links already point where we would point them and supervise
# is live, this is a boot, not a repair.  Do nothing.
links_ok=1
[ "$(readlink "/service/$SERVICE_NAME" 2>/dev/null)" = "$INSTALL_DIR/service" ] \
    || links_ok=0
[ "$(readlink "/service/$LAUNCHER_NAME" 2>/dev/null)" = "$INSTALL_DIR/service-launcher" ] \
    || links_ok=0
for ctl in "/service/$SERVICE_NAME/supervise/control" \
           "/service/$SERVICE_NAME/log/supervise/control" \
           "/service/$LAUNCHER_NAME/supervise/control" \
           "/service/$LAUNCHER_NAME/log/supervise/control"; do
    [ -p "$ctl" ] || links_ok=0
done

if [ "$links_ok" = "1" ]; then
    echo "  Service symlinks already correct and supervised — leaving them"
else

# --- Stop any stale service entries before recreating ----------------

# svc -d only asks supervise to bring the service DOWN; supervise itself
# keeps running and keeps its cwd open.  -x additionally tells it to exit
# once the service is down, which is what actually releases the log
# directory lock.  The log service needs this in its own right: its
# process is "supervise log", so the old pkill patterns below never
# matched it, and it was the one holding the lock.
for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    if [ -e "/service/$svc_name" ]; then
        svc -dx "/service/$svc_name/log" 2>/dev/null || true
        svc -dx "/service/$svc_name" 2>/dev/null || true
    fi
done

# Give supervise up to 10s to exit on its own before forcing it.
for _ in $(seq 1 10); do
    still=0
    for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
        [ -p "/service/$svc_name/supervise/control" ] && still=1
        [ -p "/service/$svc_name/log/supervise/control" ] && still=1
    done
    [ "$still" = "0" ] && break
    sleep 1
done

# Remove stale symlinks or directories
for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    rm -rf "/service/$svc_name" 2>/dev/null || true
done

# Reap any supervise that outlived its directory.  Matched by cwd, not by
# command line: a log supervisor is just "supervise log", so a pattern
# like "supervise log" would kill every other service's logger on the box
# — adc, digitalinputs, acsystem.  Our service names are unique, so the
# cwd is a safe discriminator and a "(deleted)" suffix is the signature.
for pid_dir in /proc/[0-9]*; do
    pid="${pid_dir#/proc/}"
    read -r comm 2>/dev/null < "$pid_dir/comm" || continue
    [ "$comm" = "supervise" ] || continue
    cwd="$(readlink "$pid_dir/cwd" 2>/dev/null)" || continue
    case "$cwd" in
        *"$SERVICE_NAME"*|*"$LAUNCHER_NAME"*)
            kill "$pid" 2>/dev/null || true
            ;;
    esac
done

pkill -f "python.*dbus_ble_sensors" 2>/dev/null || true

# --- Create service symlinks ---

ln -s "$INSTALL_DIR/service" "/service/$SERVICE_NAME"
ln -s "$INSTALL_DIR/service-launcher" "/service/$LAUNCHER_NAME"
echo "  Service symlinks created"

fi

# --- Ensure rc.local persistence ---

RC_LOCAL="/data/rc.local"
if [ ! -f "$RC_LOCAL" ]; then
    echo "#!/bin/bash" > "$RC_LOCAL"
    chmod 755 "$RC_LOCAL"
fi

RC_ENTRY="bash $INSTALL_DIR/enable.sh > $INSTALL_DIR/startup.log 2>&1 &"
if ! grep -qF "dbus-ble-sensors-py" "$RC_LOCAL"; then
    echo "$RC_ENTRY" >> "$RC_LOCAL"
    echo "  Added to rc.local"
fi

# --- Start services ---

svc -u "/service/$LAUNCHER_NAME" 2>/dev/null || true
sleep 2
echo "  Services started"

echo ""
echo "$SERVICE_NAME enabled."
echo ""
svstat "/service/$SERVICE_NAME" 2>/dev/null || true
svstat "/service/$LAUNCHER_NAME" 2>/dev/null || true
echo ""
