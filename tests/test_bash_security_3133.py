"""Task 3133: fix-forward of 3128 - tokenizer/bash disagreements fail closed.

The independent review of 3128 (IND3128-01..07) found two more places where
``bash_security``'s tokenizer and bash disagree, and bash ran a substitution
the checker had classified as safe:

* IND3128-01: bash deletes backslash-newline before it tokenizes, so ``$``,
  backslash-newline, ``(cmd)`` is a command substitution.
* IND3128-02: inside ``$[...]``, ``(( ))``, ``$(( ))`` and a double-quoted
  ``${x:-...}`` an apostrophe is an ordinary character, so a ``$(...)``
  between two of them runs.
* IND3128-03: arithmetic evaluation expands an array subscript again, so a
  single-quoted ``a[$(cmd)]`` runs under ``let``, ``(( ))``, ``[[ -eq ]]``,
  ``test -v``, ``printf -v`` and ``declare -i``.
* IND3128-04: ``cd /etc; echo x > f`` writes /etc/f.

The fix is a design principle rather than three patches: substitution-
looking text counts as live unless the tokenizer PROVES it inert (check 26),
continuations are joined before any analysis, and arithmetic contexts refuse
apostrophes (check 25). Every command in this file is classified only; none
is executed.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa import bash_security
from equipa.bash_security import CheckID, check_bash_command

SENTINEL = "touch /tmp/zz-sentinel"


# ---------------------------------------------------------------------------
# IND3128-01: backslash-newline continuations are joined first
# ---------------------------------------------------------------------------

class TestLineContinuations:

    @pytest.mark.parametrize(
        "command",
        [
            # The reviewer's forms: unquoted, double-quoted, process substitution.
            f"echo $\\\n({SENTINEL})",
            f'echo "$\\\n({SENTINEL})"',
            f"cat <\\\n({SENTINEL})",
            # More than one continuation between the two characters.
            f"echo $\\\n\\\n({SENTINEL})",
            f'echo "$\\\n\\\n({SENTINEL})"',
            # $[ and ${ split the same way.
            f"echo $\\\n[ $({SENTINEL}) ]",
            f"echo $\\\n{{HOME:-$({SENTINEL})}}",
            # Inside a backtick and inside a substitution.
            f"echo $(echo $\\\n({SENTINEL}))",
            # <<E\<newline>OF is the UNQUOTED delimiter EOF: the body expands.
            f"cat <<E\\\nOF\n$({SENTINEL})\nEOF",
            # A continuation inside an unquoted heredoc body.
            f"cat <<EOF\n$\\\n({SENTINEL})\nEOF",
            # Output process substitution split the same way.
            "tee >\\\n(sh) < in.txt",
        ],
    )
    def test_split_substitution_blocks(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, f"bypass allowed: {command!r}"

    @pytest.mark.parametrize(
        "command",
        [
            "python3 -m pytest -q \\\n  tests/test_config.py",
            "git log --oneline \\\n  -5",
            "echo x > /tmp/ok\\\n.txt",
            "grep -rn \\\n  'needle' equipa/",
            "echo \"a\\\nb\"",
        ],
    )
    def test_ordinary_continuations_still_pass(self, command: str):
        result = check_bash_command(command)
        assert result.safe, f"{command!r}: check {result.check_id}: {result.message}"

    @pytest.mark.parametrize(
        ("command", "joined"),
        [
            ("echo $\\\n(id)", "echo $(id)"),
            ('echo "$\\\n(id)"', 'echo "$(id)"'),
            ("cat <\\\n(id)", "cat <(id)"),
            ("a\\\n\\\nb", "ab"),
            # Bash keeps these: single quotes, $'...', comments, escaped \.
            ("echo 'a\\\nb'", "echo 'a\\\nb'"),
            ("echo $'a\\\nb'", "echo $'a\\\nb'"),
            ("echo a # x \\\necho b", "echo a # x \\\necho b"),
            ("echo a\\\\\nb", "echo a\\\\\nb"),
            # Quoted heredoc body: kept; unquoted body: joined.
            ("cat <<'E'\nx\\\ny\nE", "cat <<'E'\nx\\\ny\nE"),
            ("cat <<E\nx\\\ny\nE", "cat <<E\nxy\nE"),
            # The delimiter word itself.
            ("cat <<E\\\nOF\nx\nEOF", "cat <<EOF\nx\nEOF"),
        ],
    )
    def test_join_matches_bash(self, command: str, joined: str):
        result = bash_security._join_line_continuations(command)
        assert result.complete
        assert result.text == joined
        assert [command[index] for index in result.origin] == list(joined)

    def test_continued_delimiter_is_unquoted_after_the_join(self):
        joined = bash_security._join_line_continuations("cat <<E\\\nOF\n$(id)\nEOF")
        (heredoc,) = bash_security._scan_shell(joined.text).heredocs
        assert not heredoc.quoted

    def test_rescan_joins_continuations_a_first_pass_misread(self):
        """``$\\<nl>('`` hides a continuation inside what the raw scan reads
        as a single quote; after the first join it is code."""
        command = "echo $\\\n('$\\\n(id)')"
        result = bash_security._join_line_continuations(command)
        assert result.complete
        assert result.text == "echo $('$\\\n(id)')"
        assert not check_bash_command(command).safe

    def test_pass_cap_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(bash_security, "_MAX_CONTINUATION_PASSES", 0)
        result = check_bash_command("echo a\\\nb")
        assert not result.safe
        assert result.check_id == CheckID.UNPARSEABLE_QUOTING


# ---------------------------------------------------------------------------
# IND3128-02: apostrophes that bash treats as ordinary characters
# ---------------------------------------------------------------------------

class TestLiteralApostrophes:

    @pytest.mark.parametrize(
        "command",
        [
            # The reviewer's forms.
            f"echo \"$[ '$({SENTINEL})' ]\"",
            f"echo \"$(( '$({SENTINEL})' ))\"",
            f"(( '$({SENTINEL})' ))",
            f"echo \"${{RV_UNSET:-'$({SENTINEL})'}}\"",
            f"echo \"${{HOME:+'$({SENTINEL})'}}\"",
            f"echo \"${{RV_UNSET:-'`{SENTINEL}`'}}\"",
            # The rest of the family.
            f"echo \"${{RV_UNSET:='$({SENTINEL})'}}\"",
            f"echo \"${{RV_UNSET:?'$({SENTINEL})'}}\"",
            f"echo \"${{RV_UNSET-'$({SENTINEL})'}}\"",
            f"echo \"${{RV_UNSET:-x${{Y:-'$({SENTINEL})'}}}}\"",
            f"echo $[ '$({SENTINEL})' ]",
            f"echo $(( '$({SENTINEL})' ))",
            f"for (( i = '$({SENTINEL})'; i < 1; i++ )); do :; done",
            f"echo \"$(( $'$({SENTINEL})' ))\"",
            # Pattern operators treat them as quotes (inert), but the checker
            # does not tell the operators apart: fail closed.
            f"echo \"${{HOME#'$({SENTINEL})'}}\"",
            f"echo \"${{HOME/'$({SENTINEL})'/x}}\"",
        ],
    )
    def test_substitution_between_literal_apostrophes_blocks(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, f"bypass allowed: {command!r}"

    @pytest.mark.parametrize(
        "command",
        ["echo $(( '1' ))", "(( x == 'a' ))", "echo $[ 'x' ]", "echo $(( $'1' ))"],
    )
    def test_apostrophe_in_arithmetic_is_unparseable(self, command: str):
        scan = bash_security._scan_shell(command)
        assert scan.error is not None and "single quote" in scan.error
        result = check_bash_command(command)
        assert result.check_id == CheckID.UNPARSEABLE_QUOTING, result

    def test_double_quoted_default_word_is_scanned_as_text(self):
        command = "echo \"${X:-'$(id)'}\""
        scan = bash_security._scan_shell(command)
        assert scan.error is None
        assert [s.kind for s in scan.substitutions] == ["$("]
        assert scan.substitutions[0].in_double_quotes

    @pytest.mark.parametrize(
        "command",
        ["cat <<EOF\n${v:-'$(id)'}\nEOF", "cat <<EOF\n${v:-x${w:+'`id`'}}\nEOF"],
    )
    def test_unquoted_heredoc_body_default_word_is_scanned_as_text(self, command: str):
        """An unquoted heredoc body expands like double quotes (bash ran
        `${v:-'$(m)'}` there), so the substitution must be recorded."""
        scan = bash_security._scan_shell(command)
        assert scan.error is None
        assert len(scan.substitutions) == 1
        assert not check_bash_command(command).safe

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$((1 + 2))"',
            "(( count++ ))",
            'echo "${HOME:-/tmp}"',
            'echo "${name#prefix}"',
            "echo ${#}",
            'echo "${HOME:-\'}\'}"',
        ],
    )
    def test_ordinary_arithmetic_and_expansions_pass(self, command: str):
        assert bash_security._scan_shell(command).error is None
        result = check_bash_command(command)
        assert result.safe or result.check_id != CheckID.SUBSTITUTION_LOOKALIKE, result


# ---------------------------------------------------------------------------
# IND3128-03: arithmetic evaluation expands quoted array subscripts again
# ---------------------------------------------------------------------------

class TestArithmeticSubscripts:

    @pytest.mark.parametrize(
        "command",
        [
            # The reviewer's six forms.
            f"x='a[$({SENTINEL})]'; echo \"$((x))\"",
            f"x='a[$({SENTINEL})]'; (( x ))",
            f"x='a[$({SENTINEL})]'; [[ x -eq 1 ]]",
            f"let 'a[$({SENTINEL})]'",
            f"test -v 'a[$({SENTINEL})]'",
            f"printf -v 'a[$({SENTINEL})]' x",
            # declare -i, [ -v ], unset, read, spliced keywords and subscripts.
            f"declare -i x; x='a[$({SENTINEL})]'",
            f"[ -v 'a[$({SENTINEL})]' ]",
            f"unset 'a[$({SENTINEL})]'",
            f"read 'a[$({SENTINEL})]' <<< 1",
            f"l'e't 'a[$({SENTINEL})]'",
            f"x='a['; y='$({SENTINEL})]'; (( $x$y ))",
            f"let 'a[`{SENTINEL}`]'",
            f"declare -a 'x=($({SENTINEL}))'",
            # An escaped $ in double quotes is text too until it is evaluated.
            f"let \"a[\\$({SENTINEL})]\"",
            # A quoted heredoc body read into a variable, then evaluated.
            f"read -r x <<'EOF'\na[$({SENTINEL})]\nEOF",
        ],
    )
    def test_quoted_subscript_in_evaluating_command_blocks(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, f"bypass allowed: {command!r}"

    @pytest.mark.parametrize(
        "command",
        [
            f"let $'a[\\x24({SENTINEL})]'",
            f"let $'a[\\044({SENTINEL})]'",
            f"let $'a[\\u24({SENTINEL})]'",
            f"let $'a[\\U00000024({SENTINEL})]'",
            f"let $'a[\\x60{SENTINEL}\\x60]'",
            f"x=$'a[\\x24({SENTINEL})]'; (( x ))",
        ],
    )
    def test_ansi_c_escapes_that_spell_a_substitution_block(self, command: str):
        """Check 4 happens to refuse these today; check 26 must too, on its
        own, because bash decodes the escapes before let evaluates them."""
        assert not check_bash_command(command).safe
        result = bash_security._check_substitution_lookalikes(command)
        assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE, result
        assert "escapes spell" in result.message

    @pytest.mark.parametrize(
        ("body", "decoded"),
        [
            ("a\\x24(b", "a$(b"), ("\\044(", "$("), ("\\44(", "$("),
            ("\\u0024(", "$("), ("\\U00000024[", "$["), ("\\x60", "`"),
            ("\\t\\n\\\\\\'", "\t\n\\'"), ("\\q", "\\q"), ("\\cA", "\x01"),
            ("\\x", "\\x"), ("\\UFFFFFFFF", "�"),
        ],
    )
    def test_decode_ansi_c(self, body: str, decoded: str):
        assert bash_security._decode_ansi_c(body) == decoded

    def test_ansi_c_text_without_evaluation_is_left_to_the_other_checks(self):
        assert bash_security._substitution_lookalikes("echo $'\\x24(id)'") == []

    @pytest.mark.parametrize(
        "command",
        [
            f"let 'a[$({SENTINEL})]'",
            f"x='a[$({SENTINEL})]'; (( x ))",
            f"test -v 'a[$({SENTINEL})]'",
        ],
    )
    def test_check_26_names_the_arithmetic_reason(self, command: str):
        result = check_bash_command(command)
        assert result.check_id == CheckID.SUBSTITUTION_LOOKALIKE, result
        assert "array subscripts" in result.message


# ---------------------------------------------------------------------------
# The fail-closed principle: only proven-inert contexts pass (check 26)
# ---------------------------------------------------------------------------

class TestFailClosedLookalikes:

    @pytest.mark.parametrize(
        "command",
        [
            f"echo $'$({SENTINEL})'",
            f"echo $'`{SENTINEL}`'",
            f"echo \\`{SENTINEL}\\`",
            # Escaped $ in double quotes NESTED in a substitution.
            f'echo $(echo "\\$({SENTINEL})")',
            # Single quotes nested in a substitution.
            f"echo $(echo '$({SENTINEL})')",
            f'echo "$(echo \'$({SENTINEL})\')"',
            # Unquoted heredoc body text that bash leaves literal.
            f"cat <<EOF\n\\$({SENTINEL})\nEOF",
            # Pattern text inside ${...}.
            f'echo "${{HOME/\\$(/x}}"',
            f'echo "<({SENTINEL})"',
            f"echo \"$[1]\" 'a[$({SENTINEL})]'",
        ],
    )
    def test_unproven_contexts_block(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, f"bypass allowed: {command!r}"

    @pytest.mark.parametrize(
        "command",
        [
            "grep -rn '\\$(' equipa/",
            "awk '{print $(NF)}' data/in.txt",
            "echo '$(date) is literal text'",
            "sed -n '/`/p' notes.txt",
            'echo "\\$(id) and \\`id\\` are literal"',
            "cat > notes.md <<'EOF'\nrun `make` or $(make)\nEOF",
            "grep -c '<(' scripts/run.sh",
        ],
    )
    def test_proven_inert_contexts_pass(self, command: str):
        result = check_bash_command(command)
        assert result.safe, f"{command!r}: check {result.check_id}: {result.message}"

    def test_lookalike_classification(self):
        command = "echo '$(a)' \"\\$(b)\" \"$(c)\" $'$(d)' # $(e)"
        lookalikes = bash_security._substitution_lookalikes(command)
        by_text = {command[item.start + 2]: item for item in lookalikes}
        assert by_text["a"].inert and by_text["b"].inert
        assert not by_text["c"].inert  # live code: check 8 judges it
        assert not by_text["d"].inert and not by_text["e"].inert

    def test_evaluating_construct_revokes_every_proof(self):
        lookalikes = bash_security._substitution_lookalikes("let x; echo '$(a)'")
        assert [item.inert for item in lookalikes] == [False]
        assert lookalikes[0].quoted_context


# ---------------------------------------------------------------------------
# IND3128-04: a cd earlier in the command moves relative redirect targets
# ---------------------------------------------------------------------------

class TestRedirectAfterDirectoryChange:

    @pytest.mark.parametrize(
        "command",
        [
            "cd /etc; echo x > zz-fake",
            "cd /etc && echo x >> zz-fake",
            "cd -P /etc && echo x > zz-fake",
            "cd -- /etc && echo x > zz-fake",
            "pushd /etc && echo x > zz-fake",
            "cd /etc && sort < shadow",
            "cd; echo x > .bashrc",
            "cd ~ && echo x > zz-fake",
            "cd - && echo x > zz-fake",
            "cd .. && echo x > zz-fake",
            "cd sub/../.. && echo x > zz-fake",
            'cd "$DIR" && echo x > zz-fake',
            "popd && echo x > zz-fake",
            "c'd' /etc; echo x > zz-fake",
            "\\cd /etc; echo x > zz-fake",
            "CDPATH=/ cd etc && echo x > zz-fake",
            "(cd /etc; echo x > zz-fake)",
            "cd /tmp; cd /etc; echo x > zz-fake",
            "cd /tmp/w && cd ../../etc && echo x > zz-fake",
            "c=cd; $c /etc; echo x > zz-fake",
        ],
    )
    def test_relative_target_after_cd_outside_blocks(self, command: str):
        result = check_bash_command(command)
        assert not result.safe, f"bypass allowed: {command!r}"
        assert result.check_id in (CheckID.OUTPUT_REDIRECTION, CheckID.INPUT_REDIRECTION)

    @pytest.mark.parametrize(
        "command",
        [
            "cd tests && python3 -m pytest -q > out.txt",
            "cd /tmp/work && echo x > out.txt",
            "echo x > out.txt; cd /etc",
            "cd /etc && echo x > /tmp/out.txt",
            "cd /srv/app && git status 2>&1 | tail -5",
            "cd /srv/app && ls 2>/dev/null",
            "echo abcd > out.txt",
        ],
    )
    def test_safe_directory_changes_pass(self, command: str):
        result = check_bash_command(command)
        assert result.safe, f"{command!r}: check {result.check_id}: {result.message}"

    def test_directory_changes_are_resolved_in_order(self):
        command = "cd /tmp/a; cd b; cd /etc; cd"
        assert bash_security._directory_changes(command) == [
            (0, "/tmp/a"), (11, "/tmp/a/b"), (17, "/etc"), (26, None),
        ]

    def test_message_names_the_real_target(self):
        result = check_bash_command("cd /etc; echo x > zz-fake")
        assert "/etc/zz-fake" in result.message


# ---------------------------------------------------------------------------
# The new passes stay linear at the length cap (sandbox-07)
# ---------------------------------------------------------------------------

CAP = bash_security.MAX_COMMAND_BYTES


@pytest.mark.parametrize(
    "command",
    [
        "echo " + "$\\\n(" * ((CAP - 5) // 4),                    # continuations
        "echo " + "'$\\\n(' " * ((CAP - 5) // 7),                 # unjoined ones
        "cat <<'A'\n`x`\nA\n" * (CAP // 17),                      # bodies x look-alikes
        "echo " + "'`' " * ((CAP - 5) // 4),                      # quoted look-alikes
        "cd /tmp/a; " * (CAP // 12) + "echo x > f",               # directory changes
        "cd a; echo x > f; " * (CAP // 19),
        "echo \"" + "\\$(" * ((CAP - 7) // 3) + "\"",              # escaped in "..."
        "echo \"${x:-" + "'$(" * ((CAP - 20) // 3) + "}\"",        # literal apostrophes
    ],
    ids=["dollar-continuations", "sq-continuations", "heredoc-bodies",
         "sq-lookalikes", "cd-chain", "cd-redirect-chain", "dq-escapes",
         "brace-apostrophes"],
)
def test_new_passes_are_fast_at_the_cap(command: str):
    import time

    assert len(command.encode()) <= CAP
    start = time.process_time()
    check_bash_command(command + " ")  # bypass any cached scan
    assert time.process_time() - start < 1.0


# ---------------------------------------------------------------------------
# The reviewer's 134-command read-only corpus: 0 false positives
# ---------------------------------------------------------------------------

READ_ONLY_CORPUS = [
    "git status", "git status --short", "git log --oneline -20",
    'git log --pretty=format:"%h %an %s" -10',
    "git log --pretty=format:'%H|%ad|%s' --date=short -5",
    'git log --since="2 weeks ago" --author="Forgeborn" --oneline',
    "git diff --stat HEAD~3..HEAD", "git diff main...HEAD -- equipa/",
    "git diff --name-only HEAD~1", "git show HEAD:equipa/config.py | head -50",
    "git show --stat HEAD", "git blame -L 10,40 equipa/config.py", "git branch -a",
    "git rev-parse --abbrev-ref HEAD", "git log --format='%an <%ae>' | sort -u",
    "git grep -n \"def check_\" -- '*.py'", "git ls-files | wc -l",
    "git log -p -1 -- README.md", 'grep -rn "TODO" equipa/',
    'grep -rn "def main" --include="*.py" .',
    'grep -rnE "import (os|sys)" equipa/ | head -20',
    'grep -c "assert" tests/test_config.py',
    "grep -rn 'is_feature_enabled(' equipa/ tests/",
    'grep -rl "bash_security" . --include=*.py', 'grep -n "^class " equipa/*.py',
    'grep -rn "foo\\|bar" src/', 'grep -v "^#" config.ini',
    'rg -n "check_bash_command" equipa', 'rg --type py "def \\w+\\(" -c',
    'find . -name "*.py" -type f | head -30',
    "find . -name '*.md' -not -path './node_modules/*'",
    'find tests -name "test_*.py" -newer README.md',
    'find . -type d -name __pycache__ -prune -o -name "*.py" -print | wc -l',
    "find . -maxdepth 2 -type f -size +1M", "ls -la", "ls -la equipa/ tests/",
    "ls -1 tests | wc -l", "wc -l equipa/*.py | sort -n | tail -10", "cat README.md",
    "head -n 40 equipa/bash_security.py", "tail -n 50 /tmp/forge-tasks-12.log",
    "sed -n '100,160p' equipa/config.py",
    "awk '{print $1}' data/access.log | sort | uniq -c | sort -rn | head",
    "awk -F, 'NR>1 {sum+=$3} END {print sum}' data/report.csv",
    "cut -d: -f1 data/users.txt | sort", "sort data/in.txt | uniq -c",
    "sort < data/in.txt", "diff <(sort a.txt) <(sort b.txt)",
    "diff -u old.txt new.txt", "cmp a.bin b.bin", 'cat "$F" | wc -l',
    'while read l; do echo "$l"; done < data/list.txt',
    'for f in tests/*.py; do echo "$f"; done', "for f in $(ls tests); do echo $f; done",
    "echo {a,b}", 'echo "hello world"', "echo 'single quoted $HOME'",
    'printf "%s\\n" "a" "b"', "printf '%-20s %s\\n' name value",
    'python3 -c "import sys; print(sys.version)"',
    'python3 -c \'import json,sys; print(json.load(open("data.json"))["k"])\'',
    "python3 -m pytest -q tests/test_config.py",
    'python3 -m pytest -q -k "flag and not slow" tests/',
    "python3 -m json.tool data.json",
    "python3 - <<'EOF'\nimport json\nprint(json.dumps({\"a\": 1}))\nEOF",
    "cat > notes.txt <<'EOF'\nline one\nline <two> #three\nEOF",
    "sqlite3 -readonly db.sqlite \"SELECT id, title FROM tasks WHERE status = 'open' LIMIT 10\"",
    'sqlite3 -readonly db.sqlite "SELECT COUNT(*) FROM agent_runs WHERE cost_usd IS NULL;"',
    "sqlite3 -readonly -header -column db.sqlite 'SELECT * FROM projects;'",
    "jq '.items[] | {name, id}' data.json",
    "jq -r '.[] | select(.status == \"open\") | .title' tasks.json",
    "jq '.features' dispatch_config.json", "jq -r 'keys[]' data.json",
    "cat package.json | jq .dependencies", "curl -s https://example.invalid/api | jq .",
    "npm test", "npm run build", "npm ls --depth=0",
    'node -e "console.log(process.version)"', "pip list | grep -i pytest",
    "pip show requests", "du -sh .", "df -h /tmp", "ps aux | grep python | grep -v grep",
    "pgrep -af forge_orchestrator", "systemctl status nginx --no-pager",
    'journalctl -u nginx --since "1 hour ago" --no-pager | tail -20',
    "env | grep -i path", "which python3", "type python3", "date +%Y-%m-%d",
    'date -u +"%Y-%m-%dT%H:%M:%SZ"', 'echo "Today is $(date +%F)"',
    "BRANCH=$(git rev-parse --abbrev-ref HEAD)", 'echo "$(git rev-parse --short HEAD)"',
    "cd tests && ls", 'cd "$(git rev-parse --show-toplevel)" && git status',
    "test -f README.md && echo yes || echo no", "[ -d tests ] && echo dir",
    '[[ -n "$VAR" ]] && echo set', "stat -c '%s %n' README.md",
    "file equipa/bash_security.py", "md5sum README.md", "sha256sum dist/*.tar.gz",
    "tree -L 2 equipa", "xargs -n1 echo < data/list.txt",
    "cat data/list.txt | xargs -I{} echo {}", "tr '[:upper:]' '[:lower:]' < data/in.txt",
    "python3 scripts/gen_module_report.py --check", "make -n test", "cargo check",
    "go vet ./...", "go test ./... -run TestFoo", "ruff check equipa/",
    "mypy equipa/config.py", "timeout 60 python3 -m pytest -q tests/test_bash_security.py",
    "env -u DATABASE_URL python3 -m pytest -q",
    "git log --oneline | head -5 && git status --short", "echo \"a|b\" | cut -d'|' -f2",
    'grep -E "^(foo|bar)=" .env.example', "grep -rn '\\$(' equipa/bash_security.py | head",
    "ls -la 2>/dev/null", "python3 -m pytest -q 2>&1 | tail -5",
    "git diff > /tmp/review.patch", "echo done >> build.log", 'cat <<< "here string"',
    "column -t -s, data/report.csv", "nl -ba equipa/config.py | sed -n '1,20p'",
    'basename "$PWD"', 'dirname "$(pwd)"', "realpath equipa", "uname -a", "free -m",
    "nproc",
]


def test_read_only_corpus_size():
    """The corpus is the review's (134 commands); keep it whole."""
    assert len(READ_ONLY_CORPUS) == 134
    assert len(set(READ_ONLY_CORPUS)) == 134


@pytest.mark.parametrize("command", READ_ONLY_CORPUS)
def test_read_only_corpus_has_no_false_positive(command: str):
    result = check_bash_command(command)
    assert result.safe, f"false positive: {command!r}: check {result.check_id}: {result.message}"


# ---------------------------------------------------------------------------
# IND3128-06: documentation and docstring claim only what is true
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
WORKAROUNDS_DOC = REPO / "docs" / "BASHSECURITY-WORKAROUNDS.md"


class TestClaims:

    @pytest.mark.parametrize(
        "text",
        [
            WORKAROUNDS_DOC.read_text(encoding="utf-8"),
            bash_security.__doc__ or "",
            bash_security._scan_shell.__doc__ or "",
        ],
        ids=["workarounds-doc", "module-docstring", "scan-docstring"],
    )
    @pytest.mark.parametrize(
        "overclaim",
        [
            "the way bash parses it",
            "follows bash's parser",
            "stay in the working directory",
        ],
    )
    def test_no_bash_exact_overclaim(self, text: str, overclaim: str):
        # Normalise line wrapping so a claim split across lines still counts.
        assert overclaim not in " ".join(text.split())

    @pytest.mark.parametrize(
        "fact",
        [
            "fails closed",
            "check 26",
            "proven inert",
            "line continuation",
            "cd /etc",
            "array subscript",
            "Known limits",
        ],
    )
    def test_doc_states_the_principle_and_limits(self, fact: str):
        assert fact in WORKAROUNDS_DOC.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("command", "expected_safe"),
        [
            ("grep -n '$(' f", True),
            ('echo "\\$(x) is literal"', True),
            ("awk '{print $(NF)}' f", True),
            ("cd tests && pytest -q > out.txt", True),
            ("cd /tmp/w && echo x > out.txt", True),
            ('echo "$((1 + 2))"', True),
            ("(( i++ ))", True),
            # Known limit: run-time text is eval-equivalent and passes.
            ("read v < f; (( v ))", True),
            ("cd /etc; echo x > f", False),
            ("cd ~ && echo x > f", False),
            ('cd "$D" && echo x > f', False),
            ("echo $\\\n(cmd)", False),
            ("echo \"${X:-'$(cmd)'}\"", False),
            ("echo $'$(cmd)'", False),
            ("let 'a[$(cmd)]'", False),
            ("echo \"$(( '$(cmd)' ))\"", False),
            ("(( x == 'a' ))", False),
        ],
    )
    def test_documented_examples_match_the_checker(self, command: str, expected_safe: bool):
        doc = WORKAROUNDS_DOC.read_text(encoding="utf-8")
        shown = command.replace("\\\n", "")
        if "\\\n" not in command:
            assert shown in doc, f"example not in the doc: {command!r}"
        assert check_bash_command(command).safe is expected_safe, command

    def test_module_docstring_states_the_principle(self):
        doc = bash_security.__doc__ or ""
        assert "fail closed" in doc.lower()
        assert "proven inert" in doc
