#!/usr/bin/env python3
"""Task 3182: command-scoped redaction is fast on Python 3.10, same results.

CI on PR #43 (Python 3.10 runner) failed
``test_every_pattern_is_fast_on_64kb_adversarial_repeats`` for the curl
rule: 0.111-0.116 s on 64 KB of ``;curl``, ``curl\\n`` or ``curl;`` against
a calibrated budget of 0.106-0.112 s. The time was linear but not in the
regex: each repeat is one match whose command rest is empty, and the
callback ran every flag's ``sub()`` on it. Python 3.10 builds a ``\\1``
replacement template in Python on every ``sub()`` call, matched or not, so
curl's three flags cost 0.093 s on this host (3.12: 0.040 s). A flag is now
searched first and substituted only when it matches: 0.019-0.020 s on both.

The new callback must redact exactly what the old one did. The differential
tests below run the pre-3182 callback (copied here) and the current one over
every string constant in the redaction test modules, the 3138 timing units
and a seeded generated corpus of command lines: 0 differences, per rule and
through ``redact_secrets``.

Every secret here is an obviously fake sentinel.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import copy
import functools
import json
import random
import re
from pathlib import Path

import pytest

from equipa import redact
from equipa.redact import REDACTED, redact_secrets
# Imported by name, so the 3171 timing fence follows _assert_linear.
from tests.test_redaction_linear_3138 import (
    COMMAND_UNITS,
    PAIR_UNITS,
    PATTERN_LIMIT_SECONDS,
    REVIEW_UNITS,
    SIXTY_FOUR_KB,
    _assert_linear,
    _derived_units,
    _repeat,
)

TESTS_DIR = Path(__file__).resolve().parent
CORPUS_SEED = 3182
GENERATED_TEXTS = 30_000
UNIT_REPEAT_SIZE = 600
# Every command rule must redact at least this many corpus texts, so the
# differential tests compare real redactions, not only untouched text.
MIN_REDACTED_PER_RULE = 50

SECRET = "FAKEpw3182"


def _pre_3182_call(rule: redact._CommandScoped, match: re.Match[str]) -> str:
    """``_CommandScoped.__call__`` before task 3182: every flag's sub() runs."""
    head, rest = match.group(1), match.group(2)
    start = 0
    if rule.after is not None:
        found = rule.after.search(rest)
        if found is None:
            return match.group(0)
        start = found.end()
    tail = rest[start:]
    for pattern, replacement in rule.flags:
        tail = pattern.sub(replacement, tail, count=rule.count)
    return head + rest[:start] + tail


# --- The corpus ------------------------------------------------------------------

# Command words, flags and values of the command rules, with near misses
# (case, prefixes, other commands) and the quotes and separators that end or
# extend a command's rest.
COMMAND_WORDS = (
    "curl", "CURL", "Curl", "xcurl", "curl-config", "mysql", "MySQL",
    "mysqldump", "mariadb", "mariadb-dump", "sshpass", "ssh", "docker",
    "podman", "nerdctl", "docker login", "podman login", "nerdctl login",
    "az", "az vm", "az login", "sqlcmd", "SQLCMD",
    "redis-cli", "mongosh", "mongo", "mongodump", "htpasswd", "openssl",
    "echo",
)
FLAG_PIECES = (
    "-u", "-uuser:pw", "--user", "--user=", "-U", "-Uproxy:pw",
    "--proxy-user", "--proxy-user=", "-b", "-bsid=v", "--cookie",
    "--cookie=", "-p", "-pPW", "-p=", "-P", "-PPW", "-a", "--pass", "-bB",
    "-Bb", "-n", "login", "-passin", "pass:PW", "--password", "-c", "-",
)
VALUE_PIECES = (
    "a:b", "user:pw", "user:", ":pw", "u:p:q", "sid=v", "a=b; c=d",
    "a=b;c=d", "pw", "file", "f.txt", REDACTED, "x", "=", ":", "-",
    "https://h.invalid/p", "$VAR", "a\\:b", SECRET,
)
QUOTES = ("", "", "", '"', "'", '\\"', "\\'")
GAPS = (" ", " ", " ", "  ", "\t", "")
SEPARATORS = ("\n", ";", "; ", " && ", " & ", " | ", "|", "\\n", " ",
              "\r\n", ")")

# One command line per rule that the rule redacts.
RULE_EXAMPLES = {
    "mysql": f"mysql -p{SECRET} db",
    "sshpass": f"sshpass -p {SECRET} ssh host",
    "docker": f"docker login -p {SECRET} registry.invalid",
    "az": f"az login -p {SECRET}",
    "sqlcmd": f"sqlcmd -S host -P {SECRET}",
    "redis-cli": f"redis-cli -a {SECRET} ping",
    "mongosh": f"mongosh -p {SECRET}",
    "htpasswd": f"htpasswd -b users.txt admin {SECRET}",
    "curl": f"curl -u admin:{SECRET} https://h.invalid",
}


def _rule_for(word: str) -> redact._CommandScoped:
    """The command rule whose command word is ``word``."""
    rules = [rule for rule in redact._COMMAND_RULES
             if rule.pattern.match(f"{word} x")]
    assert len(rules) == 1, (word, rules)
    return rules[0]


def _existing_test_strings() -> list[str]:
    """Every string constant in the redaction test modules: the inputs,
    expected outputs and units each of them redacts or times."""
    strings: set[str] = set()
    for path in sorted(TESTS_DIR.glob("test_*redact*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        strings.update(node.value for node in ast.walk(tree)
                       if isinstance(node, ast.Constant)
                       and isinstance(node.value, str))
    return sorted(strings)


def _timing_units() -> list[str]:
    """The 3138 timing units, command units and each command rule's derived
    units, repeated to a short text."""
    units = set(REVIEW_UNITS + COMMAND_UNITS + PAIR_UNITS)
    for index, (_pattern, replacement, _hints) in enumerate(redact._PATTERNS):
        if isinstance(replacement, redact._CommandScoped):
            units.update(_derived_units(index))
    return [_repeat(unit, UNIT_REPEAT_SIZE)
            for unit in sorted(units)]


def _generated_command(rng: random.Random) -> str:
    text = rng.choice(COMMAND_WORDS)
    for _ in range(rng.randint(0, 5)):
        piece = rng.choice(FLAG_PIECES if rng.random() < 0.5 else VALUE_PIECES)
        quote = rng.choice(QUOTES)
        closing = quote if rng.random() < 0.85 else ""
        text += rng.choice(GAPS) + quote + piece + closing
    return text


def _generated_texts(count: int, seed: int = CORPUS_SEED) -> list[str]:
    """Seeded command lines: one to three commands joined by separators."""
    rng = random.Random(seed)
    texts = []
    for _ in range(count):
        text = _generated_command(rng)
        for _ in range(rng.randint(0, 2)):
            text += rng.choice(SEPARATORS) + _generated_command(rng)
        texts.append(text)
    return texts


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[str, ...]:
    """Every base text raw, JSON-escaped and as a rendered tool input."""
    base = (_existing_test_strings() + list(RULE_EXAMPLES.values())
            + _timing_units() + _generated_texts(GENERATED_TEXTS))
    texts: set[str] = set()
    for text in base:
        texts.add(text)
        texts.add(json.dumps(text)[1:-1])
        texts.add(json.dumps({"command": text}))
    return tuple(sorted(texts))


def _differences(old: list[str], new: list[str],
                 texts: tuple[str, ...]) -> list[str]:
    return [f"{text!r}: old {before!r} new {after!r}"
            for text, before, after in zip(texts, old, new) if before != after]


# --- Same results as before --------------------------------------------------------

def test_the_corpus_holds_every_source():
    corpus = set(_corpus())
    # Generated texts repeat, and a text without quotes or line breaks is
    # its own JSON-escaped form: well over two distinct texts per generated
    # one remain.
    assert len(corpus) > 2 * GENERATED_TEXTS
    for text in (_existing_test_strings() + _timing_units()
                 + list(RULE_EXAMPLES.values())):
        assert text in corpus
    # The CI shapes and the redaction tests' own inputs are in it.
    assert _repeat("curl;", UNIT_REPEAT_SIZE) in corpus
    assert "htpasswd -b " in corpus


@pytest.mark.parametrize(
    "rule_index", range(len(redact._COMMAND_RULES)),
    ids=[rule.pattern.pattern[:24] for rule in redact._COMMAND_RULES])
def test_each_command_rule_redacts_the_corpus_as_before(rule_index):
    rule = redact._COMMAND_RULES[rule_index]
    old_call = functools.partial(_pre_3182_call, rule)
    texts = _corpus()
    old = [rule.pattern.sub(old_call, text) for text in texts]
    new = [rule.pattern.sub(rule, text) for text in texts]
    redacted = sum(before != text for before, text in zip(old, texts))
    assert redacted >= MIN_REDACTED_PER_RULE, (rule.pattern.pattern, redacted)
    differences = _differences(old, new, texts)
    assert not differences, (len(differences), differences[:5])


def test_redact_secrets_redacts_the_corpus_as_before(monkeypatch):
    texts = _corpus()
    new = [redact_secrets(text) for text in texts]
    old_calls = []

    def pre_3182_dunder(rule, match):
        old_calls.append(match)
        return _pre_3182_call(rule, match)

    with monkeypatch.context() as patched:
        patched.setattr(redact._CommandScoped, "__call__", pre_3182_dunder)
        old = [redact_secrets(text) for text in texts]
    # The old callback really ran, and really redacted.
    assert len(old_calls) > GENERATED_TEXTS
    assert sum(before != text for before, text in zip(old, texts)) > 1000
    differences = _differences(old, new, texts)
    assert not differences, (len(differences), differences[:5])


@pytest.mark.parametrize("word", sorted(RULE_EXAMPLES))
def test_every_rule_example_is_redacted(word):
    text = "\n".join([RULE_EXAMPLES[word]] * 3)
    assert SECRET not in redact_secrets(text)
    assert redact_secrets(text).count(REDACTED) == 3


def test_every_command_rule_has_an_example():
    assert ({id(_rule_for(word)) for word in RULE_EXAMPLES}
            == {id(rule) for rule in redact._COMMAND_RULES})


# --- No flag sub() without a flag match --------------------------------------------

class _CountingPattern:
    """A compiled pattern whose search() and sub() calls are counted."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self.searches = 0
        self.subs = 0

    def search(self, text: str) -> re.Match[str] | None:
        self.searches += 1
        return self.pattern.search(text)

    def sub(self, replacement: str, text: str, count: int = 0) -> str:
        self.subs += 1
        return self.pattern.sub(replacement, text, count=count)


def _counted(word: str) -> tuple[redact._CommandScoped, list[_CountingPattern]]:
    """A copy of ``word``'s rule whose flag patterns count their calls."""
    rule = copy.copy(_rule_for(word))
    counters = [_CountingPattern(pattern) for pattern, _ in rule.flags]
    rule.flags = tuple((counter, replacement) for counter, (_, replacement)
                       in zip(counters, rule.flags))
    return rule, counters


@pytest.mark.parametrize("unit", ["{}\n", "\n{}", "{};", "{} login -x y;"])
@pytest.mark.parametrize("word", sorted(RULE_EXAMPLES))
def test_a_command_with_no_flag_never_reaches_sub(word, unit):
    """The CI shapes (``curl;`` x N) and a rest with no flag in it: no flag
    sub() call at all; before task 3182 every flag ran one per repeat."""
    rule, counters = _counted(word)
    text = unit.format(word) * 500
    assert rule.pattern.sub(rule, text) == text
    assert [counter.subs for counter in counters] == [0] * len(counters)


@pytest.mark.parametrize("word", sorted(RULE_EXAMPLES))
def test_a_command_with_its_flag_still_reaches_sub(word):
    """Positive control for the counting copy: the rule's own example."""
    rule, counters = _counted(word)
    text = "\n".join([RULE_EXAMPLES[word]] * 3)
    redacted = rule.pattern.sub(rule, text)
    assert SECRET not in redacted
    assert sum(counter.subs for counter in counters) >= 3
    assert redacted == rule.pattern.sub(
        functools.partial(_pre_3182_call, _rule_for(word)), text)


# --- Comfortably inside the budget -------------------------------------------------

@pytest.mark.parametrize("shape", [";{}", "{}\n", "{};", "\n{}"])
@pytest.mark.parametrize("word", sorted(RULE_EXAMPLES))
def test_command_word_repeats_take_half_the_pattern_budget(word, shape):
    """The CI shapes on every command rule, within HALF the 3138 per-pattern
    budget (task 3182 target), and linear."""
    rule = _rule_for(word)
    unit = shape.format(word)
    _assert_linear(
        rule.pattern.sub,
        lambda size: (rule, _repeat(unit, size)),
        SIXTY_FOUR_KB, PATTERN_LIMIT_SECONDS / 2,
        f"{unit!r} half budget")
