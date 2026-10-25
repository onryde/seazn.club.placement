"""T0->T5 lexicographic objective chain — acceptance tests.

Same board as `test_model.py` (`placement_bench_boards.production_board()`, the
shared generator) and the same "nothing is mocked" stance: a green run here
means CP-SAT actually proved six successive optima on the real board.

THE CHAIN IS SIX RUNGS as of 2026-08-13:
`placed -> days -> day_span -> day_start -> idle_gap -> imbalance`. The single
whole-board `makespan` term was RETIRED, not renamed; the rungs that replaced
it are covered here for the production board and in `test_day_objective.py`
for their own behaviour on boards built to isolate each one.

WHAT THESE TESTS HAVE TO PIN, and why the obvious assertions do not do it.
`tiers_completed == TIER_COUNT` plus the tier NAMES is satisfied by a chain
that computes a single weighted score and reports six numbers off it — which is
precisely the thing the design forbids (build.ts's D3: `placed` dominates
absolutely; a shorter day must never be bought by dropping a match). So the
ordering is pinned three further ways, each of which fails if a freeze is
removed:

  * `test_full_chain_places_exactly_what_t0_alone_places` — the T0 floor.
    Without `placed >= floor`, the rungs below it minimise their terms by
    placing almost nothing (an empty board uses no days and spans nothing), so
    the count collapses.
  * `test_reported_bounds_are_frozen_against_the_final_board` — the T1 and T2
    ceilings. The board that comes back is the LAST rung's, several solves
    after `days`/`day_span` reported theirs; if those values were not frozen as
    constraints, the rungs below are free to blow straight through them.
  * `test_reported_values_match_the_board_that_came_back` — the reported
    numbers describe THIS board, not some intermediate one.

The metric re-computations below are deliberately independent of the model's
own objective terms — plain Python over `outcome.assignments`, the way
`test_model.py` re-verifies the constraint families and the way TS's
`boardMetrics` does it for the z3 walk. That is the point: an objective term
that has drifted from the metric it claims to measure is invisible to any
assertion phrased in terms of the term itself.

--- THIS SUITE IS WALL-CLOCK SENSITIVE. Read before calling a red one a bug --

`test_every_tier_completes_on_production_board` asserts six PROVED optima at
`CHAIN_WALL_SECONDS`. `idle_gap` is still the bulk of that chain, but it is no
longer the whole of it — RE-MEASURED 2026-08-13, after T2 started counting in
tick-lattice units instead of milliseconds (`placement.model`'s T2 section):

    6 runs, load 3-6, production board
    placed 206 / days 263 / day_span 749 / day_start 251
    idle_gap 4 282-6 381 (was 10 692-16 159 at the same load)
    imbalance 200, whole chain 5 997-8 083 ms

What that changed at the 8 s PRODUCTION wall, 6 runs per arm back to back on
one box: before, 4 of 6 rungs proved on every single run (all six runs clamped
at ~8.05 s); after, 6 of 6 rungs proved on three of six runs. So the wall now
sometimes proves the whole ladder and never used to. It is NOT a guarantee at
8 s and this suite does not assert one — `CHAIN_WALL_SECONDS` is what the
correctness tests run at, for the reason that constant records.

The cost that remains is REPORTED, not hidden. Before the day rungs the same
chain proved four tiers in 1.1-2.1 s. The control run that explains the rest:
`placed` + `idle_gap` alone, with no day rungs at all, cost 12.4 s — so the old
whole-board `makespan` freeze had been doing `idle_gap`'s pruning for it, and
retiring the term is what the wall pays for. Not a defect in the new rungs;
they cost ~1.3 s of the total between them.

`idle_gap`'s remaining cost is a DUAL BOUND, not search: measured with the
solver's own log, the optimum is found in ~4 s and everything after that is
proof, at 0 conflicts and 0 LP iterations. The lattice unit made each step of
that proof 600 000x coarser; what it did not do is make the bound STRONG. See
`placement.model`'s T2 note for why `day_span`'s redundant-floor lever does not
transfer here (every entrant on this board plays exactly two fixtures).

The solver takes eight search workers on a six-core machine, so a box already
running other agents stretches those numbers past the wall and the chain
reports fewer rungs than it can prove alone. That is the budget working exactly as
designed (it under-reports rather than claiming an unproved optimum) and it is
NOT a regression in the tier code.

Observed here: identical code, two consecutive runs, `tiers_completed` 4 then
2, with `load average: 7.5`. This repo has a documented family of these
(`repair-scale` termination-under-budget, `roundrobin` idempotence, `swiss`
colour bounds — all green alone, all tripped by concurrent agents). Check the
load before diagnosing, and re-run alone.

If a tier ever needs to get genuinely faster, the lever is its DUAL bound, not
its search — see `placement.model`'s T3 note, where a squeeze that could not close
its bound in 20 s proved the same value in 0.4 s as an equality.

--- round 6: identity is POSITIONAL, `bench/` is not ------------------------

`bench/placement_bench_boards.py` is out of scope and still returns string-keyed
boards; `_board_positional.to_positional` converts every board generated in
this file immediately, before it ever reaches `build_model`. `objective.py`
itself is untouched by round 6 — it never keys on identity at all, only on
`FixtureVars`'s decision variables.
"""

import logging
import os
import time

import pytest
import structlog

from _board_positional import rule_groups_for, to_positional
from placement.model import MIN_MS, build_model, solve
from placement.objective import (
    TIER_COUNT,
    TIER_DAY_SPAN,
    TIER_DAY_START,
    TIER_DAYS,
    TIER_IMBALANCE,
    TIER_IDLE_GAP,
    TIER_ORDER,
    TIER_PLACED,
    run_tier_chain,
)

#: The production budget the design spec settled on. Used by the ONE test that
#: is actually about the budget, and by nothing else.
PRODUCTION_WALL_SECONDS = 8.0

#: The wall the chain-behaviour tests run under. Deliberately far above the
#: production budget, and it costs nothing: the chain STOPS when it has proved
#: its optima, so a generous ceiling changes the runtime of a converging chain
#: not at all — it only stops machine load from deciding how many tiers a
#: CORRECTNESS test gets to see.
#:
#: Splitting these two apart is the whole point. "Six tiers, in D3's order,
#: each freezing the last" is a property of the code and must be pinned
#: deterministically; "the chain fits in 8 s" is a property of this board on
#: this machine, and asserting it inside the same test made four unrelated
#: assertions hostage to the box's load average (measured: 4 tiers at load
#: ~2.5, 2 tiers at load ~6, same code, same commit).
#:
#: RAISED 2026-08-13, 30 s -> 90 s, when the chain went from four rungs to six.
#: 30 s was "far above" a 4.9 s chain; against a 7.7-19.5 s one (6 runs, and
#: `idle_gap` is now the bulk of it) it is close enough that a loaded box would
#: start deciding how many rungs a CORRECTNESS test gets to see, which is the
#: exact failure this constant exists to prevent. It still costs nothing: the
#: chain stops when it has proved its optima, so a generous ceiling changes a
#: converging chain's runtime not at all.
CHAIN_WALL_SECONDS = 90.0

# --- the proved optima of `production_board()` ------------------------------
#
# These are not observations of a run, they are PROVED lexicographic optima of
# a deterministic model, so each is a single exact value: a chain that reaches
# `TIER_COUNT` means CP-SAT proved there is no better one. That is what makes them safe to
# assert with `==`, and asserting them with `==` is the point — every existing
# assertion on these three numbers is one-sided in the SAFE direction
# (`recomputed <= reported`), which a WORSE value satisfies more easily. A
# chain that actively MAXIMISES player idle time therefore shipped green
# through four review rounds: inverting T2 moves the reported gap from
# 132 600 000 ms to 2 229 000 000 ms — 16.8x worse, the metric an organiser
# feels most directly — with the suite 14/14.
#
# If a deliberate board change moves these, `tests/test_bench_contract.py`
# fails first and says which part of the board moved. Do not re-baseline them
# without reading that test's failure.
#
# RE-BASELINED 2026-08-13 — T1 IS NOW THREE RUNGS, NOT ONE. The single
# whole-board `makespan` term (`mk_hi - mk_lo`) was RETIRED and replaced by
# `days` -> `day_span` -> `day_start`; see `placement.model`'s T1 section for
# why a whole-board span on a multi-day board is mostly measuring darkness.
# The retired constants and their history, kept so a future reader can tell a
# re-baseline from a drift:
#
#     T1_PROVED_MAKESPAN_MS          1 557 600 000  (was 1 519 800 000 before
#                                    #511 put pins in the span, 2026-08-11)
#     T1_PROVED_MAKESPAN_PIN_FREE_MS 1 519 800 000
#
# The BOARD did not move — `test_bench_contract.py` is untouched and still
# green, which is the evidence for that. What moved is what T1 measures, and
# this time the name moved with it (#512 §5.2: retire `makespan`, do not reuse
# it for `day_span`, because a same-named number that quietly means something
# else is the failure mode this programme keeps hitting).
#
# All five values below are PROVED lexicographic optima of a deterministic
# model, measured 6/6 identical at a 30 s wall, so `==` is the right assertion.
T1A_PROVED_DAYS = 19
T1B_PROVED_DAY_SPAN_MS = 69_600_000

#: `day_start` proves to 0 on this board: every used day CAN start on its own
#: first admissible tick, and the rung makes it do so.
#:
#: A zero is a weak assertion on its own — a term that had collapsed to its
#: domain floor would also report 0 — so the non-zero case is pinned
#: separately and deterministically in
#: `test_day_objective.py::test_day_start_is_reported_as_the_real_offset_when_a_day_cannot_start_early`.
T1C_PROVED_DAY_START_MS = 0

#: RE-BASELINED 2026-08-13, 132 600 000 -> 170 400 000. NOT a regression in T2
#: and not a board change: T2 optimises subject to every bound above it, and
#: the rungs above it changed. A board whose every day is anchored to its own
#: opening slot spreads one entrant's matches further apart than a board free
#: to slide each day's block around, and 37 800 000 ms is what that costs on
#: this board. The trade is the product ruling (`day_start` outranks
#: `idle_gap`), asserted directly in
#: `test_day_objective.py::test_day_start_anchors_each_day_to_its_own_first_slot`.
T2_PROVED_IDLE_GAP_MS = 170_400_000

#: Unchanged by the T1 rework, which is itself worth recording — T3 is below
#: everything that moved, and it still proves the same optimum.
T3_PROVED_IMBALANCE_MS = 2_400_000

#: The same optima on the PIN-FREE board (`_model_without_pins`), for the two
#: chain-behaviour tests that run there. Measured 6/6 identical.
#:
#: `day_span` is lower without the pins (45 600 000 against 69 600 000) and the
#: difference is the pins themselves: three pinned rows hold instants their own
#: days must then stretch to cover, and #512 §6a is what makes the objective
#: see them. `idle_gap` is the SAME on both boards.
T1B_PROVED_DAY_SPAN_PIN_FREE_MS = 45_600_000
T2_PROVED_IDLE_GAP_PIN_FREE_MS = 170_400_000

#: RETIRED 2026-08-13: `CUT_SHORT_WALL_SECONDS = 8.0`, a FIXED wall for the two
#: cut-short tests, justified by "the four rungs cost ~1.6-3.4 s and `idle_gap`
#: ~26 s more (never under 24)", giving ~2.4x margin above and ~3x below.
#:
#: T2 now counts in tick-lattice units and `idle_gap` proves in 4.3-6.4 s, so
#: 8.0 s cut nothing short — the chain ran to 6/6 and both tests lost the state
#: they exist to pin. Kept as a comment rather than deleted because the SHAPE of
#: the mistake outlived the number: a fixed wall racing a rung's proof time is
#: green by lottery. Only one of the two tests went red for it, on a box whose
#: load average was observed between 3 and 213 inside a single 6-run bench.
#:
#: `_cut_short_wall` replaces it, and the second constant is a SLICE, not a
#: wall, so the four rungs' cost is measured instead of assumed.

#: How much clock `idle_gap` is given AFTER the four rungs above it have
#: actually finished — see `_cut_short_wall`, which is what the two cut-short
#: tests use now instead of the constant above.
#:
#: RE-DERIVED 2026-08-13, when T2 started counting in tick-lattice units
#: instead of milliseconds. The old fixed 8.0 s wall was measured against
#: `idle_gap` needing "~26 s more (never under 24)" on this board. It now
#: proves in 4.3-6.4 s (6 runs, load 3-6), so 8.0 s stopped cutting anything
#: short: the chain ran to 6/6 and both tests below lost the state they exist
#: to pin. Only ONE of the two went red for it, which is the tell — a fixed
#: wall racing a rung's proof time is green by lottery, not by design.
#:
#: So the wall is no longer fixed. The four rungs above `idle_gap` are TIMED,
#: and this slice is added to whatever they actually cost, which takes the
#: load out of that half of the race entirely — the old constant's own comment
#: had to reason about "any plausible load" precisely because it could not.
#:
#: TWO slices, because the two tests want different things out of the same
#: mechanism and one number cannot serve both — which is what made the first
#: attempt at this still race.
#:
#: 0.75 s for the cut-short test, which needs only that `idle_gap` RAN and
#: produced a board: it takes >= 4 282 ms to prove (5.7x margin, so it cannot
#: finish by accident on an idle box) and its first solution lands ~0.2 s in.
CUT_SHORT_SLICE_SECONDS = 0.75

#: 2.0 s for the better-board test, which needs `idle_gap` to actually IMPROVE
#: on the four-rung board: it reaches 1.4424e+09 at ~1.6 s against the stopped
#: arm's 1 812 000 000, so a slice much under 2 s can leave it holding its own
#: first solution, which is WORSE (2.157e+09) than the board it is compared to.
#:
#: That test no longer asserts the cut at all — see its own note. Its subject is
#: which board comes back, and the adopted board is better whether the tier was
#: cut short or proved outright, so a load swing between the timing solve and
#: the real one cannot flip it any more.
IMPROVED_BOARD_SLICE_SECONDS = 2.0


def _cut_short_wall(board, slice_seconds: float) -> tuple[float, "object"]:
    """A wall that lands INSIDE `idle_gap` whatever else the box is running.

    Returns `(wall_seconds, four_rung_outcome)`. The caller gets the four-rung
    outcome back because both cut-short tests need it anyway — one to assert
    the rungs above proved, the other to compare boards against — so timing it
    here costs neither of them an extra solve.
    """
    model = _model_without_pins(board)
    started = time.perf_counter()
    outcome = run_tier_chain(
        model, model.fixture_vars, CHAIN_WALL_SECONDS, tiers=TIER_ORDER[:4]
    )
    elapsed = time.perf_counter() - started
    # If this ever fails the box cannot prove four cheap rungs in 90 s and
    # nothing below it means anything.
    assert outcome.tiers_completed == 4, (
        f"the four rungs above idle_gap did not prove in {CHAIN_WALL_SECONDS}s "
        f"({outcome.tiers_completed}/4) — the box, not the chain"
    )
    # DOUBLED, not just offset. The failure this replaces: the four rungs were
    # timed in one solve and re-run in another, and their own run-to-run spread
    # (1 070-2 564 ms at load 3-6, and this box has been observed at load 213)
    # is larger than any fixed slice — measured, `tiers_completed=2`, the chain
    # cut inside `day_span` having never reached `idle_gap` at all.
    #
    # Doubling is safe on the OTHER side because `idle_gap`'s cost scales with
    # the same load the four rungs do: it needs ~4x their time (4 282 ms against
    # 1 255 ms, same runs), so a budget of `elapsed + slice` leaves it around a
    # third of what proving takes, at any load. The two failure directions are
    # therefore not a trade-off here — this widens one without narrowing the
    # other.
    return 2 * elapsed + slice_seconds, outcome


def _production_board():
    """The bench's `production_board()`, converted to the positional shape,
    PLUS the `rule_groups` its declared `rest_by_division`/`day_cap_by_
    division` now have to reach `build_model` through: `division_rules`
    (proto field 10), the wire path those dict keys used to be fed through,
    is retired (`placement.schema`'s module docstring). Without this, every
    caller below would silently solve an UNCAPPED, un-rested board — see
    `test_model.py`'s own `_production_board`, which this mirrors exactly,
    for the fuller reasoning and the regressions that shipped before this
    file got the same fix.

    Returns an 8-tuple: the 7 `build_model(*board)`-shaped elements
    (`to_positional`'s own output) plus `rule_groups`.
    """
    # bench/ reaches sys.path via `pythonpath` in pyproject.toml — see
    # test_model.py's note. Module name matches its siblings placement_bench.py /
    # placement_repair_bench.py.
    from placement_bench_boards import production_board

    raw_board = production_board()
    return (*to_positional(raw_board), rule_groups_for(raw_board))


@pytest.fixture(scope="module")
def board():
    return _production_board()


def _model_for(board):
    fixtures, num_courts, grid_slots, step_minutes, constraints, existing, deps, rule_groups = board
    return build_model(
        fixtures, num_courts, grid_slots, step_minutes, constraints, existing, deps,
        rule_groups=rule_groups,
    )


def _model_without_pins(board):
    """The SAME board with `existing` stripped, for the two tests that need a
    tier the clock can cut short.

    Not a weaker board and not a different one — same fixtures, same courts,
    same grid, same constraints. Only the three pinned rows are removed.

    Why it has to exist (#511): pins now bound `mk_lo`/`mk_hi` directly rather
    than through `OnlyEnforceIf(placed[i])`, and on this board that collapses
    T1's search. T1 used to take upwards of 2 260 ms to prove and now takes
    ~450-670 ms — but the number that matters is not the speedup, it is that
    the window between "T1 has found an incumbent" and "T1 has proved it" has
    closed. Measured 8 runs per wall, counting the state these tests actually
    need (`tiers_completed == 1` AND two objective values):

        0.20 s -> 0/8   T1 returns NOTHING; one objective value, not two
        0.25 s -> 1/8
        0.30 s -> 0/8   T1 proves
        0.40 s -> 0/8   T1 proves
        pins stripped, 1.5 s -> 8/8

    So there is no wall on the pinned board that reliably produces "adopted but
    not counted", and re-tuning to one would have shipped a 1-in-8 test. The
    contract under test is the TIER CHAIN's, not the pin encoding's, so the
    honest move is to keep the original 1.5 s wall and its load-safety argument
    (T0 ~100 ms, T1 never under 2 260 ms, load only widens that gap) and run it
    on a board where that argument is still true.

    `test_t1_reports_the_proved_minimum_makespan_not_merely_a_ceiling` still
    uses the PINNED board — the proved optimum is what #511 changed, and that
    test is where the new value is asserted.
    """
    fixtures, num_courts, grid_slots, step_minutes, constraints, _existing, deps, rule_groups = board
    return build_model(
        fixtures, num_courts, grid_slots, step_minutes, constraints, [], deps,
        rule_groups=rule_groups,
    )


@pytest.fixture(scope="module")
def chain(board):
    """One full chain, shared. Module-scoped because each run is a real
    multi-second four-solve walk; the MODEL is never shared (a solved model
    carries an objective and the tier chain's frozen bounds, so re-solving one
    would not be the same question)."""
    return solve(_model_for(board), wall_seconds=CHAIN_WALL_SECONDS)


# --- the two acceptance tests named in the prompt ---------------------------


def test_every_tier_completes_on_production_board(chain):
    assert chain.tiers_completed == TIER_COUNT == 6
    names = [name for name, _ in chain.objective_values]
    assert names == ["placed", "days", "day_span", "day_start", "idle_gap", "imbalance"]


def test_tier_order_is_lexicographic_not_weighted(chain):
    placed_count = len(chain.assignments)
    assert placed_count >= 35  # T0's bound must be frozen before T1 even runs


def test_chain_fits_the_production_budget(board):
    """The budget observation, stated as the guarantees that hold at ANY load.

    Measured, production board, 8 search workers:

        idle box        4 tiers OPTIMAL, 4 940 ms   (T0 88, T1 2 257, T2 2 094, T3 501)
        load avg ~6     2-3 tiers, chain stopped by the wall at ~8 020 ms

    so the acceptance criterion — every tier inside the budget — holds,
    with roughly a 40% margin on an unloaded machine and none at all on a
    machine already running other work. Asserting `tiers_completed == 4` HERE
    would therefore be asserting something about the box, so what is asserted
    is what the design actually promises under pressure: the wall is honoured,
    the board is still complete, and the tier count is honest rather than
    optimistic. `test_all_four_tiers_complete_on_production_board` owns the
    four-tier claim, deterministically.
    """
    fixtures = board[0]
    outcome = solve(_model_for(board), wall_seconds=PRODUCTION_WALL_SECONDS)

    # The wall is a cap, not a target. 250 ms of slack for the last tier's
    # teardown and the outcome assembly.
    assert outcome.elapsed_ms <= PRODUCTION_WALL_SECONDS * 1000 + 250

    # Degrading gracefully means the BOARD is still whole — T0 completes in
    # ~100 ms and everything below it only rearranges what T0 placed.
    assert outcome.tiers_completed >= 1
    assert len(outcome.assignments) == len(fixtures)

    # ...and that it is REPORTED as a board. A tier cut off with nothing of its
    # own returns UNKNOWN, and passing that verdict through made the chain
    # answer "no result" while holding a complete, thrice-proved board —
    # observed at load ~7, with `tiers_completed=3` and 37 assignments. `status`
    # describes what came back, never the last solve that happened to run.
    assert outcome.status in ("OPTIMAL", "FEASIBLE")

    # Whatever it got through, it got through IN ORDER — never a later tier
    # reported without the ones that dominate it.
    names = [name for name, _ in outcome.objective_values]
    assert names == list(TIER_ORDER)[: len(names)]


# --- the ordering, pinned properly ------------------------------------------


def test_full_chain_places_exactly_what_t0_alone_places(board, chain):
    """T0's achieved count must survive all three tiers below it.

    This is the freeze that actually matters. T1 minimises `mk_hi - mk_lo`,
    and the cheapest way to shorten a day is to stop playing on it: unfrozen,
    T1 walks the board down towards a single fixture. So the comparison is not
    "did we place a lot" but "did we place exactly what the tier that owns
    that question achieved on its own".
    """
    t0_model = _model_for(board)
    t0_only = run_tier_chain(t0_model, t0_model.fixture_vars, CHAIN_WALL_SECONDS, tiers=(TIER_PLACED,))
    assert t0_only.tiers_completed == 1
    assert len(t0_only.assignments) == dict(t0_only.objective_values)[TIER_PLACED]
    assert len(chain.assignments) == len(t0_only.assignments)
    assert dict(chain.objective_values)[TIER_PLACED] == dict(t0_only.objective_values)[TIER_PLACED]


def test_reported_values_match_the_board_that_came_back(board, chain):
    """Every reported number is a property of the returned assignments, not of
    a board discarded three solves ago."""
    fixtures, num_courts, _slots, _step, constraints, _existing, _deps, _rule_groups = board
    dur_ms = constraints["match_minutes"] * MIN_MS
    reported = dict(chain.objective_values)

    assert reported[TIER_PLACED] == len(chain.assignments)
    # T3 is the last tier, so its own term is exact for the board it returned.
    assert reported[TIER_IMBALANCE] == _court_imbalance(chain.assignments, num_courts, dur_ms)


def test_reported_bounds_are_frozen_against_the_final_board(board, chain):
    """T1's day terms and T2's idle gap were achieved by boards that T3 then
    replaced. They are reported as BOUNDS, and the board that came back has to
    honour them — that is the whole content of "frozen", and it is exactly
    what a blended score would not give you.
    """
    fixtures, _num_courts, slots, _step, constraints, _existing, _deps, _rule_groups = board
    dur_ms = constraints["match_minutes"] * MIN_MS
    entrants_of = [ents for ents, _div in fixtures]  # identity is position now
    reported = dict(chain.objective_values)

    day_of_start = {start: day for _court, start, day in slots}
    day_open_of: dict[int, int] = {}
    for _court, start, day in slots:
        day_open_of[day] = min(day_open_of.get(day, start), start)
    assert _days_used(chain.assignments, day_of_start) <= reported[TIER_DAYS]
    assert _day_span(chain.assignments, day_of_start, dur_ms) <= reported[TIER_DAY_SPAN]
    # `day_start` too, recomputed in plain Python off the assignments. Its
    # reported value on this board is 0, and a `<=` against 0 is only as strong
    # as the recompute is honest — which is the point of recomputing rather
    # than reading the term back: a rung that had collapsed onto its domain
    # floor reports 0 while the BOARD's real offset is positive, and this
    # catches exactly that. The non-zero case is pinned directly in
    # `test_day_objective.py`.
    assert _day_start_offset(chain.assignments, day_of_start, day_open_of) <= reported[TIER_DAY_START]
    # `worst_gap` is a documented OVER-approximation for a participant with 3+
    # fixtures (max over all pairs, not consecutive ones), so the honest
    # relation is <=, in the safe direction: the true gap never exceeds the
    # frozen ceiling.
    assert _worst_consecutive_gap(chain.assignments, entrants_of, dur_ms) <= reported[TIER_IDLE_GAP]


def test_a_completed_chain_reports_optimal(chain):
    """A chain that proved every tier says OPTIMAL; anything less says
    FEASIBLE. `status` is the coarse gate a caller checks before trusting
    `assignments` at all, so OPTIMAL has to mean the whole ladder, not "the
    last solve happened to prove its own objective"."""
    assert chain.status == "OPTIMAL"
    assert chain.tiers_completed == TIER_COUNT

    # A REAL ceiling, not just "inside the generous wall it was given".
    # Splitting the six-tier claim off the 8 s budget removed the only
    # elapsed-time assertion in the suite, which would let a 3x regression in
    # tier cost pass silently.
    #
    # RE-BASELINED 2026-08-13, 15 s -> 45 s, with the reason stated because the
    # number tripled and a tripled ceiling is exactly how a real regression
    # gets waved through later. The chain went from four rungs to six, and
    # measured over 6 runs at a 30 s wall the whole chain costs 7 739 -
    # 19 485 ms (median 17 657). Almost none of that is the new rungs — they
    # cost ~2 s of the total between them — it is `idle_gap`, which went from a
    # 1 348 ms median to a 14 931 ms median because it now optimises subject to
    # three frozen day bounds. 45 s sits above the worst measured run with
    # margin for a loaded box and still under 3x the median, so it catches a
    # regression rather than an afternoon. The cost itself is REPORTED, not
    # hidden: see the PR body's bench table.
    assert chain.elapsed_ms <= 45_000


def test_a_wall_too_short_to_finish_reports_fewer_tiers_not_a_lie(board):
    """The budget-expired path. Under-reporting is safe (TS just does more
    work); over-reporting would have TS claim an optimality nobody proved.
    `tiers_completed` carries `BuildResult.tiersCompleted`'s meaning — tiers
    PROVED optimal — so a chain the clock cut short must report fewer than the
    ladder's length, whatever it managed to place.
    """
    short = _model_for(board)
    outcome = run_tier_chain(short, short.fixture_vars, wall_seconds=0.05)
    assert outcome.tiers_completed < TIER_COUNT
    # Whatever it recorded is a PREFIX of the ladder, and it never counts more
    # tiers than it recorded. (It may record one MORE than it counted: a T0 cut
    # short is adopted, since there is no earlier board to fall back on, but it
    # is not counted as proved.)
    names = [name for name, _ in outcome.objective_values]
    assert names == list(TIER_ORDER)[: len(names)]
    assert outcome.tiers_completed <= len(names) <= outcome.tiers_completed + 1


def test_a_tier_cut_short_is_adopted_but_not_counted(board):
    """The FEASIBLE case, isolated — the semantic the whole `tiers_completed`
    contract turns on.

    The wall is chosen so that a known set of tiers settles and the next one is
    provably cut short — see `_cut_short_wall`, which times the four rungs
    above it rather than assuming what they cost. The measurement it
    comes from. `idle_gap` is the rung that gets cut, because it is now the
    expensive one: the four above it prove in under 3.5 s and it needs ~26 s.

    Runs on the PIN-FREE board (`_model_without_pins`) as of #511, for the
    reason that docstring carries.

    Two things must then be true, and neither is implied by the other:

      * `tiers_completed == 4`, NOT 5. CP-SAT hands back a real board with a
        real idle gap on a FEASIBLE verdict, and counting it would tell TS that
        the tier was PROVED optimal when nothing proved it — TS gates
        `already_optimal` and its whole LNS fallback on that number.
      * the cut tier's board is ADOPTED all the same — FIVE recorded objective
        values, not four. `schema.py` slices
        `objective_values[:tiers_completed]` before the wire, so the extra
        entry publishes nothing; it is what lets the board that came back BE
        the better one.

    This reverses the rule that shipped originally ("the tier's board is
    DISCARDED... adopting it would replace a proved board with an unproved
    one"). That rationale conflated two claims. The adopted board satisfies
    every frozen bound as a HARD CONSTRAINT, so it is proved on the earlier
    tiers' metrics exactly as the discarded board was; the only thing unproved
    about it is that its OWN metric is minimal, which `tiers_completed == 4`
    already says. Measured cost of the old rule on this board and this wall,
    worst idle gap recomputed off the assignments rather than read off the
    relaxation variables: 1 812 000 000 -> 170 400 000, 6/6 (see
    `test_the_adopted_board_is_the_better_one`).
    """
    wall, _four = _cut_short_wall(board, CUT_SHORT_SLICE_SECONDS)
    model = _model_without_pins(board)
    outcome = run_tier_chain(model, model.fixture_vars, wall_seconds=wall)

    # The cut tier RAN and was cut short — without this the test would pass
    # vacuously on a chain that stopped before it ever started, which is a
    # different code path (the deadline check at the top of the loop) reaching
    # the same numbers. The four rungs above cost ~3.5 s at worst, so a chain
    # that consumed nearly the whole wall spent the rest inside `idle_gap`.
    assert outcome.elapsed_ms >= int(wall * 1000) - 600

    assert outcome.status == "FEASIBLE"  # a board, but an unfinished chain
    assert outcome.tiers_completed == 4
    assert [name for name, _ in outcome.objective_values] == list(TIER_ORDER[:5])
    # The whole board survives — adopting the cut tier's is not losing rows.
    assert len(outcome.assignments) == len(board[0])
    # A cut-short tier cannot have beaten the proved optimum; if it claims to,
    # the recorded value belongs to some other board than the one returned.
    reported = dict(outcome.objective_values)
    fixtures, _num_courts, _slots, _step, constraints, _existing, _deps, _rule_groups = board
    dur_ms = constraints["match_minutes"] * MIN_MS
    entrants_of = [ents for ents, _div in fixtures]
    assert reported[TIER_IDLE_GAP] >= T2_PROVED_IDLE_GAP_PIN_FREE_MS
    assert _worst_consecutive_gap(outcome.assignments, entrants_of, dur_ms) <= reported[TIER_IDLE_GAP]


def test_the_adopted_board_is_the_better_one(board):
    """The point of adopting: the board an organiser receives is the CUT tier's,
    and it is better — on that tier's own metric — than the board the chain
    would have returned had it stopped at the last rung that proved.

    Asserted with `<`, not `<=`, deliberately. Under the old discard rule the
    returned board simply WAS the previous rung's, so a `<=` would pass
    vacuously against the behaviour this test exists to pin.

    Both arms run the SAME wall on the SAME pin-free board, and differ only in
    whether the chain is allowed to enter `idle_gap` at all. Measured 6/6 at
    this wall, worst consecutive gap recomputed off the assignments:

        stopped at day_start   1 812 000 000 / 2 157 600 000 ms
        adopted idle_gap's       170 400 000 ms  (~10x better, never equal)

    Load hazard, stated because this repo has been bitten by it: a heavily
    loaded box gives `idle_gap` less search and narrows the margin. A red here
    with the two gaps EQUAL means it found nothing in its slice, which is the
    box; a red with the adopted gap LARGER is the code.
    """
    fixtures, _num_courts, _slots, _step, constraints, _existing, _deps, _rule_groups = board
    dur_ms = constraints["match_minutes"] * MIN_MS
    entrants_of = [ents for ents, _div in fixtures]

    # Pin-free, both arms, for the reason `_model_without_pins` records. Both
    # arms must use the same board or the comparison is meaningless.
    wall, stopped = _cut_short_wall(board, IMPROVED_BOARD_SLICE_SECONDS)
    full_model = _model_without_pins(board)
    full = run_tier_chain(full_model, full_model.fixture_vars, wall)

    assert stopped.tiers_completed == 4  # every rung above `idle_gap` proved
    # `>=`, not `==`, and that is the point of this test's rewrite. Whether
    # `idle_gap` gets CUT SHORT or proves outright is a race against the box,
    # and it is not what this test is about — the semantics of "cut short =>
    # adopted but not counted" belong to
    # `test_a_tier_cut_short_is_adopted_but_not_counted`, which forces that
    # state with a slice too small to prove in. What is asserted here is the
    # board that comes back, which is better either way.
    assert full.tiers_completed >= 4  # `idle_gap` ran
    assert len(full.objective_values) >= 5  # ...and its board was adopted
    assert len(full.assignments) == len(stopped.assignments)
    assert _worst_consecutive_gap(full.assignments, entrants_of, dur_ms) < _worst_consecutive_gap(
        stopped.assignments, entrants_of, dur_ms
    )


# --- each tier's optimisation DIRECTION, pinned two-sidedly -----------------
#
# `test_reported_bounds_are_frozen_against_the_final_board` above proves the
# freeze MECHANISM; it cannot tell minimisation from maximisation, because a
# larger reported ceiling satisfies `recomputed <= reported` more easily. The
# four tests below are the missing half. Each names the exact proved optimum,
# so a tier that optimises the wrong way fails on the VALUE — not by running
# out of clock, and not by a downstream `KeyError`.


def _proved(chain, tier, rung):
    """The value tier `rung` reported, with a precondition that it ran at all.

    Without the precondition an inverted tier that simply failed to prove
    inside the wall would raise `KeyError` instead of failing the assertion —
    a red for a reason that is about the box, not the code. This turns that
    case into a message that says which it was.
    """
    assert chain.tiers_completed >= rung, (
        f"the chain reached only {chain.tiers_completed} of {len(TIER_ORDER)} tiers in "
        f"{chain.elapsed_ms} ms at load1={os.getloadavg()[0]:.2f}, so {tier!r} never ran and this "
        "assertion cannot bind. That is the documented load hazard, not necessarily a defect — "
        "re-run alone before diagnosing."
    )
    return dict(chain.objective_values)[tier]


def test_t1a_reports_the_proved_minimum_day_count(chain):
    """T1a MINIMISES. `day_used[d]` is implied by `on_day` one-directionally,
    so a MAXIMISING rung simply turns every day's bool on and reports the grid's
    whole day count (26 here, against the true optimum's 19) while placing
    exactly the same board. Nothing downstream would notice — which is why the
    value is asserted with `==` and not `<=`."""
    assert _proved(chain, TIER_DAYS, 2) == T1A_PROVED_DAYS


def test_t1b_reports_the_proved_minimum_day_span(chain):
    """T1b MINIMISES. Each `day_lo[d]` is squeezed from above only and each
    `day_hi[d]` from below only, so maximising drives every used day's pair to
    the ends of its own window and the reported value becomes the summed width
    of the days rather than a real span — the same one-sided failure the
    retired whole-board term had, once per day."""
    assert _proved(chain, TIER_DAY_SPAN, 3) == T1B_PROVED_DAY_SPAN_MS


def test_t1c_reports_the_proved_minimum_day_start_offset(chain):
    """T1c MINIMISES. Maximising it pushes every used day's block as LATE as
    the day allows, which is the exact symptom the rung was added to remove
    (`day_start` would come back as the summed distance from each day's open to
    its last usable start).

    Asserting 0 is weak on its own; the direction is what this pins on the
    production board, and the non-zero reading is pinned deterministically in
    `test_day_objective.py`. See `T1C_PROVED_DAY_START_MS`.
    """
    assert _proved(chain, TIER_DAY_START, 4) == T1C_PROVED_DAY_START_MS


def test_t2_reports_the_proved_minimum_idle_gap_not_merely_a_ceiling(chain):
    """T2 MINIMISES — the tier whose direction nothing could detect.

    This is CRITICAL-1 from the Task 3 re-audit: two independent T2 mutations
    (term deleted, direction inverted) left the suite reproducibly green while
    the board handed to organisers had a worst idle gap 16.8x larger. T2's
    term is a per-pair lower bound, true only while something minimises it, so
    inverted it reads as the lattice span rather than a real gap.
    """
    assert _proved(chain, TIER_IDLE_GAP, 5) == T2_PROVED_IDLE_GAP_MS


def test_t3_reports_the_proved_minimum_court_imbalance(chain):
    """T3 MINIMISES. Unlike T1/T2 its term is an equality, so it reads true on
    any board — which is exactly why the existing
    `reported == _court_imbalance(board)` check is direction-BLIND: it holds
    whether T3 minimised, maximised, or did nothing. This pins the value.

    The direction claim itself does not rest on this test — see
    `test_tier_directions_are_pinned_without_the_clock`, which settles it in
    milliseconds. On the production board a maximising T3 tends to miss the
    wall, and a mutant that dies by wall clock is not a killed mutant:
    measured, the same T3 mutation was green at a 22 s wall and red at 44 s.
    """
    assert _proved(chain, TIER_IMBALANCE, 6) == T3_PROVED_IMBALANCE_MS


def test_tier_directions_are_pinned_without_the_clock():
    """T1's and T3's directions, on a board that proves every tier in ~20 ms.

    `imbalance_probe_board()` exists because a direction assertion carried on
    the production chain inherits that chain's load sensitivity, and the T3
    mutants currently die by wall clock alone. Here every tier is forced except
    T3, which chooses between a spread of one match and a spread of two:

        minimising -> PROBE_OPTIMAL_IMBALANCE_MS   (2,1,1 across three courts)
        maximising -> PROBE_WORST_IMBALANCE_MS     (2,2,0)

    both proved instantly, so the kill is on the value at any machine load.
    Its match/gap is 25/5 rather than the production 40/10 precisely so no
    number here collides with a production number — a mutant that hardcodes a
    production value must not pass by coincidence.

    T2 is NOT pinned here and cannot be: the probe board's entrants are
    pairwise disjoint, so no participant has two matches, `worst_gap` is
    constrained to 0 outright and both directions agree. Asserting that 0 keeps
    the omission explicit rather than letting it read as coverage.
    """
    from placement_bench_boards import (
        PROBE_FIXTURES,
        PROBE_OPTIMAL_DAY_SPAN_MS,
        PROBE_OPTIMAL_IMBALANCE_MS,
        PROBE_WORST_IMBALANCE_MS,
        imbalance_probe_board,
    )

    _raw_probe_board = imbalance_probe_board()
    board = (*to_positional(_raw_probe_board), rule_groups_for(_raw_probe_board))
    model = _model_for(board)
    outcome = run_tier_chain(model, model.fixture_vars, wall_seconds=5.0)
    detail = (
        f"status={outcome.status} tiers={outcome.tiers_completed} elapsed_ms={outcome.elapsed_ms} "
        f"values={outcome.objective_values} load1={os.getloadavg()[0]:.2f}"
    )
    assert outcome.tiers_completed == TIER_COUNT, detail
    reported = dict(outcome.objective_values)

    assert reported[TIER_PLACED] == PROBE_FIXTURES, detail
    # One calendar day, two ticks: `days` is 1 and the summed per-day span is
    # the same number the retired whole-board term proved here.
    assert reported[TIER_DAYS] == 1, detail
    assert reported[TIER_DAY_SPAN] == PROBE_OPTIMAL_DAY_SPAN_MS, detail
    # Both ticks are usable and the earlier one is taken, so the day is
    # anchored to its own open and the offset is 0.
    assert reported[TIER_DAY_START] == 0, detail
    assert reported[TIER_IDLE_GAP] == 0, detail
    assert reported[TIER_IMBALANCE] == PROBE_OPTIMAL_IMBALANCE_MS, detail
    # The assertion above is only two-sided because a worse value is reachable
    # on this board — state that, so a future board edit that removes the slack
    # turns this into a loud failure rather than a silently vacuous pass.
    assert PROBE_WORST_IMBALANCE_MS > PROBE_OPTIMAL_IMBALANCE_MS

    # ...and the returned board really has that imbalance, recomputed in plain
    # Python rather than read back off the model's own term.
    dur_ms = board[4]["match_minutes"] * MIN_MS
    assert _court_imbalance(outcome.assignments, board[1], dur_ms) == PROBE_OPTIMAL_IMBALANCE_MS, detail


def test_tier_completed_debug_event_fires_once_per_tier():
    """`run_tier_chain` had no logging at all before this — the only visibility
    into a WITHIN-CHAIN timing breakdown was `main.py`'s one-line-per-solve
    summary, which reports the WHOLE solve's elapsed time and cannot separate
    "T1 took the whole budget" from "T3 did". DEBUG, not INFO, for the same
    reason as `model.py`'s `model_built` event: this fires inside the hot tier
    loop and `PLACEMENT_LOG_LEVEL` defaults to INFO in production.

    Uses `imbalance_probe_board()` (proves every tier in ~20ms) rather than
    the module-scoped `chain`/`board` fixtures — those back a real multi-second
    six-solve walk on the production board, and this test only needs to know
    an event fires per tier, not what the production numbers are.
    """
    from placement_bench_boards import imbalance_probe_board

    _raw_probe_board = imbalance_probe_board()
    board = (*to_positional(_raw_probe_board), rule_groups_for(_raw_probe_board))
    model = _model_for(board)

    old_wrapper_class = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        with structlog.testing.capture_logs() as captured:
            outcome = run_tier_chain(model, model.fixture_vars, wall_seconds=5.0)
    finally:
        structlog.configure(wrapper_class=old_wrapper_class)

    events = [entry for entry in captured if entry["event"] == "tier_completed"]
    assert len(events) == outcome.tiers_completed == TIER_COUNT, captured
    assert [e["tier"] for e in events] == list(TIER_ORDER)
    for entry in events:
        assert entry["status"] in ("OPTIMAL", "FEASIBLE")
        assert isinstance(entry["achieved"], int)
        assert entry["elapsed_seconds_total"] >= 0


# --- degenerate arguments must fail loudly, not solve quietly ---------------
#
# Same family as test_model.py's `match_minutes` / day-cap guards, and the same
# root cause: proto3 scalars are non-optional, so an unset field arrives as 0
# and is indistinguishable from a deliberate one.


def test_rejects_zero_wall_seconds(board):
    """`wall_seconds=0` stops the chain before T0 runs and returns UNKNOWN with
    no assignments and 0 tiers — byte-identical to what a genuinely impossible
    board returns. A dropped `SolveBuildRequest.wall_seconds` would therefore
    be reported to the caller as a solver verdict about their request."""
    model = _model_for(board)
    with pytest.raises(ValueError, match="wall_seconds"):
        run_tier_chain(model, model.fixture_vars, wall_seconds=0)


def test_rejects_negative_wall_seconds(board):
    model = _model_for(board)
    with pytest.raises(ValueError, match="wall_seconds"):
        run_tier_chain(model, model.fixture_vars, wall_seconds=-1.0)


def test_rejects_a_tier_sequence_that_is_not_a_prefix_of_the_ladder(board):
    """Skipping or reordering rungs is not a smaller chain, it is a wrong one.

    `tiers=("imbalance",)` balances courts across a board whose placement
    was never maximised and whose day span was never bounded — and, before this
    guard, reported `status="OPTIMAL"` for it, because every requested tier had
    indeed been proved. `SolveStatus` is what Task 4/5 map onto the wire, so
    that is a lie with a straight route to a caller.
    """
    for bad in (
        (TIER_IMBALANCE,),
        (TIER_DAYS, TIER_PLACED),
        (TIER_PLACED, TIER_IDLE_GAP),
        # A REORDERING of the T1 rungs — the case the day-aware chain added,
        # and the one that matters most: `day_start` above `day_span` anchors a
        # block that has not been tightened yet, which is a different objective
        # wearing the same six names.
        (TIER_PLACED, TIER_DAYS, TIER_DAY_START, TIER_DAY_SPAN),
        (TIER_PLACED, TIER_IMBALANCE, TIER_DAYS, TIER_IDLE_GAP),
    ):
        model = _model_for(board)
        with pytest.raises(ValueError, match="prefix"):
            run_tier_chain(model, model.fixture_vars, CHAIN_WALL_SECONDS, tiers=bad)


def test_rejects_an_empty_tier_sequence(board):
    """`tiers=()` is a prefix of the ladder and passes every other guard, and
    it produces a result that cannot be told from a board nobody could solve:

        status='UNKNOWN', tiers_completed=0, assignments=[], objective_values=[]

    byte-identical to what a genuinely unsolvable board returns. It also walks
    into `_chain_status`'s equality — `tiers_completed == tiers_requested` is
    `0 == 0` — so any future change that let a board through alongside it would
    report OPTIMAL for a chain that optimised nothing.

    Rejecting is the ruling rather than defaulting to the full ladder: the
    default argument already IS the full ladder, so an empty sequence can only
    arrive from a caller that computed it, and quietly substituting the whole
    ladder for the zero they asked for would hide the bug that computed it.

    Cannot arrive from the wire — `SolveBuildRequest` carries no tier list, by
    design — so this is a domain contract check on a domain caller, exactly
    like the two guards beside it.
    """
    model = _model_for(board)
    with pytest.raises(ValueError, match="tiers must not be empty"):
        run_tier_chain(model, model.fixture_vars, CHAIN_WALL_SECONDS, tiers=())


def test_every_prefix_of_the_ladder_is_accepted(board):
    """The guard must not break the one subset that is actually used — the
    bench's `("placed",)` isolation run — nor any other honest prefix. Asserts
    acceptance only; the solving is covered above."""
    for k in range(1, len(TIER_ORDER) + 1):
        model = _model_for(board)
        outcome = run_tier_chain(model, model.fixture_vars, 0.05, tiers=TIER_ORDER[:k])
        assert outcome.tiers_completed <= k


def test_chain_status_describes_the_board_not_the_last_solve():
    """The status decision table, pinned directly.

    The case that motivated it — three tiers proved, the fourth cut off with
    nothing of its own, so the LAST solve says UNKNOWN while a complete board
    sits in hand — only reproduces on a machine loaded enough to starve T3, so
    the end-to-end test above cannot pin it on a quiet box. The rule itself is
    a pure function, and this is deterministic coverage of it.
    """
    from placement.objective import _chain_status

    # Opaque to `_chain_status` -- it only checks `if not assignments`, so the
    # exact shape does not matter. (fixture_index, court_index, start_at_ms).
    board = [(0, 0, 0)]

    # Every requested tier proved.
    assert _chain_status(board, 4, 4, "UNKNOWN") == "OPTIMAL"
    # A T0-only chain that proved T0 is complete on its own terms.
    assert _chain_status(board, 1, 1, "UNKNOWN") == "OPTIMAL"

    # A board in hand but the ladder unfinished — NOT the last solve's UNKNOWN.
    assert _chain_status(board, 3, 4, "UNKNOWN") == "FEASIBLE"
    assert _chain_status(board, 0, 4, "FEASIBLE") == "FEASIBLE"

    # No board: the solver's own verdict, verbatim. INFEASIBLE is a real answer
    # about the request and must never be flattened into UNKNOWN.
    assert _chain_status([], 0, 4, "INFEASIBLE") == "INFEASIBLE"
    assert _chain_status([], 0, 4, "UNKNOWN") == "UNKNOWN"
    assert _chain_status([], 0, 4, "MODEL_INVALID") == "MODEL_INVALID"


# --- independent metric re-computation (see the module docstring) -----------


def _days_used(assignments, day_of_start):
    """How many distinct calendar days the returned board touches.

    `day_of_start` maps an admissible start to its caller-supplied day index —
    the same mapping `build_model` builds section 9 from, rebuilt here from the
    board's own slot list rather than read off the model, per this module's
    "recompute independently" stance.
    """
    return len({day_of_start[start] for _fi, _court, start in assignments})


def _day_span(assignments, day_of_start, dur_ms):
    """Summed per-day span: for each day the board touches, its last match's
    end minus its first match's start."""
    by_day: dict[int, list[int]] = {}
    for _fi, _court, start in assignments:
        by_day.setdefault(day_of_start[start], []).append(start)
    return sum(max(starts) + dur_ms - min(starts) for starts in by_day.values())


def _day_start_offset(assignments, day_of_start, day_open_of):
    """Summed distance from each used day's first admissible tick to the first
    match actually played on it."""
    by_day: dict[int, list[int]] = {}
    for _fi, _court, start in assignments:
        by_day.setdefault(day_of_start[start], []).append(start)
    return sum(min(starts) - day_open_of[day] for day, starts in by_day.items())


def _court_imbalance(assignments, num_courts, dur_ms):
    """Busiest configured-or-used court minus the quietest. A configured court
    nobody plays on counts as a zero — build.ts:2084-2134's rule, which is why
    the dict is seeded from `range(num_courts)` rather than from the
    placements."""
    load = {c: 0 for c in range(num_courts)}
    for _fi, court, _start in assignments:
        load[court] = load.get(court, 0) + dur_ms
    if not load:
        return 0
    return max(load.values()) - min(load.values())


def _worst_consecutive_gap(assignments, entrants_of, dur_ms):
    """Largest wait between CONSECUTIVE matches, per participant with two or
    more — `boardMetrics.worstIdleGapMinutes` in ms. `entrants_of` is indexed
    by fixture POSITION now (a list), not by a fixture id (a dict)."""
    by_entrant: dict[int, list[int]] = {}
    for fi, _court, start in assignments:
        for entrant in entrants_of[fi]:
            by_entrant.setdefault(entrant, []).append(start)
    worst = 0
    for starts in by_entrant.values():
        starts.sort()
        for earlier, later in zip(starts, starts[1:]):
            worst = max(worst, later - earlier - dur_ms)
    return worst
