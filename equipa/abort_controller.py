"""Abort controller hierarchy with WeakRef for subprocess cleanup.

Ported from Claude Code's TypeScript implementation. Parent-child abort chains
using WeakRef — parent abort cascades to children, abandoned children get GC'd.

Pure Python stdlib, zero dependencies.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Callable


class AbortController:
    """Python equivalent of JavaScript AbortController.

    Provides signal-based cancellation for async operations and subprocesses.
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason: Exception | None = None
        self._callbacks: list[Callable[[], None]] = []

    @property
    def signal(self) -> AbortSignal:
        """Get the abort signal for this controller."""
        if not hasattr(self, "_signal"):
            self._signal = AbortSignal(self)
        return self._signal

    def abort(self, reason: Exception | None = None) -> None:
        """Abort this controller and trigger all registered callbacks.

        Args:
            reason: Optional exception describing why the abort occurred.
        """
        if self._aborted:
            return

        self._aborted = True
        self._reason = reason or asyncio.CancelledError("Operation aborted")

        # Trigger all callbacks (copy list to avoid mutation during iteration)
        for callback in self._callbacks[:]:
            try:
                callback()
            except Exception:
                # Swallow exceptions in abort handlers to prevent cascade failures
                pass

        # DO NOT clear callbacks here — once=True handlers auto-remove themselves
        # and some handlers may be WeakRefs that need to persist

    def _add_callback(self, callback: Callable[[], None]) -> None:
        """Internal: Register a callback to fire when aborted."""
        if self._aborted:
            # Already aborted — trigger immediately
            try:
                callback()
            except Exception:
                pass
        else:
            self._callbacks.append(callback)

    def _remove_callback(self, callback: Callable[[], None]) -> None:
        """Internal: Remove a registered callback."""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass


class AbortSignal:
    """Read-only signal for abort state.

    Mimics JavaScript AbortSignal API.
    """

    def __init__(self, controller: AbortController) -> None:
        self._controller = controller

    @property
    def aborted(self) -> bool:
        """Check if this signal has been aborted."""
        return self._controller._aborted

    @property
    def reason(self) -> Exception | None:
        """Get the reason for abort (if any)."""
        return self._controller._reason

    def add_event_listener(
        self,
        event: str,
        callback: Callable[[], None],
        once: bool = False,
    ) -> None:
        """Add an event listener for 'abort' events.

        Args:
            event: Must be 'abort'
            callback: Function to call when aborted
            once: If True, remove callback after first trigger
        """
        if event != "abort":
            return

        if once:
            # Wrap callback to auto-remove after first call
            original_callback = callback
            def once_wrapper() -> None:
                original_callback()
                self._controller._remove_callback(once_wrapper)
            self._controller._add_callback(once_wrapper)
        else:
            self._controller._add_callback(callback)

    def remove_event_listener(
        self,
        event: str,
        callback: Callable[[], None],
    ) -> None:
        """Remove an event listener.

        Args:
            event: Must be 'abort'
            callback: The callback to remove
        """
        if event != "abort":
            return
        self._controller._remove_callback(callback)


def _propagate_abort(
    weak_parent: weakref.ref[AbortController],
    weak_child: weakref.ref[AbortController],
) -> None:
    """Propagate abort from parent to weakly-referenced child controller.

    Both parent and child are weakly held — neither direction creates a
    strong reference that could prevent GC.
    Module-scope function avoids per-call closure allocation.
    """
    parent = weak_parent()
    child = weak_child()
    if child:
        child.abort(parent.signal.reason if parent else None)


def _remove_abort_handler(
    weak_parent: weakref.ref[AbortController],
    weak_handler: weakref.ref[Callable[[], None]],
) -> None:
    """Remove an abort handler from a weakly-referenced parent signal.

    Both parent and handler are weakly held — if either has been GC'd
    or the parent already aborted (once=True), this is a no-op.
    Module-scope function avoids per-call closure allocation.
    """
    parent = weak_parent()
    handler = weak_handler()
    if parent and handler:
        parent.signal.remove_event_listener("abort", handler)


def create_child_abort_controller(
    parent: AbortController,
) -> AbortController:
    """Create a child AbortController that aborts when its parent aborts.

    Aborting the child does NOT affect the parent.

    Memory-safe: Uses WeakRef so the parent doesn't retain abandoned children.
    If the child is dropped without being aborted, it can still be GC'd.
    When the child IS aborted, the parent listener is removed to prevent
    accumulation of dead handlers.

    Args:
        parent: The parent AbortController

    Returns:
        Child AbortController
    """
    child = AbortController()

    # Fast path: parent already aborted, no listener setup needed
    if parent.signal.aborted:
        child.abort(parent.signal.reason)
        return child

    # WeakRef prevents the parent from keeping an abandoned child alive.
    # If all strong references to child are dropped without aborting it,
    # the child can still be GC'd — the parent only holds a dead WeakRef.
    weak_child = weakref.ref(child)
    weak_parent = weakref.ref(parent)

    # Create bound handler for propagation
    def handler() -> None:
        _propagate_abort(weak_parent, weak_child)

    parent.signal.add_event_listener("abort", handler, once=True)

    # Auto-cleanup: remove parent listener when child is aborted (from any source).
    # Both parent and handler are weakly held — if either has been GC'd or the
    # parent already aborted (once=True), the cleanup is a harmless no-op.
    weak_handler = weakref.ref(handler)

    def cleanup_handler() -> None:
        _remove_abort_handler(weak_parent, weak_handler)

    child.signal.add_event_listener("abort", cleanup_handler, once=True)

    return child
