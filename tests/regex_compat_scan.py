"""Find regex syntax that Python 3.10 cannot compile.

Python 3.11 added possessive quantifiers (``*+``, ``++``, ``?+``, ``{m,n}+``)
and atomic groups (``(?>...)``). On 3.10 ``re.compile`` rejects them with
``re.error: multiple repeat`` (possessive) or ``unknown extension ?>``
(atomic), so a single such pattern at module level makes the whole module
unimportable there. EQUIPA supports 3.10, so equipa/ must not use either.

The scanner reads regex syntax itself instead of asking ``re``: on 3.10 the
parser cannot describe a construct it does not know, and on 3.11+ it would
accept it. It walks escapes, character classes, group openers and
quantifiers, so ``[-*+]`` (a class holding ``*`` and ``+``) or ``\\*+``
(a repeated literal star) are not reported.

``string_constants`` yields every string literal in a Python source file
(implicitly concatenated literals arrive joined, as the compiler joins them,
and f-string pieces arrive one by one). Regex fragments are assembled from
such literals all over equipa/, so the guard scans every literal rather than
only arguments of ``re`` calls.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator, NamedTuple

EQUIPA_DIR = Path(__file__).resolve().parent.parent / "equipa"

_BRACE_QUANTIFIER_RE = re.compile(r"\{[0-9]*(?:,[0-9]*)?\}")


class Construct(NamedTuple):
    """One 3.11-only construct: its offset in the pattern and its kind."""

    offset: int
    kind: str  # "possessive" or "atomic"


def _skip_class(pattern: str, start: int) -> int:
    """Return the index just past the character class opening at ``start``.

    A ``]`` right after ``[`` or ``[^`` is a literal member, as in ``re``.
    An unterminated class consumes the rest of the pattern.
    """
    index = start + 1
    if index < len(pattern) and pattern[index] == "^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "]":
            return index + 1
        index += 1
    return len(pattern)


def find_311_constructs(pattern: str) -> list[Construct]:
    """Return every possessive quantifier and atomic group in ``pattern``."""
    found: list[Construct] = []
    # True right after a quantifier that may still take a ``?`` or ``+``
    # suffix; a suffix ends the quantifier.
    after_quantifier = False
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            after_quantifier = False
            continue
        if char == "[":
            index = _skip_class(pattern, index)
            after_quantifier = False
            continue
        if char == "(":
            after_quantifier = False
            if pattern.startswith("(?#", index):
                close = pattern.find(")", index)
                index = length if close < 0 else close + 1
                continue
            if pattern.startswith("(?>", index):
                found.append(Construct(index, "atomic"))
                index += 3
                continue
            # Skip the ``?`` of an extension so it is not read as a
            # quantifier; the extension letters that follow are not
            # quantifiers either.
            index += 2 if pattern.startswith("(?", index) else 1
            continue
        if char in "*+?":
            if after_quantifier:
                if char == "+":
                    found.append(Construct(index, "possessive"))
                after_quantifier = False
            else:
                after_quantifier = True
            index += 1
            continue
        if char == "{":
            brace = _BRACE_QUANTIFIER_RE.match(pattern, index)
            if brace is not None:
                after_quantifier = True
                index = brace.end()
                continue
        after_quantifier = False
        index += 1
    return found


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """``id`` of every module, class and function docstring node: prose,
    which may quote the 3.11 syntax it explains."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            docstrings.add(id(node.body[0].value))
    return docstrings


def string_constants(source: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line, text)`` for every str/bytes literal in ``source``
    other than a docstring."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in docstrings:
            continue
        if isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node.value, bytes):
            yield node.lineno, node.value.decode("latin-1")


def scan_source_tree(root: Path = EQUIPA_DIR) -> list[str]:
    """Return ``path:line: kind at offset N in '...'`` for every finding."""
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line, text in string_constants(source):
            for construct in find_311_constructs(text):
                excerpt = text[max(0, construct.offset - 20):construct.offset + 20]
                findings.append(
                    f"{path.relative_to(root.parent)}:{line}: {construct.kind} "
                    f"at offset {construct.offset} in {excerpt!r}"
                )
    return findings
