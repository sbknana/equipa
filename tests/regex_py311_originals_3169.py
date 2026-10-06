"""The module-level regexes of equipa/ that used 3.11-only syntax
(possessive quantifiers, atomic groups) before task 3169, exactly as
they were compiled (pattern text and flags, generated from commit
04e9c39 on Python 3.12). Python 3.10 cannot compile them;
tests/test_regex_py310_compat_3169.py compares each 3.10 form with
its original. Data only: written by a script, kept ASCII.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

# (module, attribute) -> (original pattern, flags without re.UNICODE)
ORIGINALS: dict[tuple[str, str], tuple[str, int]] = {
    ('equipa.loops', '_BACKSTOP_LINK_JOIN_RE'): (
        '(?:[^\\W_]|[|\ue041-\ue07a\ue141-\ue17a])[*_~`\\\\]*+!?\\[|[\\])][*_~`\\\\!\\[]*+(?:[^\\W_]|[|\ue041-\ue07a\ue141-\ue17a])',
        0),
    ('equipa.loops', '_BACKSTOP_REFERENCE_RE'): (
        '&(?:#(0*)([0-9]++);?|#[xX](0*)([0-9A-Fa-f]++);?|[A-Za-z][A-Za-z0-9]{0,31};?)',
        0),
    ('equipa.loops', '_BACKSTOP_SPLIT_WORD_RE'): (
        '[C\ue043\ue143][*_~`\\\\]*+[R\ue052\ue152][*_~`\\\\]*+[Il1\\|\ue049\ue06c\ue149\ue16c][*_~`\\\\]*+[T\ue054\ue154][*_~`\\\\]*+[Il1\\|\ue049\ue06c\ue149\ue16c][*_~`\\\\]*+[C\ue043\ue143][*_~`\\\\]*+[A\ue041\ue141][*_~`\\\\]*+[L\ue04c\ue14c]|[H\ue048\ue148][*_~`\\\\]*+[Il1\\|\ue049\ue06c\ue149\ue16c][*_~`\\\\]*+[G\ue047\ue147][*_~`\\\\]*+[H\ue048\ue148]|[M\ue04d\ue14d][*_~`\\\\]*+[E\ue045\ue145][*_~`\\\\]*+[D\ue044\ue144][*_~`\\\\]*+[Il1\\|\ue049\ue06c\ue149\ue16c][*_~`\\\\]*+[U\ue055\ue155][*_~`\\\\]*+[M\ue04d\ue14d]',
        0),
    ('equipa.loops', '_HTML_BLOCK_TYPE7_RE'): (
        '<(?!(?:script|style|pre|textarea)(?![A-Za-z0-9-]))(?:[A-Za-z][A-Za-z0-9-]*+(?:[ \\t]++[A-Za-z_:][A-Za-z0-9_.:-]*+(?:[ \\t]*+=[ \\t]*+(?:[^ \\t\\"\'=<>`]++|\'[^\'\\n]*+\'|\\"[^\\"\\n]*+\\"))?+)*+[ \\t]*+/?|/[A-Za-z][A-Za-z0-9-]*+[ \\t]*+)>[ \\t]*',
        2),
    ('equipa.loops', '_LINK_ANY_CLOSE_RE'): (
        '!\\[(?:\\\\[^\\n]|[^\\]!\\n\\\\]|!(?!\\[)|\\n(?![ \\t]*+\\n))*+\\]|\\]',
        0),
    ('equipa.loops', '_LINK_CLOSE_RE'): (
        '!\\[(?:\\\\[^\\n]|[^\\]!\\n\\\\]|!(?!\\[)|\\n(?![ \\t]*+\\n))*+\\]|\\](?=[(\\[])',
        0),
    ('equipa.loops', '_LINK_DEFINITION_RE'): (
        '^[ \\t]{0,3}(?:>[ \\t]?)*+(?:(?:[-*+]|\\d{1,9}[.)])[ \\t]+)?+\\[((?:\\\\[^\\n]|[^\\[\\]\\\\]){1,999}+)\\]:',
        8),
    ('equipa.loops', '_LINK_LABEL_RE'): (
        '\\[(?:\\\\[^\\n]|[^\\[\\]\\\\]){0,999}+\\]',
        0),
    ('equipa.loops', '_LINK_POINTY_DESTINATION_RE'): (
        '<(?:\\\\[^\\n]|[^<>\\n\\\\])*+>',
        0),
    ('equipa.loops', '_LINK_TAIL_END_RE'): (
        '(?:(?:[ \\t]++(?:\\n[ \\t]*+)?+|\\n[ \\t]*+)(?:\\"(?:\\\\[\\s\\S]|[^\\"\\\\])*+\\"|\'(?:\\\\[\\s\\S]|[^\'\\\\])*+\'|\\((?:\\\\[\\s\\S]|[^()\\\\])*+\\)))?+[ \\t]*+(?:\\n[ \\t]*+)?+\\)',
        0),
    ('equipa.loops', '_LINK_TAIL_START_RE'): (
        '\\([ \\t]*+(?:\\n[ \\t]*+)?+',
        0),
    ('equipa.loops', '_OVERLONG_NUMERIC_REFERENCE_RE'): (
        '&#(?:0*+[0-9]{8,}+|[xX]0*+[0-9A-Fa-f]{7,}+)(?!;)',
        0),
    ('equipa.loops', '_RESOLVED_FINDING_HEADER_RE'): (
        '(?:(?<![^)\\]\\n])(?>[^)\\]\\n]*?[(\\[][ \\t]*(?i:fixed|resolved)\\b)[^)\\]\\n]*+[)\\]]|(?<![^)\\]\\n])(?>[^()\\[\\]\\n]*+[(\\[][^)\\]\\n]*?\\b(?i:not[ \\t]+counted)\\b)[^)\\]\\n]*+[)\\]]|[\u2014\u2013:\u2192-][ \\t]*[*_]{0,2}(?:FIXED|RESOLVED)\\b[*_]{0,2}(?:[ \\t]*[,;][ \\t]*[A-Za-z][A-Za-z \\t,;-]{0,40})?(?:[ \\t]*\\([^()\\n]{0,60}\\))?)[ \\t*_.\\r]*$',
        0),
    ('equipa.loops', '_STRICT_COUNTS_HEADING_RE'): (
        '##[ \\t]++Counts[ \\t]*+',
        2),
    ('equipa.loops', '_STRICT_COUNTS_LINE_RE'): (
        '[ \\t]*+(?:\\|[ \\t]*+)?+(?i:CRITICAL)[ \\t]*+:[ \\t]*+[0-9]{1,6}+(?![0-9])[ \\t]*+(?:[|,;][ \\t]*+)?+(?i:HIGH)[ \\t]*+:[ \\t]*+[0-9]{1,6}+(?![0-9])[ \\t]*+(?:[|,;][ \\t]*+)?+(?i:MEDIUM)[ \\t]*+:[ \\t]*+[0-9]{1,6}+(?![0-9])[ \\t]*+(?:[|,;][ \\t]*+)?+(?i:LOW)[ \\t]*+:[ \\t]*+[0-9]{1,6}+(?![0-9])[ \\t]*+(?:[|,;][ \\t]*+)?+(?i:INFO)[ \\t]*+:[ \\t]*+[0-9]{1,6}+(?![0-9])[ \\t]*+(?:\\|[ \\t]*+)?+',
        0),
    ('equipa.loops', '_UNTERMINATED_DECIMAL_REFERENCE_RE'): (
        '&#0*+([0-9]{1,7}+)(?![0-9;])',
        0),
    ('equipa.loops', '_UNTERMINATED_HEX_REFERENCE_RE'): (
        '&#([xX])0*+([0-9A-Fa-f]{1,6}+)(?![0-9A-Fa-f;])',
        0),
}
