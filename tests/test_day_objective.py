"""The day-aware T1 rungs — `days`, `day_span`, `day_start`.

Design of record: `docs/superpowers/specs/2026-08-11-t1-day-aware-objective-design.md`
(#512, the first two rungs) and `docs/superpowers/specs/2026-08-12-t1-day-start-rung-design.md`
(the third). Together they replace the single whole-board `makespan` term:

    placed -> days -> day_span -> day_start -> idle_gap -> imbalance

WHY THESE BOARDS LOOK OVER-BUILT. Every board below is constructed so its two
arms are STRICTLY ordered — the rung under test has a different optimum from
the chain without it, not merely a different tie-break. That is not stylistic.
CP-SAT is nondeterministic, so a board whose arms tie passes roughly half the
time with the rung reverted, and `test_pins_join_the_makespan_span_so_the_
solver_places_near_them` in `test_model.py` records the same lesson from #511.

The one that is easy to get wrong is `day_start`. Every other term in this
chain is TRANSLATION-INVARIANT within a day — slide a day's whole block later
and `days`, `day_span` and `imbalance` do not move — so a board that merely
"could" start late produces a tie, and the empty-morning symptom the rung
exists to kill would be a coin flip rather than a red. `idle_gap` is the one
rung that is NOT translation-invariant across days, and it points the wrong
way: it pulls the earlier day's match LATE to sit closer to the next day's.
So the failing-without board below is built on exactly that tension, which is
also the real mechanism behind the reported symptom.
"""

from placement.model import DAY_MS, MIN_MS, build_model, solve
from placement.objective import (
    TIER_COUNT,
    TIER_DAY_SPAN,
    TIER_DAY_START,
    TIER_DAYS,
    TIER_IDLE_GAP,
    TIER_IMBALANCE,
    TIER_ORDER,
    TIER_PLACED,
)

#: An arbitrary positive ms epoch. Positive matters: the negative-span defect
#: #512 §4.2 guards against published an epoch-shaped NEGATIVE number, so a
#: board anchored at 0 would hide it.
_ANCHOR = 1_800_000_000_000

_MATCH_MINUTES = 30
_DUR_MS = _MATCH_MINUTES * MIN_MS
_STEP_MS = 30 * MIN_MS

_CONSTRAINTS = {"match_minutes": _MATCH_MINUTES, "gap_minutes": 0}

_WALL_SECONDS = 10.0


def _reported(outcome):
    return dict(outcome.objective_values)


def _starts(outcome):
    return sorted(start for _fixture, _court, start in outcome.assignments)


# --- T1a: `days` -------------------------------------------------------------


def test_the_days_rung_packs_a_loose_board_into_fewer_days():
    """A board NOT at the day-cap floor collapses onto one day.

    Strictly ordered: two fixtures, two candidate days, no cap. One day is
    reachable and two is not an equal-scoring alternative, so `days == 1` is a
    proved optimum rather than a tie-break — `2 > 1` under any solver.

    #512 §2 is why this test uses a synthetic board and not the production
    one: `production_board()` is AT its cap floor (37 fixtures, cap 7, six days
    needed and six used), so the `days` rung has nothing to win there and a
    test phrased against it would assert the floor, not the rung.
    """
    grid_slots = [
        (0, _ANCHOR, 0),
        (0, _ANCHOR + _STEP_MS, 0),
        (0, _ANCHOR + DAY_MS, 1),
    ]
    fixtures = [([0, 1], 0), ([2, 3], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"

    assert len(outcome.assignments) == 2, detail
    assert _reported(outcome)[TIER_DAYS] == 1, detail
    assert _starts(outcome) == [_ANCHOR, _ANCHOR + _STEP_MS], detail


# --- T1b: `day_span` ---------------------------------------------------------


def test_the_day_span_rung_tightens_a_day_without_changing_the_day_count():
    """The T1b case, isolated from T1a (#512 §8 test 2).

    One day, so `days` is 1 whatever happens and cannot be the thing under
    test. Three ticks with a hole in the middle: the tight pair spans
    `step + dur`, the loose pair `5 * step + dur`. Strictly ordered.
    """
    grid_slots = [
        (0, _ANCHOR, 0),
        (0, _ANCHOR + _STEP_MS, 0),
        (0, _ANCHOR + 5 * _STEP_MS, 0),
    ]
    fixtures = [([0, 1], 0), ([2, 3], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert len(outcome.assignments) == 2, detail
    assert reported[TIER_DAYS] == 1, detail
    assert reported[TIER_DAY_SPAN] == _STEP_MS + _DUR_MS, detail
    assert _starts(outcome) == [_ANCHOR, _ANCHOR + _STEP_MS], detail


# --- T1c: `day_start` — the C2 acceptance board ------------------------------


def test_day_start_anchors_each_day_to_its_own_first_slot():
    """THE LOAD-BEARING TEST for the `day_start` rung. Failing-without.

    Board: two days, one court, three ticks each, a rule group capping ONE
    fixture per day, and two fixtures SHARING entrant 0.

    The cap forces exactly one fixture per day, so `placed`, `days` (2) and
    `day_span` (one lone match per day, `dur` each) are identical in every
    arm — all three rungs above `day_start` are ties by construction, which is
    what isolates the rung under test.

    `idle_gap` is not a tie, and it points the WRONG way. Entrant 0's idle gap
    is `s1 - s0 - dur`, minimised by dragging day 0's match as LATE as the day
    allows and day 1's as early as it allows:

        without `day_start`   s0 = ANCHOR + 2*step   (day 0's LAST tick)
                              idle_gap = DAY_MS - 2*step - dur
        with `day_start`      s0 = ANCHOR            (day 0's FIRST tick)
                              idle_gap = DAY_MS - dur          <- 2 steps WORSE

    So the empty first morning is not a tie here, it is the strict optimum of
    the chain without this rung — the same shape as the staging board in
    #512 §1.1, where the day's whole block sat at 14:30 against a window that
    opened at 10:30. The rung outranks `idle_gap` and overrides it.

    The `idle_gap` assertion below is the important one and is deliberately an
    EQUALITY on the WORSE value: it fails both if the rung is missing (the gap
    comes back 2 steps better, having been bought with the empty morning) and
    if the rung were placed BELOW `idle_gap` in the chain. A one-sided
    assertion, or asserting only the two start instants, would pin the first
    of those and not the second.
    """
    grid_slots = [
        (0, _ANCHOR + k * _STEP_MS, 0) for k in range(3)
    ] + [(0, _ANCHOR + DAY_MS + k * _STEP_MS, 1) for k in range(3)]
    fixtures = [([0, 1], 0), ([0, 2], 0)]
    # One group over both fixtures, no rest, one fixture per day.
    rule_groups = [([0, 1], 0, 1)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], [], rule_groups=rule_groups),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert len(outcome.assignments) == 2, detail
    assert reported[TIER_DAYS] == 2, detail
    assert reported[TIER_DAY_SPAN] == 2 * _DUR_MS, detail

    # The rung itself: every used day starts on its own first admissible tick.
    assert reported[TIER_DAY_START] == 0, detail
    assert _starts(outcome) == [_ANCHOR, _ANCHOR + DAY_MS], detail

    # ...bought at `idle_gap`'s expense, which is what "ranks above it" means.
    assert reported[TIER_IDLE_GAP] == DAY_MS - _DUR_MS, detail


def test_day_start_is_reported_as_the_real_offset_when_a_day_cannot_start_early():
    """The rung reports a TRUE, NON-ZERO offset — not a constant 0.

    THIS TEST IS WHY THE ZERO ASSERTIONS ELSEWHERE ARE SAFE, so it has to be
    the one board in the suite where the honest answer is not 0. `day_lo[d]` is
    bounded from above by the day's starts and from below only by its own
    domain floor, so a rung reading it would report 0 for every board if it had
    collapsed onto that floor, and every other `day_start` assertion here and
    in `test_objective.py` is `== 0`. Those would all still pass against a term
    that had stopped working. This one would not.

    An earlier revision of this test did NOT do that job, and the way it failed
    is worth keeping: it pinned day 0's only court at the first two ticks and
    claimed the answer was `2 * step`. It is 0. A pin is an OCCUPANT of its own
    day (#512 §6a) and pulls `day_lo` down to itself, so a pin sitting on the
    open tick anchors the day there however late the movable lands. The
    assertion said 0 and the docstring said `2 * step`; only the docstring was
    wrong, and a reader would have taken the test for coverage it never had.

    So the day's open tick has to be blocked by something that is NOT an
    occupant of that day. An OFF-LATTICE pin is exactly that, and it is a real
    board rather than a contrivance: an organiser's manual match, running from
    ten minutes before the window opens, is on no admissible tick, belongs to
    no day (`_day_of_pin` → `None`, #512 §6b), and therefore pulls nothing —
    while its court interval still overlaps the day's first tick and keeps the
    movable off it.

    Expected, checkable by eye: the pin occupies `[A - 10min, A + 20min)`, so
    tick `A` is unusable and tick `A + step` is the earliest the movable can
    take. `day_lo` is therefore `A + step` against a `day_open` of `A`, and the
    rung must report exactly one `step`.
    """
    grid_slots = [(0, _ANCHOR + k * _STEP_MS, 0) for k in range(3)]
    fixtures = [([0, 1], 0)]
    # Ten minutes before the day's first admissible tick: on no tick, so on no
    # day — but its court interval still covers tick 0.
    existing = [(0, _ANCHOR - 10 * MIN_MS)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, []),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert _starts(outcome) == [_ANCHOR + _STEP_MS], detail
    assert reported[TIER_DAY_START] == _STEP_MS, (
        f"the rung must report the day's REAL offset from its open tick, not its "
        f"domain floor; got {outcome.objective_values}"
    )
    # The off-lattice pin is in no day, so the span is the movable's alone.
    assert reported[TIER_DAY_SPAN] == _DUR_MS, detail
    assert reported[TIER_DAYS] == 1, detail


# --- pins (#512 §6) ----------------------------------------------------------


def test_a_day_holding_only_pins_is_not_counted_as_free():
    """#512 §6a. A day whose only occupant is a pin must still count as used.

    Without the pin-forcing bounds, `day_used[0]` is never driven by anything
    (`on_day` covers MOVABLE fixtures only), the `days` rung "saves" a day
    that is in fact occupied, and the organiser is shown matches on a day the
    objective believes is empty.

    Strictly ordered in both arms: the lone movable has exactly one tick
    available on day 1, so `days` is 1 without the fix and 2 with it. No tie,
    no solver luck.
    """
    grid_slots = [
        (0, _ANCHOR, 0),  # day 0 — the pin's own instant, its only tick
        (0, _ANCHOR + DAY_MS, 1),  # day 1 — the movable's only tick
    ]
    fixtures = [([0, 1], 0)]
    existing = [(0, _ANCHOR)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, []),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert _starts(outcome) == [_ANCHOR + DAY_MS], detail
    assert reported[TIER_DAYS] == 2, (
        f"the pinned day must count as used, got {outcome.objective_values}"
    )
    # Both days carry exactly one match, so each spans one duration.
    assert reported[TIER_DAY_SPAN] == 2 * _DUR_MS, detail
    assert reported[TIER_DAY_START] == 0, detail


def test_an_off_lattice_pin_belongs_to_no_day_at_all():
    """#512 §6b, resolved as option (1): ACCEPT and document.

    A pin between one day's last admissible tick and the next day's first
    belongs to no day — `_day_of_pin` returns `None` by deliberate design, and
    C4 already treats such a pin as counting against no day cap. Under a
    PER-DAY objective that has a consequence the whole-board span did not
    have: the pin enters neither `days` nor any day's span or start offset, so
    the objective cannot see it at all.

    Option (2) (attribute it to the nearest day for span purposes) was
    rejected because it introduces a SECOND notion of which day a pin is on,
    which is the fork this subsystem keeps being bitten by; option (3) (keep a
    board-level span floor) reintroduces the darkness-measuring term the whole
    change exists to remove. This test exists so a future reader finds a
    decision rather than an accident.

    If this ever needs to change, the place to change it is `_day_of_pin`'s
    contract — for the cap and the objective together, never for one of them.
    """
    off_lattice = _ANCHOR + DAY_MS // 2  # between day 0's tick and day 1's
    grid_slots = [
        (0, _ANCHOR, 0),
        (0, _ANCHOR + DAY_MS, 1),
    ]
    fixtures = [([0, 1], 0)]
    existing = [(0, off_lattice)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, []),
        wall_seconds=_WALL_SECONDS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert len(outcome.assignments) == 1, detail
    assert reported[TIER_DAYS] == 1, (
        "an off-lattice pin counts against no day, so only the movable's day is used; "
        f"got {outcome.objective_values}"
    )
    assert reported[TIER_DAY_SPAN] == _DUR_MS, detail


# --- the empty-day clamp (#512 §4.2) -----------------------------------------


def test_an_unplaceable_board_reports_no_negative_day_term():
    """#512 §4.2. The negative-epoch trap, multiplied by the number of days.

    The whole-board term this chain replaces shipped
    `('makespan', -1767258600000)` as a proved optimum on a board where
    nothing was placed: both ends floated over `[0, max_end]` and a MINIMISING
    tier drove the high end to 0 and the low end to `max_end`. Per-day
    variables are the same trap once per day, and `day_start` adds a second
    copy of it — `day_lo[d] - day_open[d]` is a subtraction against a constant
    epoch, so an unconstrained `day_lo` publishes a number of that shape too.

    Self-dependency makes the sole fixture unplaceable, which is the cheapest
    way to reach the state (and is DELIBERATELY still accepted — an
    unplaceable fixture shows up as a lower `placed`, which is visible).
    """
    grid_slots = [(0, _ANCHOR, 0), (0, _ANCHOR + _STEP_MS, 0)]
    fixtures = [([0, 1], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], [(0, 0)]),
        wall_seconds=_WALL_SECONDS,
    )
    reported = _reported(outcome)

    assert outcome.assignments == [], "the self-dependency should make fixture 0 unplaceable"
    assert reported[TIER_PLACED] == 0
    for tier in (TIER_DAYS, TIER_DAY_SPAN, TIER_DAY_START):
        assert reported[tier] == 0, (
            f"an empty board uses no days at all, so every day term is 0; "
            f"{tier} came back {reported[tier]} in {outcome.objective_values}"
        )


# --- the wire (C2 acceptance) ------------------------------------------------


def test_the_chain_reports_six_tiers_with_day_start_in_position_four():
    """The wire contract the C2 brief names: `objective_values` carries
    `day_start` at position 4, and `tiers_completed` reflects a 6-rung chain.

    Position is asserted by INDEX, not by membership. The chain order IS the
    product ruling (`day_start` below `day_span` so the anchor acts on an
    already-tight block, above `idle_gap` so the day's interior is arranged
    only after the block is anchored), and a set-membership assertion is
    satisfied by any permutation of it.
    """
    grid_slots = [
        (0, _ANCHOR, 0),
        (0, _ANCHOR + _STEP_MS, 0),
        (0, _ANCHOR + DAY_MS, 1),
    ]
    fixtures = [([0, 1], 0), ([2, 3], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
        wall_seconds=_WALL_SECONDS,
    )
    names = [name for name, _ in outcome.objective_values]

    assert outcome.tiers_completed == TIER_COUNT == 6, outcome.objective_values
    assert names == [
        TIER_PLACED,
        TIER_DAYS,
        TIER_DAY_SPAN,
        TIER_DAY_START,
        TIER_IDLE_GAP,
        TIER_IMBALANCE,
    ]
    assert names == list(TIER_ORDER)
    assert names[3] == "day_start"
