"""Tests for equipa.redact (review finding sandbox-12).

Copyright 2026 Forgeborn

All secret values below are obviously fake sentinels.
"""

from __future__ import annotations

import json

import pytest

from equipa.redact import REDACTED, redact_secrets, redacted_preview

FAKE_PW = "FAKEpw-not-real-1234"
FAKE_GH = "ghp_" + "FAKE0000000000000000000000000000000A"
FAKE_GHO = "gho_" + "FAKE0000000000000000000000000000000B"
FAKE_PAT = "github_pat_" + "FAKE000000000000000000_1111111111111111"
FAKE_SK = "sk-ant-api03-" + "FAKEFAKEFAKEFAKEFAKE0000"
FAKE_BEARER = "eyFAKE.not-a-real.jwt0000"


@pytest.mark.parametrize("command, secret", [
    (f"PGPASSWORD={FAKE_PW} psql -h db -U app", FAKE_PW),
    (f"PGPASSWORD='{FAKE_PW}' psql -h db", FAKE_PW),
    (f'PGPASSWORD="{FAKE_PW} with space" psql', "with space"),
    (f"export DB_PASSWORD={FAKE_PW}", FAKE_PW),
    (f"GITHUB_TOKEN={FAKE_PW} gh pr list", FAKE_PW),
    (f"MY_SERVICE_TOKEN={FAKE_PW} ./run.sh", FAKE_PW),
    (f"ANTHROPIC_API_KEY={FAKE_PW} claude -p hi", FAKE_PW),
    (f"CLIENT_SECRET={FAKE_PW} node app.js", FAKE_PW),
    (f"psql postgresql://app:{FAKE_PW}@db.example.invalid:5432/x", FAKE_PW),
    (f"git clone https://x-access-token:{FAKE_PW}@example.invalid/r.git",
     FAKE_PW),
    (f'curl -H "Authorization: Bearer {FAKE_BEARER}" https://example.invalid',
     FAKE_BEARER),
    (f"curl -H 'Authorization: token {FAKE_PW}' https://example.invalid",
     FAKE_PW),
    (f'curl --oauth2-bearer x -H "bearer {FAKE_BEARER}"', FAKE_BEARER),
    (f"echo {FAKE_SK}", FAKE_SK),
    (f"echo {FAKE_GH}", FAKE_GH),
    (f"echo {FAKE_GHO}", FAKE_GHO),
    (f"echo {FAKE_PAT}", FAKE_PAT),
])
def test_secret_shapes_are_redacted(command: str, secret: str) -> None:
    out = redact_secrets(command)
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize("command", [
    "git status",
    "ls -la /tmp",
    "timeout 60 python3 -m pytest -q -p no:cacheprovider",
    'grep -n "def " equipa/agent_runner.py',
    "export PATH=/usr/local/bin:$PATH",
    "git log --format=%H -5",
    "curl -s https://example.invalid/api/v1/items",
    "python3 -c 'print(sorted(xs, key=len))'",
    "task-3120 desk-lamp sort_key=len",
    "echo $HOME && cd /srv/app && make build",
    "ssh deploy@example.invalid uptime",
    "",
])
def test_ordinary_commands_pass_through_unchanged(command: str) -> None:
    assert redact_secrets(command) == command


def test_json_rendered_tool_input_is_redacted() -> None:
    """agent_actions stores json.dumps(tool_input): quotes are escaped."""
    tool_input = {"command": f'PGPASSWORD="{FAKE_PW}" psql -c "select 1"',
                  "description": "query"}
    rendered = json.dumps(tool_input)
    out = redact_secrets(rendered)
    assert FAKE_PW not in out
    assert "psql" in out and "select 1" in out


def test_redaction_is_idempotent() -> None:
    once = redact_secrets(f"PGPASSWORD={FAKE_PW} psql {FAKE_GH}")
    assert redact_secrets(once) == once


def test_non_string_is_returned_unchanged() -> None:
    assert redact_secrets(None) is None  # type: ignore[arg-type]


def test_preview_redacts_before_truncating() -> None:
    """A token cut by the preview limit must not leak its prefix."""
    command = "x" * 190 + " " + FAKE_GH
    preview = redacted_preview(command, 200)
    assert len(preview) <= 200
    assert "ghp_FAKE" not in preview
    naive = redact_secrets(command[:200])
    assert "ghp_FAKE" in naive  # why the lookahead window exists


def test_preview_of_large_input_is_bounded() -> None:
    preview = redacted_preview("a" * 5_000_000, 200)
    assert preview == "a" * 200
