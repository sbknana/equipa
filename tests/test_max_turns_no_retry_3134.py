#!/usr/bin/env python3
"""Task 3134 (F1 / F8 from the independent review of 3122).

F1: a streaming run that hit max turns returns success=False, so the retry
wrapper went on to ``is_retryable_error(errors, result_text)``, a substring
test. A RESULT block mentioning "timeout" relaunched a finished agent ten
times. Now a max-turns run is never retried by any wrapper, and the retry
classifiers read structured error fields (stderr, the CLI's error result),
never the agent's text.

F8: the non-streaming ``run_agent`` reported max turns as success=True; it
now reports it as not-success, like the streaming path.

Every agent is a stub; the tests count launches.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from equipa import agent_runner

# RESULT texts that each hold a marker the old substring test retried on.
MARKER_TEXTS = [
    "RESULT: partial\nBLOCKERS: the build hit a timeout",
    "RESULT: partial\nSUMMARY: fixed the HTTP 500 handler",
    "RESULT: partial\nDECISIONS: added rate limit handling",
    "RESULT: partial\nnote: connection pooling reworked",
    "RESULT: partial\nhandled 529 overloaded responses",
    "RESULT: partial\nECONNRESET and EPIPE are now logged",
]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(agent_runner, "get_retry_delay", lambda *_a, **_k: 0.0)


def _max_turns_result(text: str, errors: list[str] | None = None) -> dict:
    return {"success": False, "hit_max_turns": True, "result_text": text,
            "num_turns": 40, "duration": 1.0, "cost": 0.1,
            "errors": ["Agent hit max turns limit", *(errors or [])],
            "files_changed_set": []}


def _stub_streaming(monkeypatch, results):
    """Replace the streaming impl; returns the list of launches."""
    launches: list[int] = []
    queue = list(results)

    async def fake_impl(*_args, **_kwargs):
        launches.append(1)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(agent_runner, "_run_agent_streaming_impl", fake_impl)
    return launches


# --- F1: streaming retry wrapper --------------------------------------------


@pytest.mark.parametrize("text", MARKER_TEXTS)
def test_streaming_max_turns_is_launched_exactly_once(monkeypatch, text):
    launches = _stub_streaming(monkeypatch, [_max_turns_result(text)])

    result = asyncio.run(agent_runner.run_agent_streaming_with_retry(
        ["claude", "-p", "x"], max_retries=10))

    assert len(launches) == 1
    assert result["hit_max_turns"] is True and result["success"] is False


def test_streaming_max_turns_with_a_retryable_stderr_is_still_not_retried(
        monkeypatch):
    launches = _stub_streaming(monkeypatch, [_max_turns_result(
        "RESULT: partial", ["stderr: API Error: 503 Service Unavailable"])])

    asyncio.run(agent_runner.run_agent_streaming_with_retry(
        ["claude", "-p", "x"], max_retries=10, persistent_retry=True))

    assert len(launches) == 1


def test_run_agent_streaming_entry_point_launches_once(monkeypatch):
    launches = _stub_streaming(monkeypatch, [_max_turns_result(MARKER_TEXTS[0])])

    result = asyncio.run(agent_runner.run_agent_streaming(["claude", "-p", "x"]))

    assert len(launches) == 1 and result["hit_max_turns"] is True


@pytest.mark.parametrize("text", MARKER_TEXTS)
def test_markers_only_in_the_result_text_do_not_retry(monkeypatch, text):
    failed = {"success": False, "result_text": text, "num_turns": 3,
              "duration": 1.0, "cost": None,
              "errors": ["Agent monologue: 3 consecutive text-only messages "
                         "without tool use"],
              "early_terminated": True,
              "early_term_reason": "Agent monologue: 3 consecutive "
                                   "text-only messages without tool use"}
    launches = _stub_streaming(monkeypatch, [failed])

    asyncio.run(agent_runner.run_agent_streaming_with_retry(
        ["claude", "-p", "x"], max_retries=10))

    assert len(launches) == 1


def test_a_status_code_inside_a_number_is_not_an_api_error(monkeypatch):
    failed = {"success": False, "result_text": "", "num_turns": 1,
              "errors": ["Agent error: context used 15290 tokens, 1500 lines, "
                         "5029 cached"]}
    launches = _stub_streaming(monkeypatch, [failed])

    asyncio.run(agent_runner.run_agent_streaming_with_retry(
        ["claude", "-p", "x"], max_retries=10))

    assert len(launches) == 1


@pytest.mark.parametrize("error", [
    "stderr: API Error: 503 Service Unavailable",
    "Agent error: API Error: Connection error.",
    "Agent error: API Error: Request timeout",
    "stderr: read ECONNRESET",
])
def test_structured_api_errors_are_still_retried(monkeypatch, error):
    """Positive control: a real transient API error is retried."""
    failed = {"success": False, "result_text": "", "num_turns": 0,
              "errors": [error]}
    ok = {"success": True, "result_text": "RESULT: success", "num_turns": 2,
          "errors": []}
    launches = _stub_streaming(monkeypatch, [failed, ok])

    result = asyncio.run(agent_runner.run_agent_streaming_with_retry(
        ["claude", "-p", "x"], max_retries=10))

    assert len(launches) == 2 and result["success"] is True


# --- F1 / F8: non-streaming run_agent ----------------------------------------


def _stub_cli(monkeypatch, payloads: list[tuple[dict | None, str]]):
    """Fake CLI processes answering ``(json stdout, stderr)`` in turn."""
    launches: list[list[str]] = []
    queue = list(payloads)

    async def fake_exec(*argv, **_kwargs):
        launches.append(list(argv))
        stdout, stderr = queue.pop(0) if len(queue) > 1 else queue[0]
        body = json.dumps(stdout).encode() if stdout is not None else b""

        async def communicate():
            return body, stderr.encode()

        return SimpleNamespace(returncode=0 if stdout else 1,
                               communicate=communicate, kill=lambda: None)

    monkeypatch.setattr(agent_runner, "_agent_containment_supported",
                        lambda: False)
    monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec", fake_exec)
    return launches


def _cli_max_turns(text: str) -> dict:
    return {"type": "result", "subtype": "error_max_turns", "result": text,
            "num_turns": 40, "is_error": False,
            "usage": {"cache_read_input_tokens": 5029, "output_tokens": 1500}}


@pytest.mark.parametrize("text", MARKER_TEXTS)
def test_run_agent_reports_max_turns_as_not_success_and_launches_once(
        monkeypatch, text):
    launches = _stub_cli(monkeypatch, [(_cli_max_turns(text), "")])

    result = asyncio.run(agent_runner.run_agent(["claude", "-p", "x"],
                                                max_retries=10))

    assert len(launches) == 1
    assert result["success"] is False
    assert result["hit_max_turns"] is True
    assert "Agent hit max turns limit" in result["errors"]


def test_run_agent_error_with_token_counts_is_not_retried(monkeypatch):
    """The JSON stdout's numbers (5029, 1500) are not API status codes."""
    failed = {"type": "result", "subtype": "error_during_execution",
              "is_error": True, "result": "tool loop aborted",
              "usage": {"cache_read_input_tokens": 5029, "output_tokens": 1500}}
    launches = _stub_cli(monkeypatch, [(failed, "")])

    asyncio.run(agent_runner.run_agent(["claude", "-p", "x"], max_retries=10))

    assert len(launches) == 1


def test_run_agent_still_retries_an_api_error_result(monkeypatch):
    """Positive control: the CLI's is_error result carries the API error."""
    failed = {"type": "result", "is_error": True,
              "result": "API Error: 503 Service Unavailable"}
    ok = {"type": "result", "subtype": "success", "result": "RESULT: success",
          "num_turns": 1, "is_error": False}
    launches = _stub_cli(monkeypatch, [(failed, ""), (ok, "")])

    result = asyncio.run(agent_runner.run_agent(["claude", "-p", "x"],
                                                max_retries=10))

    assert len(launches) == 2 and result["success"] is True


# --- F1: the single-agent retry wrapper -------------------------------------


def test_run_agent_with_retries_does_not_relaunch_after_max_turns(monkeypatch):
    launches: list[int] = []

    async def fake_run_agent(_cmd, **_kwargs):
        launches.append(1)
        return _max_turns_result(MARKER_TEXTS[0])

    def no_task_check(_task_id):
        raise AssertionError("a max-turns run must return before this")

    monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_runner, "verify_task_updated", no_task_check)

    result, attempt = asyncio.run(agent_runner.run_agent_with_retries(
        ["claude", "-p", "x"], {"id": 1}, 5))

    assert len(launches) == 1 and attempt == 1
    assert result["hit_max_turns"] is True


# --- the classifiers ---------------------------------------------------------


@pytest.mark.parametrize("text, retryable", [
    ("API Error: 500 Internal Server Error", True),
    ("status 429 rate limited", True),
    ("used 1500 tokens", False),
    ("5029 cached tokens", False),
    ("id 4290", False),
])
def test_status_codes_match_as_whole_tokens(text, retryable):
    assert agent_runner.is_retryable_error(text, "") is retryable


@pytest.mark.parametrize("text, overloaded", [
    ("API Error: 529 overloaded_error", True),
    ("15290 tokens", False),
])
def test_overload_code_matches_as_a_whole_token(text, overloaded):
    assert agent_runner.is_overloaded_error(text, "") is overloaded


def test_structured_error_text_never_includes_the_result_text():
    result = {"result_text": "timeout 500 overloaded",
              "errors": ["Agent hit max turns limit", "stderr: boom"]}
    assert agent_runner._structured_error_text(result) == "stderr: boom"
