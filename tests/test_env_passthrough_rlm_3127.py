#!/usr/bin/env python3
"""Task 3127 / review LOW findings P2A-09 and P2A-07.

P2A-09: ``agent_env_passthrough`` gated only the exact name
ANTHROPIC_API_KEY. ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL and
CLAUDE_CODE_USE_BEDROCK/_VERTEX (API billing or another endpoint) now need
``agent_allow_api_key`` too, and any other credential-shaped name
(DATABASE_URL, PGPASSWORD, *_TOKEN, ...) needs ``agent_allow_credentials``.

P2A-07: the RLM decomposition ``claude -p`` calls inherited the full
orchestrator environment; they now get the allowlisted agent env.

All values are fake sentinels.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import subprocess

import pytest

import equipa.config as equipa_config
from equipa import rlm_decompose
from equipa.env_loader import build_agent_env

SOURCE = {
    "PATH": "/usr/bin:/bin",
    "ANTHROPIC_API_KEY": "sk-FAKE-3127-passthrough",
    "ANTHROPIC_AUTH_TOKEN": "FAKE-3127-auth-token",
    "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "DATABASE_URL": "postgresql://fake:FAKE-3127@db.invalid/x",
    "PGPASSWORD": "FAKE-3127-pg",
    "NPM_TOKEN": "FAKE-3127-npm",
    "EQUIPA_FAKE_PLAIN": "plain-value",
}
BILLING = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
           "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"]
CREDENTIALS = ["DATABASE_URL", "PGPASSWORD", "NPM_TOKEN"]


def _env(**config) -> dict[str, str]:
    config.setdefault("agent_env_passthrough",
                      [*BILLING, *CREDENTIALS, "EQUIPA_FAKE_PLAIN"])
    return build_agent_env(config, environ=SOURCE)


@pytest.mark.parametrize("name", BILLING)
def test_billing_and_endpoint_names_need_agent_allow_api_key(name, caplog):
    with caplog.at_level(logging.WARNING, logger="equipa.env_loader"):
        env = _env()
    assert name not in env
    assert any(name in record.message and "agent_allow_api_key" in record.message
               for record in caplog.records)
    assert _env(agent_allow_api_key=True)[name] == SOURCE[name]


@pytest.mark.parametrize("name", CREDENTIALS)
def test_credential_names_need_agent_allow_credentials(name, caplog):
    with caplog.at_level(logging.WARNING, logger="equipa.env_loader"):
        env = _env()
    assert name not in env
    assert any(name in record.message and "agent_allow_credentials"
               in record.message for record in caplog.records)
    assert name not in _env(agent_allow_api_key=True)
    assert _env(agent_allow_credentials=True)[name] == SOURCE[name]


def test_plain_names_still_pass_through_without_opt_ins():
    env = _env()
    assert env["EQUIPA_FAKE_PLAIN"] == "plain-value"
    assert env["PATH"] == SOURCE["PATH"]


def test_opt_ins_must_be_literally_true():
    env = _env(agent_allow_api_key="yes", agent_allow_credentials=1)
    assert not set(BILLING + CREDENTIALS) & set(env)


# --- P2A-07 ------------------------------------------------------------------


@pytest.fixture
def captured_runs(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})
    for name, value in SOURCE.items():
        if name != "PATH":
            monkeypatch.setenv(name, value)
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": "ok"}',
                                           stderr="")

    monkeypatch.setattr(rlm_decompose.subprocess, "run", fake_run)
    return calls


def _assert_scrubbed(kwargs: dict) -> None:
    env = kwargs.get("env")
    assert env is not None, "inherited the orchestrator environment"
    assert not (set(BILLING + CREDENTIALS) & set(env))
    assert "PATH" in env


def test_rlm_sub_query_gets_the_agent_env(captured_runs, tmp_path):
    rlm_decompose._run_sub_query("prompt", {"a.py": "x = 1"}, "fake-model",
                                 str(tmp_path), "")
    assert len(captured_runs) == 1
    _assert_scrubbed(captured_runs[0])


def test_rlm_outer_agent_gets_the_agent_env(captured_runs, tmp_path):
    rlm_decompose._call_outer_agent("prompt", "fake-model", str(tmp_path))
    assert len(captured_runs) == 1
    _assert_scrubbed(captured_runs[0])
