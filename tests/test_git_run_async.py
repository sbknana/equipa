"""Tests for equipa.git_ops.git_run_async — non-blocking git helper.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from equipa.git_ops import git_run_async


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal initialised git repo in tmp_path."""
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=str(tmp_path), check=True,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_git_run_async_returns_completed_process(tmp_git_repo: Path) -> None:
    result = await git_run_async(["rev-parse", "HEAD"], tmp_git_repo, timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip()  # non-empty SHA
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)


@pytest.mark.asyncio
async def test_git_run_async_failure_returns_nonzero(tmp_git_repo: Path) -> None:
    result = await git_run_async(
        ["rev-parse", "--verify", "nonexistent-ref"], tmp_git_repo, timeout=10,
    )
    assert result.returncode != 0
    assert "nonexistent-ref" in result.stderr or result.stderr


@pytest.mark.asyncio
async def test_git_run_async_does_not_block_event_loop(tmp_git_repo: Path) -> None:
    """Concurrent git_run_async calls should overlap — proves no event-loop block."""

    async def ticker(ticks: list[float], stop: asyncio.Event) -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.01)
            except asyncio.TimeoutError:
                pass

    ticks: list[float] = []
    stop = asyncio.Event()
    ticker_task = asyncio.create_task(ticker(ticks, stop))

    # Issue several git calls concurrently.
    results = await asyncio.gather(*[
        git_run_async(["status", "--porcelain"], tmp_git_repo, timeout=10)
        for _ in range(4)
    ])
    stop.set()
    await ticker_task

    assert all(r.returncode == 0 for r in results)
    # Ticker should have advanced multiple times during the gather, proving
    # the loop kept ticking while git was running.
    assert len(ticks) >= 2


@pytest.mark.asyncio
async def test_git_run_async_timeout_raises(tmp_path: Path) -> None:
    """A 0-second timeout should raise TimeoutExpired (not block forever)."""
    # Run against an empty dir so git fails fast OR ensure timeout fires.
    # Use a deliberately impossible-to-finish-in-0s call inside a real repo.
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    with pytest.raises(subprocess.TimeoutExpired):
        # Sleep via shell isn't a git op — use --version with a 0 timeout
        # to deterministically trip the wait_for guard.
        await git_run_async(["--version"], tmp_path, timeout=0)
