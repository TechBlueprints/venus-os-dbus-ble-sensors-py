#!/bin/sh
# victron-bluetooth-safety.sh — Patch vesmart-server to only disconnect its own GATT clients
# Version: 1.0.0
#
# Copyright 2026 TechBlueprints
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Patches vesmart-server on Venus OS so that its keep-alive timer only
# disconnects devices that have actually interacted with the VE.Smart
# GATT service (i.e., VictronConnect clients), instead of disconnecting
# ALL BLE devices on ALL adapters.
#
# This allows vesmart-server to continue running (preserving VictronConnect
# BLE access) while preventing it from interfering with third-party BLE
# connections (battery monitors, sensors, etc.).
#
# See: https://github.com/victronenergy/venus/issues/1587
#
# Usage:
#   sh victron-bluetooth-safety.sh install    # apply patch + set up rc.local
#   sh victron-bluetooth-safety.sh uninstall  # revert patch + remove rc.local hook
#   sh victron-bluetooth-safety.sh status     # check if patch is applied
#
# The patch is stored on /data (survives firmware updates) and is
# reapplied automatically on boot via /data/rc.local.

VERSION="1.0.0"
INSTALL_DIR="/data/victron-bluetooth-safety"
VESMART_DIR="/opt/victronenergy/vesmart-server"
RC_LOCAL="/data/rc.local"
RC_MARKER="# victron-bluetooth-safety"

_log() { echo "[bt-safety] $*"; }

is_venus_os() {
    [ -f /opt/victronenergy/version ]
}

_require_venus_os() {
    if ! is_venus_os; then
        _log "ERROR: This does not appear to be a Venus OS system."
        return 1
    fi
}

_is_root_ro() {
    grep -q ' / .*\bro\b' /proc/mounts
}

_DVB_DID_REMOUNT=0

_remount_root_rw() {
    if _is_root_ro; then
        _log "Remounting root filesystem read-write"
        if mount -o remount,rw / 2>/dev/null; then
            _DVB_DID_REMOUNT=1
        else
            _log "WARNING: Failed to remount root read-write."
            return 1
        fi
    fi
}

_restore_root_ro() {
    if [ "$_DVB_DID_REMOUNT" = 1 ]; then
        _log "Restoring root filesystem to read-only"
        mount -o remount,ro / 2>/dev/null || \
            _log "WARNING: Failed to restore root to read-only."
        _DVB_DID_REMOUNT=0
    fi
}

_cleanup() {
    _restore_root_ro
}

_is_patched() {
    grep -q '_gatt_clients' "$VESMART_DIR/gattserver.py" 2>/dev/null
}

_install_patches_to_data() {
    mkdir -p "$INSTALL_DIR"

    if [ ! -f "$INSTALL_DIR/gattserver.py.patch" ]; then
        _log "ERROR: Patch files not found in $INSTALL_DIR"
        _log "       Copy the patches/ directory contents to $INSTALL_DIR first."
        return 1
    fi
}

_apply_patches() {
    if _is_patched; then
        _log "Patch already applied to gattserver.py"
        return 0
    fi

	_log "Applying gattserver.py patch"
	if ! (cd / && patch -p1 -N < "$INSTALL_DIR/gattserver.py.patch") 2>/dev/null; then
		_log "ERROR: Failed to apply gattserver.py patch"
		return 1
	fi

	_log "Applying vesmart_server.py patch"
	if ! (cd / && patch -p1 -N < "$INSTALL_DIR/vesmart_server.py.patch") 2>/dev/null; then
		_log "ERROR: Failed to apply vesmart_server.py patch"
		return 1
	fi

    _log "Patches applied successfully"
}

_revert_patches() {
    if ! _is_patched; then
        _log "Patch not currently applied"
        return 0
    fi

	_log "Reverting gattserver.py patch"
	(cd / && patch -p1 -R < "$INSTALL_DIR/gattserver.py.patch") 2>/dev/null || \
		_log "WARNING: Failed to revert gattserver.py patch"

	_log "Reverting vesmart_server.py patch"
	(cd / && patch -p1 -R < "$INSTALL_DIR/vesmart_server.py.patch") 2>/dev/null || \
		_log "WARNING: Failed to revert vesmart_server.py patch"

    _log "Patches reverted"
}

_add_rc_local() {
    if [ -f "$RC_LOCAL" ] && grep -q "$RC_MARKER" "$RC_LOCAL" 2>/dev/null; then
        _log "rc.local hook already present"
        return 0
    fi

    _log "Adding boot hook to $RC_LOCAL"
    if [ ! -f "$RC_LOCAL" ]; then
        echo "#!/bin/sh" > "$RC_LOCAL"
        chmod +x "$RC_LOCAL"
    fi

    cat >> "$RC_LOCAL" << 'RCEOF'
# victron-bluetooth-safety
if [ -f /data/victron-bluetooth-safety/gattserver.py.patch ]; then
    mount -o remount,rw / 2>/dev/null && \
        (cd / && patch -p1 -N < /data/victron-bluetooth-safety/gattserver.py.patch) >/dev/null 2>&1
    (cd / && patch -p1 -N < /data/victron-bluetooth-safety/vesmart_server.py.patch) >/dev/null 2>&1
    mount -o remount,ro / 2>/dev/null
    svc -t /service/vesmart-server 2>/dev/null
fi
# end victron-bluetooth-safety
RCEOF
    _log "Boot hook added"
}

_remove_rc_local() {
    if [ ! -f "$RC_LOCAL" ] || ! grep -q "$RC_MARKER" "$RC_LOCAL" 2>/dev/null; then
        _log "No rc.local hook to remove"
        return 0
    fi

    _log "Removing boot hook from $RC_LOCAL"
    sed -i '/# victron-bluetooth-safety/,/# end victron-bluetooth-safety/d' "$RC_LOCAL"
    _log "Boot hook removed"
}

_restart_vesmart() {
    if [ -d "/service/vesmart-server" ]; then
        _log "Restarting vesmart-server to pick up changes"
        svc -t /service/vesmart-server 2>/dev/null
        sleep 2
        svstat /service/vesmart-server 2>/dev/null
    fi
}

do_install() {
    _require_venus_os || return 1
    _install_patches_to_data || return 1

    _remount_root_rw || return 1
    trap _cleanup EXIT

    _apply_patches || { _restore_root_ro; trap - EXIT; return 1; }

    _restore_root_ro
    trap - EXIT

    _add_rc_local
    _restart_vesmart
    _log "Install complete. Patch will be reapplied after firmware updates."
}

do_uninstall() {
    _require_venus_os || return 1

    _remount_root_rw || return 1
    trap _cleanup EXIT

    _revert_patches

    _restore_root_ro
    trap - EXIT

    _remove_rc_local
    _restart_vesmart
    _log "Uninstall complete. Original vesmart-server behavior restored."
}

do_status() {
    if _is_patched; then
        _log "ACTIVE: vesmart-server is patched (GATT-client-only disconnects)"
    else
        _log "INACTIVE: vesmart-server is unpatched (disconnects all devices)"
    fi

    if [ -f "$RC_LOCAL" ] && grep -q "$RC_MARKER" "$RC_LOCAL" 2>/dev/null; then
        _log "Boot hook: installed in $RC_LOCAL"
    else
        _log "Boot hook: not installed"
    fi

    if [ -f "$INSTALL_DIR/gattserver.py.patch" ]; then
        _log "Patch files: present in $INSTALL_DIR"
    else
        _log "Patch files: NOT FOUND in $INSTALL_DIR"
    fi
}

_main() {
    case "${1:-}" in
        install)
            do_install
            ;;
        uninstall|remove)
            do_uninstall
            ;;
        status)
            do_status
            ;;
        --version|-v|-V)
            echo "victron-bluetooth-safety $VERSION"
            ;;
        --help|-h|"")
            echo "victron-bluetooth-safety $VERSION"
            echo "Usage: victron-bluetooth-safety.sh <install|uninstall|status>"
            echo ""
            echo "  install    Apply patch and set up boot hook"
            echo "  uninstall  Revert patch and remove boot hook"
            echo "  status     Check if patch is currently applied"
            ;;
        *)
            _log "Unknown command: $1"
            _log "Usage: victron-bluetooth-safety.sh <install|uninstall|status>"
            return 1
            ;;
    esac
}

if [ -n "${BASH_VERSION:-}" ]; then
    if [ -z "${BASH_SOURCE:-}" ] || [ "${BASH_SOURCE}" = "$0" ]; then
        _main "$@"
    fi
else
    case "$(basename "$0")" in
        victron-bluetooth-safety*|sh|dash|ash) _main "$@" ;;
    esac
fi
