"""Handler-level filters for third-party chatter we cannot quieten by name.

velib_python's ``vedbus`` announces every service registration at INFO
through the ROOT logger — ``logging.info("registered ourselves on D-Bus
as %s")`` — so ``logging.getLogger("vedbus").setLevel()`` has nothing to
attach to.  The only handle is ``record.module``, which is also what the
system formatter (``levelname:module:message``) renders as "vedbus".

Why it matters: with ~20 devices, and a service that restarts on every
bcm deploy, that one line was a quarter of all output on prod over a
94-hour window.  We log one line per registration ourselves, carrying
the instance; the library's adds nothing to it.
"""
from __future__ import annotations

import logging


class QuietVedbusFilter(logging.Filter):
    """Drop vedbus records below WARNING.  Its warnings still pass."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.module == "vedbus"
                    and record.levelno < logging.WARNING)


def install(debug: bool = False) -> int:
    """Attach the filter to every root handler.  Returns how many.

    Skipped in debug mode — when someone asks for everything, give them
    everything.
    """
    if debug:
        return 0
    n = 0
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, QuietVedbusFilter) for f in handler.filters):
            handler.addFilter(QuietVedbusFilter())
            n += 1
    return n
