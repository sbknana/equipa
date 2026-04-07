# Abort Controller Integration Guide

Ported from Claude Code's TypeScript implementation — parent-child abort chains using WeakRef for memory-safe subprocess cleanup.

## Architecture

```python
from equipa.abort_controller import AbortController, create_child_abort_controller

# Manager creates parent controller
manager_controller = AbortController()

# Each agent gets a child controller
agent_controller = create_child_abort_controller(manager_controller)

# When manager aborts, all agents abort
manager_controller.abort("shutting down")
# → agent_controller.signal.aborted == True

# But agent abort does NOT affect manager
```

## Key Properties

1. **Cascading abort**: Parent abort cascades to all children
2. **Isolation**: Child abort does NOT affect parent or siblings
3. **Memory safety**: Abandoned children can be GC'd (WeakRef prevents parent from holding strong references)
4. **Cleanup**: When child aborts, parent listener is automatically removed

## Integration with agent_runner.py

### Pattern 1: Single subprocess with abort

```python
async def run_agent(
    cmd: list[str],
    abort_controller: AbortController | None = None,
) -> dict[str, Any]:
    """Run agent subprocess with abort support."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Set up abort handler to kill subprocess
    def abort_handler() -> None:
        if process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass

    if abort_controller:
        abort_controller.signal.add_event_listener("abort", abort_handler, once=True)

    try:
        stdout, stderr = await process.communicate()
        # ... process output
    finally:
        # Cleanup: remove handler after process exits
        if abort_controller:
            abort_controller.signal.remove_event_listener("abort", abort_handler)
```

### Pattern 2: Retry loop with abort

```python
async def run_agent_with_retry(
    cmd: list[str],
    max_retries: int = 10,
    abort_controller: AbortController | None = None,
) -> dict[str, Any]:
    """Retry loop that respects abort signal."""
    for attempt in range(1, max_retries + 1):
        # Check abort before each retry
        if abort_controller and abort_controller.signal.aborted:
            return {
                "success": False,
                "errors": ["Aborted by parent"],
            }

        result = await run_agent(cmd, abort_controller=abort_controller)
        if result["success"]:
            return result

        # Exponential backoff with abort check
        delay = get_retry_delay(attempt)
        if abort_controller:
            # Wait with abort signal
            try:
                await asyncio.wait_for(
                    abort_controller.signal.wait(),
                    timeout=delay,
                )
                # Aborted during wait
                return {"success": False, "errors": ["Aborted during retry"]}
            except asyncio.TimeoutError:
                # Normal — delay elapsed, retry
                pass
        else:
            await asyncio.sleep(delay)
```

### Pattern 3: Manager mode with multiple agents

```python
async def run_manager_loop(task_id: int) -> dict[str, Any]:
    """Manager dispatches multiple agents, all sharing abort chain."""
    manager_controller = AbortController()

    results = []
    for agent_role in ["researcher", "developer", "tester"]:
        # Each agent gets child controller
        agent_controller = create_child_abort_controller(manager_controller)

        # If any agent fails critically, abort remaining agents
        result = await run_agent(
            cmd=build_cli_command(role=agent_role),
            abort_controller=agent_controller,
        )

        if result["success"]:
            results.append(result)
        else:
            # Critical failure — abort remaining agents
            manager_controller.abort(f"Agent {agent_role} failed")
            break

    return {"results": results}
```

### Pattern 4: Streaming with abort

```python
async def run_agent_streaming(
    cmd: list[str],
    abort_controller: AbortController | None = None,
) -> dict[str, Any]:
    """Stream agent output with abort support."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    abort_handler_active = False
    def abort_handler() -> None:
        nonlocal abort_handler_active
        if abort_handler_active and process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass

    if abort_controller:
        abort_controller.signal.add_event_listener("abort", abort_handler, once=True)
        abort_handler_active = True

    try:
        while True:
            # Check abort before reading next line
            if abort_controller and abort_controller.signal.aborted:
                process.kill()
                return {"success": False, "errors": ["Aborted"]}

            line = await process.stdout.readline()
            if not line:
                break

            # Process line...
            process_streaming_output(line)

        await process.wait()
        # ... return result
    finally:
        abort_handler_active = False
        if abort_controller:
            abort_controller.signal.remove_event_listener("abort", abort_handler)
```

## Memory Safety Verification

Test that abandoned children can be GC'd:

```python
import gc
import weakref

def test_abandoned_child_gc():
    """Verify WeakRef prevents memory leak."""
    parent = AbortController()
    child = create_child_abort_controller(parent)

    # Create weak reference to child
    child_ref = weakref.ref(child)
    assert child_ref() is not None

    # Drop all strong references
    del child
    gc.collect()

    # Child should be GC'd (parent only holds WeakRef)
    assert child_ref() is None

    # Parent can still abort without error
    parent.abort()
```

## Implementation Notes

1. **Pure stdlib**: Only uses `weakref` and `asyncio` — zero pip dependencies
2. **Module-scope functions**: `_propagate_abort` and `_remove_abort_handler` are module-level to avoid closure allocation per child
3. **Fast path**: If parent already aborted, child is immediately aborted without setting up listeners
4. **once=True**: All internal listeners use `once=True` to auto-remove after first invocation
5. **Exception safety**: Handlers swallow exceptions to prevent cascade failures

## Comparison to Claude Code

| Feature | Claude Code (TS) | EQUIPA (Python) |
|---------|------------------|-----------------|
| Language | TypeScript | Python 3.12+ |
| WeakRef | `WeakRef<T>` | `weakref.ref[T]` |
| Event API | `addEventListener` | `add_event_listener` |
| Module functions | `propagateAbort.bind()` | `_propagate_abort()` |
| GC behavior | V8 engine | CPython 3.12 |
| Async support | Node.js EventEmitter | asyncio.Event |

Both implementations have identical semantics and memory safety guarantees.

## Testing

Run test suite:

```bash
pytest tests/test_abort_controller.py -v
```

14 tests covering:
- Basic abort/signal functionality
- Event listeners (once=True, exception handling)
- Parent-child abort chains
- Multiple children / nested hierarchy
- WeakRef GC verification
- Cleanup on child abort
