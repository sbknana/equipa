#!/usr/bin/env python3
"""Task 3149: follow-ups of the task 3143 severity-token backstop.

The independent review of 3143 (indep-3143) and EQUIPA's own review
(SECURITY-REVIEW-3143) found that the backstop still merged a real CRITICAL
or HIGH behind an all-zero footer through its zero-count paths. Every row
below fails on the commit before this task.

* R3143-01 / SR3143-01: a heading the parser never counted (inside <p>, after
  "&#35;" decoding) excused itself, its section and its ID.
* R3143-02 / SR3143-02: tally, negation, comparison and list exemptions
  accepted an explicit "<sev>:" or "<sev> -" finding label.
* R3143-03 / SR3143-03: Markdown inline markup split a severity word.
* R3143-04 / SR3143-04, R3143-05: long numeric references and U+0130 raised
  instead of returning a verdict; the gate now turns any parse exception into
  a logged block.
* R3143-06: the reviewer prompt's own prose used UPPER-case severity words;
  MEDIUM-only tokens are counted and logged but no longer block a merge.
* R3143-07: confusable letters and bidi controls.
* R3143-08: the invisible-character list is written with escapes.

Copyright 2026 Forgeborn
"""

import unicodedata
from pathlib import Path

from equipa import security_gate

REPO = Path(__file__).resolve().parent.parent


# --- R3143-08: the invisible-character list uses escapes ------------------------

def test_r3143_08_security_gate_source_holds_no_invisible_character():
    """The PUBLIC repo must not ship raw bidi or zero-width characters."""
    source = (REPO / "equipa" / "security_gate.py").read_text(encoding="utf-8")
    hidden = sorted({
        f"U+{ord(char):04X}" for char in source
        if security_gate._INVISIBLE_CHARS_RE.match(char)
        or unicodedata.category(char) in ("Cf", "Zl", "Zp")
    })
    assert hidden == [], hidden


def test_r3143_08_escaped_list_still_strips_every_format_character():
    missing = [
        f"U+{code_point:04X}" for code_point in range(0x110000)
        if unicodedata.category(chr(code_point)) == "Cf"
        and not security_gate._INVISIBLE_CHARS_RE.match(chr(code_point))
    ]
    assert missing == []
