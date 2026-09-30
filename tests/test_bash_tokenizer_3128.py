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
        ],
    )
    def test_blocked(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, command

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

# No glob, tilde, brace, $ or backtick characters unquoted: nothing is
# expanded, so what bash prints is exactly its word splitting and quote
# removal. The alphabet cannot spell a command either.
PLAIN = "qxzQXZ019_-./=:%+,@^"
QUOTED_TEXT = PLAIN + " \t\"'#;|&<>()*?[]{}~!$`\\\n"
ESCAPABLE = " '\"#;|&<>()$`\\*?[]{}~!q\n"


def _single_quoted(rng: random.Random) -> str:
    text = "".join(rng.choice(QUOTED_TEXT.replace("'", "")) for _ in range(rng.randint(0, 6)))
    return f"'{text}'"


def _double_quoted_body(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 6)):
        roll = rng.random()
        if roll < 0.25:
            parts.append("\\" + rng.choice('"\\$`q\n'))
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


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_tokenizer_agrees_with_bash_on_generated_commands(tmp_path: Path):
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
        [BASH, "--norc", "--noprofile", str(script)],
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
    for form in ("$'", "\\'", '$"', '\\"', "#", "\\\n", "'#", "\\\\"):
        assert form in corpus, form
    heredocs = [_heredoc_command(rng) for _ in range(HEREDOC_COMMANDS)]
    assert any(q for _, q, _ in heredocs) and any(not q for _, q, _ in heredocs)
    assert any(d for _, _, d in heredocs)
    assert any("qx\\\n" in c for c, _, _ in heredocs)
