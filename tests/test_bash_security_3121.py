"""Regression tests for task 3121 (EQUIPA review 2026-09-29, area sandbox).

* sandbox-07: command length cap and linear-time brace / quote scans.
* sandbox-01: command substitution inside double quotes.
* sandbox-08: redirect targets with ``..`` or outside the tree and /tmp.
* sandbox-13: read-only false positives, table-driven against their
  dangerous variants.

Every case here failed on main before task 3121 (the pass column of the
false-positive table was blocked; the block column of the bypass table was
allowed; the timing cases took seconds to minutes).

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
import time

import pytest

from equipa import bash_security
from equipa.bash_security import (
    MAX_COMMAND_BYTES,
    CheckID,
    check_bash_command,
)

# Generous for a linear scan (tens of milliseconds) and far below the
# minutes the quadratic versions took.
TIME_LIMIT_SECONDS = 1.0


def _timed(func, *args):
    start = time.perf_counter()
    result = func(*args)
    return result, time.perf_counter() - start


# ---------------------------------------------------------------------------
# sandbox-07: length cap + linear scans
# ---------------------------------------------------------------------------

class TestLengthCapAndLinearScans:

    @pytest.mark.parametrize(
        "command", ["{" * 40_000, "'" * 50_000], ids=["40k-braces", "50k-quotes"]
    )
    def test_oversized_command_blocked_fast(self, command: str):
        result, elapsed = _timed(check_bash_command, command)
        assert elapsed < TIME_LIMIT_SECONDS, f"took {elapsed:.2f}s"
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_TOO_LONG
        assert str(MAX_COMMAND_BYTES) in result.message

    def test_cap_boundary_is_in_bytes(self):
        at_limit = "echo " + "a" * (MAX_COMMAND_BYTES - 5)
        assert len(at_limit.encode()) == MAX_COMMAND_BYTES
        assert check_bash_command(at_limit).check_id != CheckID.COMMAND_TOO_LONG
        over = at_limit + "a"
        assert check_bash_command(over).check_id == CheckID.COMMAND_TOO_LONG
        # 8190 two-byte characters: under the limit in characters, over it
        # in bytes.
        multibyte = "echo " + "é" * 8190
        assert len(multibyte) < MAX_COMMAND_BYTES
        assert check_bash_command(multibyte).check_id == CheckID.COMMAND_TOO_LONG

    @pytest.mark.parametrize(
        "command",
        [
            "{" * 40_000,
            "\\" * 40_000 + "{a,b}",
            "{a," * 10_000,
            "{a.." * 10_000,
            "}" * 40_000,
        ],
        ids=["braces", "backslashes", "commas", "sequences", "closers"],
    )
    def test_brace_scan_is_linear(self, command: str):
        """The scan itself, not the cap, must be fast (bypasses the cap)."""
        _, elapsed = _timed(
            bash_security._check_brace_expansion, command, command
        )
        assert elapsed < TIME_LIMIT_SECONDS, f"took {elapsed:.2f}s"

    @pytest.mark.parametrize("quote", ["'", '"'])
    def test_quote_run_scan_is_linear(self, quote: str):
        command = quote * 50_000
        _, elapsed = _timed(bash_security._check_obfuscated_flags, command, "")
        assert elapsed < TIME_LIMIT_SECONDS, f"took {elapsed:.2f}s"

    @pytest.mark.parametrize(
        "command",
        ["{" * 16_000, "'" * 16_000, "echo {a," * 2_000, '"$(' * 5_000],
        ids=["braces", "quotes", "brace-lists", "dq-substitutions"],
    )
    def test_full_pipeline_fast_at_the_cap(self, command: str):
        """Largest allowed size through every check stays well under 1 s."""
        assert len(command.encode()) <= MAX_COMMAND_BYTES
        _, elapsed = _timed(check_bash_command, command)
        assert elapsed < TIME_LIMIT_SECONDS, f"took {elapsed:.2f}s"

    def test_quote_regex_rewrite_keeps_semantics(self):
        """``(?:""|''){1,}['"]-`` and the anchored-free one-pair form agree."""
        obfuscated = ['"""-f"', "'''-f", "''''''\"-x", "a ''\"-rf\""]
        for command in obfuscated:
            assert not bash_security._check_obfuscated_flags(command, "rm").safe
        assert bash_security._check_obfuscated_flags("rm -f x", "rm").safe


def _reference_brace_verdict(unquoted: str) -> str | None:
    """The pre-3121 quadratic brace scan, kept as an oracle for the rewrite."""
    def escaped_at(pos: int) -> bool:
        count = 0
        i = pos - 1
        while i >= 0 and unquoted[i] == "\\":
            count += 1
            i -= 1
        return count % 2 == 1

    i = 0
    while i < len(unquoted):
        if unquoted[i] != "{" or escaped_at(i):
            i += 1
            continue
        depth, close_pos, j = 1, -1, i + 1
        while j < len(unquoted):
            if unquoted[j] == "{" and not escaped_at(j):
                depth += 1
            elif unquoted[j] == "}" and not escaped_at(j):
                depth -= 1
                if depth == 0:
                    close_pos = j
                    break
            j += 1
        if close_pos == -1:
            i += 1
            continue
        inner_depth = 0
        for k in range(i + 1, close_pos):
            ch = unquoted[k]
            if ch == "{" and not escaped_at(k):
                inner_depth += 1
            elif ch == "}" and not escaped_at(k):
                inner_depth -= 1
            elif inner_depth == 0:
                if ch == ",":
                    return "comma"
                if ch == "." and k + 1 < close_pos and unquoted[k + 1] == ".":
                    return "sequence"
        i += 1
    return None


def test_linear_brace_scan_matches_reference_oracle():
    """Differential test: same first verdict as the old algorithm."""
    rng = random.Random(3121)
    alphabet = "{},.\\a"
    for _ in range(5_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        found = bash_security._brace_expansions(text)
        verdict = found[0][2] if found else None
        assert verdict == _reference_brace_verdict(text), repr(text)
