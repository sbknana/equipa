#!/usr/bin/env python3
"""Task 3134 IR-05 / IR-06: bounded redaction work and more credential shapes.

IR-05: redaction runs on the orchestrator's event loop for every tool call.
The review measured 37.7 s for a 3.6 MB MultiEdit-shaped input and 90 ms for
one 2.2 KB ``Pwd_`` run. Every tool-input shape below must finish in under
0.3 s at about 4 MB.

IR-06: each credential shape the review found leaking gets a row with a fake
value, checked on the text path and on the persisted preview path.

Every secret here is an obviously fake sentinel.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from equipa.redact import (
    MAX_REDACT_INPUT,
    REDACTED,
    TRUNCATED_MARKER,
    redact_secrets,
    redact_tool_input,
    redacted_json_preview,
)

FOUR_MB = 4 * 1024 * 1024
TIME_LIMIT_SECONDS = 0.3


def _best_of_three(func, *args) -> tuple[float, object]:
    """Shortest of three runs, so one scheduler hiccup cannot fail the test."""
    best, result = float("inf"), None
    for _ in range(3):
        started = time.perf_counter()
        result = func(*args)
        best = min(best, time.perf_counter() - started)
    return best, result


def _multiedit(unit: str, count: int) -> dict:
    return {"file_path": "/srv/app/x.py", "edits": [
        {"old_string": unit, "new_string": unit} for _ in range(count)]}


# --- IR-05: bounded work -----------------------------------------------------

HEAVY_UNITS = {
    # The review's shapes: long name runs full of keywords.
    "p-run-password": "p" * 1200 + "password=FAKEheavy3134",
    "Pwd-run": "Pwd_" * 300 + "=FAKEheavy3134",
    "PWD-env-run": "PWD_" * 300 + "=FAKEheavy3134",
    "PASSWORD-env-run": "PASSWORD" * 150 + "=FAKEheavy3134",
    "mysql-repeat": "mysql " * 200 + "-pFAKEheavy3134",
}


@pytest.mark.parametrize("unit", HEAVY_UNITS.values(), ids=HEAVY_UNITS.keys())
def test_four_mb_multiedit_of_heavy_strings_is_fast(unit):
    tool_input = _multiedit(unit, FOUR_MB // (2 * len(unit)))
    assert len(json.dumps(tool_input)) > 3_500_000

    elapsed, (preview, digest) = _best_of_three(redact_tool_input,
                                                tool_input, 200)

    assert elapsed < TIME_LIMIT_SECONDS, f"{elapsed:.3f}s"
    assert "FAKEheavy3134" not in preview
    assert len(preview) <= 200 and len(digest) == 64


@pytest.mark.parametrize("shape", [
    "write", "command", "many-tiny-edits", "many-assignments", "one-long-run",
])
def test_four_mb_ordinary_shapes_are_fast(shape):
    tool_input = {
        "write": {"file_path": "/srv/app/big.py",
                  "content": "x = 1  # line\n" * (FOUR_MB // 14)},
        "command": {"command": "echo " + "a" * FOUR_MB},
        "many-tiny-edits": _multiedit("a", FOUR_MB // 40),
        "many-assignments": {"lines": [f"K{i}=v" for i in range(FOUR_MB // 12)]},
        "one-long-run": {"command": "Pwd_" * (FOUR_MB // 4) + "=FAKEheavy3134"},
    }[shape]

    elapsed, (preview, _digest) = _best_of_three(redact_tool_input,
                                                 tool_input, 200)

    assert elapsed < TIME_LIMIT_SECONDS, f"{elapsed:.3f}s"
    assert "FAKEheavy3134" not in preview


def test_one_long_keyword_run_is_linear():
    run = "Pwd_" * 50_000 + "=FAKErun3134"  # 200 KB, one name run
    elapsed, out = _best_of_three(redact_secrets, run[:MAX_REDACT_INPUT])
    assert elapsed < TIME_LIMIT_SECONDS, f"{elapsed:.3f}s"
    assert "FAKErun3134" not in out


def test_redact_secrets_caps_its_input():
    text = "PGPASSWORD=FAKEcap3134 " + "a" * (MAX_REDACT_INPUT * 4)

    out = redact_secrets(text)

    assert len(out) <= MAX_REDACT_INPUT
    assert out.endswith(TRUNCATED_MARKER)
    assert "FAKEcap3134" not in out
    assert redact_secrets(out) == out  # still idempotent


def test_a_secret_past_many_shrinking_strings_is_never_shown():
    """Work limit reached before the preview filled: fail closed, no leak."""
    unit = "p" * 1200 + "password=FAKEheavy3134"
    tool_input = {"edits": [unit] * 50 + ["PGPASSWORD=FAKElate3134"]}

    preview = redacted_json_preview(tool_input, 5000)

    assert "FAKEheavy3134" not in preview
    assert "FAKElate3134" not in preview


def test_bounded_walk_keeps_the_rendering_prefix_and_hash():
    """Secret-free input: the preview and hash equal the full rendering's."""
    tool_input = {"edits": [{"old_string": f"line {i}", "new_string": f"row {i}"}
                            for i in range(20_000)]}
    rendered = json.dumps(tool_input)

    preview, digest = redact_tool_input(tool_input, 200)

    assert preview == rendered[:200]
    assert digest == hashlib.sha256(rendered.encode()).hexdigest()


def test_secret_straddling_the_preview_edge_is_redacted_after_shrinking():
    """Earlier strings that redact shorter must not pull a cut into view."""
    tool_input = {"a": "PGPASSWORD=" + "x" * 3000,
                  "b": "echo ghp_FAKEstraddle3134abcdefghijklmnop"}

    preview = redacted_json_preview(tool_input, 200)

    assert "ghp_" not in preview.replace(f"ghp_{REDACTED}", "")
    assert "FAKEstraddle3134" not in preview


# --- IR-06: credential shapes ------------------------------------------------

# (id, text, fake secret that must not survive)
SHAPES = [
    ("docker-login-p", "docker login -u ci -p FAKEdock3134 registry.invalid",
     "FAKEdock3134"),
    ("podman-login-p", "podman login --username ci -p FAKEpod3134 r.invalid",
     "FAKEpod3134"),
    ("docker-global-opt", "docker --config /tmp/c login -p 'FAKEdq 3134' r.invalid",
     "FAKEdq 3134"),
    ("db-pass", "DB_PASS=FAKEdbpass3134 ./migrate", "FAKEdbpass3134"),
    ("pass", "PASS=FAKEpass3134 ./run", "FAKEpass3134"),
    ("smtp-pass-export", "export SMTP_PASS='FAKEsmtp3134'", "FAKEsmtp3134"),
    ("htpasswd-b", "htpasswd -b /etc/nginx/.htpasswd admin FAKEhtp3134",
     "FAKEhtp3134"),
    ("htpasswd-cb", "htpasswd -cbB users.db admin FAKEhtpc3134 && ls",
     "FAKEhtpc3134"),
    ("htpasswd-nb", "htpasswd -nb admin FAKEhtpn3134", "FAKEhtpn3134"),
    ("cookie-header", "curl -H 'Cookie: sessionid=FAKEcookie3134; csrf=x' u",
     "FAKEcookie3134"),
    ("set-cookie-header", "Set-Cookie: auth=FAKEsetcookie3134; HttpOnly",
     "FAKEsetcookie3134"),
    ("slack-bot", "echo xoxb-0000000000-FAKEslack3134abcd", "FAKEslack3134"),
    ("slack-user", "SLACK=xoxp-1111-2222-FAKEslackp3134", "FAKEslackp3134"),
    ("google-api-key", "curl 'https://maps.invalid/?key=AIzaFAKEgoogle3134"
     "abcdefghijklmnopqrstu'", "FAKEgoogle3134"),
    ("gitlab-pat", "git clone https://oauth2:glpat-FAKEgitlab3134abcd@gl.invalid/r",
     "FAKEgitlab3134"),
    ("gitlab-pat-bare", "echo glpat-FAKEgitlabb3134xyz", "FAKEgitlabb3134"),
    ("pgpass-line", "echo 'db.invalid:5432:*:app:FAKEpgpass3134' > ~/.pgpass",
     "FAKEpgpass3134"),
    ("pgpass-wildcard-port", "h:*:appdb:app:FAKEpgw3134", "FAKEpgw3134"),
    ("curl-proxy-user", "curl --proxy-user proxyuser:FAKEproxy3134 https://x.invalid",
     "FAKEproxy3134"),
    ("curl-U", "curl -U proxyuser:FAKEproxyU3134 https://x.invalid",
     "FAKEproxyU3134"),
    ("az-login-p", "az login -u me@example.invalid -p FAKEaz3134",
     "FAKEaz3134"),
    ("az-sp-p", "az login --service-principal -u app -p FAKEazsp3134 --tenant t",
     "FAKEazsp3134"),
    ("sqlcmd-P", "sqlcmd -S db.invalid -U sa -P FAKEsql3134 -Q 'select 1'",
     "FAKEsql3134"),
    ("redis-cli-a", "redis-cli -h cache.invalid -a FAKEredis3134 ping",
     "FAKEredis3134"),
    ("redis-cli-pass", "redis-cli --pass FAKEredisp3134 ping", "FAKEredisp3134"),
]


@pytest.mark.parametrize("text, secret",
                         [pytest.param(t, s, id=i) for i, t, s in SHAPES])
def test_shape_is_redacted_as_text(text, secret):
    out = redact_secrets(text)

    assert secret not in out
    assert REDACTED in out
    assert redact_secrets(out) == out


@pytest.mark.parametrize("text, secret",
                         [pytest.param(t, s, id=i) for i, t, s in SHAPES])
def test_shape_is_redacted_on_the_persisted_preview_path(text, secret):
    preview, _digest = redact_tool_input({"command": text,
                                          "description": "x"}, 200)

    assert secret not in preview
    assert REDACTED in preview


@pytest.mark.parametrize("text, secret",
                         [pytest.param(t, s, id=i) for i, t, s in SHAPES])
def test_shape_is_redacted_after_a_json_line_break(text, secret):
    preview = redacted_json_preview({"command": f"cd /srv/app\n{text}"}, 400)

    assert secret not in preview


@pytest.mark.parametrize("command", [
    "BYPASS=1 make test",
    "COMPASS_DIR=/opt/compass ls",
    "ssh -p 2222 deploy@example.invalid uptime",
    "docker run -p 8080:80 nginx",
    "docker ps -a",
    "pytest -q -p no:cacheprovider",
    "grep -rn cookie src/",
    "echo 12:30:45",
    "redis-cli ping",
    "az group list -o table",
    "htpasswd -v users.db admin",
    "mysql -h db.invalid -P 3306 -u app -p appdb",
])
def test_ordinary_commands_stay_unchanged(command):
    assert redact_secrets(command) == command
