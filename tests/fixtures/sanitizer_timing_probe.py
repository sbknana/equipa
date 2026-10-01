"""Time one lesson-sanitizer call on one adversarial input (tasks 3129, 3139, 3145).

Run as a child process by tests/test_lesson_sanitizer_3129.py,
tests/test_sanitizer_3139.py and tests/test_sanitizer_3145.py, so that a
super-linear regex fails the test at the subprocess timeout instead of
freezing the whole suite (``re`` holds the GIL, so no in-process timeout can
interrupt it).

Usage: sanitizer_timing_probe.py REPO_ROOT TARGET CASE SIZE
  TARGET  "sanitize", "boundaries" or "pattern" (the compiled patterns the
          case targets, searched directly on the raw input, with no input
          cap): prints the elapsed wall time in seconds.
          "each-pattern": times every pattern the case targets separately
          and prints one "SECONDS<TAB>REASON" line per pattern.
          "validate": validate_lesson_structure(), the lesson allowlist.
          "db-context", "checkpoint", "compaction-summary", "episode": one
          real call site that injects the text into a prompt.
          "tester-context", "recovery", "agent-messages": a prompt builder
          that sanitizes several agent-authored fields, with the text in
          every one of them (task 3145).
  CASE    a key of ADVERSARIAL_CASES
  SIZE    input length in characters

Usage: sanitizer_timing_probe.py REPO_ROOT dedup LINES
  Times the compaction duplicate-line removal on LINES distinct lines.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

# Reasons tuple meaning "every pattern in _INJECTION_PATTERNS", resolved at
# run time so a pattern added later is attacked without editing this file.
EVERY_PATTERN: tuple[str, ...] = ("*",)

# A word the joiner folding splits ("ig-nore" becomes "ignore" and
# "ig nore"), so sanitize() scans the run three times (review N1 of 3129).
_HYPHENATED_PREFIX = "ig-nore."

# Whitespace runs (review N1/N2 of task 3129): "\s" before a keyword overlapped
# a "\n" start class and made one rule quadratic on newline runs, and no case
# had a long whitespace run. Each family runs against EVERY pattern, alone and
# after a hyphenated word.
_WHITESPACE_RUNS: dict[str, Callable[[int], str]] = {
    "newlines": lambda n: "\n" * n,
    "crlf": lambda n: "\r\n" * (n // 2),
    "newline-space": lambda n: "\n " * (n // 2),
    "spaces": lambda n: " " * n,
    "tabs": lambda n: "\t" * n,
    "mixed-whitespace": lambda n: " \t\n\r\n" * (n // 5),
    "sentence-end-newlines": lambda n: ". \n" * (n // 3),
    # Line separators the sanitizer now matches as newlines (review F6 of
    # task 3139): Unicode Zl / Zp, and the C0/C1 controls str.splitlines()
    # breaks on.
    "line-separators": lambda n: " " * n,
    "paragraph-separators": lambda n: " " * n,
    "sentence-end-line-separators": lambda n: ". " * (n // 2),
    "control-line-breaks": lambda n: "\x0b\x0c\x1c\x1d\x1e\x85" * (n // 6),
}


def _prefixed(build: Callable[[int], str]) -> Callable[[int], str]:
    return lambda n: _HYPHENATED_PREFIX + build(n - len(_HYPHENATED_PREFIX))


# case name -> (pattern reasons it attacks, input builder taking a length)
ADVERSARIAL_CASES: dict[str, tuple[tuple[str, ...], Callable[[int], str]]] = {
    "lt-then-spaces": (
        ("trust-boundary marker", "role tag"),
        lambda n: "<" + " " * n,
    ),
    "lt-spaces-slash-spaces": (
        ("trust-boundary marker", "role tag"),
        lambda n: "<" + " " * (n // 2) + "/" + " " * (n // 2),
    ),
    "triple-lt-then-spaces": (
        ("trust-boundary marker",),
        lambda n: "<<<" + " " * n,
    ),
    "role-tag-never-closed": (
        ("role tag",),
        lambda n: "<system " * (n // 8),
    ),
    "double-lt-then-spaces": (
        ("chat-template marker",),
        lambda n: "<<" + " " * n,
    ),
    "newline-then-spaces": (
        ("chat-template marker",),
        lambda n: "\n" + " " * n,
    ),
    "bracket-then-spaces": (
        ("chat-template marker",),
        lambda n: "[" + " " * n,
    ),
    "header-word-then-spaces": (
        ("fake system header",),
        lambda n: "system" + " " * n,
    ),
    "override-words-repeated": (
        ("role override",),
        lambda n: "ignore all act as a forget all " * (n // 31),
    ),
    "act-as-after-you-then-spaces": (
        ("role override",),
        lambda n: "you" + " " * n,
    ),
    "new-rules-after-bullet-spaces": (
        ("role override",),
        lambda n: ".-" + " " * n,
    ),
    "command-words-repeated": (
        ("command instruction",),
        lambda n: ". execute these run this " * (n // 25),
    ),
    "rm-flag-run": (
        ("dangerous command",),
        lambda n: "rm -" + "rf" * (n // 2),
    ),
    "python-c-unterminated": (
        ("dangerous command",),
        lambda n: "python -c '" + "import " * (n // 7),
    ),
    "curl-repeated": (
        ("dangerous command",),
        lambda n: "curl " * (n // 5),
    ),
    "fence-never-closed": (
        ("code block with command",),
        lambda n: "```\n" + "rm " * (n // 3),
    ),
    "fence-no-command": (
        ("code block with command",),
        lambda n: "```\n" + "x " * (n // 2),
    ),
    "base64-runs-just-short": (
        ("encoded payload",),
        lambda n: ("A" * 79 + " ") * (n // 80),
    ),
    "unicode-escape-prefixes": (
        ("encoded payload",),
        lambda n: "\\u00" * (n // 4),
    ),
    # Normalisation and lesson-allowlist costs rather than a single pattern.
    "if-clauses-repeated": ((), lambda n: "if a " * (n // 5)),
    "joiner-splits": ((), lambda n: "ig-" * (n // 3)),
    "identifier-splits": ((), lambda n: "sudo_mode " * (n // 10)),
    # The worst call-site inputs review F6 of task 3139 found: every short
    # identifier is a joiner run and a context check, and words between
    # line separators.
    "short-identifiers": ((), lambda n: "a_b " * (n // 4)),
    "words-between-line-separators": ((), lambda n: "a " * (n // 2)),
    # Phrases in statement position: each one is found and its look-back
    # read, and then the scan moves on.
    "statement-phrases": (
        ("role override", "command instruction"),
        lambda n: "ruff ships new rules: CI will execute this script " * (n // 50),
    ),
    "small-capitals": ((), lambda n: "ɪɢɴᴏʀᴇ " * (n // 7)),
    "format-characters": ((), lambda n: "a؀" * (n // 2)),
    "nfkd-expansion": ((), lambda n: "<" + "ﷺ" * (n - 1)),
}

for _name, _build in _WHITESPACE_RUNS.items():
    ADVERSARIAL_CASES[f"ws-{_name}"] = (EVERY_PATTERN, _build)
    ADVERSARIAL_CASES[f"ws-hyphenated-{_name}"] = (EVERY_PATTERN, _prefixed(_build))

WHITESPACE_CASES: tuple[str, ...] = tuple(
    name for name, (reasons, _) in ADVERSARIAL_CASES.items()
    if reasons == EVERY_PATTERN
)


def distinct_log_lines(count: int) -> list[str]:
    """*count* log lines that share no grouping key and few character n-grams."""
    alphabet = "abcdefghijklmnopqrstuvwxyz"

    def word(value: int) -> str:
        letters = []
        for _ in range(5):
            value, digit = divmod(value * 7919 + 13, 26)
            letters.append(alphabet[digit])
        return "".join(letters)

    return [" ".join(word(line * 8 + slot) for slot in range(8)) for line in range(count)]


def _selected_patterns(lesson_sanitizer, reasons: tuple[str, ...]) -> list:
    return [
        (reason, pattern)
        for reason, pattern in lesson_sanitizer._INJECTION_PATTERNS
        if reasons == EVERY_PATTERN or reason in reasons
    ]


def _elapsed(call: Callable[[], object]) -> float:
    start = time.perf_counter()
    call()
    return time.perf_counter() - start


def main(argv: list[str]) -> int:
    repo_root, target = argv[1], argv[2]
    sys.path.insert(0, repo_root)
    import lesson_sanitizer

    if target == "dedup":
        from equipa.parsing import _deduplicate_log_lines

        lines = distinct_log_lines(int(argv[3]))
        print(f"{_elapsed(lambda: _deduplicate_log_lines(lines)):.6f}")
        return 0

    case, size = argv[3], int(argv[4])
    reasons, build = ADVERSARIAL_CASES[case]
    text = build(size)
    patterns = _selected_patterns(lesson_sanitizer, reasons)

    if target == "each-pattern":
        for reason, pattern in patterns:
            elapsed = _elapsed(lambda: pattern.search(text))
            print(f"{elapsed:.6f}\t{reason}")
        return 0

    if target == "sanitize":
        def call() -> None:
            lesson_sanitizer.sanitize(text)
    elif target == "boundaries":
        def call() -> None:
            lesson_sanitizer.neutralize_boundaries(text)
    elif target == "validate":
        def call() -> None:
            lesson_sanitizer.validate_lesson_structure(text)
    elif target == "pattern":
        def call() -> None:
            for _, pattern in patterns:
                pattern.search(text)
    elif target == "db-context":
        from equipa.prompts import _sanitize_db_context

        def call() -> None:
            _sanitize_db_context(text, "decision")
    elif target == "checkpoint":
        from equipa.prompts import build_checkpoint_context

        def call() -> None:
            build_checkpoint_context(text, 2)
    elif target == "compaction-summary":
        from equipa.parsing import build_compaction_summary

        def call() -> None:
            build_compaction_summary(
                "developer", {"result_text": text}, 1, {"id": 1, "title": "t"}
            )
    elif target == "episode":
        from equipa.lessons import sanitize_episode_text

        def call() -> None:
            sanitize_episode_text(text, "reflection")
    elif target == "tester-context":
        from equipa.parsing import build_test_failure_context

        # Every Tester-authored field at SIZE: 5 details, 3 recommendations
        # and the framework name are each sanitized.
        tester_results = {
            "tests_run": 9,
            "tests_failed": 9,
            "test_framework": text,
            "failure_details": [text] * 5,
            "recommendations": [text] * 3,
        }

        def call() -> None:
            build_test_failure_context(tester_results, 1)
    elif target == "recovery":
        from equipa.checkpoints import build_compaction_recovery_context

        # The last output, every .forge-state.json field and every path list.
        soft_checkpoint = {
            "last_result_text": text,
            "files_changed": [text],
            "files_read": [text],
        }
        forge_state = {
            "current_step": text,
            "next_action": text,
            "decisions": [text],
            "files_changed": [text],
        }

        def call() -> None:
            build_compaction_recovery_context(soft_checkpoint, forge_state)
    elif target == "agent-messages":
        import json

        from equipa.messages import format_messages_for_prompt

        messages = [
            {
                "from_role": "tester",
                "message_type": "test_failures",
                "cycle_number": 1,
                "content": json.dumps({"failures": [text], "summary": text}),
            },
            {
                "from_role": "developer",
                "message_type": "note",
                "cycle_number": 1,
                "content": text,
            },
        ]

        def call() -> None:
            format_messages_for_prompt(messages)
    else:
        raise SystemExit(f"unknown target {target!r}")

    print(f"{_elapsed(call):.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
