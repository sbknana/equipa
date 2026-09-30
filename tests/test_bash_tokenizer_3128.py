"""Task 3128: the shared shell tokenizer behind every quote-sensitive check.

SECURITY-REVIEW-3121 BS3121-01 showed two words bash reads as complete -
``$'\\''`` and ``"$(echo '"')"`` - desyncing the checker's private quote
models, so checks 8 and 10 missed what followed. ``bash_security._scan_shell``
now classifies each character once. These tests pin its model:

* unit cases for the constructs it must follow (ANSI-C escapes, fresh
  quoting inside ``$(...)``, comments, heredoc bodies) and for the ones it
  refuses (unterminated quotes, ``case`` inside ``$(...)``, ambiguous
  ``((``);
* a differential test that runs a few hundred generated commands through
  bash itself and compares the argv / heredoc bodies bash produced with the
  words the tokenizer's classification implies.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from equipa import bash_security
from equipa.bash_security import CheckID, check_bash_command

scan = bash_security._scan_shell


def _kind(kind_byte: int) -> int:
    return kind_byte & bash_security._KIND_MASK


# ---------------------------------------------------------------------------
# Unit cases
# ---------------------------------------------------------------------------

class TestTokenizerModel:

    def test_ansi_c_apostrophe_is_one_complete_word(self):
        command = "echo $'\\'' $(id)"
        result = scan(command)
        assert result.error is None
        assert bash_security._extract_unquoted(command) == "echo  $(id)"
        kinds = [_kind(k) for k in result.kinds]
        assert kinds[5:7] == [bash_security._K_ANSI_DELIM] * 2
        assert kinds[9] == bash_security._K_ANSI_DELIM

    def test_quoting_restarts_inside_double_quoted_substitution(self):
        command = "echo \"$(echo '\"')\" $(id)"
        assert scan(command).error is None
        assert bash_security._extract_unquoted(command) == "echo  $(id)"
        assert bash_security._double_quoted_substitutions(command) == [
            ("$(", "echo '\"'")
        ]

    def test_keep_delimiters_view(self):
        assert bash_security._extract_unquoted_keep_delimiters(
            "a 'b' \"c\" $'d'"
        ) == "a '' \"\" $''"

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo a # it's here", "echo a # it's here"),
            ("echo a#b 'c'", "echo a#b "),
            ("echo 'a'#b", "echo #b"),
            ("echo a;#x 'y", "echo a;#x 'y"),
            ("echo $(( 1 #2 ))", "echo $(( 1 #2 ))"),
        ],
    )
    def test_comments_follow_bash(self, command: str, expected: str):
        """A # comment starts only at a token start; quotes in it are text."""
        result = scan(command)
        assert result.error is None, result.error
        assert bash_security._extract_unquoted(command) == expected

    def test_comment_hides_nothing_from_redirect_scan(self):
        assert bash_security._shell_redirects("echo a # > /etc/x") == []
        assert bash_security._shell_redirects("echo a#b > /etc/x") == [
            ("out", "/etc/x", False)
        ]

    def test_quoted_heredoc_body_is_inert_text(self):
        command = "cat <<'EOF' > out.txt\nit's $(id) > /etc/x\nEOF\necho done"
        result = scan(command)
        assert result.error is None
        (heredoc,) = result.heredocs
        assert heredoc.quoted and heredoc.top_level
        body = command[heredoc.body_start:heredoc.terminator_start]
        assert body == "it's $(id) > /etc/x\n"
        assert result.substitutions == ()
        assert bash_security._shell_redirects(command) == [
            ("out", "out.txt", False)
        ]

    def test_unquoted_heredoc_body_runs_substitutions(self):
        command = "cat <<EOF\nit's $(id)\nEOF"
        result = scan(command)
        assert result.error is None
        assert [s.kind for s in result.substitutions] == ["$("]
        assert not check_bash_command(command).safe

    def test_unquoted_heredoc_continuation_joins_the_terminator(self):
        """``foo\\`` + newline + ``X`` is the logical line ``fooX``."""
        command = "cat <<X\nfoo\\\nX\nbar\nX\necho after"
        (heredoc,) = scan(command).heredocs
        assert command[heredoc.body_start:heredoc.terminator_start] == (
            "foo\\\nX\nbar\n"
        )
        quoted = "cat <<'X'\nfoo\\\nX\necho after"
        (heredoc,) = scan(quoted).heredocs
        assert quoted[heredoc.body_start:heredoc.terminator_start] == "foo\\\n"

    def test_commit_message_heredoc_in_double_quotes(self):
        # The apostrophe and parens would have opened a quote / closed the
        # $( in a scanner that ignored the heredoc. (A double quote in the
        # body is still refused by check 12's own parser.)
        command = (
            "git commit -m \"$(cat <<'EOF'\nfix: it's done (really) `x`\n"
            "EOF\n)\""
        )
        result = scan(command)
        assert result.error is None
        assert bash_security._double_quoted_substitutions(command) == []
        assert check_bash_command(command).safe

    def test_segments_split_after_desync_word(self):
        assert bash_security._split_command_segments(
            "grep $'\\'' f; touch x"
        ) == ["grep $'\\'' f", "touch x"]
        assert bash_security._split_command_segments(
            "echo a # b; c"
        ) == ["echo a # b; c"]

    def test_dollar_quote_scan_sees_constructs_after_desync_word(self):
        assert bash_security._scan_shell_level_dollar_quotes(
            "echo $'\\'' $\"x\""
        ) == {"ansi-c", "locale"}
        assert bash_security._scan_shell_level_dollar_quotes(
            "echo \"$'x'\""
        ) == set()

    def test_arithmetic_command_and_expansion(self):
        assert scan("(( x << 2 )); echo ok").error is None
        assert scan("(( x << 2 )); echo ok").heredocs == ()
        assert scan("echo $(( (1 + 2) * 3 ))").error is None

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("echo 'a", "unterminated single quote"),
            ("echo \"a", "unterminated double quote"),
            ("echo $'a\\'", "unterminated $'...' quote"),
            ("echo $(a", "unterminated $(...) substitution"),
            ("echo $((1", "unterminated $((...)) expansion"),
            ("echo ${a", "unterminated ${...} expansion"),
            ("echo `a", "unterminated backtick substitution"),
            ("echo $(case x in a) id;; esac)", "case statement"),
            ("((echo a) ; echo b)", "not an arithmetic command"),
            ("echo $(cat <<EOF)\nx\nEOF", "heredoc has no body"),
            ("cat <<\necho x", "without a delimiter word"),
            ("cat <<$X\nx\n$X", "not a plain word"),
            ("cat <<A $(echo\n)\nx\nA", "multi-line substitution"),
            ("cat <<A\n$(cat <<B\nx\nB\n)\nA", "heredoc inside a heredoc body"),
            ("cat <<A\n$(echo x\nA\n)", "inside a heredoc body"),
        ],
    )
    def test_unparseable_constructs_are_refused(self, command: str, reason: str):
        result = scan(command)
        assert result.error is not None and reason in result.error, result.error
        verdict = check_bash_command(command)
        assert not verdict.safe
        assert verdict.check_id == CheckID.UNPARSEABLE_QUOTING, verdict

    @pytest.mark.parametrize(
        "command",
        [
            # Case inside $(...) at argument position is just a word.
            "echo $(grep case notes.txt)",
            # Subshell with a space is fine.
            "( (echo a) ; echo b )",
            "for ((i = 0; i < 3; i++)); do echo $i; done",
            "echo \"${HOME:-'}'}\"",
        ],
    )
    def test_supported_look_alikes_parse(self, command: str):
        assert scan(command).error is None

    def test_scan_is_linear_at_the_cap(self):
        """Deep nesting and long runs stay far below the hook budget."""
        cap = bash_security.MAX_COMMAND_BYTES
        commands = [
            "\"$(" * (cap // 3),
            "$'\\'' " * (cap // 6),
            "cat <<A\n" + "x\\\n" * (cap // 4),
            "((" * (cap // 2),
            "echo " + "\"$(echo '\"')\" " * (cap // 18),
        ]
        for command in commands:
            start = time.perf_counter()
            scan(command + " ")  # bypass the lru_cache entry of a prior test
            assert time.perf_counter() - start < 1.0, command[:20]


class TestNewBypassShapes:
    """Shapes found while fixing BS3121-01 (same root cause)."""

    @pytest.mark.parametrize(
        "command",
        [
            # \<newline> vanishes in bash, so the target is /tmp/../etc/...
            "echo x > /tmp/.\\\n./etc/zz-fake",
            "echo x > \"/tmp/.\\\n./etc/zz-fake\"",
            # A # at a token start inside $(...) comments out the ), so the
            # substitution continues past it.
            "echo \"$(echo x # )\" $(touch /tmp/zz-sentinel)\n)\"",
            # (( that is really a subshell with a comment in it.
            "((echo a) #'\n) ; echo $(touch /tmp/zz-sentinel) #')",
            # Checks 15 and 21 had their own quote model too.
            "echo $'\\'' a\\ b",
            "echo $'\\'' x \\> y",
            "echo \"$(echo '\"')\" a\\ b",
        ],
    )
    def test_blocked(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, command

    @pytest.mark.parametrize(
        "command",
        [
            # bash: the << is inside $'...' and the "body" lines are code.
            "cat $'\\' <<'EOF'\n'; echo $(touch /tmp/zz-sentinel) #\nEOF\n\\'",
            "cat $'\\' <<'EOF'\n'; echo $(touch /tmp/zz-sentinel) #\nEOF\n#'",
            # bash: the # starts a comment, so there is no heredoc at all.
            "cat x #' <<' <<'EOF'\necho $(touch /tmp/zz-sentinel)\nEOF",
        ],
    )
    def test_inert_heredoc_strip_needs_the_tokenizer_to_agree(self, command: str):
        assert bash_security._strip_inert_heredoc_body(command) is None
        assert not check_bash_command(command).safe

    def test_git_heredoc_strip_needs_the_tokenizer_to_agree(self):
        command = (
            "git commit -F - $'\\' <<'EOF'\n'; echo $(touch /tmp/zz-sentinel) #\nEOF"
        )
        assert bash_security._benign_git_heredoc_sanitized(command) is None
        assert not check_bash_command(command).safe
        # The canonical shape still strips.
        assert bash_security._benign_git_heredoc_sanitized(
            "git commit -F - <<'EOF'\nfix: it's done; a < b\nEOF"
        ) == "git commit -F -"

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > /tmp/ok\\\n.txt",
            "grep -n \"it's\" notes.txt # see notes",
            "echo $'\\'' done",
        ],
    )
    def test_allowed(self, command: str):
        result = check_bash_command(command)
        assert result.safe, f"{command!r}: {result.message}"


# ---------------------------------------------------------------------------
# Differential test against bash
# ---------------------------------------------------------------------------

BASH = shutil.which("bash")
SEED = 3128
WORD_COMMANDS = 400
HEREDOC_COMMANDS = 120

# No glob, tilde, brace or backtick characters unquoted, and a $ only before
# a character that makes it literal: nothing is expanded, so what bash
# prints is exactly its word splitting and quote removal. The alphabet
# cannot spell a command either.
PLAIN = "qxzQXZ019_-./=:%+,@^"
QUOTED_TEXT = PLAIN + " \t\"'#;|&<>()*?[]{}~!$`\\\n"
ESCAPABLE = " '\"#;|&<>()$`\\*?[]{}~!q\n"
# `$` followed by one of these is a literal `$` in bash, quoted or not.
LITERAL_DOLLARS = ["$.", "$/", "$=", "$:", "$%", "$+", "$,", "$^"]


def _require_bash() -> str:
    """The differential tests are the tokenizer's ground truth: a host
    without bash must fail them loudly, not skip them (BS3128-03)."""
    if BASH is None:
        pytest.fail("bash is required for the tokenizer differential tests")
    return BASH


def _single_quoted(rng: random.Random) -> str:
    text = "".join(rng.choice(QUOTED_TEXT.replace("'", "")) for _ in range(rng.randint(0, 6)))
    return f"'{text}'"


def _double_quoted_body(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 6)):
        roll = rng.random()
        if roll < 0.25:
            parts.append("\\" + rng.choice('"\\$`q\n'))
        elif roll < 0.33:
            parts.append(rng.choice(LITERAL_DOLLARS))
        else:
            parts.append(rng.choice(QUOTED_TEXT.replace('"', "").replace("\\", "")
                                    .replace("$", "").replace("`", "")))
    return "".join(parts)


def _ansi_c_body(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 6)):
        if rng.random() < 0.35:
            parts.append(rng.choice(["\\'", "\\\\", '\\"', "\\n", "\\t"]))
        else:
            parts.append(rng.choice(QUOTED_TEXT.replace("'", "").replace("\\", "")))
    return "".join(parts)


def _piece(rng: random.Random, first_in_word: bool) -> str:
    roll = rng.random()
    if roll < 0.3:
        alphabet = PLAIN if first_in_word else PLAIN + "#"
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
    if roll < 0.45:
        return _single_quoted(rng)
    if roll < 0.62:
        return f'"{_double_quoted_body(rng)}"'
    if roll < 0.8:
        return f"$'{_ansi_c_body(rng)}'"
    if roll < 0.87:
        return f'$"{_double_quoted_body(rng)}"'
    if roll < 0.93:
        return rng.choice(LITERAL_DOLLARS)
    return "\\" + rng.choice(ESCAPABLE)


def _word(rng: random.Random) -> str:
    pieces = [_piece(rng, True)]
    for _ in range(rng.randint(0, 2)):
        pieces.append(_piece(rng, False))
    word = "".join(pieces)
    if word == "\\\n":  # a lone continuation is not a word
        word = "q"
    return word


def _word_command(rng: random.Random) -> str:
    words = [_word(rng) for _ in range(rng.randint(1, 5))]
    line = "p"
    for word in words:
        line += rng.choice([" ", "  ", "\t"]) + word
    if rng.random() < 0.3:
        comment = "".join(rng.choice(QUOTED_TEXT.replace("\n", "")) for _ in range(6))
        if comment.endswith("\\"):
            comment += "q"  # keep the no-trailing-backslash invariant below
        line += rng.choice([" #", "\t# ", ";#"]) + comment
    # Never end on a bare backslash: the script's newline would continue it.
    assert not line.endswith("\\") or line.endswith("\\\\"), line
    return line


def _heredoc_command(rng: random.Random) -> tuple[str, bool, bool]:
    delimiter = rng.choice(["EOF", "X_1", "END"])
    quoting = rng.choice(["", "'", '"', "\\"])
    dash = rng.random() < 0.3
    opener = "<<-" if dash else "<<"
    if quoting == "\\":
        word = "\\" + delimiter
    else:
        word = f"{quoting}{delimiter}{quoting}"
    quoted = quoting != ""
    body_alphabet = "qxz 019#;|&<>()'\"{}*"
    if quoted:
        body_alphabet += "$`\\"
    lines = []
    for _ in range(rng.randint(0, 5)):
        roll = rng.random()
        if roll < 0.2:
            lines.append(rng.choice([delimiter + "q", " " + delimiter, delimiter + " ",
                                     "q" + delimiter]))
        elif roll < 0.3 and not dash:
            lines.append("\t" + delimiter)
        elif roll < 0.4 and not quoted and not dash:
            # Continuation: joined with the next line, so not a terminator.
            lines.append("qx\\")
            lines.append(delimiter)
        elif roll < 0.5 and not quoted:
            lines.append("q\\\\ \\$ \\` z")
        else:
            text = "".join(rng.choice(body_alphabet) for _ in range(rng.randint(0, 8)))
            lines.append(("\t" if dash and rng.random() < 0.5 else "") + text)
    terminator = ("\t" if dash and rng.random() < 0.5 else "") + delimiter
    command = f"h {opener}{word}\n" + "".join(line + "\n" for line in lines) + terminator
    return command, quoted, dash


def _decode_run(kind: int, text: str) -> str:
    if kind == bash_security._K_ANSI:
        escapes = {"'": "'", "\\": "\\", '"': '"', "n": "\n", "t": "\t"}
        out, index = [], 0
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text):
                out.append(escapes.get(text[index + 1], text[index:index + 2]))
                index += 2
                continue
            out.append(text[index])
            index += 1
        return "".join(out)
    if kind == bash_security._K_DQ:
        out, index = [], 0
        while index < len(text):
            if text[index] == "\\" and index + 1 < len(text):
                nxt = text[index + 1]
                if nxt in '$`"\\':
                    out.append(nxt)
                elif nxt != "\n":
                    out.append(text[index:index + 2])
                index += 2
                continue
            out.append(text[index])
            index += 1
        return "".join(out)
    return text


def _tokenizer_words(command: str) -> list[str]:
    """The argv the tokenizer's classification implies for a simple command."""
    result = scan(command)
    assert result.error is None, (command, result.error)
    words: list[str] = []
    current: list[str] | None = None
    run_kind, run_text = 0, []

    def flush_run() -> None:
        nonlocal run_kind, run_text
        if run_text and current is not None:
            current.append(_decode_run(run_kind, "".join(run_text)))
        run_kind, run_text = 0, []

    for index, ch in enumerate(command):
        kind = _kind(result.kinds[index])
        if kind == bash_security._K_COMMENT:
            break
        if kind == bash_security._K_CODE and ch in " \t;":
            flush_run()
            if current is not None:
                words.append("".join(current))
                current = None
            if ch == ";":
                rest = [_kind(k) for k in result.kinds[index + 1:]]
                assert all(k == bash_security._K_COMMENT for k in rest), command
                break
            continue
        if current is None:
            current = []
        if kind in (bash_security._K_ANSI, bash_security._K_DQ):
            if run_kind != kind:
                flush_run()
                run_kind = kind
            run_text.append(ch)
            continue
        flush_run()
        if kind in (bash_security._K_SQ_DELIM, bash_security._K_DQ_DELIM,
                    bash_security._K_ANSI_DELIM, bash_security._K_ESCAPE):
            continue
        if kind == bash_security._K_ESCAPED and ch == "\n":
            continue
        assert kind in (bash_security._K_CODE, bash_security._K_ESCAPED,
                        bash_security._K_SQ), (command, index, kind)
        current.append(ch)
    flush_run()
    if current is not None:
        words.append("".join(current))
    return words[1:]  # drop the function name


def _tokenizer_heredoc_body(command: str, quoted: bool, dash: bool) -> str:
    result = scan(command)
    assert result.error is None, (command, result.error)
    (heredoc,) = result.heredocs
    assert heredoc.quoted is quoted
    assert heredoc.terminator_start < len(command), command
    body = command[heredoc.body_start:heredoc.terminator_start]
    if dash:
        body = "".join(line.lstrip("\t") for line in body.splitlines(keepends=True))
    if not quoted:
        out, index = [], 0
        while index < len(body):
            if body[index] == "\\" and index + 1 < len(body):
                nxt = body[index + 1]
                if nxt in "$`\\":
                    out.append(nxt)
                elif nxt != "\n":
                    out.append(body[index:index + 2])
                index += 2
                continue
            out.append(body[index])
            index += 1
        body = "".join(out)
    return body


PRELUDE = (
    "set -f\n"
    "p() { printf '%s\\0' \"$#\" \"$@\"; }\n"
    "h() { IFS= read -r -d '' body; printf '%s\\0' 1 \"$body\"; }\n"
)


def test_tokenizer_agrees_with_bash_on_generated_commands(tmp_path: Path):
    bash = _require_bash()
    rng = random.Random(SEED)
    cases: list[tuple[str, str, bool, bool]] = []
    for _ in range(WORD_COMMANDS):
        cases.append(("words", _word_command(rng), False, False))
    for _ in range(HEREDOC_COMMANDS):
        command, quoted, dash = _heredoc_command(rng)
        cases.append(("heredoc", command, quoted, dash))
    rng.shuffle(cases)

    script = tmp_path / "corpus.sh"
    script.write_text(
        PRELUDE + "".join(command + "\n" for _, command, _, _ in cases),
        encoding="utf-8",
    )
    start = time.perf_counter()
    proc = subprocess.run(
        [bash, "--norc", "--noprofile", str(script)],
        capture_output=True, timeout=20, cwd=tmp_path,
        # Nothing external can run even if a line were misparsed.
        env={"PATH": "/nonexistent", "LC_ALL": "C"},
    )
    assert proc.stderr == b"", proc.stderr.decode(errors="replace")[:500]

    fields = proc.stdout.decode("utf-8").split("\0")
    position = 0
    for kind, command, quoted, dash in cases:
        count = int(fields[position])
        bash_view = fields[position + 1:position + 1 + count]
        position += 1 + count
        if kind == "words":
            assert _tokenizer_words(command) == bash_view, command
        else:
            assert [_tokenizer_heredoc_body(command, quoted, dash)] == bash_view, command
    assert position == len(fields) - 1  # trailing NUL
    assert time.perf_counter() - start < 20


def test_differential_corpus_exercises_the_tricky_forms():
    """Guard against a generator change that silently drops coverage."""
    rng = random.Random(SEED)
    corpus = "".join(_word_command(rng) for _ in range(WORD_COMMANDS))
    for form in ("$'", "\\'", '$"', '\\"', "#", "\\\n", "'#", "\\\\", "\\$"):
        assert form in corpus, form
    assert any(dollar in corpus for dollar in LITERAL_DOLLARS)
    assert any(f'"{dollar}' in corpus or f'{dollar}"' in corpus for dollar in LITERAL_DOLLARS)
    heredocs = [_heredoc_command(rng) for _ in range(HEREDOC_COMMANDS)]
    assert any(q for _, q, _ in heredocs) and any(not q for _, q, _ in heredocs)
    assert any(d for _, _, d in heredocs)
    assert any("qx\\\n" in c for c, _, _ in heredocs)


# ---------------------------------------------------------------------------
# Differential test: substitutions (task 3133, IND3128-07)
# ---------------------------------------------------------------------------
#
# The word corpus above never expands anything. This one places numbered
# marker substitutions - `m N`, a shell function that only writes N to a
# file descriptor - in every context the review found disagreements in,
# runs the lines in bash, and checks the fail-closed property: a marker bash
# RAN is never classified as proven inert, and the whole line is refused.
# Only the prelude's functions and bash builtins can run (PATH=/nonexistent,
# cwd in tmp_path), and `m` touches nothing but the marks file.

SUBSTITUTION_LINES = 300
OPENER = "\x01"  # placeholder: the first character of the substitution
MARKER = "\x02"  # placeholder: replaced by the marker call "m N"

# Word-position templates. Comments say what bash does, for the reader; the
# test takes the answer from bash, not from these comments.
SUBSTITUTION_WORDS = [
    f"{OPENER}$({MARKER})",                       # runs
    f'"{OPENER}$({MARKER})"',                     # runs
    f"{OPENER}`{MARKER}`",                        # runs
    f'"{OPENER}`{MARKER}`"',                      # runs
    f"'{OPENER}$({MARKER})'",                     # inert: top-level quote
    f"'{OPENER}`{MARKER}`'",                      # inert
    f'"\\{OPENER}$({MARKER})"',                   # inert: \$ in "..."
    f'"\\{OPENER}`{MARKER}\\`"',                  # inert
    f"$'{OPENER}$({MARKER})'",                    # literal, not proven
    f"{OPENER}$\\\n({MARKER})",                   # runs (continuation)
    f'"{OPENER}$\\\n({MARKER})"',                 # runs
    f"'{OPENER}$\\\n({MARKER})'",                 # inert: no join in '...'
    f"{OPENER}$\\\n\\\n({MARKER})",               # runs
    f"{OPENER}<({MARKER})",                       # runs
    f"{OPENER}<\\\n({MARKER})",                   # runs
    f"{OPENER}>({MARKER})",                       # runs
    f"$(( 0 {OPENER}$({MARKER}) ))",              # runs
    f'"$(( 0 {OPENER}$({MARKER}) ))"',            # runs
    f"$[ 0 {OPENER}$({MARKER}) ]",                # runs
    f'"$[ 0 {OPENER}$({MARKER}) ]"',              # runs
    f"${{v:-{OPENER}$({MARKER})}}",               # runs (v is unset)
    f'"${{v:-{OPENER}$({MARKER})}}"',             # runs
    f"\"${{v:-'{OPENER}$({MARKER})'}}\"",         # runs: ' is literal
    f"\"${{w:+'{OPENER}$({MARKER})'}}\"",         # runs (w is set)
    f"\"${{v:-'{OPENER}`{MARKER}`'}}\"",          # runs
    f"\"${{w#'{OPENER}$({MARKER})'}}\"",          # inert: pattern quotes
    f"\"${{w/'{OPENER}$({MARKER})'/q}}\"",        # inert
    f"${{v:+{OPENER}$({MARKER})}}",               # does not run
    f"\"$(p '{OPENER}$({MARKER})')\"",            # nested quote: literal
]
# Command-position templates, run before the `p` command on the same line.
SUBSTITUTION_STATEMENTS = [
    f"(( 0 {OPENER}$({MARKER}) ))",               # runs
    f"let 'a[{OPENER}$({MARKER})]'",              # runs (IND3128-03)
    f"xa='a[{OPENER}$({MARKER})]'; (( xa ))",     # runs
    f"xa='a[{OPENER}$({MARKER})]'; : $(( xa ))",  # runs
    f"test -v 'a[{OPENER}$({MARKER})]'",          # runs
    f"printf -v 'a[{OPENER}$({MARKER})]' q",      # runs
    f"xa='a[{OPENER}$({MARKER})]'; [[ xa -eq 1 ]]",  # runs
    f": '{OPENER}$({MARKER})'",                   # inert
    f"let {OPENER}$'a[\\x24({MARKER})]'",         # runs: escapes decoded
]
SUBSTITUTION_HEREDOCS = [
    f"p <<'EOF'\n{OPENER}$({MARKER})\nEOF",       # inert: quoted body
    f"p <<EOF\n{OPENER}$({MARKER})\nEOF",         # runs
    f"p <<E\\\nOF\n{OPENER}$({MARKER})\nEOF",     # runs: EOF is unquoted
    f"p <<EOF\n{OPENER}$\\\n({MARKER})\nEOF",     # runs
    f"p <<\"EOF\"\nq {OPENER}`{MARKER}`\nEOF",    # inert
    f"p <<EOF\n${{v:-'{OPENER}$({MARKER})'}}\nEOF",   # runs: ' is literal
    f"p <<EOF\n${{w#'{OPENER}$({MARKER})'}}\nEOF",    # inert: pattern quotes
]

SUBSTITUTION_PRELUDE = (
    "set -f\n"
    "exec 3>>marks.txt\n"
    "p() { :; }\n"
    "m() { printf '%s\\n' \"$1\" >&3; }\n"
    "unset v\n"
    "w=set\n"
)


def _fill(template: str, prefix: str, marker: int) -> tuple[str, int]:
    """Return (text, opener index in prefix + text) for one template."""
    opener = template.index(OPENER)
    text = template.replace(OPENER, "", 1).replace(MARKER, f"m {marker}", 1)
    return text, len(prefix) + opener


def _substitution_line(rng: random.Random, first_marker: int) -> tuple[str, list[tuple[int, int]]]:
    """One generated line and its (marker, opener index) pairs."""
    markers: list[tuple[int, int]] = []
    marker = first_marker
    if rng.random() < 0.12:
        text, opener = _fill(rng.choice(SUBSTITUTION_HEREDOCS), "", marker)
        return text, [(marker, opener)]
    line = ""
    if rng.random() < 0.25:
        text, opener = _fill(rng.choice(SUBSTITUTION_STATEMENTS), line, marker)
        markers.append((marker, opener))
        marker += 1
        line += text + "; "
    line += "p"
    for _ in range(rng.randint(0, 3)):
        line += " "
        if rng.random() < 0.3:
            line += "".join(rng.choice("qxz019") for _ in range(rng.randint(1, 3)))
            continue
        text, opener = _fill(rng.choice(SUBSTITUTION_WORDS), line, marker)
        markers.append((marker, opener))
        marker += 1
        line += text
    if rng.random() < 0.15:
        text, opener = _fill(f" # {OPENER}$({MARKER})", line, marker)  # comment
        markers.append((marker, opener))
        line += text
    return line, markers


def _substitution_corpus() -> list[tuple[str, list[tuple[int, int]]]]:
    rng = random.Random(SEED + 5)
    corpus = []
    next_marker = 1
    for _ in range(SUBSTITUTION_LINES):
        line, markers = _substitution_line(rng, next_marker)
        next_marker += len(markers) + 1
        corpus.append((line, markers))
    return corpus


def _lookalikes_by_original_index(line: str) -> dict[int, "bash_security._Lookalike"]:
    joined = bash_security._join_line_continuations(line)
    assert joined.complete, line
    return {
        joined.origin[item.start]: item
        for item in bash_security._substitution_lookalikes(joined.text)
    }


def test_substitutions_bash_runs_are_never_proven_inert(tmp_path: Path):
    bash = _require_bash()
    corpus = _substitution_corpus()
    script = tmp_path / "substitutions.sh"
    script.write_text(
        SUBSTITUTION_PRELUDE + "".join(line + "\n" for line, _ in corpus),
        encoding="utf-8",
    )
    start = time.perf_counter()
    proc = subprocess.run(
        [bash, "--norc", "--noprofile", str(script)],
        capture_output=True, timeout=20, cwd=tmp_path,
        env={"PATH": "/nonexistent", "LC_ALL": "C"},
    )
    # Arithmetic on literal apostrophes is an expansion error in bash; it
    # aborts only that line. Anything else on stderr is a generator bug.
    for message in proc.stderr.decode(errors="replace").splitlines():
        assert "syntax error" in message and "error token" in message, message
    ran = {int(n) for n in (tmp_path / "marks.txt").read_text().split()}

    inert_seen = ran_seen = 0
    for line, markers in corpus:
        lookalikes = _lookalikes_by_original_index(line)
        for marker, opener in markers:
            lookalike = lookalikes.get(opener)
            assert lookalike is not None, (line, marker, opener)
            if marker in ran:
                ran_seen += 1
                assert not lookalike.inert, f"bash ran marker {marker} in {line!r}"
            inert_seen += lookalike.inert
        if any(marker in ran for marker, _ in markers):
            assert not check_bash_command(line).safe, f"allowed, but bash ran: {line!r}"
    # The corpus exercises both sides of the property.
    assert ran_seen >= 150, ran_seen
    assert inert_seen >= 40, inert_seen
    assert time.perf_counter() - start < 20


def test_substitution_corpus_exercises_every_template():
    corpus = "\n".join(line for line, _ in _substitution_corpus())
    for template in SUBSTITUTION_WORDS + SUBSTITUTION_STATEMENTS + SUBSTITUTION_HEREDOCS:
        head = template.split(MARKER)[0].replace(OPENER, "")
        assert head in corpus, template
