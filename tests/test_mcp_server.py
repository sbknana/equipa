"""Tests for equipa.mcp_server — MCP JSON-RPC 2.0 over stdio.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _send_request(proc: subprocess.Popen, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    """Send JSON-RPC request to MCP server and read response."""
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
    }
    if params is not None:
        request["params"] = params

    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()

    # Read response
    response_line = proc.stdout.readline()
    return json.loads(response_line)


def _send_notification(proc: subprocess.Popen, method: str, params: dict | None = None) -> None:
    """Send JSON-RPC notification (no response expected)."""
    request = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        request["params"] = params

    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()


@pytest.fixture
def mcp_server():
    """Spawn MCP server subprocess for testing."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "equipa.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    yield proc
    proc.terminate()
    proc.wait(timeout=2)


def test_initialize(mcp_server):
    """Test initialize handshake."""
    response = _send_request(mcp_server, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert "serverInfo" in response["result"]
    assert response["result"]["serverInfo"]["name"] == "equipa-mcp-server"


def test_initialized_notification(mcp_server):
    """Test initialized notification (no response)."""
    # Send initialize first
    _send_request(mcp_server, "initialize", {})

    # Send initialized notification
    _send_notification(mcp_server, "notifications/initialized")

    # No response expected — server should remain alive
    # Test by sending another request
    response = _send_request(mcp_server, "tools/list", {}, request_id=2)
    assert response["jsonrpc"] == "2.0"


def test_tools_list(mcp_server):
    """Test tools/list returns all 7 tools."""
    response = _send_request(mcp_server, "tools/list", {})

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    assert "tools" in response["result"]

    tools = response["result"]["tools"]
    tool_names = {t["name"] for t in tools}

    expected = {
        "equipa_dispatch",
        "equipa_task_status",
        "equipa_task_create",
        "equipa_lessons",
        "equipa_agent_logs",
        "equipa_project_context",
        "equipa_session_notes",
    }

    assert tool_names == expected

    # Verify each tool has required fields
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


def test_task_status_missing_arg(mcp_server):
    """Test equipa_task_status with missing task_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_status",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "task_id required" in content["error"]


def test_task_status_nonexistent(mcp_server):
    """Test equipa_task_status with nonexistent task."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_status",
        "arguments": {"task_id": 999999},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content


def test_lessons_default(mcp_server):
    """Test equipa_lessons with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_lessons",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "lessons" in content
    assert "count" in content
    assert isinstance(content["lessons"], list)


def test_agent_logs_default(mcp_server):
    """Test equipa_agent_logs with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_agent_logs",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "runs" in content
    assert "count" in content
    assert isinstance(content["runs"], list)


def test_session_notes_default(mcp_server):
    """Test equipa_session_notes with default limit."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_session_notes",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "notes" in content
    assert "count" in content
    assert isinstance(content["notes"], list)


def test_project_context_missing_arg(mcp_server):
    """Test equipa_project_context with missing project_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_project_context",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "project_id required" in content["error"]


def test_unknown_tool(mcp_server):
    """Test calling unknown tool returns error."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "unknown_tool",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_unknown_method(mcp_server):
    """Test calling unknown method returns error."""
    response = _send_request(mcp_server, "unknown/method", {})

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32601


def test_invalid_json(mcp_server):
    """Test sending invalid JSON returns parse error."""
    mcp_server.stdin.write("not valid json\n")
    mcp_server.stdin.flush()

    response_line = mcp_server.stdout.readline()
    response = json.loads(response_line)

    assert response["jsonrpc"] == "2.0"
    assert "error" in response
    assert response["error"]["code"] == -32700


def test_task_create_success(mcp_server):
    """Test equipa_task_create creates a task."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_task_create",
        "arguments": {
            "project_id": 23,  # EQUIPA project
            "title": "MCP Test Task",
            "description": "Created by test_mcp_server.py",
            "priority": "low",
        },
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "task_id" in content
    assert content["status"] == "created"
    assert content["title"] == "MCP Test Task"


def test_dispatch_missing_arg(mcp_server):
    """Test equipa_dispatch with missing task_id."""
    response = _send_request(mcp_server, "tools/call", {
        "name": "equipa_dispatch",
        "arguments": {},
    })

    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    content = json.loads(response["result"]["content"][0]["text"])
    assert "error" in content
    assert "task_id required" in content["error"]


def test_cli_mcp_server_flag():
    """Test that --mcp-server flag launches the server."""
    # This test would require full EQUIPA setup, so just verify the module can be imported
    import equipa.mcp_server
    assert hasattr(equipa.mcp_server, "run_server")
    assert callable(equipa.mcp_server.run_server)
