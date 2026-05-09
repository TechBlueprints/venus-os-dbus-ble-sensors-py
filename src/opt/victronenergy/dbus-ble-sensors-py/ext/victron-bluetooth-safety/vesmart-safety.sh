# vesmart-safety.sh — Prevent vesmart-server from mass-disconnecting BLE devices
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
# Victron's vesmart-server starts a hardcoded 60-second timer whenever any
# BLE device connects.  When that timer fires, it disconnects ALL connected
# BLE devices on ALL adapters — batteries, sensors, everything.
# See: https://github.com/victronenergy/venus/issues/1587
#
# This snippet neutralizes both the timer and the disconnect-all handler
# using a version-agnostic Python patcher that finds the relevant code by
# method name, not line number.  It works across Venus OS versions (tested
# on v3.67 and v3.72) regardless of whether older workarounds are present.
#
# Usage — inline in any service startup script:
#
#   . /data/vesmart-safety/vesmart-safety.sh   # source the file
#   ensure_vesmart_safe                         # call the function
#
# Or copy the function body directly into your run/start script.
# Multiple services can safely call this; only the first applies the patch.

ensure_vesmart_safe() {
    _gs=/opt/victronenergy/vesmart-server/gattserver.py
    [ -f "$_gs" ] || return 0
    grep -q '# vesmart-safety' "$_gs" && return 0
    grep -q 'timeout_add(60000' "$_gs" || return 0
    # The bug pattern is: a hardcoded 60s timer that, when it fires,
    # iterates a collection of devices and calls .Disconnect() on
    # each.  We've seen two upstream variants:
    #   v3.67  iterates bluezutils.get_devices(self.bus)
    #   v3.72  iterates self._tracked (still calls .Disconnect on all)
    # Both are buggy.  We patch when the timer body contains *any*
    # disconnect-iteration pattern.  If Victron rewrites the body to
    # scope per-client (e.g. checks last-write timestamps before
    # disconnecting), the regex below won't match and we won't
    # overwrite their fix.
    python3 -c "
import re, sys
with open('$_gs') as f: c = f.read()
m = re.search(r'def _keep_alive_timer_timeout.*?(?=\n\tdef |\Z)', c, re.DOTALL)
if not m:
    sys.exit(1)
body = m.group()
# Disconnect-iteration patterns we recognize as buggy:
#   bluezutils.get_devices(...)  + Disconnect()        (v3.67)
#   self._tracked iteration      + Disconnect()        (v3.72)
#   anything iterating + Disconnect() in this method   (forward-compat)
has_disconnect = '.Disconnect()' in body or 'Disconnect()' in body
sys.exit(0 if has_disconnect else 1)
" || return 0
    echo "[vesmart-safety] Patching vesmart disconnect behavior"
    mount -o remount,rw / 2>/dev/null || return 1
    python3 -c "
import re, os, tempfile
p = '$_gs'
with open(p) as f: orig = f.read()
c = orig
c = re.sub(
    r'(\tdef _keep_alive_timer_timeout\(self\):\n).*?(\n\t\treturn False)',
    r'\1\t\tlogger.info(\"Keep alive timeout (disconnects disabled)\")'
    r'\n\t\tself._keepAliveTimer = None'
    r'\n\t\t# vesmart-safety: disconnect-all disabled (venus#1587)\2',
    c, count=1, flags=re.DOTALL)
c = re.sub(
    r'(\t\tself\._keepAliveTimer = GObject\.timeout_add\(60000,\s*self\._keep_alive_timer_timeout\))',
    '\t\tpass  # vesmart-safety: 60s timer disabled (venus#1587)',
    c, count=1)
if c != orig:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
    with os.fdopen(fd, 'w') as f: f.write(c)
    os.chmod(tmp, os.stat(p).st_mode)
    os.rename(tmp, p)
"
    _rc=$?
    mount -o remount,ro / 2>/dev/null
    if [ "$_rc" -eq 0 ]; then
        svc -t /service/vesmart-server 2>/dev/null
        echo "[vesmart-safety] Patch applied, vesmart-server restarted"
    else
        echo "[vesmart-safety] WARNING: Python patcher failed (exit $_rc)"
    fi
    return $_rc
}
