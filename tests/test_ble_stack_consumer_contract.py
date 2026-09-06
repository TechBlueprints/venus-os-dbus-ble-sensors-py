"""sensors-py meets the BCM consumer contract (CONSUMERS.md section 2).

The shared bleak-connection-manager install is sourced IN-PROCESS by
ble_stack.ensure_ble_stack, keyed on our own config value, with no
launcher shim and no shared config file.  These tests pin the parts of
the contract the monitor and the fleet depend on:

- the reference module is lifted as-is (imports only os + sys),
- the four import roots are in the exact order install.sh writes,
- the three coordination log strings are emitted verbatim per state,
- StartNotify policy is passed as force_start_notify at the catcher,
- the run script execs a plain interpreter, not the /data/bcm shim.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys

import pytest

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"_bcmc_{name}", os.path.join(SRC, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ble_stack():
    return _load("ble_stack")


def test_reference_module_imports_only_os_and_sys(ble_stack) -> None:
    src = open(os.path.join(SRC, "ble_stack.py")).read()
    tops = [l.split()[1].split(".")[0] for l in src.splitlines()
            if l.startswith(("import ", "from ")) and "__future__" not in l]
    assert set(tops) <= {"os", "sys"}, f"ble_stack must import only os/sys, got {tops}"


def test_import_roots_are_in_contract_order(ble_stack) -> None:
    roots = ble_stack.shared_lib_paths("/data/bcm")
    assert roots == [
        "/data/bcm/src",
        "/data/bcm/ext",
        "/data/bcm/ext/upstream/bleak",
        "/data/bcm/ext/upstream/bleak-retry-connector/src",
    ], "the four roots must match install.sh / bcm_autowire._lib_paths order"


def test_presence_is_the_package_not_the_folder(ble_stack, tmp_path) -> None:
    assert not ble_stack.shared_install_present(str(tmp_path))  # empty dir
    (tmp_path / "src" / "bleak_connection_manager").mkdir(parents=True)
    assert ble_stack.shared_install_present(str(tmp_path))
    assert not ble_stack.shared_install_present("")  # empty value = never look


def test_already_provided_inserts_nothing(ble_stack, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "bleak_connection_manager", object())
    before = list(sys.path)
    assert ble_stack.ensure_ble_stack("/data/bcm", vendored_dir=None) == "provided"
    assert sys.path == before


def test_absent_shared_dir_returns_vendored_without_failure(ble_stack, tmp_path) -> None:
    sys.modules.pop("bleak_connection_manager", None)
    state = ble_stack.ensure_ble_stack(str(tmp_path / "nope"), vendored_dir=None)
    assert state == "vendored" and ble_stack.shared_failure is None


def test_all_seven_coordination_strings_are_verbatim_in_the_catcher() -> None:
    """The monitor greps these across the fleet; a refactor must not drift them.

    They live in ble_catcher (INFO on install, WARNING/ERROR when the
    catcher does not install, WARNING when the install predates the
    force_start_notify parameter).  Checked as source substrings so the
    wording, not just the behaviour, is pinned.
    """
    raw = open(os.path.join(SRC, "ble_catcher.py")).read()
    # Collapse Python implicit string concatenation ("a" \n "b" -> "ab") so a
    # log string wrapped across source lines still matches verbatim.
    src = re.sub(r'"\s*\n\s*"', "", raw)
    assert "BLE coordination: bleak_connection_manager loaded from %s" in src
    assert ("BLE coordination: no shared install at %s; running uncoordinated, "
            "no claims, no adapter routing, no card recovery") in src
    assert ("BLE coordination: BLUETOOTH_CONNECTION_MANAGER is on but "
            "BLUETOOTH_CONNECTION_MANAGER_DIR is empty; running uncoordinated, "
            "no claims, no adapter routing, no card recovery") in src
    assert ("BLE coordination: shared install at %s is present but unusable, "
            "running uncoordinated: %s") in src
    assert ("BLE coordination: shared install at %s predates the "
            "force_start_notify parameter; StartNotify policy passed "
            "through the legacy BCM_FORCE_START_NOTIFY environment") in src
    assert ("BLE coordination: catcher would not install from %s, "
            "running uncoordinated: %s") in src
    assert ("BLE coordination: catcher installed (force_start_notify=%s, "
            "adapters=%s configured, %s pinned)") in src


def test_catcher_is_enable_gated_and_signature_guards_the_policy() -> None:
    catcher = open(os.path.join(SRC, "ble_catcher.py")).read()
    assert "if not conf.BLUETOOTH_CONNECTION_MANAGER:" in catcher, (
        "manager-off must skip the catcher (rule 6)")
    assert "inspect.signature(install_bleak_catcher)" in catcher, (
        "the policy pass must be guarded by the install's signature")
    assert 'policy["force_start_notify"]' in catcher
    assert "conf.BLUETOOTH_CONNECTION_MANAGER_FORCE_START_NOTIFY" in catcher


def test_the_launcher_prod_actually_execs_is_a_plain_interpreter() -> None:
    """Guard the file /service really runs, not an adjacent one.

    install.sh symlinks /service/<name> to the repo's ROOT-level service/
    directory, so service/run is the launcher.  The in-tree
    start-dbus-ble-sensors-py.sh is checked too, but a shim left in
    service/run is the one that would silently keep prod on the shim.
    """
    repo = os.path.normpath(os.path.join(SRC, "..", "..", "..", ".."))
    for rel in ("service/run", "service-launcher/run"):
        run = open(os.path.join(repo, rel)).read()
        assert "/data/bcm/python3" not in run, f"{rel}: the shim exec must be gone"
    launcher = open(os.path.join(repo, "service", "run")).read()
    assert "exec python3 " in launcher, "service/run must exec a plain interpreter"
    start = open(os.path.join(SRC, "start-dbus-ble-sensors-py.sh")).read()
    assert "/data/bcm/python3" not in start and "exec python3 " in start


def test_config_keys_exist_with_the_contract_defaults() -> None:
    conf = _load("conf")
    assert conf.BLUETOOTH_CONNECTION_MANAGER is True
    assert conf.BLUETOOTH_CONNECTION_MANAGER_DIR == "/data/bcm"
    assert conf.BLUETOOTH_CONNECTION_MANAGER_FORCE_START_NOTIFY is True
