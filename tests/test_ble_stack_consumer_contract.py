"""sensors-py meets the BCM consumer contract (CONSUMER_MIGRATION.md section 2).

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


def test_coordination_log_strings_are_verbatim(caplog) -> None:
    """The three strings the monitor greps for, per state."""
    saved = dict(sys.modules)
    try:
        fake = _load("ble_stack")
        c = type(sys)("conf")
        c.BLUETOOTH_CONNECTION_MANAGER_DIR = "/data/bcm"
        c.FORCE_START_NOTIFY = True
        sys.modules["conf"] = c
        sys.modules["ble_stack"] = fake
        bep = _load("ble_ext_path")
        _body(bep, fake, caplog)
    finally:
        for k in [k for k in sys.modules if k not in saved]:
            del sys.modules[k]
        sys.modules.update(saved)


def _body(bep, fake, caplog):
    def run(state, failure=None):
        fake.shared_failure = failure
        bep._sourced = None
        bep.ble_stack = type(sys)("s")
        bep.ble_stack.ensure_ble_stack = lambda *a, **k: state
        bep.ble_stack.shared_failure = failure
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            bep._source_shared_stack()
        return caplog.text

    assert "BLE coordination: bleak_connection_manager loaded from /data/bcm" in run("shared")
    assert "BLE coordination: no shared install at /data/bcm" in run("vendored", None)
    assert ("BLE coordination: shared install at /data/bcm is present but unusable: boom"
            in run("vendored", "boom"))


def test_catcher_passes_force_start_notify_and_run_script_is_plain() -> None:
    catcher = open(os.path.join(SRC, "ble_catcher.py")).read()
    assert "force_start_notify=conf.FORCE_START_NOTIFY" in catcher, (
        "StartNotify policy must be passed at install_bleak_catcher, not via env")
    run = open(os.path.join(SRC, "start-dbus-ble-sensors-py.sh")).read()
    assert "/data/bcm/python3" not in run, "the shim exec must be gone"
    assert "exec python3 " in run, "run script must exec a plain interpreter"


def test_config_keys_exist_with_the_contract_defaults() -> None:
    conf = _load("conf")
    assert conf.BLUETOOTH_CONNECTION_MANAGER_DIR == "/data/bcm"
    assert conf.FORCE_START_NOTIFY is True
