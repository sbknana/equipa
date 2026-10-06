"""Task 3170: polish of the independent review of task 3167.

* IR67-03: a finding heading followed by 200 KB of " - FIXED" or of an
  em-dash FIXED run took 0.42 and 0.44 s through the merge gate. Both are in
  the production-timed families now (tests/test_review_gate_backstop_3143.py),
  and two shared costs are lower:

  - ``_ends_in_resolved_status`` answers what
    ``_RESOLVED_FINDING_HEADER_RE.search`` answers (compared here on every
    short token string) without trying the regex at every character;
  - ``_translated`` replaces the few characters a fold table changes, one
    str.replace pass each, instead of reading every character through the
    table (the lookalike-eta, combining-mark and em-dash families).

* R3167-03: every module-level regex of ``loops.py`` is timed at two sizes
  on a wider set of units than bracket runs, and a growth ratio above
  linear fails, as well as the absolute budget.

The source stays ASCII: special characters are named.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import itertools
import math
import random
import statistics

import pytest

from equipa import loops
from tests import test_review_gate_linear_3167 as linear_3167
from tests.host_timing import (
    GROWTH,
    GROWTH_FLOOR_SECONDS,
    GROWTH_LIMIT,
    HOST_FACTOR_ENVIRONMENT_VARIABLE,
    PER_UNIT_GROWTH_LIMIT,
    PER_UNIT_OVER_RETRIES,
    SETTLE_RETRIES,
    TimingCheckFailed,
    assert_linear_per_unit,
    assert_linear_time,
    budget,
    collector_paused,
    host_calibration,
)
from tests.review_gate_production import blocked_by_the_gate
from tests.review_gate_timing import median_cpu_seconds, timing_test
from tests.test_review_gate_backstop_3143 import (
    BACKSTOP_FAMILIES,
    ZERO,
    backstop_families,
    review,
)
from tests.test_review_gate_linear_3167 import (
    SCAN_RUNS,
    _best_scan_seconds,
    _called_anchored_only,
    _module_patterns,
    _scan_seconds,
    scan_growth,
    superlinear_scan,
)

EM_DASH = "\N{EM DASH}"
EN_DASH = "\N{EN DASH}"
ARROW = "\N{RIGHTWARDS ARROW}"
RB = 200 * 1024
RESOLVED_RE = loops._RESOLVED_FINDING_HEADER_RE
DASH_FIXED_FAMILIES = ("heading_then_dash_fixed_runs",
                       "heading_then_em_dash_fixed_runs")

# Every piece the three alternatives and the status end read, a line feed
# (which "$" allows last) and LONG S, which re reads as "s" in the
# case-insensitive "resolved".
TOKENS = ["(", "[", ")", "]", " ", "\t", "x", "\n", "\r", ".", "*", "_",
          ",", ";", "-", ":", EM_DASH, EN_DASH, ARROW, "not counted",
          "fixed", "Re\N{LATIN SMALL LETTER LONG S}olved", "FIXED",
          "RESOLVED"]


# --- IR67-03: the resolved status in one pass ----------------------------------

def _mismatches(texts):
    return [text for text in texts
            if loops._ends_in_resolved_status(text)
            != bool(RESOLVED_RE.search(text))]


def test_the_split_status_check_is_built_from_the_regex_pieces():
    """The combined regex is the two halves the check reads, so they
    cannot drift apart."""
    assert RESOLVED_RE.pattern == (
        "(?:" + loops._RESOLVED_BRACKET_STATUS + "|"
        + loops._RESOLVED_DASH_STATUS + ")" + loops._RESOLVED_STATUS_END)
    assert loops._RESOLVED_BRACKET_STATUS_RE.pattern == (
        "(?:" + loops._RESOLVED_BRACKET_STATUS + ")"
        + loops._RESOLVED_STATUS_END)
    assert loops._RESOLVED_DASH_STATUS_RE.pattern == (
        loops._RESOLVED_DASH_STATUS + loops._RESOLVED_STATUS_END)


def test_every_short_token_string_resolves_as_the_regex_says():
    texts = ("".join(combination) for length in range(5)
             for combination in itertools.product(TOKENS, repeat=length))
    assert _mismatches(texts) == []


def test_random_long_token_strings_resolve_as_the_regex_says():
    rng = random.Random(3170)
    texts = ["".join(rng.choice(TOKENS) for _ in range(rng.randint(5, 40)))
             for _ in range(40_000)]
    assert _mismatches(texts) == []


@pytest.mark.parametrize("line, resolved", [
    ("### [S1] HIGH (fixed)", True),
    ("### [S1] HIGH (fixed)\n", True),
    ("### [S1] HIGH (fixed)\n\n", False),
    ("### [S1] HIGH (fixed) *. \r", True),
    ("### [S1] HIGH (fixed) (open)", False),
    ("### [S1] HIGH [see (x)] (not counted)", True),
    ("### [S1] HIGH (see [S2] and (S3), not counted)", False),
    ("### [S1] HIGH " + EM_DASH + " FIXED, verified (see ] x)", True),
    ("### [S1] HIGH - FIXED - FIXED", True),
    ("### [S1] HIGH - FIXED x", False),
    (")", False),
    ("", False),
])
def test_resolved_status_edge_cases(line, resolved):
    assert bool(RESOLVED_RE.search(line)) is resolved
    assert loops._ends_in_resolved_status(line) is resolved


@timing_test
@pytest.mark.parametrize("unit", [" - FIXED", f" {EM_DASH} FIXED", "(",
                                  "a)", "sql injection "])
def test_a_200kb_heading_status_is_read_in_one_pass(unit):
    """The combined regex took 0.03-0.04 s per 200 KB heading on Python
    3.10, twice per review; the split check 0.002-0.011 s. Budget
    host-calibrated, growth from 50 KB to 200 KB linear (task 3171)."""
    def seconds_at(size):
        line = "### [S1] HIGH " + unit * (size // len(unit.encode()))
        return median_cpu_seconds(loops._ends_in_resolved_status, line)

    assert_linear_time(seconds_at, RB, 0.025, repr(unit))


@pytest.mark.parametrize("name", DASH_FIXED_FAMILIES)
def test_the_dash_fixed_families_block_through_the_gate(name):
    """A HIGH heading whose status reads resolved still counts (S3033-01),
    so the gate blocks; the 200 KB timing is in the 3143 families."""
    assert name in BACKSTOP_FAMILIES
    analysis = blocked_by_the_gate(
        review("No findings.", BACKSTOP_FAMILIES[name], ZERO))
    assert analysis.trusted, analysis
    assert analysis.counts["HIGH"] == 1, analysis.counts


# --- R3167-04: the few characters a fold changes --------------------------------

class FillingTable(dict):
    """A table that fills itself per key, as the backstop's tables do."""

    def __init__(self, changes):
        super().__init__()
        self.changes = changes

    def __missing__(self, code_point):
        value = self.changes.get(code_point, chr(code_point))
        self[code_point] = value
        return value


ETA = 0x0397
KAPPA = 0x039A
TABLES = {
    "one-letter": lambda: {ETA: "H"},
    "deletion": lambda: {0x0301: None, 0x200B: ""},
    "code-point-value": lambda: {ETA: 0x48, 0x00CD: 0x49},
    "longer-value": lambda: {0x2026: "...", ETA: "H"},
    # Kappa is replaced, and is what eta becomes: the passes would feed one
    # another, so the whole text is translated.
    "chained": lambda: {ETA: chr(KAPPA), KAPPA: "K"},
    "many": lambda: {code_point: chr(code_point - 0x0391 + 0x41)
                     for code_point in range(0x0391, 0x03A9)},
    "filling": lambda: FillingTable({ETA: "H", 0x0301: ""}),
    "unchanged": dict,
}
POOL = (list("HIGH high\n\t|-") + [chr(code_point) for code_point in (
    ETA, KAPPA, 0x0391, 0x03A3, 0x0301, 0x200B, 0x00CD, 0x2026, 0x4E00,
    0xFF28, 0x3164)])


def _random_texts(count, seed):
    generator = random.Random(seed)
    return ["".join(generator.choice(POOL)
                    for _ in range(generator.randrange(0, 80)))
            for _ in range(count)]


@pytest.mark.parametrize("table", sorted(TABLES))
def test_the_replaced_characters_give_what_translate_gives(table):
    for text in _random_texts(2000, seed=3170):
        assert loops._translated(text, TABLES[table]()) == text.translate(
            TABLES[table]()), text.encode("unicode_escape")


def test_a_text_the_table_leaves_alone_is_returned_as_it_is():
    text = "\N{CJK UNIFIED IDEOGRAPH-4E00} HIGH" * 10
    assert loops._translated(text, {ETA: "H"}) is text


def test_the_chained_table_is_not_read_as_two_passes():
    """Replaced one after the other, eta would become kappa, then K."""
    text = chr(ETA) + chr(KAPPA)
    assert loops._translated(text, {ETA: chr(KAPPA), KAPPA: "K"}) == (
        chr(KAPPA) + "K")


@timing_test
@pytest.mark.parametrize("name", ["eta_led", "mark_after_each_word"])
def test_a_200kb_fold_of_one_character_reads_the_text_once(name):
    """str.translate on 200 KB of a dense eta or mark family took about
    0.02 s per call (several calls per review); one str.replace pass is a
    fraction of that. Budget host-calibrated, growth from 50 KB to 200 KB
    linear (task 3171)."""
    table = loops._BackstopCharacterTable()

    def seconds_at(rb):
        text = review("No findings.", backstop_families(rb)[name], ZERO)
        return median_cpu_seconds(loops._translated, text, table)

    assert_linear_time(seconds_at, RB, 0.012, name)
    text = review("No findings.", BACKSTOP_FAMILIES[name], ZERO)
    assert loops._translated(text, table) == text.translate(table)


# --- R3167-03: every loops.py regex, growth ratio and budget --------------------

GROWTH_PREFIXES = ["", "### [S1] HIGH ", "- **[S1] HIGH** "]
GROWTH_UNITS = [
    "(", "[", "([", "(not counted ", "(fixed ", "[x", EM_DASH + "FIXED ",
    "( ", " ", "\t", "-", "*", "_", "`", "|", "<", "&#", "](", "<!--",
    "HIGH ", "high ", "a", ":", "> ", "- ", "\\", "\n", "\r", "a\n",
    "\N{HANGUL FILLER}", "\N{GREEK CAPITAL LETTER ETA}",
    "a\N{COMBINING ACUTE ACCENT}", "\N{NO-BREAK SPACE}",
]
SMALL_BYTES = 8 * 1024
# A pair whose scan at 32 KB takes less than this is fast whatever its
# growth, and is not measured further.
SCREEN_FLOOR_SECONDS = 0.004
# Scaled by the host factor (tests/host_timing.py, task 3171); GROWTH and
# GROWTH_LIMIT are the shared ones.
LARGE_BUDGET_SECONDS = 0.05
# Quadratic on a digit run, and only ever reads `git diff --shortstat`.
KNOWN_SUPERLINEAR = {"_SHORTSTAT_RE"}


def test_the_growth_scan_sees_every_gate_regex():
    names = set(_module_patterns())
    assert {"_RESOLVED_FINDING_HEADER_RE", "_RESOLVED_DASH_STATUS_RE",
            "_RESOLVED_BRACKET_STATUS_RE", "_FINDING_CANDIDATE_RE",
            "_SEVERITY_CLAUSE_RE"} <= names
    assert KNOWN_SUPERLINEAR <= names


@timing_test
def test_no_loops_regex_grows_faster_than_linear():
    """Timed the way loops.py calls each regex (see
    tests/test_review_gate_linear_3167.py) on every (prefix, unit) pair.
    A pair that takes measurable time at 32 KB is held there to its budget
    (best of three) and must grow linearly from 32 KB on, decided on scans
    of at least GROWTH_FLOOR_SECONDS (``superlinear_scan``). CI flagged
    _INLINE_CODE_RE at 0.0006 s against 0.0052 s: a sub-millisecond ratio
    of a linear regex (task 3175)."""
    slow = []
    for name, pattern in sorted(_module_patterns().items()):
        if name in KNOWN_SUPERLINEAR:
            continue
        anchored = _called_anchored_only(name)
        for prefix, unit in itertools.product(GROWTH_PREFIXES, GROWTH_UNITS):
            count = SMALL_BYTES // len(unit.encode()) * GROWTH
            large = prefix + unit * count
            if _scan_seconds(pattern, anchored, large) < SCREEN_FLOOR_SECONDS:
                continue
            large_seconds = _best_scan_seconds(pattern, anchored, large)
            if large_seconds > budget(LARGE_BUDGET_SECONDS):
                slow.append((name, prefix, unit, "budget",
                             round(large_seconds, 4)))
                continue
            growth = superlinear_scan(pattern, anchored, prefix, unit, count,
                                      large_seconds)
            if growth is not None:
                slow.append((name, prefix, unit, growth))
    assert slow == []


CONTROL_SMALL_BYTES = 1024


def test_the_growth_scan_flags_a_quadratic_regex():
    """The scan's own control: the known quadratic regex on a digit run
    (from 1 KB, doubled until a scan takes the floor; 32 KB takes
    seconds)."""
    pattern = loops._SHORTSTAT_RE
    anchored = _called_anchored_only("_SHORTSTAT_RE")
    growth = scan_growth(pattern, anchored, "", "1", CONTROL_SMALL_BYTES)
    assert growth.quarter_seconds >= GROWTH_FLOOR_SECONDS
    assert growth.ratio > GROWTH_LIMIT
    seconds = _best_scan_seconds(pattern, anchored, "1" * CONTROL_SMALL_BYTES)
    flagged = superlinear_scan(pattern, anchored, "", "1",
                               CONTROL_SMALL_BYTES, seconds)
    assert flagged is not None and flagged.ratio > GROWTH_LIMIT


CI_PREFIX = "- **[S1] HIGH** "


def test_the_growth_scan_never_compares_sub_millisecond_scans():
    """The CI shape: one scan of the quarter size takes under a millisecond,
    so the size doubles until it takes the floor, and the linear regex reads
    about 4x."""
    pattern = loops._INLINE_CODE_RE
    anchored = _called_anchored_only("_INLINE_CODE_RE")
    growth = scan_growth(pattern, anchored, CI_PREFIX, "`", SMALL_BYTES)
    assert growth.quarter_count > SMALL_BYTES
    assert growth.quarter_seconds >= GROWTH_FLOOR_SECONDS / 2
    assert growth.ratio < GROWTH_LIMIT


def test_a_noisy_first_look_is_not_the_verdict():
    """CI's reading (0.0006 s at the smaller size, 8.7x) only sends the pair
    to ``scan_growth``, which reads the linear regex as linear."""
    pattern = loops._INLINE_CODE_RE
    anchored = _called_anchored_only("_INLINE_CODE_RE")
    assert superlinear_scan(pattern, anchored, CI_PREFIX, "`",
                            SMALL_BYTES * GROWTH, 0.0006) is None


# A linear scan whose cost per character steps up past a size (its state
# outgrows the memory the allocator keeps), and whose first scan of each
# text is a noisy reading at the floor: the shape that flagged
# _BLANK_LINE_RUN_RE under contention (task 3175).
STEP_CHARACTERS = 300_000
BELOW_STEP_SECONDS_PER_CHARACTER = 5e-8
ABOVE_STEP_SECONDS_PER_CHARACTER = 1.6e-7
STEP_QUARTER_COUNT = 256 * 1024


def test_the_quarter_size_is_chosen_on_a_median_not_one_noisy_scan(
        monkeypatch):
    """One scan of the quarter size reaching the floor must not fix the
    sizes: the median of that size is still under it, so the size doubles
    past the step and both sizes cost the same per character. Choosing on
    the noisy scan compares 256K (under the step) with 1M (over it): 8.4x."""
    scanned_lengths = set()

    def stepped_scan_seconds(pattern, anchored, text):
        per_character = (BELOW_STEP_SECONDS_PER_CHARACTER
                         if len(text) < STEP_CHARACTERS
                         else ABOVE_STEP_SECONDS_PER_CHARACTER)
        seconds = len(text) * per_character
        if len(text) not in scanned_lengths:
            scanned_lengths.add(len(text))
            seconds = max(seconds, GROWTH_FLOOR_SECONDS)
        return seconds

    monkeypatch.setattr(linear_3167, "_scan_seconds", stepped_scan_seconds)
    growth = scan_growth(None, False, "", "\n", STEP_QUARTER_COUNT)
    assert growth.quarter_count == 2 * STEP_QUARTER_COUNT
    assert growth.quarter_seconds >= GROWTH_FLOOR_SECONDS
    assert growth.ratio == pytest.approx(GROWTH)


@timing_test
def test_a_regex_whose_cost_steps_with_its_memory_reads_linear():
    """_BLANK_LINE_RUN_RE on a newline run, the pair contention flagged:
    linear on each side of its memory step, so the growth scan reads it
    under the limit, on a quarter size whose median takes the floor."""
    pattern = loops._BLANK_LINE_RUN_RE
    anchored = _called_anchored_only("_BLANK_LINE_RUN_RE")
    growth = scan_growth(pattern, anchored, "", "\n", SMALL_BYTES * GROWTH)
    assert growth.quarter_seconds >= GROWTH_FLOOR_SECONDS / 2, growth
    assert growth.ratio < GROWTH_LIMIT, growth


# The CI shape at 50 KB, 200 KB and 2 MB (task 3175). One run scans a size
# as many times as covers CI_SHAPE_RUN_BYTES, so every compared time is a
# run of about 0.14 s, never one sub-millisecond scan.
CI_SHAPE_KILOBYTES = (50, 200, 2048)
CI_SHAPE_RUN_BYTES = 1024 * 1024
# Measured on the development host: 0.137 s per MB on Python 3.12, 0.128 s
# on 3.10, at every size from 50 KB to 8 MB.
CI_SHAPE_BUDGET_SECONDS_PER_MEGABYTE = 0.5
# Linear work costs the same per megabyte at every size; the shared growth
# check allows GROWTH_LIMIT for GROWTH times the input, so 2x per unit
# (``assert_linear_per_unit`` holds every size to it).
CI_SHAPE_PER_MEGABYTE_LIMIT = GROWTH_LIMIT / GROWTH


def _ci_shape_seconds_per_megabyte(pattern, anchored, text):
    """The median CPU time of SCAN_RUNS runs, each scanning ``text`` as many
    times as covers CI_SHAPE_RUN_BYTES, per megabyte scanned. A first scan
    over the budget is returned at once: a quadratic regex must fail on it,
    not be scanned again for minutes."""
    megabytes = len(text) / (1024 * 1024)
    with collector_paused():
        first = _scan_seconds(pattern, anchored, text)
    if first / megabytes >= budget(CI_SHAPE_BUDGET_SECONDS_PER_MEGABYTE):
        return first / megabytes
    scans = math.ceil(CI_SHAPE_RUN_BYTES / len(text))
    runs = []
    for _ in range(SCAN_RUNS):
        with collector_paused():
            runs.append(sum(_scan_seconds(pattern, anchored, text)
                            for _ in range(scans)))
    return statistics.median(runs) / (scans * megabytes)


def _ci_shape_per_megabyte_at(pattern, anchored):
    """``_ci_shape_seconds_per_megabyte`` of the CI shape at a size in KB."""
    def seconds_per_megabyte(kilobytes):
        text = CI_PREFIX + "`" * (kilobytes * 1024)
        return _ci_shape_seconds_per_megabyte(pattern, anchored, text)
    return seconds_per_megabyte


@timing_test
def test_the_ci_shape_is_linear_from_50kb_to_2mb():
    """_INLINE_CODE_RE on the shape CI flagged costs the same per megabyte
    at 50 KB, 200 KB and 2 MB (a quadratic regex costs 4x and 41x as much
    per megabyte at the larger sizes), within a host-calibrated budget.
    Sizes run smallest first, each checked before the next is scanned.
    Each pair (50 KB and a larger size) is settled under contention by the
    shared helper: CI read 0.067 s per MB at 50 KB and 0.137 s at 200 KB
    (2.04x) on Python 3.10 when only the larger size was measured again
    (task 3185)."""
    pattern = loops._INLINE_CODE_RE
    anchored = _called_anchored_only("_INLINE_CODE_RE")
    assert_linear_per_unit(
        _ci_shape_per_megabyte_at(pattern, anchored), CI_SHAPE_KILOBYTES,
        CI_SHAPE_BUDGET_SECONDS_PER_MEGABYTE, "_INLINE_CODE_RE on the CI shape")


def test_the_ci_shape_is_held_to_the_same_per_megabyte_limit():
    """The shared per-unit check holds the CI shape to the 2x per megabyte
    the hand-written check held it to: not weaker."""
    assert PER_UNIT_GROWTH_LIMIT == CI_SHAPE_PER_MEGABYTE_LIMIT == 2.0


class ScriptedPerUnit:
    """``per_unit_at`` returning scripted readings per size, in order,
    then the last one again; records the sizes in the order read."""

    def __init__(self, readings):
        self.readings = {size: list(values) for size, values in readings.items()}
        self.calls = []

    def __call__(self, size):
        self.calls.append(size)
        values = self.readings[size]
        return values.pop(0) if len(values) > 1 else values[0]


@pytest.fixture
def unloaded(monkeypatch):
    """A host that never needs its budget loosened: every reading here is
    under its base budget, so the reference never decides."""
    monkeypatch.setenv(HOST_FACTOR_ENVIRONMENT_VARIABLE, "1.0")
    host_calibration.cache_clear()
    yield
    host_calibration.cache_clear()


def _pre_3185_ci_shape_check(per_unit_at):
    """The CI-shape check as it was before task 3185: the larger size
    measured again GROWTH_RETRIES times, the smallest never, then the
    per-unit ratio compared by hand (growth_ratio: the smaller reading
    raised to the floor). Returns the readings it failed on, None if it
    passed."""
    def ratio(smallest, seconds):
        return seconds / max(smallest, GROWTH_FLOOR_SECONDS)

    per_megabyte = {}
    for kilobytes in CI_SHAPE_KILOBYTES:
        seconds = per_unit_at(kilobytes)
        smallest = per_megabyte.setdefault(CI_SHAPE_KILOBYTES[0], seconds)
        for _ in range(2):
            if ratio(smallest, seconds) < CI_SHAPE_PER_MEGABYTE_LIMIT:
                break
            seconds = min(seconds, per_unit_at(kilobytes))
        per_megabyte[kilobytes] = seconds
        if not ratio(smallest, seconds) < CI_SHAPE_PER_MEGABYTE_LIMIT:
            return per_megabyte
    return None


# CI run 37447348074 (Python 3.10): 50 KB read 0.067 s per MB once, 200 KB
# read 0.137 s on all three of its readings; linear work, so a quieter
# 200 KB reading follows.
CI_READINGS = {50: [0.067], 200: [0.137, 0.137, 0.137, 0.066], 2048: [0.068]}


def test_the_pre_3185_check_fails_on_the_ci_readings():
    """The scripted readings reproduce CI's failure on the old check, so the
    test below proves the conversion, not the script."""
    failed_at = _pre_3185_ci_shape_check(ScriptedPerUnit(CI_READINGS))

    assert failed_at == {50: 0.067, 200: 0.137}


def test_the_ci_reading_is_settled_on_both_sizes_and_passes(unloaded):
    """The same readings through the shared per-unit check: the pair over
    the limit is measured again PER_UNIT_OVER_RETRIES times, both sizes
    interleaved, and the quieter fourth 200 KB reading settles it as
    linear. The 2 MB pair, linear on its first readings, is read once
    more before it passes (SETTLE_RETRIES, task 3187)."""
    work = ScriptedPerUnit(CI_READINGS)

    settled = assert_linear_per_unit(work, CI_SHAPE_KILOBYTES, 0.5, "ci")

    assert settled == {50: 0.067, 200: 0.066, 2048: 0.068}
    assert work.calls[:2] == [50, 200]
    assert work.calls[2:8] == [50, 200] * 3
    assert work.calls[8:] == [2048] + [50, 2048] * SETTLE_RETRIES


def test_a_burst_over_every_reading_of_the_larger_size_still_fails(unloaded):
    """Settling is not a pass: linear-looking work whose larger size reads
    2.04x per unit on every one of its readings fails, after
    PER_UNIT_OVER_RETRIES interleaved rounds."""
    work = ScriptedPerUnit({50: [0.067], 200: [0.137], 2048: [0.068]})

    with pytest.raises(TimingCheckFailed, match="fastest of 5 interleaved"):
        assert_linear_per_unit(work, CI_SHAPE_KILOBYTES, 0.5, "burst")

    assert work.calls == [50, 200] * (1 + PER_UNIT_OVER_RETRIES)


@pytest.mark.parametrize("quarter_burst", [1.0, 2.0, 2.5, 3.0])
def test_quadratic_work_per_unit_fails_whatever_burst_hides_it(
        unloaded, quarter_burst):
    """Quadratic work reads 4x per unit at 4 times the input. A burst that
    inflated the first 50 KB reading 2.5-3x read 1.6x and 1.3x per unit,
    under the 2x the hand-written check failed at; settled, the quieter
    50 KB readings outrun it and the pair still fails."""
    work = ScriptedPerUnit({50: [0.05 * quarter_burst, 0.05], 200: [0.2],
                            2048: [2.0]})

    with pytest.raises(TimingCheckFailed, match="superlinear per unit"):
        assert_linear_per_unit(work, (50, 200, 2048), 10.0, "quadratic")

    assert 2048 not in work.calls


def test_a_size_over_its_budget_per_unit_fails_before_the_next_is_read(unloaded):
    work = ScriptedPerUnit({50: [0.1], 200: [0.6], 2048: [0.1]})

    with pytest.raises(TimingCheckFailed, match="over the budget"):
        assert_linear_per_unit(work, (50, 200, 2048), 0.5, "budget")

    assert work.calls == [50, 200]


@pytest.mark.parametrize("sizes", [(50,), (200, 50), (50, 50, 200)])
def test_per_unit_sizes_are_at_least_two_smallest_first(sizes):
    with pytest.raises(ValueError):
        assert_linear_per_unit(lambda size: 0.0, sizes, 0.5)
