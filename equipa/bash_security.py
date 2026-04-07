"""EQUIPA bash_security — pre-execution security filter for bash commands.

Ported from Claude Code's bashSecurity.ts (23 exploit patterns).
This module detects dangerous bash command patterns *before* they are
passed to subprocess, blocking prompt-injection attacks that trick agents
into running shell exploits.

Pure Python stdlib — NO pip dependencies. Uses ``re`` for regex patterns.

Layer 5: No EQUIPA imports (standalone utility module).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_unquoted(command: str) -> str:
    """Strip single- and double-quoted content, returning only unquoted text.

    Respects bash quoting rules:
    - Backslash escapes the next character (outside single quotes).
    - Single quotes cannot be escaped inside single quotes.
    - Double quotes do not affect single-quote toggling and vice-versa.
    """
    result: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "\\" and not in_single:
            escaped = True
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            continue

        if not in_single and not in_double:
            result.append(ch)

    return "".join(result)


def _extract_unquoted_keep_delimiters(command: str) -> str:
    """Strip quoted *content* but preserve quote delimiter characters.

    Like ``_extract_unquoted`` but keeps ``'`` and ``"`` in the output.
    This is needed by ``_check_mid_word_hash`` to detect quote-adjacent
    ``#`` patterns like ``'x'#`` where full stripping would hide the
    adjacency.
    """
    result: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in command:
        if escaped:
            escaped = False
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "\\" and not in_single:
            escaped = True
            if not in_single and not in_double:
                result.append(ch)
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            result.append(ch)  # Keep the delimiter
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)  # Keep the delimiter
            continue

        if not in_single and not in_double:
            result.append(ch)

    return "".join(result)


def _get_base_command(command: str) -> str:
    """Extract the first word (base command) from *command*."""
    stripped = command.lstrip()
    # Skip env-var assignments like VAR=val
    while stripped and re.match(r"^[A-Za-z_]\w*=\S*\s+", stripped):
        stripped = re.sub(r"^[A-Za-z_]\w*=\S*\s+", "", stripped, count=1)
    parts = stripped.split(None, 1)
    return parts[0] if parts else ""


def _has_backslash_escaped_whitespace(command: str) -> bool:
    """Detect backslash-space or backslash-tab outside quotes."""
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and not in_single:
            if not in_double and i + 1 < len(command):
                nxt = command[i + 1]
                if nxt in (" ", "\t"):
                    return True
            i += 2
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        i += 1
    return False


def _has_backslash_escaped_operator(command: str) -> bool:
    r"""Detect ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes."""
    operators = frozenset(";|&<>")
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and not in_single:
            if not in_double and i + 1 < len(command):
                if command[i + 1] in operators:
                    return True
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        i += 1
    return False


def _is_escaped_at(content: str, pos: int) -> bool:
    """Return True if character at *pos* is preceded by an odd number of backslashes."""
    count = 0
    i = pos - 1
    while i >= 0 and content[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


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
    (re.compile(r"\$\("), "$() command substitution"),
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
    if re.search(r"(?:\||&&|\|\||;)\s*$", trimmed):
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


def _check_git_commit_substitution(
    command: str, base_cmd: str,
) -> BashSecurityResult:
    """Check 12: Command substitution inside git commit -m messages.

    ``git commit -m "$(evil)"`` expands the substitution before git sees it.
    Double-quoted messages allow expansion; single-quoted are safe.

    Also blocks:
    - Shell metacharacters in the remainder after the -m "..." argument
      (e.g., ``git commit -m 'msg' > ~/.bashrc``)
    - Commit messages starting with ``-`` (flag-like obfuscation)
    """
    if base_cmd != "git":
        return _SAFE
    if not re.match(r"^git\s+commit\s+", command):
        return _SAFE

    # Backslashes can desync our regex quote-boundary detection — bail
    if "\\" in command:
        return _SAFE  # Let other validators handle it

    # Match: git commit [flags] -m <quote><message><quote> [remainder]
    # The character class before -m excludes shell metacharacters so that
    # ``git commit ; evil -m 'x'`` does NOT match (`;`` is not in the class).
    msg_match = re.match(
        r"""^git[ \t]+commit[ \t]+[^;&|`$<>()\n\r]*?"""
        r"""-m[ \t]+(["'])([\s\S]*?)\1(.*)$""",
        command,
    )
    if not msg_match:
        return _SAFE

    quote = msg_match.group(1)
    message_content = msg_match.group(2) or ""
    remainder = msg_match.group(3) or ""

    # Double-quoted message with $(), ``, or ${}
    if quote == '"' and re.search(r"\$\(|`|\$\{", message_content):
        return BashSecurityResult(
            safe=False,
            check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
            message="Git commit message contains command substitution patterns",
        )

    # Remainder after the -m argument contains shell operators
    if remainder and re.search(r"[;|&()`]|\$\(|\$\{", remainder):
        return BashSecurityResult(
            safe=False,
            check_id=CheckID.GIT_COMMIT_SUBSTITUTION,
            message="Git commit has shell metacharacters after -m argument",
        )

    # Check for unquoted redirect operators in remainder
    if remainder:
        unquoted_rem = _extract_unquoted(remainder)
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

    # ANSI-C quoting: $'...'
    if re.search(r"\$'[^']*'", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains ANSI-C quoting ($'...') which can hide characters",
        )

    # Locale quoting: $"..."
    if re.search(r'\$"[^"]*"', command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains locale quoting ($\"...\") which can hide characters",
        )

    # Empty ANSI-C or locale quotes before dash: $''-exec or $""-exec
    if re.search(r"""\$['\"]{2}\s*-""", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.OBFUSCATED_FLAGS,
            message="Command contains empty special quotes before dash",
        )

    # Empty quote pairs before dash: ''-exec, ""-exec
    if re.search(r"""(?:^|\s)(?:''|""){1,}\s*-""", command):
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
    if re.search(r"""(?:""|''){1,}['\"]-""", command):
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


def _check_command_substitution(unquoted: str) -> BashSecurityResult:
    """Check 8: Backticks and command substitution patterns."""
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

    for pattern, desc in _COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted):
            return BashSecurityResult(
                safe=False, check_id=CheckID.COMMAND_SUBSTITUTION,
                message=f"Command contains {desc}",
            )
    return _SAFE


def _check_redirections(unquoted: str) -> BashSecurityResult:
    """Checks 9-10: Input and output redirection in unquoted content."""
    if "<" in unquoted:
        return BashSecurityResult(
            safe=False, check_id=CheckID.INPUT_REDIRECTION,
            message="Command contains input redirection (<) which could read sensitive files",
        )
    if ">" in unquoted:
        return BashSecurityResult(
            safe=False, check_id=CheckID.OUTPUT_REDIRECTION,
            message="Command contains output redirection (>) which could write to arbitrary files",
        )
    return _SAFE


def _check_dangerous_variables(unquoted: str) -> BashSecurityResult:
    """Check 6: Variables used in redirect/pipe context."""
    if (re.search(r"[<>|]\s*\$[A-Za-z_]", unquoted)
            or re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\s*[|<>]", unquoted)):
        return BashSecurityResult(
            safe=False, check_id=CheckID.DANGEROUS_VARIABLES,
            message="Command contains variables in dangerous contexts (redirections or pipes)",
        )
    return _SAFE


def _check_newlines(command: str, unquoted: str) -> BashSecurityResult:
    """Check 7: Newlines that could separate multiple commands."""
    if not re.search(r"[\n\r]", unquoted):
        return _SAFE
    # Newline/CR followed by non-whitespace (except \<newline> continuations)
    if re.search(r"(?<![\s]\\)[\n\r]\s*\S", unquoted):
        return BashSecurityResult(
            safe=False, check_id=CheckID.NEWLINES,
            message="Command contains newlines that could separate multiple commands",
        )
    # Carriage return outside double quotes
    if "\r" in command:
        in_single = False
        in_double = False
        esc = False
        for ch in command:
            if esc:
                esc = False
                continue
            if ch == "\\" and not in_single:
                esc = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
                continue
            if ch == "\r" and not in_double:
                return BashSecurityResult(
                    safe=False, check_id=CheckID.NEWLINES,
                    message="Command contains carriage return which can cause parser differentials",
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


def _check_brace_expansion(command: str, unquoted: str) -> BashSecurityResult:
    """Check 16: Brace expansion ({a,b} or {1..5}) in unquoted content."""
    # Count unescaped braces
    open_count = 0
    close_count = 0
    for i, ch in enumerate(unquoted):
        if ch == "{" and not _is_escaped_at(unquoted, i):
            open_count += 1
        elif ch == "}" and not _is_escaped_at(unquoted, i):
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

    # Scan for {a,b} or {1..5} patterns in unquoted content
    i = 0
    while i < len(unquoted):
        if unquoted[i] != "{" or _is_escaped_at(unquoted, i):
            i += 1
            continue
        # Find matching close brace with nesting
        depth = 1
        close_pos = -1
        j = i + 1
        while j < len(unquoted):
            if unquoted[j] == "{" and not _is_escaped_at(unquoted, j):
                depth += 1
            elif unquoted[j] == "}" and not _is_escaped_at(unquoted, j):
                depth -= 1
                if depth == 0:
                    close_pos = j
                    break
            j += 1
        if close_pos == -1:
            i += 1
            continue
        # Check for comma or .. at outermost level
        inner_depth = 0
        for k in range(i + 1, close_pos):
            ch = unquoted[k]
            if ch == "{" and not _is_escaped_at(unquoted, k):
                inner_depth += 1
            elif ch == "}" and not _is_escaped_at(unquoted, k):
                inner_depth -= 1
            elif inner_depth == 0:
                if ch == ",":
                    return BashSecurityResult(
                        safe=False, check_id=CheckID.BRACE_EXPANSION,
                        message="Command contains brace expansion that could alter parsing",
                    )
                if ch == "." and k + 1 < close_pos and unquoted[k + 1] == ".":
                    return BashSecurityResult(
                        safe=False, check_id=CheckID.BRACE_EXPANSION,
                        message="Command contains brace sequence expansion ({a..z})",
                    )
        i += 1
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
    """Check 19: Heredoc inside command substitution ($(...<<...))."""
    if re.search(r"\$\(.*<<", command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.HEREDOC_IN_SUBSTITUTION,
            message="Command contains heredoc inside command substitution",
        )
    return _SAFE


def _check_backslash_escaped_operators(command: str) -> BashSecurityResult:
    r"""Check 21: ``\;``, ``\|``, ``\&``, ``\<``, ``\>`` outside quotes."""
    if _has_backslash_escaped_operator(command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.BACKSLASH_ESCAPED_OPERATORS,
            message="Command contains backslash before shell operator which can hide command structure",
        )
    return _SAFE


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


def _check_quoted_newline_comment(command: str) -> BashSecurityResult:
    """Check 23: Newline inside quotes where next line starts with #.

    This exploits line-based comment stripping to hide arguments from
    path validation.
    """
    if "\n" not in command or "#" not in command:
        return _SAFE

    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue

        # Inside quotes and hit a newline — check if next line starts with #
        if (in_single or in_double) and ch == "\n":
            rest = command[i + 1:]
            if rest.lstrip().startswith("#"):
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
    """
    # Quoted args with metacharacters inside
    if re.search(r'''(?:^|\s)["'][^"']*[;&][^"']*["'](?:\s|$)''', command):
        return BashSecurityResult(
            safe=False, check_id=CheckID.SHELL_METACHARACTERS,
            message="Command contains shell metacharacters (;, |, or &) in arguments",
        )

    # Find-specific patterns: -name, -path, -iname with metacharacters
    for pattern in (
        r'''-name\s+["'][^"']*[;|&][^"']*["']''',
        r'''-path\s+["'][^"']*[;|&][^"']*["']''',
        r'''-iname\s+["'][^"']*[;|&][^"']*["']''',
        r'''-regex\s+["'][^"']*[;&][^"']*["']''',
    ):
        if re.search(pattern, command):
            return BashSecurityResult(
                safe=False, check_id=CheckID.SHELL_METACHARACTERS,
                message="Command contains shell metacharacters in arguments",
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

    base_cmd = _get_base_command(command)
    unquoted = _extract_unquoted(command)

    # Run each check in priority order. First failure wins.
    checks: list[BashSecurityResult] = [
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
        _check_redirections(unquoted),
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
    ]

    for result in checks:
        if not result.safe:
            log.warning(
                "Bash security check %d BLOCKED command: %s — %s",
                result.check_id, command[:120], result.message,
            )
            return result

    return _SAFE
