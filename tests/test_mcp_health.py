"""Tests for equipa.mcp_health — MCP health monitoring.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import time

import pytest

from equipa.mcp_health import (
    DEFAULT_BACKOFF,
    HEALTHY_TTL,
    MAX_BACKOFF,
    MCPHealthMonitor,
)


@pytest.fixture()
def monitor(tmp_path, monkeypatch):
    """Create a MCPHealthMonitor with a temp cache file."""
    cache = tmp_path / ".mcp-health-cache.json"
    monkeypatch.setattr("equipa.mcp_health.HEALTH_CACHE", cache)
    return MCPHealthMonitor()


class TestIsHealthy:
    """Tests for is_healthy status checking."""

    def test_unknown_server_is_healthy(self, monitor):
        healthy, reason = monitor.is_healthy("unknown-server")
        assert healthy is True
        assert reason == "unknown"

    def test_healthy_server_within_ttl(self, monitor):
        monitor.mark_healthy("server-a")
        healthy, reason = monitor.is_healthy("server-a")
        assert healthy is True
        assert reason == "cached healthy"

    def test_healthy_server_expired_ttl(self, monitor, monkeypatch):
        monitor.mark_healthy("server-a")
        # Simulate TTL expiry
        monitor.state["servers"]["server-a"]["expires_at"] = time.time() - 1
        healthy, reason = monitor.is_healthy("server-a")
        assert healthy is True
        assert reason == "expired"

    def test_unhealthy_server_in_backoff(self, monitor):
        monitor.mark_unhealthy("server-b", error="connection refused")
        healthy, reason = monitor.is_healthy("server-b")
        assert healthy is False
        assert reason == "backoff"

    def test_unhealthy_server_past_backoff(self, monitor):
        monitor.mark_unhealthy("server-b")
        # Simulate backoff expiry
        monitor.state["servers"]["server-b"]["next_retry_at"] = time.time() - 1
        healthy, reason = monitor.is_healthy("server-b")
        assert healthy is True
        assert reason == "expired"


class TestMarkHealthy:
    """Tests for mark_healthy."""

    def test_sets_status_and_ttl(self, monitor):
        before = time.time()
        monitor.mark_healthy("srv")
        entry = monitor.state["servers"]["srv"]
        assert entry["status"] == "healthy"
        assert entry["failure_count"] == 0
        assert entry["expires_at"] >= before + HEALTHY_TTL

    def test_resets_failure_count(self, monitor):
        monitor.mark_unhealthy("srv")
        monitor.mark_unhealthy("srv")
        assert monitor.state["servers"]["srv"]["failure_count"] == 2
        monitor.mark_healthy("srv")
        assert monitor.state["servers"]["srv"]["failure_count"] == 0


class TestMarkUnhealthy:
    """Tests for mark_unhealthy with exponential backoff."""

    def test_first_failure_uses_default_backoff(self, monitor):
        before = time.time()
        monitor.mark_unhealthy("srv", error="timeout")
        entry = monitor.state["servers"]["srv"]
        assert entry["status"] == "unhealthy"
        assert entry["failure_count"] == 1
        assert entry["next_retry_at"] >= before + DEFAULT_BACKOFF
        assert entry["last_error"] == "timeout"

    def test_exponential_backoff_doubles(self, monitor):
        monitor.mark_unhealthy("srv")
        monitor.mark_unhealthy("srv")
        entry = monitor.state["servers"]["srv"]
        assert entry["failure_count"] == 2
        expected_backoff = DEFAULT_BACKOFF * 2
        assert entry["next_retry_at"] >= entry["checked_at"] + expected_backoff - 1

    def test_backoff_capped_at_max(self, monitor):
        # Simulate many failures to exceed MAX_BACKOFF
        for _ in range(20):
            monitor.mark_unhealthy("srv")
        entry = monitor.state["servers"]["srv"]
        # Backoff should never exceed MAX_BACKOFF
        actual_backoff = entry["next_retry_at"] - entry["checked_at"]
        assert actual_backoff <= MAX_BACKOFF + 1  # +1 for timing tolerance

    def test_error_truncated_to_500_chars(self, monitor):
        long_error = "x" * 1000
        monitor.mark_unhealthy("srv", error=long_error)
        assert len(monitor.state["servers"]["srv"]["last_error"]) == 500


class TestPersistence:
    """Tests for cache file persistence."""

    def test_save_and_reload(self, tmp_path, monkeypatch):
        cache = tmp_path / ".mcp-health-cache.json"
        monkeypatch.setattr("equipa.mcp_health.HEALTH_CACHE", cache)

        m1 = MCPHealthMonitor()
        m1.mark_healthy("alpha")
        m1.mark_unhealthy("beta", error="down")

        # Create a new monitor — should load from cache
        m2 = MCPHealthMonitor()
        assert "alpha" in m2.state["servers"]
        assert m2.state["servers"]["alpha"]["status"] == "healthy"
        assert m2.state["servers"]["beta"]["status"] == "unhealthy"

    def test_corrupt_cache_returns_empty(self, tmp_path, monkeypatch):
        cache = tmp_path / ".mcp-health-cache.json"
        cache.write_text("NOT VALID JSON{{{")
        monkeypatch.setattr("equipa.mcp_health.HEALTH_CACHE", cache)
        m = MCPHealthMonitor()
        assert m.state == {"servers": {}}

    def test_missing_cache_returns_empty(self, tmp_path, monkeypatch):
        cache = tmp_path / "nonexistent.json"
        monkeypatch.setattr("equipa.mcp_health.HEALTH_CACHE", cache)
        m = MCPHealthMonitor()
        assert m.state == {"servers": {}}


class TestUtilityMethods:
    """Tests for get_status, get_all_statuses, clear."""

    def test_get_status_known(self, monitor):
        monitor.mark_healthy("srv")
        status = monitor.get_status("srv")
        assert status is not None
        assert status["status"] == "healthy"

    def test_get_status_unknown(self, monitor):
        assert monitor.get_status("nope") is None

    def test_get_all_statuses(self, monitor):
        monitor.mark_healthy("a")
        monitor.mark_unhealthy("b")
        all_statuses = monitor.get_all_statuses()
        assert set(all_statuses.keys()) == {"a", "b"}

    def test_clear_single(self, monitor):
        monitor.mark_healthy("a")
        monitor.mark_healthy("b")
        monitor.clear("a")
        assert monitor.get_status("a") is None
        assert monitor.get_status("b") is not None

    def test_clear_all(self, monitor):
        monitor.mark_healthy("a")
        monitor.mark_healthy("b")
        monitor.clear()
        assert monitor.get_all_statuses() == {}
