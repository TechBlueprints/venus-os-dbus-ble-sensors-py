"""Run the launcher's shell-level test as part of the suite.

The launcher is a /bin/sh script, so its logic cannot be imported.  The
shell test drives the real control() extracted from the shipping script;
this wrapper just makes it fail the normal `tests/run.sh` run rather
than waiting to be remembered separately.
"""
from __future__ import annotations

import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "test_launcher_control.sh")


@pytest.mark.skipif(not os.path.exists(SCRIPT), reason="shell test missing")
def test_launcher_control_shell() -> None:
    proc = subprocess.run([SCRIPT], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
