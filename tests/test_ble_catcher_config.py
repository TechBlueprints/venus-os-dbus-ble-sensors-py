"""Deployment config for the bcmv2 connection layer."""
from __future__ import annotations

import ble_catcher


def _write(tmp_path, text):
    path = tmp_path / "ble-connect.conf"
    path.write_text(text)
    return str(path)


def test_missing_file_means_unconfigured(tmp_path) -> None:
    # Unconfigured is a real mode, not an error: bcmv2 then treats every
    # adapter the kernel exposes as a candidate.
    assert ble_catcher.catcher_options(str(tmp_path / "nope.conf")) == ([], {})


def test_adapters_and_caps_are_parsed(tmp_path) -> None:
    path = _write(tmp_path, """
        # deployment notes
        adapters = hci1 hci2
        link_caps = hci1:5 hci2:7
        """.replace("        ", ""))
    adapters, caps = ble_catcher.catcher_options(path)
    assert adapters == ["hci1", "hci2"]
    assert caps == {"hci1": 5, "hci2": 7}


def test_commas_and_pins_survive_verbatim(tmp_path) -> None:
    # Entries are passed to bcmv2 untouched — "MAC@hciX" is a pin.
    path = _write(tmp_path,
                  "adapters = AA:BB:CC:DD:EE:FF@hci1, hci2\n"
                  "link_caps = hci2:4\n")
    adapters, caps = ble_catcher.catcher_options(path)
    assert adapters == ["AA:BB:CC:DD:EE:FF@hci1", "hci2"]
    assert caps == {"hci2": 4}


def test_non_numeric_cap_is_ignored(tmp_path) -> None:
    path = _write(tmp_path, "link_caps = hci0:many hci1:3\n")
    _adapters, caps = ble_catcher.catcher_options(path)
    assert caps == {"hci1": 3}


def test_inline_comments_are_stripped(tmp_path) -> None:
    path = _write(tmp_path, "adapters = hci1  # the good dongle\n")
    adapters, _caps = ble_catcher.catcher_options(path)
    assert adapters == ["hci1"]
