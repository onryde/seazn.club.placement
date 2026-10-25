"""The T0->T5 lexicographic objective chain.

T0 max-placed -> T1a days used -> T1b summed per-day span -> T1c summed per-day
start offset -> T2 worst idle gap -> T3 court imbalance, in that fixed order.

T1 was ONE rung (whole-board `makespan`) until 2026-08-13; see `TIER_ORDER`
below and `placement.model`'s T1 section for why a whole-board span on a
multi-day board is mostly measuring the hours nobody can play in.

The order is a protocol constant shared with the TS side, not wire data (design spec, "Tier/objective semantics are a fixed protocol
constant"), and it encodes a product ruling: `placed` dominates absolutely.
A shorter day, a tighter idle gap or a flatter court load must NEVER be
bought by dropping a match an organiser asked to fit. `build.ts:1577-1585`
records that the trade was re-affirmed against measurement, not chosen in
ignorance of it — the sweep showed the lower three metrics getting WORSE when
one more match fits, and that is the tiers working, not a defect.

This is a port of `bench/placement_bench.py`'s `run_full_chain`, which is itself
the sequential-solve port of z3's bound walk in `build.ts:1602-1672`.

--- the mechanism, and how it differs from both of its ancestors ------------

z3 (build.ts) walks each tier DOWNWARD: `push()`, assert `metric <= best-1`,
`check()`, `pop()`, repeat until UNSAT proves `best` optimal, then assert
`metric <= best` at the top level so every later tier inherits it. CP-SAT has
native optimisation, so one `Minimize` + one `Solve` replaces the whole walk
and returns the proved optimum directly.

That collapses the push/pop question rather than answering it. `push`/`pop`
exist in the z3 version because the walk asserts bounds it intends to RETRACT
(`best-1` is a question, and an UNSAT answer must not stay in the solver).
Nothing here is ever retracted: the only assertion this module adds is a
tier's ACHIEVED value, which build.ts adds at the top level too, outside every
push. So the accumulated bounds are monotone and one `CpModel` carries them
all — `model.Add(...)` here is exactly `build.ts:1670`'s
`tier.atMost(tier.of(incumbentMetrics))`, and it has no `pop` for the same
reason that line does not.

Differing from the BENCH: `run_full_chain` REBUILDS the model per tier and
passes the previous tiers' bounds into `build_model` as `placed_floor` /
`makespan_ceiling` / `idlegap_ceiling` constructor arguments. That is a
benchmark measuring build cost per tier; there is no reason to pay it four
times in the service. One model is built once (`placement.model.build_model`,
which deliberately sets NO objective for this reason) and this module drives
it through the chain's objectives one at a time. `model.Maximize`/`Minimize`
REPLACES the objective rather than adding to it, so the tiers do not blend — verified, not assumed:
`test_full_chain_places_exactly_what_t0_alone_places` fails if they do.

--- what `tiers_completed` means, and why FEASIBLE does not count -----------

`SolveBuildResponse.tiers_completed` is specified as "same semantics as
`BuildResult.tiersCompleted`" (design spec). On the TS side that counter is
incremented ONLY for a tier that settled — one that ran to a verdict rather
than to the budget (`build.ts:1669-1671`: `if (!settled) break;`). An
`unknown` verdict explicitly does not count; the header is emphatic that the
absence of a proof must never be read as one.

CP-SAT's equivalent of "settled" is `OPTIMAL`. `FEASIBLE` means the time limit
fired before the solver could prove optimality — it is the `unknown` case
wearing a friendlier name, because CP-SAT hands back its incumbent instead of
nothing. So:

  * a tier counts only when it returns OPTIMAL, and
  * the first non-OPTIMAL tier ENDS the chain, exactly as `!settled` does.

Under-reporting is safe (TS runs LNS it did not strictly need); over-reporting
is not — `tiersCompleted === TIER_COUNT` is the sole gate on TS's
`already_optimal` claim (`build.ts:1863`) and on skipping LNS entirely.

--- why a cut-short tier's board is ADOPTED rather than discarded -----------

REVERSED 2026-08-10, by measurement. This module originally discarded it, and
that decision was reasoned rather than measured. What the reasoning missed is
recorded below, because the argument still LOOKS right.

The old argument: a FEASIBLE board satisfies every frozen bound, so it is no
worse on any EARLIER tier's metric — but its own tier's metric may be worse
than the board already in hand, and there is no honest way to check from here,
because the T1 term and `worst_gap` are ONE-SIDED. (The T1 term was
whole-board `makespan` when this was written — `mk_lo <= start[i]` and
`mk_hi >= start[i] + dur`, squeezed onto the true extremes only because
something minimised the difference. It is three per-day rungs now, and each is
one-sided in exactly the same way; see `_TIER_SPECS`.) `worst_gap`'s per-pair
variables are likewise only bounded from below. Each reads true ONLY for the
tier optimising it; anywhere else `solver.Value()` returns arbitrary slack.
(That part is still true and still matters — the bench records all its
numbers on every tier row and some of them are meaningless. `placed` and,
since the T3 encoding became a max/min EQUALITY, `imbalance` read true
everywhere.) Re-deriving the other two in Python would be a second
implementation of what the model already states, which is the placer/verifier
fork this repo keeps getting bitten by.

What that reasoning never established is that the risk it guards against
actually occurs — and it does not. Measured over 6 runs per arm on two boards,
comparing the incumbent against the adopted board WITHIN one run (the only
comparison that means anything; two arms are two different solves): 0
regressions. What the old rule cost, every time, was the better board. True
span read off the assignments: 130 800 000 -> 79 200 000 (-39%) on a 2-day
board, -24% on a 3-day, -22% on an 8-day, 1 557 600 000 -> 1 519 800 000 on
the bench production board.

That cost is not occasional. On a real multi-day board T1 essentially never
proved — the model handed CP-SAT no counting relation between the span and how
many fixtures must fit it, so the dual bound started at 0 and had to be walked
up by branching. A 2-day board of 304 variables and 575 constraints did not
close in 15 s, and neither did a hand-fed floor within 4% of the optimum. So
"the tier was cut short" was the NORMAL path for T1, and discarding its board
meant an organiser reliably received T0's.

That diagnosis outlived the term it was about, and 2026-08-13 acted on it: the
day rungs carry an explicit per-day floor (`placement.model`'s T1 section)
precisely so the counting relation EXISTS, and with it `day_span` proves in
~1 s where it had not proved in 180 s. `idle_gap` still has no such floor and
is now the rung that eats the wall — the adopt-don't-discard rule below is
what keeps its board.

Adopting is sound for the reason the old argument half-stated: every earlier
bound is a hard constraint in this model, so the adopted board is proved on
those metrics exactly as the incumbent was. The one unproved thing about it is
that its own metric is minimal — which `tiers_completed` not counting it says
already, and which `schema.py`'s `objective_values[:tiers_completed]` slice
keeps off the wire.

A solution hint was the obvious way to make the guarantee airtight rather than
merely observed, and it was measured too, over the same 6 runs per arm. It is
NOT used: the regression count was 0 either way, and hinting cost search
quality — one hinted run returned 80 400 000 where six unhinted runs returned
79 200 000, and under load the hinted arm proved fewer tiers. Note also that a
hint over `placed` and `start` alone is INCOMPLETE ("37 out of 187 non fixed
variables hinted"); the court booleans are needed too, so the cheap version of
this idea does not even carry the guarantee it appears to.

T0 still adopts whatever the status, for its own separate reason: there is no
incumbent to fall back on. That mirrors TS returning its greedy board with
`tiersCompleted: 0`.

This module is domain logic and imports NOTHING from `placement.generated`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from ortools.sat.python import cp_model

from placement.model import (
    DEFAULT_SOLVER_KNOBS,
    FixtureVars,
    SolveOutcome,
    SolverKnobs,
    extract_assignments,
)

log = structlog.get_logger(__name__)

# --- the protocol constant --------------------------------------------------
#
# Names are snake_case service-side; `build.ts`'s own `Tier.name` values are
# `makespan` / `idleGap` / `imbalance` (and T0 is unnamed there, being handled
# before the tier loop). They travel as `Tier.name` strings on the wire, so
# whichever side reads them has to agree — Prompt 06 owns that comparison.
#
# T3 is `imbalance`, NOT `court_imbalance`. The DDD standard's
# ubiquitous-language rule allows the CASE convention to change at the language
# boundary and nothing else, so `idle_gap` <-> `idleGap` is fine while
# `court_imbalance` <-> `imbalance` is a genuine word mismatch that no amount
# of case-folding reconciles. `build.ts` is the side that cannot move — it is
# shared with the z3 and greedy placers, which have their own tier tables — so
# the service moved.
#
# `makespan` was RETIRED, not renamed, on 2026-08-13 (#512 §5.2). It meant
# whole-board span; the three rungs that replaced it mean something else.
# Reusing the name for `day_span` would have shipped a number whose MEANING
# changed while its name did not — the failure mode this programme keeps
# hitting. A TS consumer keying on `"makespan"` now gets an unknown name and
# fails loudly, which is the point; a same-named number that quietly means
# something else is the outcome being avoided.
TIER_PLACED = "placed"
TIER_DAYS = "days"
TIER_DAY_SPAN = "day_span"
TIER_DAY_START = "day_start"
TIER_IDLE_GAP = "idle_gap"
TIER_IMBALANCE = "imbalance"

# The ORDER is the product ruling, not an implementation detail:
#
#   * `days` first of the three — a board that touches fewer calendar days is
#     better before any question about how those days are shaped.
#   * `day_span` next, so the anchor below acts on an already-tight block.
#   * `day_start` below `day_span` and ABOVE `idle_gap`: the day's block is
#     anchored to the day's first slot first, and only then are the day's
#     interior gaps arranged inside it. Reversing those two buys a tighter
#     idle gap with an empty first morning, which is the symptom the rung
#     exists to kill (`tests/test_day_objective.py` asserts the resulting
#     idle_gap as an EQUALITY on the worse value, so the ordering is pinned
#     and not merely the presence of the term).
TIER_ORDER: tuple[str, ...] = (
    TIER_PLACED,
    TIER_DAYS,
    TIER_DAY_SPAN,
    TIER_DAY_START,
    TIER_IDLE_GAP,
    TIER_IMBALANCE,
)
#: `build.ts`'s `TIER_COUNT`. Reaching it means every tier was PROVED optimal.
TIER_COUNT = len(TIER_ORDER)

# A tier gets at least this much wall, and the chain stops rather than start a
# tier with less left. Same floor the bench uses, and the same one
# `CpSolver.max_time_in_seconds` is clamped to: a solve given ~0 s returns
# UNKNOWN after paying the model-copy cost, which is worse than not starting.
MIN_TIER_SECONDS = 0.05


@dataclass(frozen=True)
class _TierSpec:
    """One rung of the ladder. `term` is pulled off `FixtureVars` at solve time
    rather than stored, so the spec table stays a module-level constant."""

    name: str
    maximize: bool
    term: Callable[[FixtureVars], Any]


_TIER_SPECS: dict[str, _TierSpec] = {
    # T0. The only tier that maximises, and the only one whose frozen bound is
    # a FLOOR (`>=`). Also the only exact term of the four: a sum of literals
    # reads true whether or not it is the current objective.
    TIER_PLACED: _TierSpec(TIER_PLACED, maximize=True, term=lambda fv: fv.placed_sum),
    # T1-T5 minimise, and freeze CEILINGS (`<=`) — `Tier.atMost` in build.ts.
    #
    # `day_span` and `day_start` are one-sided in the same way `makespan` and
    # `worst_gap` are (see the module docstring), with one consequence worth
    # stating because the chain is what resolves it. `day_lo[d]` is bounded
    # from above by the day's starts and from below only by its own domain
    # floor, so `day_start` would read as a floor-hugging 0 if it ran on its
    # own. It does not: `day_span`'s freeze is `sum(day_hi - day_lo) <=
    # PROVED_MINIMUM`, and every feasible board's summed true span is `>=` that
    # minimum, so the freeze forces each day's `hi`/`lo` ONTO the true extremes
    # for every rung below it. `day_start` therefore reads a true offset — but
    # only because it sits below a PROVED `day_span`, which is exactly what
    # `tiers_completed` not counting a cut-short tier already guarantees (a
    # tier that did not prove ENDS the chain, so `day_start` never runs on an
    # unfrozen `day_span`).
    TIER_DAYS: _TierSpec(TIER_DAYS, maximize=False, term=lambda fv: fv.days_used),
    TIER_DAY_SPAN: _TierSpec(TIER_DAY_SPAN, maximize=False, term=lambda fv: fv.day_span),
    TIER_DAY_START: _TierSpec(TIER_DAY_START, maximize=False, term=lambda fv: fv.day_start),
    TIER_IDLE_GAP: _TierSpec(TIER_IDLE_GAP, maximize=False, term=lambda fv: fv.worst_gap),
    TIER_IMBALANCE: _TierSpec(TIER_IMBALANCE, maximize=False, term=lambda fv: fv.imbalance),
}


def run_tier_chain(
    model: cp_model.CpModel,
    fixture_vars: FixtureVars,
    wall_seconds: float,
    tiers: Sequence[str] = TIER_ORDER,
    knobs: SolverKnobs = DEFAULT_SOLVER_KNOBS,
) -> SolveOutcome:
    """Drive `model` through the lexicographic tier chain under ONE wall clock.

    Args:
        model: a model from `placement.model.build_model`, with no objective set.
            It is MUTATED — each tier sets an objective on it and adds its
            achieved bound as a constraint. A model that has been through this
            function cannot be re-used for a different chain.
        fixture_vars: the model's decision variables and objective terms.
            Also reachable as `model.fixture_vars`; passed explicitly because
            that is the interface the design fixes, and because it keeps this
            module honest about what it reads.
        wall_seconds: the budget for the WHOLE chain, not per tier. Deliberately
            not sliced per tier — `build.ts:1587-1597` rules that out, because a
            slice makes "which tier ran" a property of the machine, and the same
            request would come back differently optimised on a faster box.
        tiers: the rungs to attempt. Defaults to the whole ladder, and must be a PREFIX
            of `TIER_ORDER` — the bench's `tiers=("placed",)` isolation run is
            the reason this is a parameter at all, and the only subset in use.
        knobs: the CP-SAT search settings every tier in this chain runs under.
            One value for the whole chain, never per tier. Defaults to the
            shipped settings; the service resolves its own from the
            environment via `config.Settings` and passes them down.

    Raises:
        ValueError: on a non-positive `wall_seconds`, an unknown tier name, or
            a `tiers` sequence that is not a prefix of `TIER_ORDER`.

    Returns:
        `SolveOutcome`. `tiers_completed` counts tiers PROVED optimal (see the
        module docstring); `objective_values` carries one `(name, value)` per
        tier whose board was adopted, in tier order; `status` describes the
        board that came back, per `_chain_status`.
    """
    started = time.perf_counter()

    # --- degenerate arguments that would otherwise produce a confidently
    # --- WRONG answer. Same class as `build_model`'s `match_minutes` guard;
    # --- see that module's docstring.
    if float(wall_seconds) <= 0:
        raise ValueError(
            f"wall_seconds must be > 0, got {wall_seconds!r}. `SolveBuildRequest.wall_seconds` is a "
            "plain proto3 double, so UNSET arrives as 0.0 and is indistinguishable from a deliberate "
            "0 — and a 0 budget stops the chain before T0 ever runs, returning UNKNOWN with no "
            "assignments and 0 tiers. That is the same answer a genuinely impossible board gives, so "
            "a dropped field would read as a solver verdict about the request."
        )

    # An empty ladder is a prefix of `TIER_ORDER` and clears every check below,
    # and it returns UNKNOWN / 0 tiers / no assignments — byte-identical to the
    # answer for a board nobody could solve. It also reaches `_chain_status`'s
    # `tiers_completed == tiers_requested` as `0 == 0`. Rejected rather than
    # defaulted to the full ladder: the default argument already IS the full
    # ladder, so an empty sequence can only come from a caller that computed
    # one, and substituting the whole ladder for the zero they asked for would hide
    # whatever computed it.
    if len(tiers) == 0:
        raise ValueError(
            f"tiers must not be empty; expected a non-empty prefix of {list(TIER_ORDER)}. A chain "
            "with no rungs returns UNKNOWN with no assignments and 0 tiers completed, which is "
            "exactly what an unsolvable board returns — the caller cannot tell the two apart."
        )

    unknown = [name for name in tiers if name not in _TIER_SPECS]
    if unknown:
        raise ValueError(f"unknown tier(s) {unknown!r}; expected some ordering of {list(TIER_ORDER)}")

    # The ladder is a PREFIX of `TIER_ORDER` or it is not a ladder. Subsetting
    # or reordering is not a lesser version of the chain, it is a different and
    # silently wrong one: `tiers=("imbalance",)` balances courts across a
    # board whose placement was never maximised, and `_chain_status` would then
    # report OPTIMAL for it — a status Task 4/5 maps straight onto the wire.
    # D3's order is the product ruling this whole module exists to enforce, so
    # it is enforced on the way in too. `("placed",)` — the bench's isolation
    # run, and the only subset anything actually uses — is a prefix and passes.
    #
    # ValueError, not `assert`: `python -O` strips asserts, and this is a
    # contract check on a caller argument exactly like the two above it.
    if tuple(tiers) != TIER_ORDER[: len(tiers)]:
        raise ValueError(
            f"tiers must be a prefix of {list(TIER_ORDER)}, got {list(tiers)}. The tier ORDER is the "
            "product ruling (build.ts's D3: placed dominates absolutely), so a subset that skips or "
            "reorders a rung optimises a metric the tiers above it were never allowed to constrain."
        )

    deadline = started + float(wall_seconds)

    assignments: list[tuple[str, str, int]] = []
    objective_values: list[tuple[str, int]] = []
    tiers_completed = 0
    # Only used when the chain produced NO board at all — see `_chain_status`.
    last_barren_status = "UNKNOWN"

    for spec in (_TIER_SPECS[name] for name in tiers):
        if deadline - time.perf_counter() <= MIN_TIER_SECONDS:
            # Out of budget. Whatever earlier tiers proved still stands.
            break

        term = spec.term(fixture_vars)
        # Replaces the previous tier's objective; it does not add to it. This
        # is what makes the chain lexicographic rather than a blended score.
        if spec.maximize:
            model.Maximize(term)
        else:
            model.Minimize(term)

        solver = _tier_solver(deadline, knobs)
        status = solver.Solve(model)
        last_barren_status = solver.StatusName(status)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # INFEASIBLE cannot legitimately reach here after T0 — every frozen
            # bound is a value some board actually achieved, so the previous
            # tier's solution is still feasible — but if it ever does, the
            # honest move is the same one: keep what was proved, stop.
            break

        achieved = int(solver.Value(term))

        # `elapsed_seconds_total`, not `elapsed_seconds`: this is time since the
        # WHOLE CHAIN started (`started`, above), not this tier's own duration —
        # a per-tier duration is recoverable by diffing consecutive events, but
        # a misleadingly-named cumulative figure read as a per-tier one is
        # exactly the kind of misreading DEBUG-level output invites in an
        # incident, when nobody has time to check the field's own definition.
        log.debug(
            "tier_completed",
            tier=spec.name,
            status=last_barren_status,
            achieved=achieved,
            elapsed_seconds_total=time.perf_counter() - started,
        )

        if status != cp_model.OPTIMAL:
            # Cut short by the clock. ADOPT the board anyway; the tier is not
            # counted and the chain is over.
            #
            # This reverses the original rule, which kept the incumbent unless
            # there was nothing to fall back on. That rule was reasoned, not
            # measured, and the measurement reverses it: the board it threw
            # away is the better board, by 22-39% of the very metric the tier
            # exists to minimise (true span off the assignments — 130 800 000
            # -> 79 200 000 on a 2-day board, 1 557 600 000 -> 1 519 800 000 on
            # the bench production board).
            #
            # Its stated justification — "adopting it would replace a proved
            # board with an unproved one" — conflated two different claims.
            # Every earlier tier's bound is frozen into this model as a HARD
            # CONSTRAINT (the `model.Add` below), so any board CP-SAT returns
            # here satisfies all of them: it is proved on the earlier tiers'
            # metrics exactly as the incumbent was. The single unproved thing
            # about it is that its OWN metric is minimal — which is precisely
            # what `tiers_completed` not counting it already says, and what
            # `schema.py`'s `objective_values[:tiers_completed]` slice already
            # keeps off the wire.
            #
            # Deliberately NOT hinted. Seeding the incumbent as a solution hint
            # was the obvious way to guarantee this board cannot come back
            # worse than the one it replaces, and it was measured over 6 runs
            # per arm on two boards: the guarantee turned out to be unnecessary
            # (0/6 regressions either way, on a within-run comparison) and the
            # hint actively cost search quality — one hinted run returned
            # 80 400 000 where every unhinted run returned 79 200 000, and
            # under load the hinted arm proved FEWER tiers. `model.py`'s
            # docstring is right that a hint supplies nothing but an incumbent;
            # what it costs is the search that would have improved on it.
            assignments = extract_assignments(solver, fixture_vars)
            objective_values.append((spec.name, achieved))
            break

        assignments = extract_assignments(solver, fixture_vars)
        objective_values.append((spec.name, achieved))
        tiers_completed += 1

        # Freeze it. Every later tier optimises subject to this, which is the
        # whole content of "lexicographic": the tiers below cannot spend what
        # the tiers above have already won.
        if spec.maximize:
            model.Add(term >= achieved)
        else:
            model.Add(term <= achieved)

    return SolveOutcome(
        assignments=assignments,
        status=_chain_status(assignments, tiers_completed, len(tiers), last_barren_status),
        tiers_completed=tiers_completed,
        objective_values=objective_values,
        elapsed_ms=int(round((time.perf_counter() - started) * 1000)),
    )


def _chain_status(
    assignments: list[tuple[str, str, int]],
    tiers_completed: int,
    tiers_requested: int,
    last_barren_status: str,
) -> str:
    """CP-SAT's status vocabulary, describing the BOARD BEING RETURNED.

    A chain is not one solve, so "the" status has to be defined rather than
    passed through, and the obvious definition — the verdict of the last solve
    that ran — is wrong in a way that only shows up under load. Observed here:
    T0, T1 and T2 all proved, T3 got the few milliseconds left and returned
    UNKNOWN having found nothing of its own, and the chain reported

        status='UNKNOWN', tiers_completed=3, 37 assignments

    which reads as "no answer" while holding a complete, three-times-proved
    board. `SolveStatus` is the field a caller checks before trusting
    `assignments` at all, so it must describe what came back:

        OPTIMAL      every requested tier PROVED optimal
        FEASIBLE     a board, but the chain did not finish proving it
        otherwise    no board — the last solver verdict passed through verbatim
                     (INFEASIBLE is a real answer about the request and must
                     never be flattened into UNKNOWN)

    `tiers_completed` remains the finer-grained signal; this is the coarse gate.
    """
    if not assignments:
        return last_barren_status
    return "OPTIMAL" if tiers_completed == tiers_requested else "FEASIBLE"


def _tier_solver(deadline: float, knobs: SolverKnobs = DEFAULT_SOLVER_KNOBS) -> cp_model.CpSolver:
    """A solver for one tier, given the CHAIN's shared deadline and the search
    settings the whole chain runs under.

    `knobs` arrives as an argument rather than being read from module state:
    see `SolverKnobs` for why the domain may not read the environment, and for
    what each of the three settings guards against. Read those notes before
    concluding the two presolve knobs are dead weight — no test fails without
    them (they measure ~3x slower on the BENCH board), but the failure they
    guard against is silent.
    """
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(MIN_TIER_SECONDS, deadline - time.perf_counter())
    solver.parameters.num_search_workers = knobs.num_search_workers
    solver.parameters.symmetry_level = knobs.symmetry_level
    solver.parameters.cp_model_probing_level = knobs.cp_model_probing_level
    return solver
