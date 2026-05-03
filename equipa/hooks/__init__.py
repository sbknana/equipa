"""EQUIPA pipeline hook handlers.

This package contains migrated guard logic that was previously scattered
inline in equipa/loops.py:

    * vacuous_pass        — vacuous-pass check (no real changes despite "success")
    * classifier_retry    — classifier false-positive retry (tester reports
                            failure but developer made file changes)
    * security_review_gate — security-reviewer output gate (only treat
                             findings as actionable if non-empty)

Each handler module exposes a ``register(dispatcher)`` function that wires
its callbacks to the colon-namespaced pipeline events defined in
equipa.hooks (the parent module). Handlers are pure functions that accept
event keyword arguments and may return a dict result; they MUST NOT raise
— the dispatcher logs and swallows exceptions.

The dispatcher (see ``equipa.hooks.dispatcher``) discovers handler modules
in this package at orchestrator start and registers them with the
``equipa.hooks`` callback registry. Adding a new handler is as simple as
dropping a new ``my_handler.py`` file here that exposes ``register``.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from equipa.hooks.dispatcher import (
    bootstrap_pipeline_hooks,
    discover_handlers,
    register_handler_module,
)

__all__ = [
    "bootstrap_pipeline_hooks",
    "discover_handlers",
    "register_handler_module",
]
