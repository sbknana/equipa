"""Time one lesson-sanitizer call on one adversarial input (task 3129).

Run as a child process by tests/test_lesson_sanitizer_3129.py, so that a
super-linear regex fails the test at the subprocess timeout instead of
freezing the whole suite (``re`` holds the GIL, so no in-process timeout can
interrupt it).

Usage: sanitizer_timing_probe.py REPO_ROOT TARGET CASE SIZE
  TARGET  "sanitize", "boundaries" or "pattern" (the compiled patterns the
          case targets, searched directly on the raw input, with no input cap)
  CASE    a key of ADVERSARIAL_CASES
  SIZE    input length in characters

Prints the elapsed wall time in seconds.

Copyright 2026 Forgeborn.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

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
    # Normalisation costs rather than a single pattern.
    "joiner-splits": ((), lambda n: "ig-" * (n // 3)),
    "small-capitals": ((), lambda n: "ɪɢɴᴏʀᴇ " * (n // 7)),
    "format-characters": ((), lambda n: "a؀" * (n // 2)),
    "nfkd-expansion": ((), lambda n: "<" + "ﷺ" * (n - 1)),
}


def main(argv: list[str]) -> int:
    repo_root, target, case, size = argv[1], argv[2], argv[3], int(argv[4])
    sys.path.insert(0, repo_root)
    import lesson_sanitizer

    reasons, build = ADVERSARIAL_CASES[case]
    text = build(size)
    if target == "sanitize":
        def call() -> None:
            lesson_sanitizer.sanitize(text)
    elif target == "boundaries":
        def call() -> None:
            lesson_sanitizer.neutralize_boundaries(text)
    elif target == "pattern":
        patterns = [
            pattern
            for reason, pattern in lesson_sanitizer._INJECTION_PATTERNS
            if reason in reasons
        ]

        def call() -> None:
            for pattern in patterns:
                pattern.search(text)
    else:
        raise SystemExit(f"unknown target {target!r}")

    start = time.perf_counter()
    call()
    print(f"{time.perf_counter() - start:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
