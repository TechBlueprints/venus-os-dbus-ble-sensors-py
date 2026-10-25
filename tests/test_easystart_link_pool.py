"""The EasyStart link goes on a GATT-pool card, not on the scanning card
that heard the advertisement.

Since the 2026-09-15 radio layout the scanning cards (hci1, hci2) carry
no links: a GATT connection on the listening card makes the kernel
disable that card's scan until the next rotation tick.  The driver used
to ConnectDevice on the card that heard the unit; on prod after the
2026-10-25 reboot that put the link on hci1 six times in four minutes
and the connection manager logged each one as outside its pool.
"""
from __future__ import annotations

import os
import sys

import pytest

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

es = pytest.importorskip("ble_device_easystart")

HEARD = "000195CC32F7"          # hci1, a scanning card
POOL = ["000195CC330B", "684E054477B0"]   # hci4, hci0 in config order


def test_unconfigured_pool_keeps_the_card_that_heard_it() -> None:
    assert es.link_adapter_key(HEARD, [], lambda k: True) == HEARD


def test_pool_naming_the_heard_card_uses_it() -> None:
    assert es.link_adapter_key(HEARD, [HEARD, POOL[0]], lambda k: True) == HEARD


def test_otherwise_the_first_present_pool_card_wins() -> None:
    assert es.link_adapter_key(HEARD, POOL, lambda k: True) == POOL[0]
    # first pool card unplugged -> second
    assert es.link_adapter_key(HEARD, POOL, lambda k: k != POOL[0]) == POOL[1]


def test_pool_with_no_card_present_falls_back_rather_than_refusing() -> None:
    assert es.link_adapter_key(HEARD, POOL, lambda k: False) == HEARD


def test_pool_keys_come_from_ble_connect_conf_in_config_order(monkeypatch) -> None:
    monkeypatch.setattr(es.ble_catcher, "catcher_options",
                        lambda path=None: (["00:01:95:CC:33:0B", "38:18:2B:FA:24:FA@68:4E:05:44:77:B0"], {}))
    assert es._pool_keys() == ["000195CC330B", "684E054477B0"]
    monkeypatch.setattr(es.ble_catcher, "catcher_options",
                        lambda path=None: (_ for _ in ()).throw(OSError("no file")))
    assert es._pool_keys() == []


def test_resolve_path_uses_the_chooser_and_names_the_pool_card_in_errors() -> None:
    src = open(os.path.join(SRC, "ble_device_easystart.py")).read()
    body = src[src.index("async def _resolve_without_scanning"):]
    body = body[:body.index("\n    async def ")]
    assert "key = link_adapter_key(self._adapter_key, _pool_keys()," in body
    assert "index = adapter_identity.index_for(key)" in body
    assert "adapter_identity.index_for(self._adapter_key)" not in body
    assert "(link pool) rather than" in body
