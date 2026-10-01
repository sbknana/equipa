#!/usr/bin/env python3
"""Task 3138 RR-01 / RR-06: every redaction pattern is linear, more shapes.

RR-01: the IR-06 docker-login and htpasswd patterns chained two bounded
gaps; the review measured 51 s for ``redact_secrets("htpasswd -b " * 5461)``
and 1.2 s for a 4 MB tool input. The single-gap command patterns (mysql,
sshpass, az, sqlcmd, redis-cli, curl) cost 0.2-0.5 s each on 64 KB. Every
entry of ``_PATTERNS`` is now timed here on 64 KB repeats of:

* the reviewers' shapes and every command/flag pair the redactor scopes;
* the words and flags taken from that pattern's own source and hints, so a
  new pattern is covered without editing this file;
* a generated family of two-character repeats (a per-pattern word list
  missed a quadratic ``[\\n...]\\s*`` in task 3129).

Limits: ``redact_secrets`` on 64 KB under 0.1 s, one pattern on 64 KB under
0.1 s, ``redact_tool_input`` on a 4 MB tool input under 0.3 s.

``redact_secrets`` skips a pattern whose hint words are all absent; the
hints must never hide a match, including the Unicode letters ``(?i)``
matches to ASCII (long s, Kelvin sign, dotless i).

RR-06: xapp-, hf_, REDISCLI_AUTH=, openssl pass:, mongosh -p and curl -b
cookie values are redacted; ``a:1:b:c:d`` is no longer taken for a .pgpass
line.

Every secret here is an obviously fake sentinel.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import itertools
import json
import random
import re
import string
import sys
import time

import pytest

from equipa import redact
from equipa.redact import (
    REDACTED,
    redact_secrets,
    redact_tool_input,
    redacted_json_preview,
)

SIXTY_FOUR_KB = 64 * 1024
FOUR_MB = 4 * 1024 * 1024
PATTERN_LIMIT_SECONDS = 0.1
REDACT_LIMIT_SECONDS = 0.1
TOOL_INPUT_LIMIT_SECONDS = 0.3

# Command words the redactor scopes flags to, and the flags it looks for.
COMMAND_WORDS = ("mysql", "mariadb-dump", "sshpass", "docker", "podman",
                 "docker login", "az", "az vm", "sqlcmd", "redis-cli",
                 "mongosh", "htpasswd", "curl", "openssl")
FLAGS = ("-p", "-p=", "-P", "-a", "--pass", "-b", "-bB x", "-u a:", "-U a:",
         "--cookie", "-passin pass:", "login")

REVIEW_UNITS = (
    # RR-01 / SR3134-01 shapes.
    "htpasswd -b ", "htpasswd -bB x ", "docker login ",
    # IR-05 shapes (task 3134).
    "Pwd_", "PWD_", "PASSWORD", "password=", "mysql ",
    # Quotes and escapes that the value and command-rest patterns walk.
    '"', "'", '\\"', "\\n", '" ', "' ", '\\" ', "a:5432:b:c:", "5432:",
    "-----BEGIN PRIVATE KEY-----", "Bearer x", "cookie: ", "eyJa.a",
    "x://a:b@", "hf_", "xapp-", "REDISCLI_AUTH=", "A_AUTH",
    # The JWT pattern's start per "-eyJ" (task 3138 family search).
    "-eyJ", "eyJ-", "-eyJa.",
)
COMMAND_UNITS = tuple(
    f"{command} {flag} " for command in COMMAND_WORDS for flag in FLAGS
) + tuple(f'{command} {flag} "'
          for command in COMMAND_WORDS for flag in ("-p", "-b"))

PAIR_ALPHABET = " -p:=\"'\\\nb_/;"
PAIR_UNITS = tuple("".join(pair)
                   for pair in itertools.product(PAIR_ALPHABET, repeat=2))
# Separators put before and after each of a pattern's own words: "-eyJ" x N
# gave the JWT pattern a word-boundary start per repeat, each scanning to the
# end of the run (1.7 s on 64 KB), and no word or pair list above had it.
SEPARATORS = " \t-_.:=\"'\\\n/@;"

PATTERN_INDEXES = range(len(redact._PATTERNS))


def _pattern_sources(index: int) -> list[str]:
    """Every regex source pattern ``index`` applies (command rules: all)."""
    pattern, replacement, _hints = redact._PATTERNS[index]
    sources = [pattern.pattern]
    sources += [flag.pattern for flag, _ in getattr(replacement, "flags", ())]
    after = getattr(replacement, "after", None)
    if after is not None:
        sources.append(after.pattern)
    return sources


def _source_tokens(source: str) -> set[str]:
    """Literal words and flags in a regex source (``mysql``, ``-p``, ...)."""
    text = re.sub(r"\\.", " ", source)  # escapes: \b \s \" \[
    text = re.sub(r"\[[^\]]*\]", " ", text)  # character classes
    text = re.sub(r"\(\?(?:[a-zA-Z]*:|<?[=!]|P?<[^>]*>)", " ", text)
    return set(re.findall(r"-{0,2}[A-Za-z][A-Za-z0-9_:-]*", text))


def _pattern_tokens(index: int) -> list[str]:
    tokens = set(redact._PATTERNS[index][2])
    for source in _pattern_sources(index):
        tokens |= _source_tokens(source)
    return sorted(tokens)


def _derived_units(index: int) -> list[str]:
    """64 KB repeat units built from pattern ``index``'s own words."""
    tokens = _pattern_tokens(index)
    units = [f"{token} " for token in tokens] + tokens
    units += [f"{first} {second} "
              for first, second in itertools.permutations(tokens[:10], 2)]
    units.append(" ".join(tokens) + " ")
    units += [unit for token in tokens for separator in SEPARATORS
              for unit in (separator + token, token + separator)]
    return units


def _repeat(unit: str, size: int = SIXTY_FOUR_KB) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _timed(func, *args) -> float:
    started = time.perf_counter()
    func(*args)
    return time.perf_counter() - started


def _best_time(func, *args, limit: float) -> float:
    """One run; on a miss, the best of three (a scheduler hiccup is not a fail)."""
    elapsed = _timed(func, *args)
    if elapsed >= limit:
        elapsed = min(elapsed, *(_timed(func, *args) for _ in range(2)))
    return elapsed


def _pattern_id(index: int) -> str:
    return f"{index}:{redact._PATTERNS[index][0].pattern[:40]}"


def _unhinted(text: str) -> str:
    """redact_secrets without the hint skip: every pattern runs."""
    for pattern, replacement, _hints in redact._PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --- RR-01: every pattern is linear ------------------------------------------

@pytest.mark.parametrize("index", PATTERN_INDEXES,
                         ids=[_pattern_id(i) for i in PATTERN_INDEXES])
def test_every_pattern_has_hints_and_trigger_words(index):
    """Hints drive the skip, words drive the timing rows; neither is empty."""
    hints = redact._PATTERNS[index][2]
    assert hints and all(hint == hint.lower() for hint in hints)
    assert _pattern_tokens(index), _pattern_id(index)


@pytest.mark.parametrize("index", PATTERN_INDEXES,
                         ids=[_pattern_id(i) for i in PATTERN_INDEXES])
def test_every_pattern_is_fast_on_64kb_adversarial_repeats(index):
    """The raw pattern, without the hint skip, on every generated unit."""
    pattern, replacement, _hints = redact._PATTERNS[index]
    units = set(REVIEW_UNITS + PAIR_UNITS) | set(_derived_units(index))
    if hasattr(replacement, "flags"):
        units |= set(COMMAND_UNITS)
    slow = []
    for unit in sorted(units):
        elapsed = _best_time(pattern.sub, replacement, _repeat(unit),
                             limit=PATTERN_LIMIT_SECONDS)
        if elapsed >= PATTERN_LIMIT_SECONDS:
            slow.append(f"{unit!r}: {elapsed:.3f}s")
    assert not slow, f"pattern {_pattern_id(index)} is slow on {slow}"


@pytest.mark.parametrize("unit", sorted(set(REVIEW_UNITS + COMMAND_UNITS)))
def test_redact_secrets_on_64kb_adversarial_input_is_fast(unit):
    elapsed = _best_time(redact_secrets, _repeat(unit),
                         limit=REDACT_LIMIT_SECONDS)
    assert elapsed < REDACT_LIMIT_SECONDS, f"{unit!r}: {elapsed:.3f}s"


@pytest.mark.parametrize("index", PATTERN_INDEXES,
                         ids=[_pattern_id(i) for i in PATTERN_INDEXES])
def test_redact_secrets_on_each_patterns_own_words_is_fast(index):
    tokens = _pattern_tokens(index)
    slow = []
    for unit in [f"{token} " for token in tokens] + [" ".join(tokens) + " "]:
        elapsed = _best_time(redact_secrets, _repeat(unit),
                             limit=REDACT_LIMIT_SECONDS)
        if elapsed >= REDACT_LIMIT_SECONDS:
            slow.append(f"{unit!r}: {elapsed:.3f}s")
    assert not slow, f"pattern {_pattern_id(index)}: {slow}"


def test_redact_secrets_with_every_hint_present_is_fast():
    """No pattern is skipped: the sum of all of them stays in the bound."""
    hints = sorted({hint for _, _, pattern_hints in redact._PATTERNS
                    for hint in pattern_hints})
    unit = " ".join(hints) + " "
    elapsed = _best_time(redact_secrets, _repeat(unit),
                         limit=REDACT_LIMIT_SECONDS)
    assert elapsed < REDACT_LIMIT_SECONDS, f"{elapsed:.3f}s"


def _four_mb_inputs(unit: str) -> list[dict]:
    """A Bash command and a MultiEdit, each rendering to about 4 MB."""
    edit = {"old_string": unit, "new_string": unit}
    edit_size = len(json.dumps(edit)) + 2
    return [
        {"command": _repeat(unit, FOUR_MB)},
        {"file_path": "/srv/app/x.py",
         "edits": [edit] * (FOUR_MB // edit_size)},
    ]


def _assert_four_mb_is_fast(unit: str) -> None:
    for tool_input in _four_mb_inputs(unit):
        assert len(json.dumps(tool_input)) > 3_500_000
        elapsed = _best_time(redact_tool_input, tool_input, 200,
                             limit=TOOL_INPUT_LIMIT_SECONDS)
        assert elapsed < TOOL_INPUT_LIMIT_SECONDS, f"{unit!r}: {elapsed:.3f}s"


@pytest.mark.parametrize("index", PATTERN_INDEXES,
                         ids=[_pattern_id(i) for i in PATTERN_INDEXES])
def test_four_mb_tool_input_of_each_patterns_words_is_fast(index):
    _assert_four_mb_is_fast(" ".join(_pattern_tokens(index)) + " ")


@pytest.mark.parametrize("unit", [
    "htpasswd -b ", "htpasswd -bB x ", "docker login ", "docker login -p ",
    "mysql ", "sshpass ", "az vm ", "sqlcmd ", "redis-cli ", "curl -U ",
    "curl -b ", "mongosh ", "-passin ",
])
def test_four_mb_tool_input_of_the_review_shapes_is_fast(unit):
    _assert_four_mb_is_fast(unit)


# --- The JWT pattern: linear, and the same matches as before ------------------

# The JWT pattern before task 3138: a start at every "-eyJ" of a run.
JWT_BEFORE_3138 = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@pytest.mark.parametrize("text, kept", [
    ("token-eyJhbGciOi.eyJzdWIiOi.FAKEjwt3138", "token-"),
    ("x eyJ-eyJa.b.FAKEjwt3138 y", "x "),
    ("-eyJ-eyJ.x.FAKEjwt3138", "-"),
    ("a.eyJa.b.FAKEjwt3138.eyJc.d.FAKEjwt3138", "a."),
    ('{"jwt": "eyJa.eyJb.FAKEjwt3138"}', '{"jwt": "'),
])
def test_jwt_is_redacted_and_the_text_before_it_kept(text, kept):
    redacted = redact_secrets(text)
    assert "FAKEjwt3138" not in redacted
    assert redacted.startswith(kept + REDACTED)
    assert redacted == JWT_BEFORE_3138.sub(REDACTED, text)


def test_jwt_pattern_matches_the_old_one_on_random_text():
    """Same result as the pre-3138 pattern on 20 000 seeded random texts
    built from the characters around the JWT boundary cases."""
    pieces = ["eyJ", "e", "y", "J", "-", "_", ".", "a", "9", " ",
              "\N{LATIN SMALL LETTER E WITH ACUTE}", "=", "eyJa", ".b", "\n"]
    rng = random.Random(3138)
    for _ in range(20_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        assert (redact._JWT.sub(r"\1" + REDACTED, text)
                == JWT_BEFORE_3138.sub(REDACTED, text)), repr(text)


# --- The hint skip never hides a match ------------------------------------------

def test_hint_fold_covers_every_letter_ignorecase_matches():
    """Every character (?i) matches to an ASCII letter folds to exactly that
    letter. Task 3144 (RR3138-D): "letter in fold" held for U+0130, which
    casefold() turns into "i" plus a combining dot that splits a hint word,
    so the check now demands the letter alone."""
    any_letter = re.compile("(?i)[a-z]")
    missed = []
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if code < 128 or 0xD800 <= code <= 0xDFFF:
            continue
        if not any_letter.fullmatch(char):
            continue
        for letter in string.ascii_lowercase:
            if (re.fullmatch("(?i)" + letter, char)
                    and redact._fold_for_hints(char) != letter):
                missed.append((hex(code), letter))
    assert not missed


@pytest.mark.parametrize("text, secret", [
    ("authorizat\N{LATIN SMALL LETTER DOTLESS I}on: FAKEdotless3138",
     "FAKEdotless3138"),
    ("\N{LATIN SMALL LETTER LONG S}ecret: FAKElongs3138", "FAKElongs3138"),
    ("api_\N{KELVIN SIGN}ey = FAKEkelvin3138", "FAKEkelvin3138"),
    ("BEARER FAKEupperbearer3138", "FAKEupperbearer3138"),
    ("MYSQL -uroot -pFAKEupmysql3138", "FAKEupmysql3138"),
])
def test_unicode_and_upper_case_keys_are_still_redacted(text, secret):
    assert secret not in redact_secrets(text)
    assert redact_secrets(text) == _unhinted(text)


# --- The linear command scope still redacts what the gap patterns did --------

COMMAND_ROWS = [
    ("docker login -u bot -p FAKEdock3138 registry.invalid", "FAKEdock3138"),
    ("podman login registry.invalid -p=FAKEpod3138", "FAKEpod3138"),
    ("htpasswd -b /etc/htpasswd admin FAKEhtp3138", "FAKEhtp3138"),
    ("htpasswd -bB -C 10 /etc/htpasswd admin FAKEhtpb3138; ls",
     "FAKEhtpb3138"),
    ("mysql -u root -pFAKEmy3138 appdb", "FAKEmy3138"),
    # A quoted password holding a command separator: the old gap stopped at
    # the ";" and the new scope walks the quotes.
    ('mysql -e "select 1; select 2" -pFAKEmyq3138', "FAKEmyq3138"),
    ("mysql -p'FAKE;semi3138' appdb", "semi3138"),
    ("sshpass -p FAKEssh3138 ssh host.invalid", "FAKEssh3138"),
    ("az login -u bot -p FAKEaz3138", "FAKEaz3138"),
    ("sqlcmd -S db.invalid -U sa -P FAKEsql3138", "FAKEsql3138"),
    ("redis-cli -h cache.invalid -a FAKEredis3138 ping", "FAKEredis3138"),
    ("curl -u bot:FAKEcurl3138 https://api.invalid", "FAKEcurl3138"),
    # A long command: the old patterns gave up 1024 characters from the
    # command word.
    ("mysql " + "--verbose " * 200 + "-pFAKElong3138", "FAKElong3138"),
]


@pytest.mark.parametrize("text, secret", COMMAND_ROWS)
def test_command_scoped_shapes_are_redacted(text, secret):
    redacted = redact_secrets(text)
    assert secret not in redacted
    assert REDACTED in redacted
    assert redact_secrets(redacted) == redacted
    assert redacted == _unhinted(text)
    assert secret not in redacted_json_preview({"command": text}, 4000)


@pytest.mark.parametrize("text", [
    "ssh -p 2222 host.invalid",
    "docker ps -a; ls -p /tmp",
    "curl -b cookies.txt https://api.invalid",
    "htpasswd -D /etc/htpasswd admin",
    "grep -p pattern file.txt",
    "mongod --port 27017",
    "mongosh -p --host db.invalid",
])
def test_command_scoped_rules_leave_ordinary_commands_alone(text):
    assert redact_secrets(text) == text


# --- RR-06: more shapes ---------------------------------------------------------

RR06_ROWS = [
    ("slack-app-token", "export SLACK_APP=xapp-1-A0FAKE3138-1234-abcdef0123",
     "xapp-1-A0FAKE3138"),
    ("rediscli-auth-env", "REDISCLI_AUTH=FAKErca3138 redis-cli ping",
     "FAKErca3138"),
    ("huggingface-token", "HF=hf_FAKEhuggingface3138abcdefghijklmnop",
     "hf_FAKEhuggingface3138"),
    ("openssl-passin", "openssl rsa -in k.pem -passin pass:FAKEossl3138",
     "FAKEossl3138"),
    ("openssl-passout-quoted",
     'openssl genrsa -aes256 -passout "pass:FAKEosslq3138" 2048',
     "FAKEosslq3138"),
    ("openssl-pass", "openssl enc -aes-256-cbc -pass pass:FAKEosp3138",
     "FAKEosp3138"),
    ("mongosh-p", "mongosh --host db.invalid -u admin -p FAKEmongo3138",
     "FAKEmongo3138"),
    ("mongodump-p", "mongodump -u admin -p=FAKEmdump3138 --db app",
     "FAKEmdump3138"),
    ("curl-b-quoted", "curl -b 'session=FAKEcookie3138' https://a.invalid",
     "FAKEcookie3138"),
    ("curl-b-two-cookies",
     'curl -b "sid=FAKEsid3138; csrf=FAKEcsrf3138" https://a.invalid',
     "FAKEcsrf3138"),
    ("curl-b-bare", "curl -b session=FAKEbare3138 https://a.invalid",
     "FAKEbare3138"),
    ("curl-cookie-long", "curl --cookie=token=FAKEclong3138 https://a.invalid",
     "FAKEclong3138"),
]


@pytest.mark.parametrize("_name, text, secret", RR06_ROWS,
                         ids=[row[0] for row in RR06_ROWS])
def test_rr06_shapes_are_redacted(_name, text, secret):
    redacted = redact_secrets(text)
    assert secret not in redacted, redacted
    assert redact_secrets(redacted) == redacted
    assert redacted == _unhinted(text)
    preview, _digest = redact_tool_input({"command": text}, 4000)
    assert secret not in preview, preview


@pytest.mark.parametrize("text", [
    "ls a:1:b:c:d",
    "time:12:30:45:foo",
    "echo 1:2:3:4:5",
    "openssl rsa -in k.pem -passin env:PASSPHRASE_VAR",
    "openssl rsa -in k.pem -passin file:/run/secrets/p",
])
def test_rr06_non_secrets_are_left_alone(text):
    assert redact_secrets(text) == text


@pytest.mark.parametrize("text, secret", [
    ("db.invalid:5432:appdb:app:FAKEpg3138", "FAKEpg3138"),
    ("h:*:appdb:app:FAKEpgw3138", "FAKEpgw3138"),
    ("db:15432:appdb:app:FAKEpgh3138", "FAKEpgh3138"),
])
def test_pgpass_lines_are_still_redacted(text, secret):
    assert secret not in redact_secrets(text)


# --- Every pattern's hints admit its own matches ----------------------------

# One sample per pattern, in _PATTERNS order; each must be redacted.
PATTERN_SAMPLES = [
    "-----BEGIN RSA PRIVATE KEY-----\nFAKE3138\n-----END RSA PRIVATE KEY-----",
    "ghp_FAKE3138abcdefghijklmnop",
    "github_pat_FAKE3138_abcdefghijklmnop",
    "sk-FAKE3138abcdefghijklmnop",
    "AKIAFAKE3138ABCDEFGH",
    "xoxb-FAKE3138-abcdef",
    "xapp-1-FAKE3138-abcdef",
    "hf_FAKE3138abcdefghijklmnopqrstuvwxyz",
    "AIzaFAKE3138abcdefghijklmnopqrstuvwxyz",
    "glpat-FAKE3138abcdefghijk",
    "eyJhbGciOi.eyJzdWIiOi.FAKE3138sig",
    "postgres://app:FAKE3138pw@db.invalid/app",
    "PGPASSWORD=FAKE3138",
    "DB_PASS=FAKE3138",
    "GH_TOKEN=FAKE3138",
    "X_AUTH=FAKE3138",
    "tool --api-key FAKE3138",
    "docker login -p FAKE3138",
    "openssl rsa -passin pass:FAKE3138",
    "db.invalid:5432:app:app:FAKE3138",
    "Cookie: sid=FAKE3138",
    "password: FAKE3138",
    "Authorization: token FAKE3138",
    "curl -H 'x: Bearer FAKE3138abcdef'",
]


@pytest.mark.parametrize("text", PATTERN_SAMPLES)
def test_one_sample_per_pattern_is_redacted(text):
    assert "FAKE3138" not in redact_secrets(text)


@pytest.mark.parametrize(
    "text", PATTERN_SAMPLES + [row[0] for row in COMMAND_ROWS]
    + [row[1] for row in RR06_ROWS])
def test_hint_skip_never_changes_the_result(text):
    assert redact_secrets(text) == _unhinted(text)
