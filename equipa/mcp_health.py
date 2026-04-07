"""MCP health monitoring with exponential backoff.

Tracks MCP server health status, caches results, and applies
exponential backoff to unhealthy servers to avoid repeated failures.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import time
from pathlib import Path

HEALTH_CACHE = Path(".mcp-health-cache.json")
DEFAULT_BACKOFF = 30
MAX_BACKOFF = 600
HEALTHY_TTL = 120


class MCPHealthMonitor:
    """Monitor MCP server health with caching and exponential backoff.

    Maintains a JSON cache of server health states. Healthy servers are
    cached for HEALTHY_TTL seconds. Unhealthy servers use exponential
    backoff (DEFAULT_BACKOFF * 2^(failures-1), capped at MAX_BACKOFF).
    """

    def __init__(self) -> None:
        self.state: dict = self._load()

    def _load(self) -> dict:
        """Load cached health state from disk."""
        if HEALTH_CACHE.exists():
            try:
                return json.loads(HEALTH_CACHE.read_text())
            except (json.JSONDecodeError, OSError):
                return {"servers": {}}
        return {"servers": {}}

    def _save(self) -> None:
        """Persist health state to disk."""
        try:
            HEALTH_CACHE.write_text(json.dumps(self.state, indent=2))
        except OSError:
            pass  # Non-critical — cache is best-effort

    def is_healthy(self, name: str) -> tuple[bool, str]:
        """Check if an MCP server is considered healthy.

        Returns:
            Tuple of (is_healthy, reason). Reasons:
            - "unknown": server has no cached state (treated as healthy)
            - "cached healthy": server was recently verified healthy
            - "backoff": server is unhealthy and in backoff window
            - "expired": cached state has expired (treated as healthy)
        """
        entry = self.state["servers"].get(name)
        if not entry:
            return True, "unknown"
        now = time.time()
        if entry["status"] == "healthy" and entry.get("expires_at", 0) > now:
            return True, "cached healthy"
        if entry["status"] == "unhealthy" and entry.get("next_retry_at", 0) > now:
            return False, "backoff"
        return True, "expired"

    def mark_healthy(self, name: str) -> None:
        """Record a server as healthy with a TTL-based expiry."""
        now = time.time()
        self.state["servers"][name] = {
            "status": "healthy",
            "checked_at": now,
            "expires_at": now + HEALTHY_TTL,
            "failure_count": 0,
        }
        self._save()

    def mark_unhealthy(self, name: str, error: str = "") -> None:
        """Record a server as unhealthy with exponential backoff.

        Each consecutive failure doubles the backoff period, starting
        from DEFAULT_BACKOFF seconds and capping at MAX_BACKOFF.
        """
        prev = self.state["servers"].get(name, {})
        count = prev.get("failure_count", 0) + 1
        backoff = min(DEFAULT_BACKOFF * (2 ** (count - 1)), MAX_BACKOFF)
        now = time.time()
        self.state["servers"][name] = {
            "status": "unhealthy",
            "checked_at": now,
            "failure_count": count,
            "next_retry_at": now + backoff,
            "last_error": str(error)[:500],
        }
        self._save()

    def get_status(self, name: str) -> dict | None:
        """Get raw cached status for a server, or None if unknown."""
        return self.state["servers"].get(name)

    def get_all_statuses(self) -> dict:
        """Get all cached server statuses."""
        return dict(self.state["servers"])

    def clear(self, name: str | None = None) -> None:
        """Clear cached state for one server, or all servers if name is None."""
        if name is None:
            self.state["servers"] = {}
        else:
            self.state["servers"].pop(name, None)
        self._save()
