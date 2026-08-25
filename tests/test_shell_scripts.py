"""Run the shell-level tests as part of the suite.

The launcher is a /bin/sh script, so its logic cannot be imported.  The
shell test extracts the real function from the shipping script and
drives it; this wrapper just makes it fail the normal `tests/run.sh` run
rather than waiting to be remembered separately.
"""
from __future__ import annotations

import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SHELL_TESTS = ["test_launcher_control.sh"]


@pytest.mark.parametrize("name", SHELL_TESTS)
def test_shell_suite(name: str) -> None:
    script = os.path.join(HERE, name)
    assert os.path.exists(script), f"{name} is missing"
    proc = subprocess.run([script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
