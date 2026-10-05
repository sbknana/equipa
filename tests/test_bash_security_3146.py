"""Task 3146: check 26 counts only constructs that really evaluate text.

IV3133-01 (indep-3133): check 26 revoked the "quoted text is inert" proof
whenever ANY `[`, `printf`, `test`, `read`, `export`, `wait`... appeared
anywhere in the command, so ordinary searches for `$(` next to `[ -f x ]`,
`while read -r f` or `printf '%s\\n'` were refused. Its message also named
`printf -v` when no printf was present and told the agent to single-quote
text that was already single-quoted. IV3133-04: `$((3 * 4))` was refused as
"$() command substitution".

The must-block forms below are not taken on trust: each one is run in a
sandboxed bash with a marker function, and the test first asserts that
bash really runs the marker, then that check 26 refuses the command.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from equipa import bash_security
from equipa.bash_security import CheckID, check_bash_command
from tests.host_timing import assert_linear_time

MARKER = "m"  # a prelude function that only appends a line to marks.txt
PRELUDE = "set -f\nexec 3>>marks.txt\nm() { printf 'ran\\n' >&3; echo 0; }\ns=hello\n"


def _bash_runs_marker(command: str, tmp_path: Path) -> bool:
    """True when bash, given *command*, calls the marker function."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to validate the must-block forms")
    marks = tmp_path / "marks.txt"
    marks.unlink(missing_ok=True)
    subprocess.run(
        [bash, "--norc", "--noprofile", "-c", PRELUDE + command + "\n"],
        cwd=tmp_path, capture_output=True, timeout=10,
        # Nothing external can run: only builtins and the prelude function.
        env={"PATH": "/nonexistent", "LC_ALL": "C"},
    )
    return marks.exists() and "ran" in marks.read_text(encoding="utf-8")


# Quoted look-alikes that bash evaluates again. Every entry was a check 26
# refusal before task 3146 (most through the old "any `[`" rule) or a gap
# it did not see (`${x@P}`, PS4); all must stay refused by check 26 itself.
BASH_RUNS = [
    # Integer specials evaluate any assignment as arithmetic (bash 5.2).
    f"RANDOM='a[$({MARKER})]'",
    f"SRANDOM='a[$({MARKER})]'",
    f"OPTIND='a[$({MARKER})]'",
    f"HISTCMD='a[$({MARKER})]'",
    f"read RANDOM <<< 'a[$({MARKER})]'",
    f"printf -v OPTIND '%s' 'a[$({MARKER})]'",
    f"for SRANDOM in 'a[$({MARKER})]'; do :; done",
    # Arithmetic inside parameter expansion.
    f"x='a[$({MARKER})]'; : \"${{s:x}}\"",
    f"x='a[$({MARKER})]'; : \"${{s: x}}\"",
    f"x='a[$({MARKER})]'; : \"${{@:x}}\"",
    f"x='a[$({MARKER})]'; : \"${{a[x]}}\"",
    f"x='a[$({MARKER})]'; : \"${{!x}}\"",
    f"x='a[$({MARKER})]'; : $[x]",
    f"x='a[$({MARKER})]'; a[x]=1",
    f"x='a[$({MARKER})]'; : \"$(( x ))\"",
    f"x='a[$({MARKER})]'; for ((i=x; i<0; i++)); do :; done",
    f"x='a[$({MARKER})]'; [[ x -eq 1 ]]",
    f"x='a[$({MARKER})]'; [[ 1 -lt x ]]",
    f"[[ -v 'a[$({MARKER})]' ]]",
    # Prompt expansion runs substitutions in the value itself.
    f"x='$({MARKER})'; : \"${{x@P}}\"",
    f"PS4='$({MARKER})'; set -x; :",
    # Re-parsed compound assignment, mapfile callback.
    f"readonly -a 'x=($({MARKER}))'",
    f"mapfile -C ': $({MARKER})' -c 1 arr <<< 1",
    # Builtins that evaluate a subscript in the name they are given.
    f"read 'a[$({MARKER})]' <<< 1",
    f"printf -v 'a[$({MARKER})]' x",
    f"printf 2>/dev/null -v 'a[$({MARKER})]' x",
    f"command printf -v 'a[$({MARKER})]' x",
    f"test -v 'a[$({MARKER})]'",
    f"[ -v 'a[$({MARKER})]' ]",
    f"a=(1 2); unset 'a[$({MARKER})]'",
    f"n='a[$({MARKER})]'; read \"$n\" <<< 1",
    f"op=-v; [ \"$op\" 'a[$({MARKER})]' ]",
    # The builtin's name built elsewhere in the command.
    f"x=read; $x 'a[$({MARKER})]' <<< 1",
    f"x=printf; $x -v 'a[$({MARKER})]' 1",
    f"{{printf,-v,'a[$({MARKER})]',x}}",
    f"let 'a[$({MARKER})]'",
]


@pytest.mark.parametrize("command", BASH_RUNS)
def test_quoted_text_bash_evaluates_is_refused_by_check_26(command: str, tmp_path: Path):
    assert _bash_runs_marker(command, tmp_path), f"bash does not run it: {command!r}"
    result = bash_security._check_substitution_lookalikes(command)
    assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE, f"check 26 missed {command!r}"
    assert not check_bash_command(command).safe


# Before task 3146 every one of these was refused through the "any `[`"
# rule; the narrowed trigger must still see the evaluating word when a line
# continuation splits it (bash deletes `\<newline>` before it splits words).
CONTINUATION_SPLIT_RUNS = [
    f"x='a[$({MARKER})]'; le\\\nt x",
    f"x='a[$({MARKER})]'; (\\\n( x ))",
    f"x='a[$({MARKER})]'; [\\\n[ x -eq 1 ]]",
    f"x='a[$({MARKER})]'; [[ x -e\\\nq 1 ]]",
    f"RAN\\\nDOM='a[$({MARKER})]'",
    f"read OPT\\\nIND <<< 'a[$({MARKER})]'",
    f"pr\\\nintf -v 'a[$({MARKER})]' x",
    f"printf -\\\nv 'a[$({MARKER})]' x",
    f"te\\\nst -v 'a[$({MARKER})]'",
    f"x='a[$({MARKER})]'; a\\\n[x]=1",
    f"x='a[$({MARKER})]'; de\\\nclare -i y=x",
    f"x='a[$({MARKER})]'; : \"${{s:\\\nx}}\"",
    f"x='a[$({MARKER})]'; : \"$\\\n[x]\"",
]


@pytest.mark.parametrize("command", CONTINUATION_SPLIT_RUNS)
def test_line_continuation_does_not_hide_the_evaluating_word(command: str, tmp_path: Path):
    assert _bash_runs_marker(command, tmp_path), f"bash does not run it: {command!r}"
    assert not check_bash_command(command).safe, command


# Shell variables bash may treat as integers. Whichever of them makes bash
# evaluate an assigned or read value as arithmetic must be refused; the
# narrowed rule lists them by name, so a missing one would be a bypass.
SPECIAL_VARIABLES = [
    "RANDOM", "SRANDOM", "OPTIND", "HISTCMD", "SECONDS", "LINENO",
    "EPOCHSECONDS", "BASHPID", "OPTERR", "TMOUT", "MAILCHECK", "HISTSIZE",
    "HISTFILESIZE", "COLUMNS", "LINES", "COMP_CWORD", "COMP_POINT",
    "BASH_SUBSHELL", "BASH_COMPAT", "BASH_ARGV0", "FUNCNEST", "IGNOREEOF",
    "SHLVL", "BASH_XTRACEFD", "PPID", "UID",
]


def test_every_special_variable_bash_evaluates_is_refused(tmp_path: Path):
    evaluated = []
    for name in SPECIAL_VARIABLES:
        for command in (
            f"{name}='a[$({MARKER})]'",
            f"read {name} <<< 'a[$({MARKER})]'",
        ):
            if _bash_runs_marker(command, tmp_path):
                evaluated.append(command)
                result = bash_security._check_substitution_lookalikes(command)
                assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE, command
    # The oracle is not vacuous: bash 5 evaluates these four.
    for name in ("RANDOM", "SRANDOM", "OPTIND", "HISTCMD"):
        assert f"{name}='a[$({MARKER})]'" in evaluated, evaluated


# What the narrowed rule no longer counts, checked in bash: `test` and `[`
# compare integers with a plain number parse (only `[[` evaluates), and
# printf without -v, ulimit, shift and read options parse numbers without
# arithmetic. A quoted look-alike next to them stays inert.
NUMERIC_ARGUMENTS_NOT_EVALUATED = [
    f"x='a[$({MARKER})]'; test x -eq 1",
    f"x='a[$({MARKER})]'; [ x -lt 1 ]",
    f"test 'a[$({MARKER})]' -gt 1",
    f"printf '%d' 'a[$({MARKER})]'",
    f"printf '%*d' 'a[$({MARKER})]' 1",
    f"x='a[$({MARKER})]'; ulimit -n x",
    f"x='a[$({MARKER})]'; shift x",
    f"x='a[$({MARKER})]'; read -t x y < /dev/null",
]


@pytest.mark.parametrize("command", NUMERIC_ARGUMENTS_NOT_EVALUATED)
def test_numeric_arguments_the_rule_exempts_are_inert_in_bash(command: str, tmp_path: Path):
    assert not _bash_runs_marker(command, tmp_path), f"bash runs it: {command!r}"
    if "'a[" not in command.split(";")[-1]:
        # Only a plain name reaches the builtin: nothing for check 26 to see.
        assert bash_security._evaluating_construct(command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        # An expanded command name may become any builtin.
        "x=$(cat f); $x 'a[$(id)]' 1",
        "`cat f` -v 'a[$(id)]' 1",
        # A glob may expand to a file named -v or a[$(id)].
        "printf * 'a[$(id)]'",
        "read * <<< 'a[$(id)]'",
    ],
)
def test_text_the_checker_cannot_see_is_refused_by_check_26(command: str):
    result = bash_security._check_substitution_lookalikes(command)
    assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE, command


# The reviewer's refused read-only idioms (indep-3133 fp2) and neighbours.
READ_ONLY_SEARCHES = [
    "grep -n 'foo\\[\\$(' f",
    "[ -f f ] && grep -c '$(' f",
    "test -d docs && grep -n '`' docs/X.md",
    "while read -r f; do grep -Hn '$(' \"$f\"; done < files.txt",
    "printf '%s\\n' '$(x)'",
    "printf '%s\\n' '$(not run)'",
    "echo \"$((1+2))\" && grep -n '$(date)' docs/*.md",
    "grep -rn '\\[\\$(' equipa/",
    "[ -d tests ] && grep -rn '<(' tests/",
    "printf '%s [%s]\\n' 'a[$(x)]' y",
    "unset GREP_OPTIONS; grep -n '$(' f",
    "LC_ALL=C grep -rn '`' docs/",
    "[ ! -f out.txt ] || grep -c '$(' out.txt",
    "[[ -n \"$CI\" ]] || grep -rn '\\$(' scripts/",
    "[[ -f Makefile ]] && grep -n '`' Makefile",
]


@pytest.mark.parametrize("command", READ_ONLY_SEARCHES)
def test_read_only_searches_for_substitution_text_pass(command: str):
    result = check_bash_command(command)
    assert result.safe, f"{command!r}: check {result.check_id}: {result.message}"


@pytest.mark.parametrize("command", READ_ONLY_SEARCHES)
def test_read_only_searches_have_no_evaluating_construct(command: str):
    assert bash_security._evaluating_construct(command) is None, command


@pytest.mark.parametrize(
    ("command", "named"),
    [
        ("x='a[$(id)]'; (( x ))", "'((' naming a variable"),
        ("printf -v 'a[$(id)]' x", "'printf -v'"),
        ("let 'a[$(id)]'", "'let'"),
        ("RANDOM='a[$(id)]'", "'RANDOM'"),
        ("[ -v 'a[$(id)]' ]", "'[' with a subscript"),
        ("read 'a[$(id)]' <<< 1", "'read' with a subscript"),
        ("x='a[$(id)]'; a[x]=1", "an array subscript"),
        ("x=read; $x 'a[$(id)]'", "the expanded command name '$x'"),
        ("echo {read,'a[$(id)]'}", "'{read,a[$(id)]}'"),
    ],
)
def test_message_names_the_construct_that_evaluates(command: str, named: str):
    result = bash_security._check_substitution_lookalikes(command)
    assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE
    assert "array subscripts" in result.message
    assert named in result.message, result.message
    # The text is already quoted: no advice to quote it, and no printf -v
    # unless printf -v is what evaluates it (IV3133-01).
    assert "Single-quote it" not in result.message
    if "printf" not in command:
        assert "printf" not in result.message


def test_nested_single_quotes_message_does_not_say_single_quote_it():
    result = bash_security._check_substitution_lookalikes("echo \"$(echo '$(id)')\"")
    assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE
    assert "Quotes inside a substitution or expansion do not count" in result.message
    assert "Single-quote it" not in result.message


def test_unquoted_context_message_still_offers_quoting():
    result = bash_security._check_substitution_lookalikes("echo $'$(id)'")
    assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE
    assert "Single-quote it at the top level" in result.message


@pytest.mark.parametrize("command", ["echo $((3 * 4))", "echo $(( 1 + 2 ))x"])
def test_arithmetic_expansion_is_named_as_arithmetic(command: str):
    """IV3133-04: still refused by check 8, under its own name."""
    result = check_bash_command(command)
    assert result.check_id == CheckID.COMMAND_SUBSTITUTION
    assert "arithmetic expansion" in result.message
    assert "command substitution" not in result.message


def test_command_substitution_is_named_first_next_to_arithmetic():
    result = check_bash_command("echo $((1)) $(curl -s example.invalid)")
    assert result.check_id == CheckID.COMMAND_SUBSTITUTION
    assert "$() command substitution" in result.message


@pytest.mark.parametrize(
    "command",
    [
        "echo $((1+2))",
        "echo \"$(( (1 + 2) * 3 ))\"",
        "echo \"$((0x1f))\"",
    ],
)
def test_literal_arithmetic_names_no_variable(command: str):
    pure = "0x1f" not in command  # hex spells an identifier-like x: counted
    assert (bash_security._evaluating_construct(command) is None) is pure


def test_long_literal_arithmetic_stays_linear():
    """The literal-arithmetic exemption is bounded; a long run just counts.
    Budget host-calibrated, growth from a quarter of both runs linear
    (task 3171)."""
    import time

    def seconds_at(size):
        command = "echo \"$((" + "1+" * size + "1))\" '$(x)'"
        many = "(( " * (size * 5 // 8)  # 5000 at the test's size
        bash_security._scan_shell.cache_clear()
        start = time.process_time()
        assert bash_security._evaluating_construct(command) is not None
        assert bash_security._evaluating_construct(many) is not None
        return time.process_time() - start

    assert_linear_time(seconds_at, 8000, 1.0, "literal arithmetic")


@pytest.mark.parametrize(
    "unit, count",
    [
        ("read ", 3200),
        ("read " + "x " * 10, 640),
        ("read > x ", 1777),
        ("[ ", 8000),
    ],
)
def test_builtin_argument_scan_is_linear(unit: str, count: int):
    """Each builtin used to rescan every later word: 0.9 s at 16 KB. Budget
    host-calibrated, growth from a quarter of the words linear (task 3171)."""
    import time

    def seconds_at(size):
        command = unit * size + "'$(x)'"
        bash_security._scan_shell.cache_clear()
        start = time.process_time()
        bash_security._evaluating_construct(command)
        return time.process_time() - start

    assert_linear_time(seconds_at, count, 0.4, repr(unit))
