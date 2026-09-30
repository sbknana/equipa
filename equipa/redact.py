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

import hashlib
import json
import re
from typing import Any

REDACTED = "[REDACTED]"

# How far past a preview limit the redactor looks, so a secret that starts
# inside the preview but ends after it is still recognised whole.
_PREVIEW_LOOKAHEAD = 1024

# Hard cap on the text one redact_secrets call scans (IR-05). Previews and
# log lines are far shorter; only a caller passing a whole tool input or
# output reaches it, and gets the redacted head plus this marker.
MAX_REDACT_INPUT = 64 * 1024
TRUNCATED_MARKER = " [TRUNCATED]"
# Replaces the rest of a tool input whose strings would cost more regex work
# than _WALK_WORK_FACTOR x the preview budget (IR-05). Only a pathological
# input (thousands of credential-shaped strings that each redact down to a
# few characters) gets here; the preview then ends early instead of showing
# anything unredacted.
WORK_LIMIT_MARKER = "[REDACTION WORK LIMIT]"
_WALK_WORK_FACTOR = 4

# A variable value after ``NAME=``: a double-quoted string (raw ``"`` or the
# JSON-escaped ``\"``; the closing quote may be missing when the text was
# truncated), a single-quoted string, or a bare word.
_VALUE = (
    r"(?:\\?\"(?:[^\"\\]|\\(?!\"))*(?:\\?\")?"
    r"|'[^']*'?"
    r"|[^\s'\"`\\;&|,)}\]]+)"
)

# The value of a command-line password flag (``mysql -p...``, ``sshpass -p
# ...``): a quoted string as in _VALUE, or a bare word that may contain the
# commas and brackets a variable value may not.
_FLAG_VALUE = (
    r"(?:\\?\"(?:[^\"\\]|\\(?!\"))*(?:\\?\")?"
    r"|'[^']*'?"
    r"|[^\s'\"\\;&|]+)"
)

# Names that hold credentials by convention: anything containing SECRET,
# PASSWORD or PASSWD (PGPASSWORD, DB_PASSWORD, CLIENT_SECRET_ID, ...), PASS
# as a whole ``_``-separated word (PASS, DB_PASS, SMTP_PASS_1; not BYPASS or
# COMPASS), and anything ending in _TOKEN or _KEY (GITHUB_TOKEN,
# ANTHROPIC_API_KEY, ...). Upper-case only: environment variables are
# upper-case, and matching lower-case identifiers would mangle ordinary code
# such as ``sort_key=len``.
#
# Linear (IR-05): the keyword is found by a lookahead that scans the name run
# once, then the run is consumed once. The earlier ``X*KEYWORD X*`` shape
# backtracked quadratically on a run holding many keywords (``PWD_PWD_...``).
_SECRET_NAME = (
    r"(?:(?=[A-Z0-9_]*?(?:SECRET|PASSWORD|PASSWD|(?<![A-Z0-9])PASS(?![A-Z0-9])))"
    r"[A-Z0-9_]+"
    r"|[A-Z0-9_]*_(?:TOKEN|KEY)|TOKEN)"
)

# Start of a name: not preceded by an identifier character, or preceded by a
# JSON escape (``\n``, ``\t``, ``\r``), which is how a line break inside a
# ``json.dumps`` rendering reaches the redactor (P2A-08).
_NAME_START = r"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))"

# Key/field/flag names of any case that hold credentials (P2A-03):
# password/passwd/pwd (libpq ``password=``, YAML, JSON, db_password),
# secrets, API and access keys, private keys, and names ENDING in token
# (``access_token``, but not ``max_tokens``). Bare ``key`` is not included,
# so ``sort(key=len)`` is left alone. Linear like _SECRET_NAME: a lookahead
# finds the keyword, then the run is consumed once (IR-05).
_SECRET_KEY = (
    r"(?i:(?=[a-z0-9_.-]*?(?:password|passwd|pwd|secret|api[-_]?key"
    r"|access[-_]?key|private[-_]?key))[a-z0-9_.-]+|[a-z0-9_.-]*token)"
)

# How far a command-scoped flag pattern (``mysql ... -p``) looks from the
# command word to the flag. Bounded so a text repeating the command word
# cannot make the search quadratic (IR-05).
_FLAG_REACH = r"[^\n;&|]{0,1024}?"

# A .pgpass line (``host:port:database:user:password``, IR-06): the port
# field is a number or ``*``. The password may hold ``\:`` escapes.
_PGPASS_START = r"(?:(?<![^\s'\"=>])|(?<=\\[nrt]))"
_PGPASS_LINE = re.compile(
    rf"{_PGPASS_START}((?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9_.*-]+):(?:\d{{1,5}}|\*)"
    r":[^:\s'\"]+:[^:\s'\"]+:)"
    r"(?!\[REDACTED\])((?:\\[^\"\s]|[^\s'\"\\])+)"
)
# Start of such a key: one start per run of name characters, which keeps the
# match linear on long runs; a JSON escape counts as a separator (P2A-08).
_KEY_START = r"(?:(?<![A-Za-z0-9_.-])|(?<=\\[nrt]))"

# URL-shaped tokens; the userinfo is found by parsing, not by one regex, so a
# password containing ``/`` or ``@`` is still recognised whole. The scheme
# length is bounded so a long dotted run cannot make the search quadratic.
_URL_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s'\"\\<>`]+")
# userinfo is greedy, so it runs to the LAST ``@`` that is followed by a host.
_URL_PARTS = re.compile(
    r"(?P<scheme>[^:]+://)(?P<userinfo>.*)@(?P<host>[^@/\s]*)(?P<rest>[/:?#].*)?",
    re.DOTALL,
)

_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*)PRIVATE KEY-----(?!\[REDACTED\])"
    r"(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*)",
    re.DOTALL,
)


def _redact_url_token(match: re.Match[str]) -> str:
    token = match.group(0)
    parts = _URL_PARTS.fullmatch(token)
    if parts is None:
        return token
    user, colon, password = parts.group("userinfo").partition(":")
    if not colon or not password or password == REDACTED:
        return token
    return (f"{parts.group('scheme')}{user}:{REDACTED}@{parts.group('host')}"
            f"{parts.group('rest') or ''}")


def _redact_pem(match: re.Match[str]) -> str:
    return f"-----BEGIN {match.group(1)}PRIVATE KEY-----{REDACTED}"


_PATTERNS: tuple[tuple[re.Pattern[str], Any], ...] = (
    # PEM private keys: header kept, body (to END, or to the end) replaced.
    (_PEM_PRIVATE_KEY, _redact_pem),
    # GitHub tokens: classic/OAuth/user/server/refresh and fine-grained PATs.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
     REDACTED),
    # Anthropic / OpenAI style API keys (sk-..., sk-ant-..., sk-proj-...).
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-" + REDACTED),
    # AWS access key ids.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    # Slack tokens (xoxb-, xoxp-, xoxa-, xoxr-, xoxs-, xoxe-...).
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}"), REDACTED),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    # GitLab personal / project / group access tokens.
    (re.compile(r"\bglpat-[0-9A-Za-z_-]{16,}"), REDACTED),
    # JWTs (header.payload.signature, base64url; the header starts "eyJ").
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
     REDACTED),
    # Inline URL credentials: scheme://user:password@host, parsed.
    (_URL_TOKEN, _redact_url_token),
    # NAME=value assignments of credential-named variables.
    (re.compile(rf"{_NAME_START}({_SECRET_NAME})=(?!\[REDACTED\])({_VALUE})"),
     r"\1=" + REDACTED),
    # --password VALUE / --api-key VALUE (the = form is covered below).
    (re.compile(rf"{_KEY_START}(--{_SECRET_KEY}\s+)(?![-\[])({_VALUE})"),
     r"\1" + REDACTED),
    # mysql -p<value> (attached, bare or quoted; a bare -p prompts and is left
    # alone). Scoped to the mysql/mariadb clients: -p means something else to
    # most tools.
    (re.compile(
        rf"((?i:\b(?:mysql[a-z]*|mariadb[a-z-]*))\b{_FLAG_REACH}\s-p)"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    # sshpass -p <value> / -p<value>. Scoped to sshpass: ssh -p is a port.
    (re.compile(
        rf"(\bsshpass\b{_FLAG_REACH}\s-p\s*)"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    # Password flags scoped to the command that means it (IR-06): docker /
    # podman / nerdctl login -p, az ... -p, sqlcmd -P, redis-cli -a / --pass.
    # Elsewhere these letters mean ports, paths or "all".
    (re.compile(
        rf"(\b(?:docker|podman|nerdctl)\b{_FLAG_REACH}\blogin\b{_FLAG_REACH}"
        r"\s-p(?:\s+|=))"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    (re.compile(
        rf"(\baz\s+[a-z]{_FLAG_REACH}\s-p(?:\s+|=))"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    (re.compile(
        rf"((?i:\bsqlcmd)\b{_FLAG_REACH}\s-P\s*)"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    (re.compile(
        rf"(\bredis-cli\b{_FLAG_REACH}\s(?:-a|--pass)\s+)"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"),
     r"\1" + REDACTED),
    # htpasswd -b [other flags] file user PASSWORD: the password is the last
    # word of the command (IR-06).
    (re.compile(
        rf"(\bhtpasswd\b{_FLAG_REACH}\s-[A-Za-z]*b[A-Za-z]*\s{_FLAG_REACH}\s)"
        rf"(?!\[REDACTED\])({_FLAG_VALUE})"
        r"(?=[ \t]*(?:[\n;&|)`\"\\]|$))"),
     r"\1" + REDACTED),
    # curl -u / --user and -U / --proxy-user user:password (no colon: curl
    # prompts).
    (re.compile(
        rf"(\bcurl\b{_FLAG_REACH}\s"
        r"(?:-u\s*|--user(?:=|\s+)|-U\s*|--proxy-user(?:=|\s+))"
        r"(?:\\?[\"'])?[^\s:'\"\\]*:)"
        r"(?!\[REDACTED\])([^\s'\"\\;&|]+)"),
     r"\1" + REDACTED),
    # .pgpass lines: host:port:database:user:password.
    (_PGPASS_LINE, r"\1" + REDACTED),
    # Cookie / Set-Cookie header values, to the end of the header (IR-06).
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)(?!\[REDACTED\])[^\r\n'\"\\]+"),
     r"\1" + REDACTED),
    # key = value / key: value / "key": "value" of credential-named keys in
    # any case and format: libpq DSNs, YAML, JSON, headers (X-Api-Key: ...).
    (re.compile(
        rf"{_KEY_START}({_SECRET_KEY}(?:\\?[\"'])?\s*[:=]\s*)"
        rf"(?!\[REDACTED\])({_VALUE})"),
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
    ``*_KEY=`` assignments; password/passwd/pwd/secret/api-key/token keys in
    any case and format (libpq ``password=``, YAML, JSON, ``X-Api-Key:``
    headers, ``--password VALUE``); ``mysql -p<value>``;
    ``scheme://user:password@`` URLs (passwords containing ``/`` or ``@``
    too); ``Authorization``/``Bearer`` headers; ``sk-...`` API keys, GitHub
    ``ghp_``/``gho_``/``github_pat_`` tokens, AWS ``AKIA`` key ids, JWTs and
    PEM private-key bodies; also Slack ``xox?-``, Google ``AIza`` and GitLab
    ``glpat-`` tokens, ``PASS=``/``*_PASS=``, ``docker login -p``,
    ``htpasswd -b``, ``az -p``, ``sqlcmd -P``, ``redis-cli -a``, curl
    ``--proxy-user``, ``.pgpass`` lines and ``Cookie``/``Set-Cookie``
    headers. Idempotent. Non-string input is returned unchanged so a
    malformed tool payload never breaks logging.

    Text longer than ``MAX_REDACT_INPUT`` is cut to that length (ending in
    ``TRUNCATED_MARKER``) before any pattern runs, so no caller can hand the
    patterns an unbounded amount of work (IR-05). Nothing past the cut is
    returned, so nothing unredacted leaks.
    """
    if not isinstance(text, str) or not text:
        return text
    if len(text) > MAX_REDACT_INPUT:
        text = text[:MAX_REDACT_INPUT - len(TRUNCATED_MARKER)] + TRUNCATED_MARKER
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


# Deeper tool-input nesting than this is rendered with str() and redacted
# as text rather than walked.
_MAX_STRUCTURE_DEPTH = 32


class _WalkBudget:
    """What is left while redacting one tool input (IR-05).

    ``output`` counts down the rendered characters still inside the preview
    budget. Each string is counted at its redacted length plus its quotes,
    a lower bound of what ``json.dumps`` renders (escapes only add), so the
    walk never stops before the rendering has really passed the budget.
    ``work`` counts down the characters the patterns may still scan.
    """

    __slots__ = ("output", "work")

    def __init__(self, output: int) -> None:
        self.output = output
        self.work = output * _WALK_WORK_FACTOR

    @property
    def spent(self) -> bool:
        return self.output <= 0


def _redact_text(text: str, budget: _WalkBudget) -> str:
    """One string of a tool input, cut and redacted within ``budget``."""
    piece = text[: max(budget.output, 0) + _PREVIEW_LOOKAHEAD]
    if len(piece) > budget.work:
        # Out of regex work before the preview filled: end the walk here.
        budget.work = budget.output = 0
        return WORK_LIMIT_MARKER
    budget.work -= len(piece)
    redacted = redact_secrets(piece)
    budget.output -= len(redacted) + 2
    return redacted


def _redact_values(value: Any, budget: _WalkBudget, depth: int = 0) -> Any:
    """Copy of ``value``, strings redacted, in ``json.dumps`` order.

    Items after the point where the rendering passes the budget are left
    out: they cannot reach the preview, and skipping them keeps the work
    proportional to the preview, not to the input (IR-05). The rendering of
    the copy is identical to the full rendering up to that point.
    """
    if isinstance(value, str):
        return _redact_text(value, budget)
    if depth >= _MAX_STRUCTURE_DEPTH:
        return _redact_text(str(value), budget)
    if isinstance(value, dict):
        copied: dict[Any, Any] = {}
        for key, item in value.items():
            if budget.spent:
                break
            budget.output -= len(str(key)) + 4  # "key": (lower bound)
            copied[key] = _redact_values(item, budget, depth + 1)
        return copied
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            if budget.spent:
                break
            budget.output -= 1  # separator (lower bound)
            items.append(_redact_values(item, budget, depth + 1))
        return items
    budget.output -= 1  # a number, true/false or null: at least one character
    return value


def _render(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def redacted_json_preview(value: Any, limit: int) -> str:
    """``json.dumps(value)`` redacted, truncated to ``limit`` characters.

    Every string VALUE is redacted before serialising (P2A-08): after
    ``json.dumps`` a line break is the two characters ``\\n``, so an
    assignment at the start of a later line would no longer look like one.
    The rendering is then redacted again, which catches credentials that are
    only recognisable with their key (``{"api_key": "..."}``). The walk stops
    once the rendering passes ``limit`` plus the lookahead, and each string
    is cut to what is left plus the lookahead; nothing past that can appear
    in the preview. Total work is bounded by the preview size, not by the
    input (IR-05).
    """
    budget = _WalkBudget(limit + _PREVIEW_LOOKAHEAD)
    return redacted_preview(_render(_redact_values(value, budget)), limit)


def redact_tool_input(value: Any, limit: int) -> tuple[str, str]:
    """``(preview, sha256 hex)`` to persist for one tool input.

    Hashing the raw input would be an offline oracle for the redacted span:
    whoever reads the preview knows the rest of a short input and can test
    guesses against the hash (P2A-04). The hash therefore covers the
    REDACTED rendering of the window the preview is cut from, followed by
    the raw rendering past that window. Equal inputs still hash equal across
    runs (a secret-free input hashes exactly as ``sha256(json.dumps(...))``
    did), and a secret past the window is not in the preview, so guessing
    it would mean guessing all the unseen text around it as well.
    """
    window = limit + _PREVIEW_LOOKAHEAD
    redacted_window = redacted_json_preview(value, window)
    remainder = _render(value)[window:]
    digest = hashlib.sha256(
        (redacted_window + remainder).encode("utf-8", errors="replace")
    ).hexdigest()
    return redacted_window[:limit], digest
