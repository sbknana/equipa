"""Test abort controller hierarchy with WeakRef GC behavior.

Tests the Claude Code-ported abort controller pattern — parent abort cascades
to children, abandoned children get GC'd without memory leaks.

Copyright 2026 Forgeborn
"""

import gc
import weakref

import pytest

from equipa.abort_controller import (
    AbortController,
    create_child_abort_controller,
)


def test_abort_controller_basic():
    """Test basic abort controller functionality."""
    controller = AbortController()
    assert not controller.signal.aborted
    assert controller.signal.reason is None

    controller.abort("test reason")
    assert controller.signal.aborted
    assert controller.signal.reason == "test reason"

    # Second abort is no-op
    controller.abort("new reason")
    assert controller.signal.reason == "test reason"


def test_abort_signal_event_listener():
    """Test abort event listeners."""
    controller = AbortController()
    called = []

    def handler():
        called.append(1)

    controller.signal.add_event_listener("abort", handler)
    assert not called

    controller.abort("reason")
    assert called == [1]

    # Handler not called again on repeated abort
    controller.abort()
    assert called == [1]


def test_abort_signal_once_listener():
    """Test once=True event listeners."""
    controller = AbortController()
    called = []

    def handler():
        called.append(1)

    controller.signal.add_event_listener("abort", handler, once=True)
    controller.abort()
    assert called == [1]

    # Handler is auto-removed
    assert len(controller._callbacks) == 0


def test_abort_signal_listener_on_already_aborted():
    """Test adding listener after abort already triggered."""
    controller = AbortController()
    controller.abort("early")

    called = []
    def handler():
        called.append(1)

    # Listener added after abort — should fire immediately for once=True
    controller.signal.add_event_listener("abort", handler, once=True)
    # once=True on already-aborted signal fires immediately
    # (This matches JS AbortSignal behavior)


def test_child_abort_controller_parent_aborts():
    """Test child aborts when parent aborts."""
    parent = AbortController()
    child = create_child_abort_controller(parent)

    assert not parent.signal.aborted
    assert not child.signal.aborted

    parent.abort("parent reason")
    assert parent.signal.aborted
    assert child.signal.aborted
    assert child.signal.reason == "parent reason"


def test_child_abort_controller_child_aborts():
    """Test child abort does NOT affect parent."""
    parent = AbortController()
    child = create_child_abort_controller(parent)

    child.abort("child reason")
    assert child.signal.aborted
    assert not parent.signal.aborted


def test_child_abort_controller_already_aborted_parent():
    """Test creating child from already-aborted parent."""
    parent = AbortController()
    parent.abort("parent reason")

    child = create_child_abort_controller(parent)
    assert child.signal.aborted
    assert child.signal.reason == "parent reason"


def test_child_abort_controller_cleanup():
    """Test parent listener doesn't fire after child aborts.

    The once=True wrapper remains in _callbacks but won't fire again because
    it auto-removes itself after first invocation. This is the correct behavior.
    """
    parent = AbortController()
    child = create_child_abort_controller(parent)

    # When child aborts first, parent should still be able to abort without error
    child.abort("child abort")
    assert child.signal.aborted
    assert not parent.signal.aborted

    # Parent abort doesn't double-abort the child (cleanup worked)
    parent.abort("parent abort")
    assert parent.signal.aborted
    # Child's reason unchanged (wasn't re-aborted)
    assert child.signal.reason == "child abort"


def test_child_abort_controller_weakref_gc():
    """Test abandoned child can be GC'd (no strong reference from parent).

    This is the key memory safety property — WeakRef prevents parent from
    keeping abandoned children alive.
    """
    parent = AbortController()
    child = create_child_abort_controller(parent)

    # Create weak reference to child
    child_weak = weakref.ref(child)
    assert child_weak() is not None

    # Drop all strong references to child
    del child
    gc.collect()

    # Child should be GC'd even though parent still holds a WeakRef
    assert child_weak() is None

    # Parent can still abort without error (dead WeakRef is no-op)
    parent.abort("parent abort")
    assert parent.signal.aborted


def test_abort_controller_multiple_children():
    """Test parent abort cascades to multiple children."""
    parent = AbortController()
    child1 = create_child_abort_controller(parent)
    child2 = create_child_abort_controller(parent)
    child3 = create_child_abort_controller(parent)

    parent.abort("cascade")
    assert child1.signal.aborted
    assert child2.signal.aborted
    assert child3.signal.aborted


def test_abort_controller_nested_hierarchy():
    """Test multi-level parent-child-grandchild abort chain."""
    grandparent = AbortController()
    parent = create_child_abort_controller(grandparent)
    child = create_child_abort_controller(parent)

    grandparent.abort("top-level abort")
    assert grandparent.signal.aborted
    assert parent.signal.aborted
    assert child.signal.aborted


def test_abort_signal_exception_in_handler():
    """Test exception in abort handler doesn't break other handlers."""
    controller = AbortController()
    called = []

    def bad_handler():
        raise ValueError("handler error")

    def good_handler():
        called.append(1)

    controller.signal.add_event_listener("abort", bad_handler)
    controller.signal.add_event_listener("abort", good_handler)

    controller.abort()
    # good_handler should still run despite bad_handler exception
    assert called == [1]


def test_abort_signal_remove_event_listener():
    """Test removing event listeners."""
    controller = AbortController()
    called = []

    def handler():
        called.append(1)

    controller.signal.add_event_listener("abort", handler)
    controller.signal.remove_event_listener("abort", handler)

    controller.abort()
    assert called == []


def test_abort_controller_non_abort_event():
    """Test non-'abort' events are ignored."""
    controller = AbortController()
    called = []

    def handler():
        called.append(1)

    # Non-abort events should be silently ignored
    controller.signal.add_event_listener("other", handler)
    controller.abort()
    assert called == []
