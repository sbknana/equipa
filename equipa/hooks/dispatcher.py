"""EQUIPA pipeline hook dispatcher.

Discovers handler modules in the ``equipa.hooks`` package and registers
their callbacks with the parent ``equipa.hooks`` event registry (defined
in ``equipa/hooks.py``).

The dispatcher is invoked once at orchestrator start via
``bootstrap_pipeline_hooks()``. It walks every module in the package
(except this one and ``__init__``) and calls ``module.register(dispatcher)``
if such a function exists. Handler modules are responsible for
registering their own callbacks — the dispatcher does not introspect
function names or guess wiring.

Synchronous vs asynchronous handlers:

    Pipeline events fire via ``equipa.hooks.fire_async`` from the orchestrator
    so external command hooks can run concurrently. Python handlers
    registered here are called synchronously inside the dispatcher to keep
    the contract simple. Long-running handlers should spawn their own
    asyncio task (or use ``asyncio.to_thread``) and return immediately to
    avoid blocking the orchestrator loop.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from types import ModuleType
from typing import Any, Callable

# Import the parent hooks module that holds the registry. We import lazily
# inside functions to avoid a circular import at package-load time.
logger = logging.getLogger(__name__)

# Sentinel module names that should not be treated as handler modules.
_RESERVED_MODULES: frozenset[str] = frozenset({"dispatcher", "__init__"})


def _hooks_module() -> ModuleType:
    """Return the parent equipa.hooks module (the registry)."""
    # Imported here to avoid a circular import: equipa.hooks (package)
    # imports equipa.hooks.dispatcher, which imports the equipa/hooks.py
    # module. Python resolves the package first; importing the .py file
    # by attribute access on the package would mask it. Import explicitly
    # by absolute name to get the .py module that defines `register`,
    # `fire`, and `LIFECYCLE_EVENTS`.
    return importlib.import_module("equipa.hooks")


def discover_handlers(package_name: str = "equipa.hooks") -> list[ModuleType]:
    """Discover all handler modules in the given package.

    Returns the list of module objects that look like handlers (i.e. have
    a top-level ``register`` callable). Reserved modules (dispatcher,
    __init__) and modules whose import fails are skipped with a warning.
    """
    try:
        pkg = importlib.import_module(package_name)
    except ImportError as exc:
        logger.warning("Cannot import hooks package %s: %s", package_name, exc)
        return []

    if not hasattr(pkg, "__path__"):
        logger.warning("%s is not a package (no __path__)", package_name)
        return []

    handlers: list[ModuleType] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name in _RESERVED_MODULES:
            continue
        full_name = f"{package_name}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except ImportError as exc:
            logger.warning("Failed to import hook handler %s: %s", full_name, exc)
            continue

        if not callable(getattr(module, "register", None)):
            logger.debug(
                "Module %s has no top-level register() — skipping", full_name
            )
            continue
        handlers.append(module)
    return handlers


def register_handler_module(module: ModuleType) -> int:
    """Invoke ``module.register(dispatcher)`` and return number of callbacks added.

    The handler module's ``register`` function is expected to call
    ``dispatcher.on(event, callback)`` for each callback it wires. The
    ``dispatcher`` argument is this module itself, so handlers can use the
    ``on`` helper without importing the registry directly.
    """
    hooks_mod = _hooks_module()
    before = hooks_mod.get_registered_count()
    try:
        module.register(_DispatcherHandle())
    except Exception as exc:  # noqa: BLE001 — never crash bootstrap
        logger.warning(
            "Hook handler %s.register() failed: %s: %s",
            module.__name__, type(exc).__name__, exc,
        )
        return 0
    after = hooks_mod.get_registered_count()
    delta = after - before
    logger.info(
        "Registered %d callback(s) from hook module %s", delta, module.__name__
    )
    return delta


def bootstrap_pipeline_hooks(package_name: str = "equipa.hooks") -> int:
    """Discover and register all pipeline hook handlers.

    Call this once during orchestrator startup. Returns the total number
    of callbacks registered across all handler modules.
    """
    total = 0
    for module in discover_handlers(package_name):
        total += register_handler_module(module)
    logger.info("Pipeline hooks bootstrap registered %d callback(s) total", total)
    return total


def on(event: str, callback: Callable[..., Any]) -> None:
    """Register a callback with the parent hooks registry.

    Convenience wrapper used by handler modules so they don't need to
    import ``equipa.hooks`` directly.
    """
    hooks_mod = _hooks_module()
    hooks_mod.register(event, callback)


class _DispatcherHandle:
    """Object passed to handler ``register(dispatcher)`` functions.

    Exposes ``on(event, callback)`` for handler convenience and a
    ``pipeline_events`` tuple for handlers that want to introspect.
    """

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        on(event, callback)

    @property
    def pipeline_events(self) -> tuple[str, ...]:
        return _hooks_module().PIPELINE_EVENTS
