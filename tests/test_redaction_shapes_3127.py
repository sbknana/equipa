#!/usr/bin/env python3
"""Task 3127 / review findings P2A-03, P2A-08, P2A-04: persisted redaction.

Every credential shape the review found leaking is sent by a fake agent CLI
as a real tool_use, run through the streaming monitor, persisted with
``bulk_log_agent_actions`` into the (test) TheForge DB and read back from
``agent_actions.tool_input_preview``: the path later agents can read
through the theforge MCP server.

P2A-08: an assignment on the second or later line of a command, after a tab
or after ``\\r\\n``, reached the preview in clear, because the preview was
redacted after ``json.dumps`` had turned the line break into ``\\n``.
P2A-04: the unsalted hash of the unredacted input next to the preview let
anyone recover a weak redacted password offline.

All values are fake sentinels.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import zlib
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import agent_runner
from equipa.db import bulk_log_agent_actions, db_conn
from equipa.redact import (
    REDACTED,
    redact_secrets,
    redact_tool_input,
    redacted_json_preview,
)

FAKE_CLI = '''import os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
for line in open(os.path.join(here, "stream.jsonl"), encoding="utf-8"):
    sys.stdout.write(line)
    sys.stdout.flush()
    time.sleep(0.01)
'''

FINAL = {"type": "result", "subtype": "success", "result": "RESULT: success",
         "num_turns": 2, "total_cost_usd": 0.0}

JWT = ("eyJ" + "hbGciOiJGQUtFIn0" + ".eyJGQUtFIjoiMzEyNyJ9"
       + ".RkFLRVNJRzMxMjdmYWtl")

# (id, tool, tool input, secret fragments that must not be persisted)
SHAPES = [
    ("libpq-dsn", "Bash", {"command": "psql 'host=db.invalid user=app "
                                      "password=FAKEdsn3127 dbname=x'"},
     ["FAKEdsn3127"]),
    ("url-password-with-slash", "Bash",
     {"command": "psql postgres://app:FAKE/sl+3127@db.example.invalid/x"},
     ["FAKE/sl+3127", "sl+3127"]),
    ("url-password-with-at", "Bash",
     {"command": "psql postgres://app:FAKEat3127@pw9@db.example.invalid/x"},
     ["FAKEat3127", "pw9@db"]),
    ("mysql-p-attached", "Bash",
     {"command": "mysql -uapp -pFAKEmy3127 -h db.invalid x"}, ["FAKEmy3127"]),
    ("mysql-p-single-quoted", "Bash",
     {"command": "mysql -u root -p'FAKEmysq3127' appdb"}, ["FAKEmysq3127"]),
    ("mysql-p-double-quoted", "Bash",
     {"command": "mysqldump -u root -p\"FAKEmydq3127\" appdb"},
     ["FAKEmydq3127"]),
    ("sshpass-p", "Bash",
     {"command": "sshpass -p FAKEssh3127 ssh deploy@db.invalid uptime"},
     ["FAKEssh3127"]),
    ("curl-user-password", "Bash",
     {"command": "curl -s -u admin:FAKEcurl3127 https://x.invalid/api"},
     ["FAKEcurl3127"]),
    ("password-flag-space", "Bash",
     {"command": "mycli --password FAKEflag3127 -h db.invalid"},
     ["FAKEflag3127"]),
    ("password-flag-equals", "Bash",
     {"command": "some-client --db-password=FAKEeq3127 run"}, ["FAKEeq3127"]),
    ("lower-case-assignment", "Bash",
     {"command": "db_password=FAKElow3127 ./run.sh"}, ["FAKElow3127"]),
    ("mixed-case-pwd", "Bash",
     {"command": "connect Pwd=FAKEpwd3127;Server=db.invalid"}, ["FAKEpwd3127"]),
    ("spaced-api-key", "Bash",
     {"command": "echo \"API_KEY = 'FAKEspace3127'\" | tee cfg.py"},
     ["FAKEspace3127"]),
    ("json-api-key-field", "Bash",
     {"command": "echo '{\"api_key\": \"FAKEjson3127\"}' | jq ."},
     ["FAKEjson3127"]),
    ("yaml-password", "Bash",
     {"command": "printf 'db:\\n  password: FAKEyaml3127\\n'"},
     ["FAKEyaml3127"]),
    ("x-api-key-header", "Bash",
     {"command": "curl -H 'X-Api-Key: FAKEhdr3127' https://x.invalid"},
     ["FAKEhdr3127"]),
    ("aws-access-key-id", "Bash",
     {"command": "aws configure set aws_access_key_id AKIAFAKE3127FAKE3127"},
     ["AKIAFAKE3127FAKE3127"]),
    ("jwt", "Bash",
     {"command": f"curl -H 'X-Session: {JWT}' https://x.invalid"},
     ["RkFLRVNJRzMxMjdmYWtl", "eyJGQUtFIjoiMzEyNyJ9"]),
    ("pem-private-key", "Bash",
     {"command": "printf '-----BEGIN RSA PRIVATE KEY-----\nMIIFAKEpem3127"
                 "AAAA\n-----END RSA PRIVATE KEY-----\n'"},
     ["MIIFAKEpem3127"]),
    # P2A-08: after a newline, a tab or CRLF inside the raw command.
    ("newline-then-pgpassword", "Bash",
     {"command": "cd /srv/app\nPGPASSWORD=FAKEnl3127 psql -c 'select 1'"},
     ["FAKEnl3127"]),
    ("tab-then-token", "Bash",
     {"command": "env\tGITHUB_TOKEN=FAKEtab3127 gh api user"},
     ["FAKEtab3127"]),
    ("crlf-then-db-password", "Bash",
     {"command": "cd x\r\nexport_it=1\r\nDB_PASSWORD=FAKEcr3127 make"},
     ["FAKEcr3127"]),
    ("newline-then-lower-password", "Bash",
     {"command": "true\npassword=FAKEnlpw3127 ./login"}, ["FAKEnlpw3127"]),
    ("write-dotenv-content", "Write",
     {"file_path": "/tmp/fake-project/.env",
      "content": "DEBUG=1\nSTRIPE_SECRET=FAKEwrite3127\n"}, ["FAKEwrite3127"]),
]


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


def _persisted_previews(tmp_path: Path, tool: str, tool_input: dict,
                        task_id: int) -> tuple[dict, list[str], list[str]]:
    """Run one tool_use through the streaming monitor and the DB writer."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "fake_claude.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": tool, "input": tool_input}]}},
        FINAL]
    (bin_dir / "stream.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    output: list[str] = []

    result = asyncio.run(agent_runner._run_agent_streaming_impl(
        [sys.executable, str(fake)], role="developer", timeout=60,
        max_turns=40, output=output, project_dir=str(project)))
    bulk_log_agent_actions(result["action_log"], task_id=task_id, run_id=None,
                           cycle=1, role="developer")
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT tool_input_preview, input_hash FROM agent_actions "
            "WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
    return result, [row[0] for row in rows], output


@pytest.mark.parametrize(
    "tool, tool_input, secrets",
    [pytest.param(tool, tool_input, secrets, id=shape_id)
     for shape_id, tool, tool_input, secrets in SHAPES])
def test_shape_is_redacted_in_the_persisted_preview(
        tmp_path, tool, tool_input, secrets):
    task_id = 3127_000 + zlib.crc32(json.dumps(tool_input).encode()) % 100_000
    result, previews, output = _persisted_previews(
        tmp_path, tool, tool_input, task_id)

    assert previews, "nothing was persisted"
    persisted = "\n".join(previews)
    for secret in secrets:
        assert secret not in persisted, persisted
        assert secret not in "\n".join(output)
    assert REDACTED in persisted


@pytest.mark.parametrize(
    "tool_input, secrets",
    [pytest.param(tool_input, secrets, id=shape_id)
     for shape_id, _tool, tool_input, secrets in SHAPES])
def test_shape_is_redacted_by_redact_tool_input(tool_input, secrets):
    preview, _digest = redact_tool_input(tool_input, 200)
    for secret in secrets:
        assert secret not in preview
    assert REDACTED in preview


@pytest.mark.parametrize(
    "tool_input",
    [pytest.param(tool_input, id=shape_id)
     for shape_id, _tool, tool_input, _secrets in SHAPES])
def test_redaction_of_every_shape_is_idempotent(tool_input):
    once = redacted_json_preview(tool_input, 400)
    assert redact_secrets(once) == once
    for value in tool_input.values():
        assert redact_secrets(redact_secrets(value)) == redact_secrets(value)


@pytest.mark.parametrize("command", [
    "mkdir -p src/pkg && pytest -q -p no:cacheprovider",
    "mysql -h db.invalid -P 3306 -u app -p appdb",
    "python3 -c 'print(sorted(xs, key=len))'",
    "grep -n max_tokens: config.yaml",
    "echo 'max_tokens: 4096'",
    "curl -s https://example.invalid/users/@me",
    "git clone https://example.invalid/org/repo.git",
    "ssh deploy@example.invalid uptime",
    "ssh -p 2222 deploy@example.invalid uptime",
    "curl -s -u admin https://example.invalid/prompts-for-password",
    "curl --upload-file notes.txt https://example.invalid/u",
    "echo a.b.c.d.e.f && ls -la",
])
def test_ordinary_commands_are_unchanged(command):
    assert redact_secrets(command) == command


def test_long_name_runs_are_redacted_in_linear_time():
    """No quadratic backtracking on long runs of name characters."""
    blob = "a.b-" * 50_000 + " password=FAKElin3127"
    started = time.monotonic()
    out = redact_secrets(blob)
    assert time.monotonic() - started < 2.0
    assert "FAKElin3127" not in out


# --- P2A-04: the persisted hash is no oracle for the redacted password -------

WEAK_PASSWORD = "hunter2"
GUESSES = ["123456", "password", "hunter1", WEAK_PASSWORD, "letmein"]


def test_persisted_hash_cannot_confirm_a_guessed_password(tmp_path):
    tool_input = {"command": f"PGPASSWORD={WEAK_PASSWORD} psql -h db.invalid "
                             f"-c 'select 1'"}
    task_id = 3127_900_001
    _result, previews, _output = _persisted_previews(
        tmp_path, "Bash", tool_input, task_id)
    with db_conn() as conn:
        stored_hash = conn.execute(
            "SELECT input_hash FROM agent_actions WHERE task_id = ?",
            (task_id,)).fetchone()[0]
    preview = previews[0]
    assert WEAK_PASSWORD not in preview

    # The reviewer's probe: substitute guesses for the redacted span and
    # compare against the stored hash of the (short) input.
    for guess in GUESSES:
        candidate = preview.replace(
            "PGPASSWORD=[REDACTED]", f"PGPASSWORD={guess}", 1)
        candidate_hash = hashlib.sha256(candidate.encode()).hexdigest()
        assert candidate_hash != stored_hash, f"hash confirmed guess {guess!r}"
    raw_hash = hashlib.sha256(json.dumps(tool_input).encode()).hexdigest()
    assert stored_hash != raw_hash


def test_secret_free_input_hash_is_unchanged():
    tool_input = {"command": "ls -la /tmp", "description": "list"}
    _preview, digest = redact_tool_input(tool_input, 200)
    assert digest == hashlib.sha256(json.dumps(tool_input).encode()).hexdigest()


def test_equal_inputs_hash_equal_and_different_inputs_differ():
    first = {"command": "PGPASSWORD=FAKEa3127 psql -c 'select 1'"}
    same = dict(first)
    other = {"command": "PGPASSWORD=FAKEa3127 psql -c 'select 2'"}
    assert redact_tool_input(first, 200)[1] == redact_tool_input(same, 200)[1]
    assert redact_tool_input(first, 200)[1] != redact_tool_input(other, 200)[1]
