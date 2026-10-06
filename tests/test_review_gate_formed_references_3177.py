"""Task 3177: the review gate applies its Unicode table to every reference it
decodes and every text it normalises (IR3174-01), and reads non-ASCII text
only under Unicode data it is proven on (IR3174-03).

Task 3172 checked the table on the review as written only. indep-3174 found
references that exist only once the review is normalised: under NFKC
"<FULLWIDTH AMPERSAND>#x1AC1;" is "&#x1AC1;", and deleting a zero-width
space joins "&", "#x1AC1;". U+1AC1 is a mark the backstop deletes on Python
3.12 and unassigned on 3.10, so "CRI" + such a reference + "TICAL" blocked
on 3.12 and merged on 3.10 (12,171 of 107,455 generated bodies differed).
This task also found references the renderer forms by removing a comment or
a tag ("&<!-- -->#xA7F2;") diverging the same way.

* A generated look-alike set (tests/gate_unicode_lookalikes.py, built from
  the checked-in table only) is decided through
  ``dispatch._security_review_blocks_merge``; a digest of every decision is
  pinned, and CI runs this file on 3.10 and 3.12, so both interpreters give
  identical verdicts (scripts/gate_unicode_verdicts.py compares 100,000 and
  more bodies between two interpreters).
* A code point outside the table decides as U+0378 (unassigned in every
  version) does, in every spelling and placement.
* Every reference decoder of the gate decodes through
  ``loops._gate_unescaped``, which refuses a character outside the table.
* Under Unicode data the table is not proven on (Python 3.14 ships 16.0, in
  which U+1171E, a table character, became a spacing mark), the gate parses
  no review that is not ASCII, and says why.

The source stays ASCII: special characters are named or built with chr().

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import functools
import hashlib
import html
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import equipa.security_gate as security_gate
from equipa import loops
from tests import gate_unicode_lookalikes as lookalikes
from tests.review_gate_production import (
    as_reviewer_artifact,
    decision_and_analysis,
    production_decision,
)

REPOSITORY = Path(__file__).resolve().parent.parent

# The code points indep-3174 reported (one mark of each block new in Unicode
# 14 and 15, U+A7F2, U+1734) and U+0378, in every spelling and placement.
REPORTED_CODE_POINTS = sorted(
    lookalikes.one_mark_per_block()
    + [lookalikes.MODIFIER_CAPITAL_C, 0x1734, lookalikes.UNASSIGNED])
# More code points outside the table (a seeded sample and the other two
# Unicode 15 changes), U+1171E and a spread of the table's lookalikes, in
# every spelling, in two placements.
EXTRA_CODE_POINTS = sorted(
    set(lookalikes.outside_table_sample(12, seed=3177))
    | {0x10FC, 0xAB69, lookalikes.CHANGED_BY_UNICODE_16}
    | set(lookalikes.table_lookalikes()[::60]))
EXTRA_PLACEMENTS = ("severity-field", "heading-first-letter")
GENERATED_BODIES = {
    **lookalikes.lookalike_bodies(REPORTED_CODE_POINTS),
    **lookalikes.lookalike_bodies(EXTRA_CODE_POINTS,
                                  placements=EXTRA_PLACEMENTS),
}
# sha256 of every generated body's decision (see _decisions_digest), the
# same on every interpreter CI runs; and how many of them block.
GENERATED_BODY_COUNT = 2_482
GENERATED_BLOCKED_COUNT = 1_934
GENERATED_DECISIONS_DIGEST = (
    "5e08a73e5bc20e41ae39f930317ceaf17efe9e7f494f0cd6213887cd6bc57ff0"
)


def _decide(body: str) -> tuple[bool, str, str]:
    decision, analysis = decision_and_analysis(
        as_reviewer_artifact(lookalikes.review(body)))
    assert decision.provenance.trusted, decision.provenance.reason
    return decision.blocks, analysis.verdict, analysis.detail


@functools.lru_cache(maxsize=1)
def _generated_decisions() -> dict[str, tuple[bool, str, str]]:
    return {name: _decide(body) for name, body in GENERATED_BODIES.items()}


def _decisions_digest(decisions: dict[str, tuple[bool, str, str]]) -> str:
    return hashlib.sha256(json.dumps(sorted(
        [name, *decision] for name, decision in decisions.items()
    )).encode("utf-8")).hexdigest()


def test_generated_lookalikes_decide_alike_on_every_interpreter():
    decisions = _generated_decisions()
    blocked = sum(1 for blocks, _verdict, _detail in decisions.values()
                  if blocks)
    assert (len(decisions), blocked) == (GENERATED_BODY_COUNT,
                                         GENERATED_BLOCKED_COUNT)
    assert _decisions_digest(decisions) == GENERATED_DECISIONS_DIGEST


def _html_decodes_to_itself(code_point: int) -> bool:
    """False for the code points ``html.unescape`` drops (noncharacters and
    some controls), whose references decode to nothing."""
    return html.unescape(f"&#x{code_point:X};") == chr(code_point)


@pytest.mark.parametrize("code_point", [
    code_point for code_point in REPORTED_CODE_POINTS + EXTRA_CODE_POINTS
    if not loops._in_gate_unicode_table(chr(code_point))
    and code_point != lookalikes.UNASSIGNED
    and _html_decodes_to_itself(code_point)
], ids=lambda code_point: f"U+{code_point:04X}")
def test_a_code_point_outside_the_table_decides_as_an_unassigned_one(
        code_point):
    """Whatever the interpreter's data says of it (a new mark, a new letter,
    unassigned), its decision is U+0378's, with its own name."""
    decisions = _generated_decisions()
    own = f"U+{code_point:04X}"
    unassigned = f"U+{lookalikes.UNASSIGNED:04X}"
    placements = (lookalikes.PLACEMENTS if code_point in REPORTED_CODE_POINTS
                  else EXTRA_PLACEMENTS)
    for spelling in lookalikes.SPELLINGS:
        for placement in placements:
            blocks, verdict, detail = decisions[
                lookalikes.body_name(placement, spelling, code_point)]
            expected = decisions[lookalikes.body_name(
                placement, spelling, lookalikes.UNASSIGNED)]
            assert (blocks, verdict, detail.replace(own, unassigned)) \
                == expected, (placement, spelling)


@pytest.mark.parametrize("code_point", REPORTED_CODE_POINTS,
                         ids=lambda code_point: f"U+{code_point:04X}")
def test_every_reported_reference_is_refused(code_point):
    """Each spelling that is a reference once normalised, and the ones the
    renderer forms, is refused and names the code point."""
    decisions = _generated_decisions()
    for spelling in (*lookalikes.NORMALIZED_SPELLINGS, "comment", "tag"):
        for placement in lookalikes.PLACEMENTS:
            blocks, verdict, detail = decisions[
                lookalikes.body_name(placement, spelling, code_point)]
            assert blocks, (placement, spelling)
            assert verdict == loops.REVIEW_VERDICT_INCOMPLETE, (
                placement, spelling, detail)
            assert detail.startswith(
                f"{loops.REVIEW_UNICODE_DATA_REASON}: U+{code_point:04X} "
                f"reference "), (placement, spelling, detail)


# --- Every decoder goes through _gate_unescaped ---------------------------

# The functions allowed to call html.unescape: the chokepoint, and the
# table check itself (it decodes a reference to name what it refuses).
_UNESCAPE_CALLERS = {"_gate_unescaped", "_reference_decoded"}


def _unescape_callers(path: Path) -> list[tuple[str, int]]:
    """(enclosing function, line) of every html.unescape call in ``path``,
    also through ``from html import unescape``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call):
                callee = child.func
                if ((isinstance(callee, ast.Attribute)
                     and callee.attr == "unescape")
                        or (isinstance(callee, ast.Name)
                            and callee.id == "unescape")):
                    found.append((function, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_every_gate_decoder_decodes_through_the_table_check():
    callers = _unescape_callers(REPOSITORY / "equipa" / "loops.py")
    # Not vacuous: both allowed callers are seen.
    assert {function for function, _line in callers} == _UNESCAPE_CALLERS, (
        callers)
    assert _unescape_callers(REPOSITORY / "equipa" / "security_gate.py") == []


def test_the_fence_sees_a_decoder_that_bypasses_the_check(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import html\n"
        "from html import unescape\n"
        "def _decoded(reference):\n"
        "    return html.unescape(reference)\n"
        "class Table:\n"
        "    def __missing__(self, key):\n"
        "        return unescape(key)\n",
        encoding="utf-8",
    )
    assert _unescape_callers(planted) == [("_decoded", 4),
                                          ("__missing__", 7)]


@pytest.mark.parametrize("reference, code_point", [
    ("&#x1AC1;", 0x1AC1),
    ("&#42994;", 0xA7F2),
    ("&#x0378;", 0x0378),
    ("&#x31350;", 0x31350),
])
def test_gate_unescaped_refuses_a_character_outside_the_table(
        reference, code_point):
    with pytest.raises(loops._ReferenceOutsideGateUnicodeData) as refused:
        loops._gate_unescaped(reference)
    assert refused.value.code_point == code_point
    assert str(refused.value) == f"U+{code_point:04X}"


@pytest.mark.parametrize("reference, decoded", [
    ("&#x2014;", "\N{EM DASH}"),
    ("&Eta;", "\N{GREEK CAPITAL LETTER ETA}"),
    ("&#x110000;", "\N{REPLACEMENT CHARACTER}"),
    ("&#xFDD0;", ""),
    ("&#72;", "H"),
    ("&unknown;", "&unknown;"),
])
def test_gate_unescaped_reads_table_characters(reference, decoded):
    assert loops._gate_unescaped(reference) == decoded


# --- IR3174-03: Unicode data the table is not proven on --------------------

class _UnicodeDataOfVersion:
    """``unicodedata`` reporting another ``unidata_version``."""

    def __init__(self, version: str) -> None:
        self.unidata_version = version

    def __getattr__(self, name: str) -> Any:
        return getattr(unicodedata, name)


# Every table the gate fills per character or reference seen: a decision
# under another version must not reuse what an earlier one cached.
_GATE_TABLES = ("_BACKSTOP_CHARACTERS", "_BACKSTOP_SPELLINGS",
                "_BACKSTOP_SEPARATORS", "_BACKSTOP_SEPARATED_SPELLINGS")
_GATE_REFERENCE_TABLES = ("_RENDERED_REFERENCES", "_HTML_BLOCK_REFERENCES",
                          "_BACKSTOP_REFERENCES")


def _use_unicode_data_version(version: str,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GATE_TABLES:
        monkeypatch.setattr(loops, name, type(getattr(loops, name))())
    for name in _GATE_REFERENCE_TABLES:
        table = getattr(loops, name)
        monkeypatch.setattr(loops, name, type(table)(table._decode))
    data = _UnicodeDataOfVersion(version)
    monkeypatch.setattr(loops, "unicodedata", data)
    monkeypatch.setattr(security_gate, "unicodedata", data)


def _unproven(version: str) -> str:
    return (f": the interpreter's Unicode data {version} is not one the "
            f"gate's table is proven on (13.0.0, 15.0.0) (name the "
            f"character, never paste it)")


AHOM_MEDIAL_RA = chr(lookalikes.CHANGED_BY_UNICODE_16)
# indep-3174 (Python 3.14 vs 3.12): bodies that blocked on 3.12 and merged
# on 3.14, and other non-ASCII bodies; each is refused under unproven data.
UNPROVEN_REFUSED_BODIES = {
    "u1171e-severity-field": (f"- **Severity:** CRITI{AHOM_MEDIAL_RA}CAL",
                              "U+1171E at line 8"),
    "u1171e-heading": (f"### [S2] HI{AHOM_MEDIAL_RA}GH - upload",
                       "U+1171E at line 8"),
    "u1171e-ascii-reference": ("### [S2] HI&#x1171E;GH - upload",
                               "U+1171E reference at line 8"),
    "em-dash": ("- **Status:** fixed \N{EM DASH} nothing else",
                "U+2014 at line 8"),
    # Formed by the renderer in an ASCII review, and a named reference: only
    # where they are decoded is either seen.
    "comment-formed-ascii-review": ("### [S2] HI&<!-- -->#xE9;GH - upload",
                                    "U+00E9 reference once decoded"),
    "named-reference": ("- **Status:** caf&eacute; fixed",
                        "U+00E9 reference once decoded"),
}


@pytest.mark.parametrize("version",
                         ["16.0.0", "15.1.0", "14.0.0", "17.0.0", "15.0.1"])
@pytest.mark.parametrize("name", sorted(UNPROVEN_REFUSED_BODIES))
def test_unproven_unicode_data_parses_no_non_ascii_review(
        name, version, monkeypatch):
    body, named = UNPROVEN_REFUSED_BODIES[name]
    _use_unicode_data_version(version, monkeypatch)
    assert not loops._gate_unicode_data_is_current()
    decision, analysis = decision_and_analysis(
        as_reviewer_artifact(lookalikes.review(body)))
    assert decision.provenance.trusted, decision.provenance.reason
    assert decision.blocks
    assert analysis.verdict == loops.REVIEW_VERDICT_INCOMPLETE
    assert analysis.detail == (f"{loops.REVIEW_UNICODE_DATA_REASON}: "
                               f"{named}" + _unproven(version))


@pytest.mark.parametrize("version", ["16.0.0", "14.0.0"])
def test_unproven_unicode_data_still_parses_an_ascii_review(
        version, monkeypatch):
    """ASCII reads the same in every Unicode version, references to ASCII
    included."""
    _use_unicode_data_version(version, monkeypatch)
    decision = production_decision(as_reviewer_artifact(lookalikes.review(
        "- **Status:** fixed &#45; nothing else")))
    assert decision.provenance.trusted and not decision.blocks
    decision = production_decision(as_reviewer_artifact(lookalikes.review(
        "### [S2] HIGH - upload")))
    assert decision.provenance.trusted and decision.blocks


@pytest.mark.parametrize("version, proven", [
    ("13.0.0", True), ("15.0.0", True), ("16.0.0", False),
    ("15.1.0", False), ("14.0.0", False), ("13.0", False), ("15.0.0 ", False),
])
def test_only_the_proven_versions_are_current(version, proven, monkeypatch):
    monkeypatch.setattr(loops, "unicodedata", _UnicodeDataOfVersion(version))
    assert loops._gate_unicode_data_is_current() is proven


def test_the_proven_versions_are_the_ones_ci_runs():
    """CI runs the pinned table digest on 3.10 (Unicode 13.0.0) and 3.12
    (15.0.0); the running interpreter is one of them."""
    assert loops._GATE_PROVEN_UNICODE_VERSIONS == ("13.0.0", "15.0.0")
    assert unicodedata.unidata_version in loops._GATE_PROVEN_UNICODE_VERSIONS
    workflow = (REPOSITORY / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8")
    assert 'python-version: ["3.10", "3.12"]' in workflow
