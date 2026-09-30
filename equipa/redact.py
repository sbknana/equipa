"""Write-time secret redaction for agent telemetry and logs (sandbox-12).

Copyright 2026 Forgeborn

Agent tool input (Bash commands above all) is persisted to
``agent_actions.tool_input_preview`` and echoed into orchestrator log lines.
Commands routinely carry credentials inline (``PGPASSWORD=... psql``,
``postgres://user:pw@host``, ``curl -H "Authorization: Bearer ..."``), so
every such string passes through :func:`redact_secrets` before it is stored
or logged.

The patterns are deliberately shape-based and conservative: they replace the
secret VALUE with ``[REDACTED]`` and keep the surrounding text, so the
preview still shows what the agent did. Ordinary commands pass through
unchanged. The input may be raw text or a ``json.dumps`` rendering of a tool
input, where quotes appear escaped (``\\"``); both forms are handled.

Standard library only.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# How far past a preview limit the redactor looks, so a secret that starts
# inside the preview but ends after it is still recognised whole.
_PREVIEW_LOOKAHEAD = 1024

# A variable value after ``NAME=``: a double-quoted string (raw ``"`` or the
# JSON-escaped ``\"``; the closing quote may be missing when the text was
# truncated), a single-quoted string, or a bare word.
_VALUE = (
    r"(?:\\?\"(?:[^\"\\]|\\(?!\"))*(?:\\?\")?"
    r"|'[^']*'?"
    r"|[^\s'\"`\\;&|,)}\]]+)"
)

# Names that hold credentials by convention: anything containing SECRET,
# PASSWORD or PASSWD (PGPASSWORD, DB_PASSWORD, CLIENT_SECRET_ID, ...), and
# anything ending in _TOKEN or _KEY (GITHUB_TOKEN, ANTHROPIC_API_KEY, ...).
# Upper-case only: environment variables are upper-case, and matching
# lower-case identifiers would mangle ordinary code such as ``sort_key=len``.
_SECRET_NAME = (
    r"(?:[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD)[A-Z0-9_]*"
    r"|[A-Z0-9_]*_(?:TOKEN|KEY)|TOKEN)"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub tokens: classic/OAuth/user/server/refresh and fine-grained PATs.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
     REDACTED),
    # Anthropic / OpenAI style API keys (sk-..., sk-ant-..., sk-proj-...).
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-" + REDACTED),
    # NAME=value assignments of credential-named variables.
    (re.compile(rf"(?<![A-Za-z0-9_])({_SECRET_NAME})=(?!\[REDACTED\])({_VALUE})"),
     r"\1=" + REDACTED),
    # Inline URL credentials: scheme://user:password@host.
    (re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://[^\s:/@'\"\\]*:)[^\s@/'\"\\]+(?=@)"),
     r"\1" + REDACTED),
    # Authorization headers, whatever the scheme word.
    (re.compile(
        r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+|basic\s+|token\s+)?)"
        r"[^\s'\"\\,}]+"),
     r"\1" + REDACTED),
    # A bare "Bearer <token>" outside an Authorization header.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential values replaced by ``[REDACTED]``.

    Covers ``PGPASSWORD=``/``*_PASSWORD=``/``*_SECRET=``/``*_TOKEN=``/
    ``*_KEY=`` assignments, ``scheme://user:password@`` URLs,
    ``Authorization``/``Bearer`` headers, ``sk-...`` API keys and GitHub
    ``ghp_``/``gho_``/``github_pat_`` tokens. Idempotent. Non-string input
    is returned unchanged so a malformed tool payload never breaks logging.
    """
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redacted_preview(text: str, limit: int) -> str:
    """Redact, then truncate to ``limit`` characters.

    Redacting after truncation could cut a token short of the length its
    pattern needs and leak the prefix, so the redactor sees ``limit`` plus a
    lookahead window. Only that window is scanned: previews of large tool
    inputs (a Write of a whole file) stay cheap.
    """
    if not isinstance(text, str):
        text = str(text)
    return redact_secrets(text[: limit + _PREVIEW_LOOKAHEAD])[:limit]
