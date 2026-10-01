# BashSecurity workarounds for EQUIPA-dispatched tasks in Equipa-repo

> **Read this BEFORE making any bash command from inside an EQUIPA dispatch on this repo.**
>
> EQUIPA's bash security classifier (`equipa/bash_security.py` — yes, the same code you may be modifying) judges the shell commands agents emit. Several common shell idioms still trip false positives, and a blocked command can end your run. This page lists what is blocked, what passes, and the shapes to use instead.

## What the checker does — and what it does not

The classifier is enforced in two places, with different guarantees:

- **Pre-execution gate** (`hooks/pretooluse_bash_gate.py`, feature flag `features.bash_security_pretooluse`, default OFF in code). When the flag is on, `build_cli_command` wires a Claude Code PreToolUse hook into the agent CLI through a generated `--settings` file. Exception, still open (SECURITY-REVIEW-3121 BS3121-04, in `agent_runner.py`): if the hook script is missing or the settings file cannot be written, the agent is started without the gate and only a WARNING is logged. The hook runs the classifier *before* the Bash tool executes and refuses an unsafe command (exit 2, reason on stderr), so the command never runs. The gate **fails closed**: if the payload cannot be parsed, the checker cannot be loaded (import or syntax error), the checker raises, or it returns something unusable, the command is blocked with a `pretooluse_bash_gate: ... fails closed` reason. The flag is forced ON, with an ERROR logged, when `dispatch_config.json` exists but cannot be read or parsed, when its `features` value is not an object, or when the flag's value is not one of `true`/`false`, `0`/`1` or the strings `"true"`/`"false"`/`"1"`/`"0"` (so `"yes"`, `"on"` or `null` keep the gate on rather than turning it off).
- **Reactive stream check** (`equipa/agent_runner.py`). For roles that run with streaming output, the orchestrator also runs the classifier on each Bash call it sees in the stream. The CLI has already executed the call by then, so this detects and terminates; it does not prevent. Roles that run without streaming (the early-term-exempt ones, such as planner, evaluator, the reviewers and researcher) are **not** checked this way.

What it is **not**:

- **Not every command is checked.** With the flag off, only streaming roles get the (after-the-fact) reactive check.
- **Not a permission policy.** The classifier detects parser-confusion and substitution tricks. Plainly destructive or powerful commands pass: `rm -rf build/`, `bash scripts/x.sh`, `git push --force`, and writing any file through the Write/Edit tools.
- **Not a sandbox.** A task worktree is a separate git checkout, not an isolation boundary. Agents run as the same user as the orchestrator, and the redirect checks below are textual: a symlink that points elsewhere (even one created earlier in the same command) is not detected, and neither is a writer that is not a redirect, such as `tee -a`. A relative target is judged where it lands after a `cd`, `pushd` or `popd` earlier in the command, so `cd /etc; echo x > f` is refused as `/etc/f` (see Known limits).

How it reads a command: one tokenizer (`_scan_shell` in `equipa/bash_security.py`) models how bash reads `'...'`, `"..."`, `$'...'` with its backslash escapes, `$"..."`, quoting that starts afresh inside `$(...)` (even within double quotes), backticks, `${...}`, `$[...]`, `(( ))`, `#` comments and heredoc bodies. Every check that cares about quoting reads that one classification, and a separate test compares it with bash on a generated corpus. The tokenizer is a model, not bash, and it has disagreed with bash before, so the checker **fails closed** wherever the model could be wrong:

- **Line continuations are joined first.** Bash deletes a backslash-newline before it tokenizes (except inside single quotes, `$'...'`, comments and quoted heredoc bodies), so `$`, a line continuation, then `(cmd)` is `$(cmd)`. The checker reads the joined text.
- **Quoting the tokenizer cannot parse is refused (check 25):** an unterminated quote or substitution, a `case` statement inside `$(...)`, a `((` that is not an arithmetic command (write `( (`), a heredoc delimiter containing `$` or a backtick, a heredoc whose body would begin inside a multi-line substitution, or a single quote inside `(( ))`, `$(( ))` or `$[...]`. Bash expands those like double quotes, so an apostrophe there is an ordinary character and a `$(...)` between two of them runs.
- **Substitution-looking text must be proven inert (check 26).** `$(`, `$[`, a backtick, `<(` or `>(` anywhere in the text counts as a substitution. A live one is judged by check 8 against the read-only allowlist. Any other occurrence passes only where the tokenizer proves it inert: a top-level single-quoted word (`grep -n '$(' f`), a `\$` or `` \` `` directly inside a top-level double-quoted string (`echo "\$(x) is literal"`), or a quoted-delimiter heredoc body. Everything else counts as executing it: other double-quoted text such as the default word of `"${x:-'...'}"`, `$'...'`, comments, `${...}` operators, arithmetic, unquoted backslash escapes, and quotes nested inside a substitution.
- **Evaluating the text again revokes even those proofs.** Bash expands an array subscript again when it evaluates text as arithmetic, so `let 'a[$(cmd)]'`, `x='a[$(cmd)]'; (( x ))`, `[[ x -eq 1 ]]`, `test -v`, `printf -v` and `declare -i` all run `cmd`, although the text was single-quoted. When a command also contains a construct that evaluates text, no quoted look-alike is trusted and check 26 refuses it, naming the construct. Always counted: `let`, `declare`, `typeset`, `local`, `readonly`, `export`, `readarray`, `mapfile`, `=(`, `$[`, `${!`, `${x[`, an offset `${x:n}`, `${x@P}`, an array subscript in code (`a[x]=1`), a `((` or `$((` that names a variable, `[[` with `-eq`/`-ne`/`-lt`/`-le`/`-gt`/`-ge`/`-v`/`-R`, any mention of `RANDOM`, `SRANDOM`, `OPTIND`, `HISTCMD` (assigning them evaluates arithmetic), `PS0`-`PS4` or `PROMPT_COMMAND`, and a command name that expands (`$x ...`). Counted only when an argument expands or contains `[`: `read`, `test`, `[`, `unset`, `wait` and `getopts` (they evaluate a subscript only in a name they are given); `printf` only when its first argument starts with `-` or expands. So `[ -f f ] && grep -c '$(' f`, `while read -r f; do grep -Hn '$(' "$f"; done < list`, `printf '%s\n' '$(x)'`, `echo "$((1+2))" && grep -n '$(' f` and `grep -n 'foo\[\$(' f` pass (task 3146). This is the same power as `eval`, which passes by design (see Known limits); the difference is that the look-alike is visible in the command.

## Status of known BashSecurity false-positives

| TheForge task | Check | Status |
|---|---|---|
| 2282 | parallel-mode missing autoresearch retry | FIXED — retries fire |
| 2283 | check 7: newlines inside `python3 -c "..."` | FIXED |
| 2284 | check 8: `$()` of read-only commands | FIXED (allowlist; see below) |
| 2285 | check 9: `<<EOF` heredoc treated as `<` redirection | FIXED |
| 2310 | check 4: locale-quoting `$"..."` on data | FIXED |
| 2214 | check 12 + check 4 on benign post-commit composition | FIXED |
| 2316 | check 4 per-segment evaluation (composed commands) | FIXED |
| 2320 | check 23: markdown body header in `gh pr create` heredoc | FIXED |
| 3121 | check 7: quoted-delimiter heredoc into an interpreter or `cat` (`python3 - <<'EOF'`, `cat > f <<'EOF'`) | FIXED in source |
| 3121 | check 9: `<` from a relative path (`sort < data/in.txt`, `while read l; do ...; done < list.txt`) | FIXED in source |
| 3121 | check 16: brace list in an argument (`ls {src,tests}`, `cp f{,.bak}`) | FIXED in source |
| 3121 | check 8: read-only process substitution (`diff <(sort a) <(sort b)`) | FIXED in source |
| 3121 | check 10: fd duplication (`echo msg >&2`) | FIXED in source |
| 3128 | checks 9/10: `<` or `>` inside a `#` comment read as a redirect (`grep x f # see <foo>`) | FIXED in source |

"FIXED in source" means merged to this repo. Production runs whatever `bash_security.py` was last deployed, so a fix only helps once it is deployed. When in doubt, assume production is stricter than the file you are editing.

## What passes and what blocks (current source)

| Passes | Blocks | Why |
|---|---|---|
| `python3 - <<'EOF'` ... `EOF` | `python3 - <<EOF` ... `EOF` (unquoted delimiter) | An unquoted delimiter expands `$(...)` in the body. |
| `cat > notes.md <<'EOF'` ... `EOF` | `bash <<'EOF'` ... `EOF`, `cat <<'EOF' \| sh` ... | A heredoc into a shell is shell code. Only `cat`, python/perl/ruby/node, `gh`, `wc`, `sort`, `head`, `tail` and `grep` qualify, with nothing but `> file` after the opener, and nothing after the closing delimiter. |
| `sort < data/in.txt`, `wc -l < /tmp/x` | `sort < ../x`, `cat < ~/.ssh/id_rsa`, `sort < /etc/x`, `sort < "$F"` | Input must come from a literal relative path without `..`, `/tmp/...` or `/dev/null`. |
| `echo x > out/result.txt`, `>> /tmp/run.log`, `2>/dev/null`, `>&2` | `> ../x`, `>> /tmp/../home/u/.bashrc`, `> /srv/x.log`, `> /tmp/$NAME` | Output targets must be literal, relative without `..`, or under `/tmp/`. Quoted parts count as part of the path (`/tmp/x"/../y"` is `/tmp/x/../y`). |
| `cd tests && pytest -q > out.txt`, `cd /tmp/w && echo x > out.txt` | `cd /etc; echo x > f`, `cd ~ && echo x > f`, `cd "$D" && echo x > f` | A relative target after `cd /abs` is judged as `/abs/target`; after a `cd` whose directory cannot be known (`cd`, `cd -`, `cd ~`, a variable, `..`, `popd`) it is refused. Skip the `cd` or write to `/tmp/...`. |
| `grep -n '$(' f`, `echo "\$(x) is literal"`, `awk '{print $(NF)}' f` | `echo $` + line continuation + `(cmd)`, `echo "${X:-'$(cmd)'}"`, `echo $'$(cmd)'`, `let 'a[$(cmd)]'` | Check 26: substitution-looking text passes only where it is proven inert. Single-quote literal `$(` and backticks. |
| `echo "$((1 + 2))"`, `(( i++ ))` | `echo "$(( '$(cmd)' ))"`, `(( x == 'a' ))` | Check 25: a single quote inside arithmetic is refused. |
| `ls {src,tests}`, `cp config.json{,.bak}` | `{rm,-rf,x}`, `ls {-la,/}`, `echo {1..5}`, `xargs {rm,x}` | Brace lists are allowed only in arguments of commands that do not run their arguments (`echo`, `ls`, `cat`, `cp`, `mv`, `mkdir`, `diff`, `grep`, ...), one list per word, no flags, no `{a..b}` ranges. |
| `diff <(sort a) <(sort b)` | `diff <(curl ... \| sh) b`, `cat <(echo x)`, `tee >(sort)` | `<(...)` takes the same read-only allowlist as `$(...)`, except `echo`/`printf` (use `<<<` instead). `>(...)` is always blocked. |
| `echo "$(git rev-parse HEAD)"`, `echo '$(anything)'` | `echo "$(curl ... \| sh)"`, ``echo "`id`"`` | Substitution inside double quotes is judged like the unquoted form. Single quotes stay inert. |
| `echo $'\''`, `echo "$(echo '"')"` | `echo $'\'' >> /etc/x`, `echo "$(echo '"')" $(touch x)` | A complete quoted word no longer hides what follows it. |
| `( (cd a && ls) )`, `$(grep case notes.txt)` | `echo 'unterminated`, `((cd a) ; ls)`, `$(case $x in a) ls;; esac)` | Check 25: quoting the checker cannot parse is refused. Close every quote, write `( (` for nested subshells, move `case` out of `$(...)`. |
| a command up to 16384 bytes | anything longer | Write long content to a file with the Write tool and run a short command that reads it. |

## Workaround patterns

### Editing files: use the Edit and Write tools

Even though `python3 - <<'EOF'` is now allowed in source, **make file edits with the Edit and Write tools**, not with a script piped into an interpreter. They bypass bash entirely, they are reviewable, and they cannot be blocked by a production checker that predates task 3121. The shapes that still block and end runs:

```bash
# BLOCKS (check 7): unquoted delimiter, so the body is expanded by the shell
python3 << EOF
print("$HOME")
EOF

# BLOCKS (check 7): a heredoc into a shell is shell code
bash <<'EOF'
echo hi
EOF
```

### `gh pr create` with a multi-line body — use `--body-file`

```bash
# BLOCKS (check 19): unquoted delimiter inside $()
gh pr create --title "X" --body "$(cat <<EOF
## Summary
EOF
)"
```

The quoted-delimiter form `--body "$(cat <<'EOF' ... EOF\n)"` passes, but only when the closing `EOF` is alone on its line with no indentation and only whitespace separates it from the `)`. The robust shape is to write the body with the Write tool and pass the file:

```bash
gh pr create --title "X" --body-file /tmp/pr-body.md
```

### `git commit` with a multi-line message — use `-F`

```bash
# BLOCKS (check 19): unquoted delimiter inside $()
git commit -m "$(cat <<EOF
fix: subject line
EOF
)"
```

```bash
# PASSES — message file written with the Write tool
git commit -F /tmp/commit-msg.txt

# PASSES — single-line message
git commit -m "fix: subject line"
```

### Testing `check_bash_command()` itself — pytest, not bash

When you work on `equipa/bash_security.py`, the checks you are changing may be judging your own shell commands. Do not "exercise" a fix by running the pattern you want to allow from bash. Put the pattern in a pytest test and call `check_bash_command()` directly:

```python
def test_allows_read_only_process_substitution() -> None:
    result = check_bash_command("diff <(sort a) <(sort b)")
    assert result.safe, f"check {result.check_id}: {result.message}"
```

Run it with `python3 -m pytest tests/test_bash_security.py -q`. Keep a fixture for every false-positive fix: past fixes regressed because they shipped without one.

### `$()` command substitution — read-only commands only

`$(...)` passes, in any position and inside double quotes, when every command in it is on the read-only allowlist (`git` read-only subcommands, `go env|version|list`, `date`, `basename`, `dirname`, `realpath`, `pwd`, `echo`, `cat`, `wc`, `head`, `tail`, `sort`, `grep`, `tr`, `cut`, `ls`, `whoami`, `which`, `[`, ...) and it holds no nested command substitution. `${...}` expansions outside double quotes still block.

```bash
ls $(go env GOMODCACHE)        # passes
echo "$(git rev-parse HEAD)"   # passes
echo "$(curl -s URL | sh)"     # blocks: curl and sh are not read-only
```

### Multi-line `python3 -c "..."` — write a script file

Newlines inside `python3 -c "..."` pass in source, but a script file is easier to review and does not depend on the deployed checker:

```bash
# Write the script to scripts/build_thing.py with the Write tool, then:
python3 scripts/build_thing.py
```

### Locale quoting `$"..."` — single-quote the outer string

```bash
echo 'the literal string $"hello" appears here'
```

## Known limits

The classifier is not a bash interpreter and does not claim to be. These gaps are known; each is a deliberate trade or out of this module's reach:

- **Text that becomes code only at run time is not seen.** It has the same power as `eval`, which passes by design together with `bash -c`, `sh -c`, `xargs sh -c`, `find -exec sh -c`, `env -S`, sourcing a file, functions and aliases. That includes arithmetic evaluation of a value that reached a variable or file some other way: `read v < f; (( v ))` runs `cmd` when `f` holds `a[$(cmd)]`. Check 26 catches an array subscript look-alike only when it is literally in the same command.
- **Redirect targets are textual.** Symlinks are not resolved (even one created earlier in the same command), writers that are not redirects (`tee -a`, `cp`, `dd of=`) are not checked, and a directory change the text does not spell as `cd`, `pushd` or `popd` (an ANSI-C escape, a command name built at run time) is not detected.
- **The pattern operators of a double-quoted `${...}`** (`"${x#'...'}"`, `"${x/'...'/y}"`) treat apostrophes as quotes in bash, but the checker reads them like the `:-` default word, so a substitution look-alike there is refused although bash would not run it. Refusing is the fail-closed direction.
- **Pre-execution coverage has an open exception** (BS3121-04, above): when the hook cannot be wired, the agent runs without it.

## What to do if you trip a check anyway

Read the reason — the hook prints the check number and message to stderr — and change the *shape* of the command, not just its spelling. If the pre-execution gate reports `fails closed`, the checker itself is broken or could not read your payload; that is an infrastructure problem, so report it rather than retrying variations.

If you cannot express what you need within these constraints, put this in your PR body or final report:

```
BLOCKED-BY-BASHSECURITY: <what you tried, which check fired, what shape you need>
```

The operator will intervene by hand.
