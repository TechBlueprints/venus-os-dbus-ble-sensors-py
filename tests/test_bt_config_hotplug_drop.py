"""install.sh removes us from bt-config's hotplug restart list; disable.sh restores stock.

bt-config restarts every service in its ``services=`` line on ANY adapter
add.  We handle the adapter lifecycle from BlueZ signals, so the installer
DROPS our token (and the stock one a firmware update restores) rather than
renaming stock's into ours.  These pin the exact sed expressions against
the three inputs they must handle, and the uninstaller's re-add.
"""
import os
import re
import subprocess

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _sed_expr(script, marker):
    """The -E sed expression in the block that ends with *marker* (the sed
    precedes its echo, so search the window BEFORE the marker)."""
    src = open(os.path.join(REPO, script)).read()
    idx = src.index(marker)
    hits = re.findall(r"sed -i -E '([^']+)' \"\$BT_CONFIG\"", src[max(0, idx - 800):idx])
    assert hits, f"{script}: no -E sed in the block before {marker!r}"
    return hits[-1]


def _apply(expr, line):
    return subprocess.run(["sed", "-E", expr], input=line, capture_output=True,
                          text=True, check=True).stdout


def test_installer_drops_our_token_from_every_form_idempotently() -> None:
    expr = _sed_expr("install.sh", "removed our service from the hotplug")
    patched = 'services="/service/dbus-ble-sensors-py /service/vesmart-server"\n'
    stock = 'services="/service/dbus-ble-sensors /service/vesmart-server"\n'
    dropped = 'services="/service/vesmart-server"\n'
    assert _apply(expr, patched) == dropped, "patched-to-py form"
    assert _apply(expr, stock) == dropped, "stock form (post-firmware-update)"
    assert _apply(expr, dropped) == dropped, "already dropped: idempotent"


def test_installer_no_longer_renames_stock_into_us() -> None:
    src = open(os.path.join(REPO, "install.sh")).read()
    assert "s|/service/dbus-ble-sensors |/service/dbus-ble-sensors-py |g" not in src, (
        "the rename-sed keeps a restart the code does not need")


def test_uninstaller_re_adds_the_stock_token_when_absent() -> None:
    expr = _sed_expr("disable.sh", "stock service re-added")
    dropped = 'services="/service/vesmart-server"\n'
    out = _apply(expr, dropped)
    assert "/service/dbus-ble-sensors" in out and "/service/vesmart-server" in out
    assert "dbus-ble-sensors-py" not in out, "re-add is the STOCK token"


def test_per_card_radio_setup_is_left_alone() -> None:
    """The installer only touches the restart line, never btmgmt setup."""
    src = open(os.path.join(REPO, "install.sh")).read()
    block = src[src.index("removed our service from the hotplug") - 1200:
                src.index("removed our service from the hotplug")]
    assert "btmgmt" not in block and "public-addr" not in block.replace("(public-addr, le on, bredr off)", "")
