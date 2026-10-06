#!/usr/bin/env python3
"""Task 3169: every regex in equipa/ compiles on Python 3.10, matching as before.

Python 3.10 has no possessive quantifiers (``*+``, ``{m,n}+``) and no
atomic groups (``(?>...)``): one such pattern at module level made
equipa.loops, and everything importing it, unimportable there (``re.error:
multiple repeat``). The 17 patterns that used them were rewritten in 3.10
syntax (see the comment above ``loops._atomic``). Four guards:

* every equipa module imports under the running interpreter, and every
  module-level pattern in it recompiles from its own text;
* no string literal in equipa/ and no compiled module-level pattern holds a
  possessive quantifier or an atomic group (tests/regex_compat_scan.py), so
  the 3.11-only syntax cannot come back unnoticed on a 3.11+ host;
* each rewritten pattern matches exactly as its original
  (tests/regex_py311_originals_3169.py) on a generated corpus: compared
  match for match where the original compiles (3.11+), and through a digest
  of the original's results (recorded on 3.12) on every interpreter, so 3.10
  checks the same matching;
* each rewritten pattern runs a long run of what its loops read in a few
  bytes per character, as the possessive original did (a plain loop over a
  group keeps a backtracking entry per pass: 366 MB for a 3.2 MB title).

Linear time is held by the existing timing tests (review-gate families),
which run these patterns on 200 KB inputs.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import pkgutil
import re
import sys
import tracemalloc
from pathlib import Path
from types import ModuleType

import pytest

import equipa
from equipa import loops
from tests.regex_compat_scan import (
    EQUIPA_DIR,
    find_311_constructs,
    scan_source_tree,
    string_constants,
)
from tests.regex_py311_originals_3169 import ORIGINALS


def _equipa_module_names() -> list[str]:
    return sorted(info.name for info in
                  pkgutil.walk_packages(equipa.__path__, "equipa."))


def _module_patterns(module: ModuleType) -> list[tuple[str, re.Pattern]]:
    """Every compiled pattern a module holds at module level: bound to a
    name, or one level down in a tuple, list, set or dict value."""
    found: list[tuple[str, re.Pattern]] = []
    for name, value in sorted(vars(module).items()):
        if isinstance(value, re.Pattern):
            found.append((name, value))
        elif isinstance(value, (tuple, list, frozenset, set)):
            found.extend((f"{name}[{index}]", item)
                         for index, item in enumerate(value)
                         if isinstance(item, re.Pattern))
        elif isinstance(value, dict):
            found.extend((f"{name}[{key!r}]", item)
                         for key, item in value.items()
                         if isinstance(item, re.Pattern))
    return found


def _is_re_compile(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re")


def _assigned_pattern_names(module: ModuleType) -> set[str]:
    """Names a top-level ``NAME = re.compile(...)`` statement binds."""
    source = Path(module.__file__).read_text(encoding="utf-8")
    names: set[str] = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and _is_re_compile(statement.value):
            names.update(target.id for target in statement.targets
                         if isinstance(target, ast.Name))
        elif (isinstance(statement, ast.AnnAssign)
              and statement.value is not None
              and _is_re_compile(statement.value)
              and isinstance(statement.target, ast.Name)):
            names.add(statement.target.id)
    return names


# --- every module imports; every module-level pattern compiles here --------


@pytest.mark.parametrize("module_name", _equipa_module_names())
def test_every_module_level_regex_compiles_under_this_interpreter(module_name):
    # Importing runs every module-level re.compile under this interpreter.
    module = importlib.import_module(module_name)
    patterns = _module_patterns(module)
    for name, pattern in patterns:
        re.purge()  # compile again, not from the cache
        try:
            recompiled = re.compile(pattern.pattern, pattern.flags)
        except re.error as exc:
            pytest.fail(f"{module_name}.{name} does not compile on "
                        f"{sys.version.split()[0]}: {exc}")
        assert recompiled.groups == pattern.groups, name
    # Each top-level ``NAME = re.compile(...)`` is among the patterns read.
    missing = _assigned_pattern_names(module) - {name for name, _ in patterns}
    assert not missing, f"{module_name}: {sorted(missing)} not found"


def test_the_module_walk_reaches_the_patterns_that_broke_on_3_10():
    """The walk is not vacuous: it reaches subpackages and loops' patterns."""
    names = _equipa_module_names()
    assert "equipa.loops" in names
    assert any(name.startswith("equipa.hooks.") for name in names)
    loop_patterns = {name for name, _ in _module_patterns(loops)}
    assert {name for (_module, name) in ORIGINALS} <= loop_patterns


# --- no 3.11-only syntax in equipa/ ------------------------------------------


def test_no_string_literal_in_equipa_holds_3_11_only_regex_syntax():
    assert scan_source_tree() == []


def test_no_compiled_equipa_pattern_holds_3_11_only_regex_syntax():
    """Catches syntax assembled from pieces the literal scan sees apart
    (a fragment ending in ``*`` joined to one starting with ``+``)."""
    findings = []
    for module_name in _equipa_module_names():
        module = importlib.import_module(module_name)
        for name, pattern in _module_patterns(module):
            if not isinstance(pattern.pattern, str):
                continue
            for construct in find_311_constructs(pattern.pattern):
                findings.append(f"{module_name}.{name}: {construct.kind} "
                                f"at {construct.offset}")
    assert findings == []


POSSESSIVE_311 = [r"a*+", r"a++", r"a?+", r"a{2}+", r"a{1,3}+", r"a{,3}+",
                  r"a{2,}+", r"(?:ab)*+", r"[ab]++", r"\d++", r"x[ \t]*+y"]
ATOMIC_311 = [r"(?>a)", r"x(?>[^)]*?\))y", r"(?:(?>a|ab)c)"]
VALID_310 = [r"a+", r"a*", r"a*?", r"a+?", r"a??", r"a{1,3}?", r"[*+]",
             r"[-*+]", r"\*+", r"\++", r"\?+", r"[]+]", r"[^]*+]", r"(?#*+)",
             r"(?=a)", r"(?!a)", r"(?<=a)", r"(?<!a)", r"(?:a)+", r"(?P<n>a)+",
             r"(?P<m>a)(?P=m)+", r"x{a}+", r"x{}+", r"a{,", r"(?i:a)+",
             r"(?=(?P<_atomic1>a+))(?P=_atomic1)"]


@pytest.mark.parametrize("pattern", POSSESSIVE_311)
def test_the_scanner_finds_possessive_quantifiers(pattern):
    assert [construct.kind for construct in find_311_constructs(pattern)] == [
        "possessive"]


@pytest.mark.parametrize("pattern", ATOMIC_311)
def test_the_scanner_finds_atomic_groups(pattern):
    assert [construct.kind for construct in find_311_constructs(pattern)] == [
        "atomic"]


@pytest.mark.parametrize("pattern", POSSESSIVE_311 + ATOMIC_311)
def test_what_the_scanner_finds_is_syntax_only_3_11_compiles(pattern):
    """The scanner's findings are exactly what 3.10 rejects: re.error there,
    a valid pattern from 3.11 on (the same test, on each interpreter)."""
    re.purge()
    if sys.version_info >= (3, 11):
        re.compile(pattern)
    else:
        with pytest.raises(re.error):
            re.compile(pattern)


@pytest.mark.parametrize("pattern", VALID_310)
def test_the_scanner_passes_3_10_syntax(pattern):
    assert find_311_constructs(pattern) == []
    re.compile(pattern)


def test_the_scanner_finds_every_original_pattern():
    """Positive control on the real data: each pre-3169 pattern is found."""
    for (_module, name), (pattern, _flags) in ORIGINALS.items():
        assert find_311_constructs(pattern), name


def test_string_constants_join_adjacent_literals_and_skip_docstrings():
    source = (
        '"""Module prose may quote a*+ and (?>x)."""\n'
        'SPLIT = r"[ \\t]*" "+x"\n'
        "def helper():\n"
        '    """So may a function docstring: a++."""\n'
        '    return "(?>y)"\n'
        'DATA = b"z?+"\n'
    )
    constants = [(line, text) for line, text in string_constants(source)]
    found = {text: [construct.kind for construct in find_311_constructs(text)]
             for _line, text in constants}
    assert found == {r"[ \t]*+x": ["possessive"], "(?>y)": ["atomic"],
                     "z?+": ["possessive"]}


def test_the_scanner_reads_the_equipa_tree():
    assert EQUIPA_DIR == Path(equipa.__file__).resolve().parent
    assert (EQUIPA_DIR / "loops.py").is_file()


# --- each rewritten pattern matches exactly as its original ------------------

_FOLD_H = chr(loops._BACKSTOP_FOLD_MARK + ord("H"))
_NEW_FOLD_I = chr(loops._BACKSTOP_NEW_FOLD_MARK + ord("I"))

# Token alphabets per pattern family: the characters and words each pattern
# reads, so short concatenations reach every branch, and the ones next to
# them (a letter, a space) that end a run.
_RESOLVED_TOKENS = ["(", "[", ")", "]", "\n", " ", "\t", "fixed", "Resolved",
                    "not counted", "NOT\tcounted", "x", "\N{EM DASH}",
                    "\N{EN DASH}", ":", "\N{RIGHTWARDS ARROW}", "-", "FIXED",
                    "RESOLVED", "**", "_", ",", ";", "ok, verified", "(note)"]
_HTML_TOKENS = ["<", ">", "/", "a", "b-1", "_:x.y", " ", "\t", "=", "'", '"',
                "v", "script", "pre", "\n", "`", "x="]
_REFERENCE_TOKENS = ["&", "#", "&#", "&#x", "&#X", "x", "X", "0", "00", "1",
                     "9", "a", "F", "g", ";", "1234567", "12345678", "fffffff",
                     "ABCDEF12", "000000001", "amp", "lt"]
_WORD_TOKENS = ["H", "I", "G", "C", "R", "T", "A", "L", "M", "E", "D", "U",
                "l", "1", "|", "*", "_", "~", "`", "\\", "x", "HIGH", "MEDIUM",
                "CRITICAL", _FOLD_H, _NEW_FOLD_I]
_LINK_TOKENS = [" ", "\t", "\n", '"', "'", "(", ")", "\\", "<", ">", "[", "]",
                "!", "x", ":", "-", "1", ".", "*", "+", "  "]
_JOIN_TOKENS = ["a", "1", "_", "*", "~", "`", "\\", "!", "[", "]", "(", ")",
                "|", " ", _FOLD_H, _NEW_FOLD_I, "\N{LATIN SMALL LETTER E WITH ACUTE}"]
_COUNTS_TOKENS = ["##", "#", " ", "\t", "Counts", "counts", "CRITICAL", "high",
                  "Medium", "LOW", "INFO", ":", "0", "12", "1234567", "|", ",",
                  ";", "x", "\n"]

CORPUS_TOKENS = {
    "_RESOLVED_FINDING_HEADER_RE": _RESOLVED_TOKENS,
    "_HTML_BLOCK_TYPE7_RE": _HTML_TOKENS,
    "_OVERLONG_NUMERIC_REFERENCE_RE": _REFERENCE_TOKENS,
    "_UNTERMINATED_DECIMAL_REFERENCE_RE": _REFERENCE_TOKENS,
    "_UNTERMINATED_HEX_REFERENCE_RE": _REFERENCE_TOKENS,
    "_BACKSTOP_REFERENCE_RE": _REFERENCE_TOKENS,
    "_BACKSTOP_SPLIT_WORD_RE": _WORD_TOKENS,
    "_LINK_TAIL_START_RE": _LINK_TOKENS,
    "_LINK_POINTY_DESTINATION_RE": _LINK_TOKENS,
    "_LINK_TAIL_END_RE": _LINK_TOKENS,
    "_LINK_LABEL_RE": _LINK_TOKENS,
    "_LINK_CLOSE_RE": _LINK_TOKENS,
    "_LINK_ANY_CLOSE_RE": _LINK_TOKENS,
    "_LINK_DEFINITION_RE": _LINK_TOKENS + ["  >", "- ", "12.", "3)"],
    "_BACKSTOP_LINK_JOIN_RE": _JOIN_TOKENS,
    "_STRICT_COUNTS_HEADING_RE": _COUNTS_TOKENS,
    "_STRICT_COUNTS_LINE_RE": _COUNTS_TOKENS,
}

# Texts at the edges a give-back would cross (a run one longer than its
# bound, followed by what the original refused), which random token strings
# reach too rarely.
_REFERENCE_EDGES = [
    "&#123456789;", "&#12345678;", "&#1234567;", "&#12345678", "&#0012345678;",
    "&#00000000;", "&#000;", "&#;", "&#x;", "&#x123456789;", "&#x12345678;",
    "&#x1234567;", "&#X00ABCDEF1;", "&#x0000000;", "&#x10FFFF;", "&#1114111;",
    "&#1114112", "&#9999999999999;", "&#x0000000000001;", "&amp;&#38;&#x26",
]
CORPUS_EDGES = {
    "_RESOLVED_FINDING_HEADER_RE": [
        "### SR29-00 HIGH (fixed, verified, not counted) - x",
        "(x [fixed] y)", "[(not counted)]", "(note) (fixed)", "x \N{EM DASH} FIXED",
        ": RESOLVED; verified (ok)", "(((fixed)", "(fixed", "x (not counted) (y",
        "(a)(b [not  counted] c)", "] (RESOLVED)\n(fixed)"],
    "_HTML_BLOCK_TYPE7_RE": [
        '<a b="x" c>', "<a b='x'c>", "<a b=c d=e/>", "<a b = c >", "<a b=>",
        "</a >", "<script>", "<scripts>", '<a b="x>', "<a\tb=c\t/>  ",
        "<a b=c=d>", '<a b="x"c="y">', "<a b='x\ny'>", "<a-1 _:b.c-d=v>"],
    "_OVERLONG_NUMERIC_REFERENCE_RE": _REFERENCE_EDGES,
    "_UNTERMINATED_DECIMAL_REFERENCE_RE": _REFERENCE_EDGES,
    "_UNTERMINATED_HEX_REFERENCE_RE": _REFERENCE_EDGES,
    "_BACKSTOP_REFERENCE_RE": _REFERENCE_EDGES,
    "_BACKSTOP_SPLIT_WORD_RE": ["H*I_G~H", "C*R*I*T*I*C*A*L", "M_E_D_I_U_M**",
                                "H\\\\IGH", "l1|H", "HI**", "**HIGH"],
    "_LINK_TAIL_START_RE": ["(  \n  x", "(\n\n", "( \t"],
    "_LINK_POINTY_DESTINATION_RE": ["<a\\>b>", "<a<b>", "<a\nb>", "<\\\n>"],
    "_LINK_TAIL_END_RE": ['  "t" )', " <a> 'b' )", "\n  (t)\n)", ' "a\\"b" )',
                          ' "t"x)', " (t) )", "\n\n)", ' "t"\n  )'],
    "_LINK_LABEL_RE": ["[a\\]b]", "[a[b]", "[\\\n]", "[" + "x" * 1000 + "]"],
    "_LINK_CLOSE_RE": ["![x](y)", "![a\n\nb]", "![a![b]", "](", "][", "!["],
    "_LINK_ANY_CLOSE_RE": ["![x](y)", "![a\n\nb]", "![a![b]", "]", "!\\]]"],
    "_LINK_DEFINITION_RE": ["  > - [lbl]: x", "1. [a]:", "   >>[a]:",
                            "1234567890) [a]:", "[a\\]]:", "    [a]:"],
    "_BACKSTOP_LINK_JOIN_RE": ["a*_![x", "](a", ")!*[" + _FOLD_H, "|[", "]|"],
    "_STRICT_COUNTS_HEADING_RE": ["## Counts", "##  Counts  ", "##Counts",
                                  "## counts\t"],
    "_STRICT_COUNTS_LINE_RE": [
        "| CRITICAL: 0 | HIGH: 1 | MEDIUM: 2 | LOW: 3 | INFO: 4 |",
        "CRITICAL:0,HIGH:0;MEDIUM:0 LOW:0|INFO:0",
        "CRITICAL: 1234567 | HIGH: 0 | MEDIUM: 0 | LOW: 0 | INFO: 0",
        "critical: 1 | high: 2 | medium: 3 | low: 4 | info: 5 | x"],
}

_EXHAUSTIVE_LIMIT = 2_000
_RANDOM_TEXTS = 3_000
_RANDOM_MAX_TOKENS = 8


def corpus_for(name: str) -> list[str]:
    return corpus(CORPUS_TOKENS[name]) + CORPUS_EDGES[name]


def corpus(tokens: list[str]) -> list[str]:
    """Every concatenation of up to n tokens (n as large as fits in
    _EXHAUSTIVE_LIMIT texts), then _RANDOM_TEXTS longer ones from a fixed
    LCG: the same texts on every interpreter (no ``random`` algorithm)."""
    texts = [""]
    level = [""]
    while True:
        longer = [text + token for text in level for token in tokens]
        if len(texts) + len(longer) > _EXHAUSTIVE_LIMIT:
            break
        texts.extend(longer)
        level = longer
    state = 3169

    def draw(bound: int) -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % 2 ** 64
        return (state >> 33) % bound

    for _ in range(_RANDOM_TEXTS):
        count = 1 + draw(_RANDOM_MAX_TOKENS)
        texts.append("".join(tokens[draw(len(tokens))] for _ in range(count)))
    return texts


def _kept_groups(pattern: re.Pattern) -> list[int]:
    """The group numbers of the original's groups: every group but the ones
    loops._atomic adds (named _atomicN)."""
    added = {index for name, index in pattern.groupindex.items()
             if name.startswith("_atomic")}
    return [index for index in range(1, pattern.groups + 1)
            if index not in added]


def results(pattern: re.Pattern, text: str) -> tuple:
    """What every caller can observe: each search match (finditer, as sub
    and findall walk), the full match, and a match at every position (as
    the link reader's ``.match(view, position)``), with group texts."""
    groups = _kept_groups(pattern)

    def seen(match: re.Match | None):
        if match is None:
            return None
        return (match.start(), match.end(),
                tuple(match.group(index) for index in groups))

    return (tuple(seen(match) for match in pattern.finditer(text)),
            seen(pattern.fullmatch(text)),
            tuple(seen(pattern.match(text, position))
                  for position in range(len(text) + 1)))


def results_digest(pattern: re.Pattern, texts: list[str]) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(repr(results(pattern, text)).encode("utf-8"))
    return digest.hexdigest()


# SHA-256 of results(original, text) over corpus_for(name), the
# original compiled from ORIGINALS on Python 3.12. To refresh after a
# deliberate change of a pattern, compute it from that pattern's new
# reference form on 3.11+ (the direct comparison below names the texts).
EXPECTED_DIGESTS: dict[str, str] = {
    "_BACKSTOP_LINK_JOIN_RE":
        "51e915e7c71ed1e5d52b6be8cfdd75bdd075ad73cdf4d7b4fe7f0e50f43ce2cb",
    "_BACKSTOP_REFERENCE_RE":
        "79dc71326f1f55862ec7679752ff618497bde23d8a5a68e73dd85cd2f80c97c6",
    "_BACKSTOP_SPLIT_WORD_RE":
        "8148bdef769425959ee30b7fd89635f7592fcd5268ee4d4b6b1600e78e0a5dec",
    "_HTML_BLOCK_TYPE7_RE":
        "c278a8e26de1c8cc04d250714bdd5f5c45604f3d37c5426537db60da1dafd1bd",
    "_LINK_ANY_CLOSE_RE":
        "46ead4e9bc25f4a1f88896ba5d1b240f7ed724fd27a954df818320f2bbf8a110",
    "_LINK_CLOSE_RE":
        "be8444659743b06abe1bb6f75e37a0c7c7fe173da7ef10600cbe10d745e65a5b",
    "_LINK_DEFINITION_RE":
        "8a7851d985fd9c1d65d36376c0309b702ee17fa6e0df4c4230035c61d56b5734",
    "_LINK_LABEL_RE":
        "3e3e2733b7bff95c89562f21adc0657ffbfbe5a336ee4731c46000fc67fe6cdd",
    "_LINK_POINTY_DESTINATION_RE":
        "0369916201abc11016332b339b7131020436ac5a3dc7e88af41a86413544ff9d",
    "_LINK_TAIL_END_RE":
        "c5c37f9a0c773f8db4af23b11c076b8629756c4cd4e255e0fd6f1a7634e25ea2",
    "_LINK_TAIL_START_RE":
        "4ed2fa8d799a08b182a762a6e9bc7f821d64b7f281fddb1544d12fe9fe043074",
    "_OVERLONG_NUMERIC_REFERENCE_RE":
        "abb511a634a8690e14d1ff947cf3ef25c74d357bf595e35c0a32d2b61e287750",
    "_RESOLVED_FINDING_HEADER_RE":
        "d5f21f10515c36e61a76bed5f05833137a7500f8a6a4dcea0415eeb0b1961e9f",
    "_STRICT_COUNTS_HEADING_RE":
        "fcc43f66c76a95753a20453171ed60610ad82c934a660a09ad7992d78e4cd5ba",
    "_STRICT_COUNTS_LINE_RE":
        "5f696dea94b3518550610b13fd33f9f438f52b9e337b142215fbb8f0903cdabd",
    "_UNTERMINATED_DECIMAL_REFERENCE_RE":
        "eb781d18cd394cfd78b8ae9a378a4633994450418320a87a27a2aae6aa618cf1",
    "_UNTERMINATED_HEX_REFERENCE_RE":
        "10928f964e1af62a26a8509f9db58ca07c9f0209495161db2c496b07ff9074fb",
}


def test_every_original_has_a_corpus_and_a_digest():
    names = {name for (_module, name) in ORIGINALS}
    assert names == set(CORPUS_TOKENS) == set(CORPUS_EDGES) == set(EXPECTED_DIGESTS)
    assert len(names) == 17


@pytest.mark.parametrize("key", sorted(ORIGINALS), ids=lambda key: key[1])
def test_the_3_10_form_matches_exactly_as_the_original(key):
    module_name, name = key
    current = getattr(importlib.import_module(module_name), name)
    original_text, original_flags = ORIGINALS[key]
    assert current.flags & ~re.UNICODE == original_flags
    assert find_311_constructs(current.pattern) == []
    texts = corpus_for(name)
    assert results_digest(current, texts) == EXPECTED_DIGESTS[name]
    re.purge()
    if sys.version_info >= (3, 11):
        original = re.compile(original_text, original_flags)
        assert len(_kept_groups(current)) == original.groups
        differing = [text for text in texts
                     if results(current, text) != results(original, text)]
        assert differing == []
    else:
        # The reason for the rewrite: 3.10 cannot compile the original.
        with pytest.raises(re.error):
            re.compile(original_text, original_flags)


# --- each rewritten pattern runs in bounded memory, as the original did -------
#
# A possessive loop over a group kept no backtracking entries; a plain loop
# keeps one per pass (about 115 bytes in re), so a 3.2 MB link title took
# 366 MB where the original took none. The 3.10 forms read such loops as
# runs of one class or as loops._committed_loop batches. Each long run below
# is what a pattern's loop reads, given as (method, text); the peak memory
# traced while it runs is held to a few bytes per character of input.

MEMORY_RUN = 200_000
MEMORY_BYTES_PER_CHARACTER = 2

MEMORY_FLOODS: dict[str, list[tuple[str, str]]] = {
    "_LINK_TAIL_END_RE": [
        ("match", ' "' + "a" * MEMORY_RUN + '")'),
        ("match", ' "' + "\\a" * (MEMORY_RUN // 2) + '")'),
        ("match", " '" + "\\'" * (MEMORY_RUN // 2) + "')"),
        ("match", " (" + "\\)" * (MEMORY_RUN // 2) + "))"),
        ("match", ' "' + "\\a" * (MEMORY_RUN // 2) + ")"),
    ],
    "_LINK_POINTY_DESTINATION_RE": [
        ("match", "<" + "a" * MEMORY_RUN + ">"),
        ("match", "<" + "\\>" * (MEMORY_RUN // 2) + ">"),
        ("match", "<" + "\\>" * (MEMORY_RUN // 2)),
    ],
    "_LINK_CLOSE_RE": [
        ("search", "![" + "a" * MEMORY_RUN + "]"),
        ("search", "![" + "!" * MEMORY_RUN + "]"),
        ("search", "![" + "\\]" * (MEMORY_RUN // 2) + "]"),
        ("search", "![" + "a\n" * (MEMORY_RUN // 2) + "]"),
    ],
    "_LINK_ANY_CLOSE_RE": [
        ("search", "![" + "!a" * (MEMORY_RUN // 2)),
        ("search", "![" + "\\a" * (MEMORY_RUN // 2)),
    ],
    "_LINK_DEFINITION_RE": [
        ("search", ">" * MEMORY_RUN + "[a]:"),
        ("search", "> " * (MEMORY_RUN // 2) + "[a]:"),
        ("search", ">\t" * (MEMORY_RUN // 2) + " x"),
    ],
    "_HTML_BLOCK_TYPE7_RE": [
        ("fullmatch", "<span" + " a=b" * (MEMORY_RUN // 4) + ">"),
        ("fullmatch", "<span" + " a='b'" * (MEMORY_RUN // 6) + " x"),
        ("fullmatch", "<span" + " a" * (MEMORY_RUN // 2) + " ="),
        ("fullmatch", "<span" + " " * MEMORY_RUN + "x"),
    ],
    "_RESOLVED_FINDING_HEADER_RE": [
        ("search", "(" * MEMORY_RUN),
        ("search", "(fixed" + "x" * MEMORY_RUN + ")"),
        ("search", "[" + "x" * MEMORY_RUN + " not counted]"),
    ],
    "_STRICT_COUNTS_LINE_RE": [
        ("fullmatch", "CRITICAL: 0 |" + " " * MEMORY_RUN + "HIGH: 0"),
    ],
    "_STRICT_COUNTS_HEADING_RE": [
        ("fullmatch", "##" + " " * MEMORY_RUN + "Counts"),
    ],
    "_OVERLONG_NUMERIC_REFERENCE_RE": [
        ("finditer", "&#" + "0" * MEMORY_RUN + "123456789"),
    ],
    "_UNTERMINATED_DECIMAL_REFERENCE_RE": [
        ("finditer", "&#" + "0" * MEMORY_RUN + "1"),
    ],
    "_UNTERMINATED_HEX_REFERENCE_RE": [
        ("finditer", "&#x" + "0" * MEMORY_RUN + "f"),
    ],
    "_BACKSTOP_REFERENCE_RE": [
        ("finditer", "&#" + "0" * MEMORY_RUN),
        ("finditer", "&#x" + "f" * MEMORY_RUN),
    ],
    "_BACKSTOP_SPLIT_WORD_RE": [
        ("finditer", "H" + "*" * MEMORY_RUN + "IGH"),
    ],
    "_BACKSTOP_LINK_JOIN_RE": [
        ("finditer", "a" + "*" * MEMORY_RUN + "["),
        ("finditer", "]" + "!" * MEMORY_RUN + "a"),
    ],
    "_LINK_TAIL_START_RE": [
        ("match", "(" + " " * MEMORY_RUN + "\n" + "\t" * MEMORY_RUN),
    ],
    "_LINK_LABEL_RE": [
        ("finditer", "[" + "\\a" * 499 + "]" + "[" * MEMORY_RUN),
    ],
}


def _peak_traced_bytes(pattern: re.Pattern, method: str, text: str) -> int:
    """Peak bytes traced above the start while one call runs; the text is
    built before, and finditer is drained without keeping a match."""
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        if method == "finditer":
            for _match in pattern.finditer(text):
                pass
        else:
            getattr(pattern, method)(text)
        return tracemalloc.get_traced_memory()[1] - before
    finally:
        if started:
            tracemalloc.stop()


def test_every_rewritten_pattern_has_memory_floods():
    assert set(MEMORY_FLOODS) == {name for (_module, name) in ORIGINALS}


@pytest.mark.parametrize("name", sorted(MEMORY_FLOODS))
def test_the_3_10_form_runs_in_bounded_memory(name):
    pattern = getattr(loops, name)
    for method, text in MEMORY_FLOODS[name]:
        re.purge()
        peak = _peak_traced_bytes(pattern, method, text)
        assert peak <= MEMORY_BYTES_PER_CHARACTER * len(text), (
            f"{name}.{method} on {text[:12]!r}... ({len(text)} characters): "
            f"{peak} bytes")


def test_a_plain_loop_over_a_group_exceeds_the_memory_bound():
    """Positive control: the measure sees re's backtracking entries. The
    pre-3169 link title as a plain loop (the first 3.10 form) takes far more
    than the bound on the title flood the committed form passes."""
    plain_loop = re.compile(r"\"(?:\\[\s\S]|[^\"\\])*\"")
    for text in ('"' + "a" * MEMORY_RUN + '"',
                 '"' + "\\a" * (MEMORY_RUN // 2) + '"'):
        peak = _peak_traced_bytes(plain_loop, "match", text)
        assert peak > 20 * MEMORY_BYTES_PER_CHARACTER * len(text), peak
    committed = re.compile(r"\"" + loops._link_escaped_run(r"[^\"\\]") + r"\"")
    text = '"' + "\\a" * (MEMORY_RUN // 2) + '"'
    assert committed.match(text).end() == len(text)
    assert (_peak_traced_bytes(committed, "match", text)
            <= MEMORY_BYTES_PER_CHARACTER * len(text))
