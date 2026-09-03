"""F7 on the HEX control channel is a credit request, not an error.

The peer asks for a specific number of credits and withholds the next
chunk until it gets exactly that many.  smartshunt_hex learned this and
answers with the requested count; hex_key_session called F7 an "error",
logged it, and kept re-writing its fixed standing window -- so a device
that gates on F7 never pushes 0xEC65.  The SmartSolar MPPT 75/15 is such
a device, and our own failure message named the F7 while treating it as
the failure ("F7 / no EC65 push").
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))


@pytest.fixture(scope="module")
def hks():
    """Load the real module without disturbing anyone else's sys.modules.

    Importing it plainly, or even under a private spec name, still
    registers its dependency imports globally -- enough to break
    test_notify_release, which installs its own modules under their real
    names and passed alone but not in the full run.  Snapshot sys.modules
    around the load and restore it afterwards so this file is invisible
    to every other test regardless of ordering.
    """
    before = dict(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(
            "_hex_key_session_f7_test",
            os.path.join(SRC, "hex_key_session.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for name in [n for n in sys.modules if n not in before]:
            del sys.modules[name]
        sys.modules.update(before)


def test_f7_is_parsed_as_a_credit_request(hks) -> None:
    c = hks._Collector()
    assert c.f7 is False
    c.on_ctrl(None, bytearray(b"\xf7\x08\x00"))
    assert c.f7 is True and c.f7_n == 8, "must capture the requested count"


def test_a_bare_f7_still_asks_for_credits(hks) -> None:
    c = hks._Collector()
    c.on_ctrl(None, bytearray(b"\xf7"))
    assert c.f7 is True


def test_other_control_traffic_is_not_a_credit_request(hks) -> None:
    c = hks._Collector()
    for raw in (b"\xf9\x80", b"\xf8", b""):
        c.on_ctrl(None, bytearray(raw))
    assert c.f7 is False


def test_reset_keeps_a_pending_credit_request(hks) -> None:
    """Dropping an F7 stalls the session; an extra grant is harmless."""
    c = hks._Collector()
    c.on_ctrl(None, bytearray(b"\xf7\x04\x00"))
    c.reset()
    assert c.f7 is True and c.f7_n == 4


def test_credits_grants_exactly_what_was_asked(hks) -> None:
    class _C:
        def __init__(self): self.writes = []
        async def write_gatt_char(self, char, payload, response=False):
            self.writes.append(bytes(payload))
    c = _C()
    # NOT asyncio.run(): it closes the loop and leaves none current, and
    # test_notify_release then dies on get_event_loop() with "no current
    # event loop".  Reuse the loop the way the rest of this suite does.
    loop = asyncio.get_event_loop()
    loop.run_until_complete(hks._credits(c, 8))
    loop.run_until_complete(hks._credits(c))
    assert c.writes[0] == bytes([0xF9, 8]), "must answer with the asked count"
    assert c.writes[1] == hks._CTRL_CREDITS, "no count -> standing window"
