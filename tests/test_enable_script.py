"""enable.sh runs at EVERY boot from rc.local, so it must not tear down a
healthy supervise tree.

Observed on dev twice: svscan builds supervise trees from the /service
symlinks at boot, then rc.local runs enable.sh, which removed those
symlinks and re-created them.  The old teardown missed the log
supervisors entirely -- their process is "supervise log", which matches
neither `pkill -f "supervise dbus-ble-sensors-py"` nor the launcher
pattern -- so they survived with a "(deleted)" cwd and kept holding the
log directory's multilog lock.  The live generation's logger could then
never start: the service wrote into a pipe nobody drained, hiding 14
minutes of startup output and heading for a hard block at the 64 kB pipe
buffer.

These are source-level assertions because the failure needs a real
daemontools tree, root, and a boot to reproduce.
"""
from __future__ import annotations

import os
import re

ENABLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "enable.sh")


def _src() -> str:
    return open(ENABLE).read()


def test_boot_path_leaves_a_healthy_tree_alone() -> None:
    src = _src()
    assert "links_ok" in src, (
        "enable.sh must detect an already-correct, already-supervised "
        "tree and skip the teardown; it runs on every boot")
    # The guard must check supervise liveness, not just the symlinks --
    # a correct symlink with a dead supervisor still needs repair.
    assert "supervise/control" in src, (
        "the fast path must confirm supervise is actually running")
    assert "/log/supervise/control" in src, (
        "the fast path must check the LOG supervisor too -- that is the "
        "one that held the lock")


def test_teardown_asks_supervise_to_exit_not_just_go_down() -> None:
    src = _src()
    assert "svc -dx" in src, (
        "svc -d only brings the service down and leaves supervise "
        "running with its cwd open; -x makes it exit and release the "
        "log directory lock")
    assert re.search(r'svc -dx "/service/\$svc_name/log"', src), (
        "the log service needs its own svc -dx -- it is the lock holder")


def test_reaper_does_not_match_other_services_loggers() -> None:
    """`pkill -f "supervise log"` would kill every logger on the box."""
    src = _src()
    bad = re.findall(r'pkill -f "supervise[^"]*"', src)
    assert not bad, (
        "supervise must be reaped by cwd, not by command line: a log "
        "supervisor is just 'supervise log', so a command-line pattern "
        "either misses ours or kills adc/digitalinputs/acsystem too. "
        f"found: {bad}")
    assert "readlink" in src and "/cwd" in src, (
        "the reaper must identify our supervisors by their cwd")
