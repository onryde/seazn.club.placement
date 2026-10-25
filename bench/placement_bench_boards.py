#!/usr/bin/env python3
"""Board generation for the Placement scheduling bench, extracted so the bench
sweep and the service's own tests drive the SAME generator instead of two
copies that drift.

Everything above `production_board()` is lifted verbatim out of
`placement_bench.py` — same constants, same dataclasses, same math. Read that
file's module docstring for WHY each of these shapes is what it is (lattice
re-spacing at `gcd(matchMinutes, gapMinutes)`, the `existing`/dependency
families being modeled at all, the `not_after` pin). `placement_bench.py` imports
these names back, so the sweep is unchanged by the move.

`production_board()` is the only new code here: it renders the sweep's
`prod-37x77k` point into the SERVICE's plain-Python argument shape
(`placement.model.build_model`'s parameter list, which mirrors
`proto/scheduler.proto`'s `SolveBuildRequest`). It deliberately does NOT
introduce a second board generator — it calls `build_board` like the sweep
does and only translates the result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

MATCH_MIN = 40
GAP_MIN = 10  # real court-turnaround gap now modeled (was 0 in the first pass)
STEP_MIN = math.gcd(MATCH_MIN, GAP_MIN)  # 10 — the lattice re-spacing, see module docstring.
TICKS_PER_COARSE_SLOT = MATCH_MIN // STEP_MIN  # 4
OPEN_MS = 8 * HOUR_MS
CLOSE_MS = 20 * HOUR_MS
# The SIZING cap (used only by `choose_lattice`, unchanged from the first pass
# so `target_slots` still means what it meant there). The ACTUAL number of
# generated ticks per court-day is `spcd * TICKS_PER_COARSE_SLOT` — see
# `build_board`.
SLOTS_PER_COURT_DAY_CAP = (CLOSE_MS - OPEN_MS) // (MATCH_MIN * MIN_MS)  # 18
DIVISIONS = ["d1", "d2"]

# 2026-01-01T00:00:00Z. Moved off 0 deliberately (Prompt 05b Step 1): with a
# zero epoch, "unset proto3 `start_at_ms`" and "legitimate test timestamp" are
# THE SAME VALUE, which is how a Critical survived four review rounds — an
# `existing` row with an unset start builds its blocking interval at epoch 0,
# overlaps nothing real, and the pin is silently ignored. A zero corpus also
# makes an int64->int32 narrowing of `Assignment.start_at_ms` invisible,
# because no timestamp ever exceeds 2^31.
#
# MUST STAY MIDNIGHT-ALIGNED (`EPOCH_MS % DAY_MS == 0`), and
# `tests/test_bench_contract.py` fails if it ever stops being. This is not
# tidiness: `build_board` opens each session day at `EPOCH_MS + d*DAY_MS +
# OPEN_MS` and runs it for `spcd * TICKS_PER_COARSE_SLOT` ticks, while
# `day_index_of` below buckets the per-division day cap by `start_ms // DAY_MS`
# — a UTC day. An epoch offset from midnight slides the session day across a
# UTC midnight, so ONE session day maps to TWO cap buckets and the cap silently
# doubles. Measured on the production board at 2026-01-01T10:00:00Z
# (1767261600000): 27 buckets instead of 26, and the returned board puts two
# `d1` fixtures on session day 0 and two `d2` on session day 15 against a cap
# of 1 — while `test_model.py`'s day-cap assertion still passes, because it
# re-derives the same `// DAY_MS` bucket. See the Prompt 05b report.
#
# Prompt 05c moved that quotient OUT of `placement.model` (the wire now carries a
# caller-resolved `Slot.day_index`, because a UTC bucket is the wrong one for
# any org away from UTC) and into `day_index_of` in this file. The requirement
# is unchanged and so is every measurement above — the corpus is a UTC corpus
# and still derives its days the same way. Only the OWNER of the derivation
# moved, from the solver to the caller, which is what the corpus now stands in
# for.
EPOCH_MS = 1_767_225_600_000


# --- lattice sizing -----------------------------------------------------


def choose_lattice(courts: int, target_slots: int) -> tuple[int, int]:
    """(days, slots_per_court_day) whose product * courts is closest to
    target_slots, subject to slots_per_court_day <= SLOTS_PER_COURT_DAY_CAP
    and days >= 2. Unchanged from the first pass — GAP_MIN no longer
    re-spaces the lattice (see module docstring), so this sizing logic still
    applies verbatim."""
    best = None
    for days in range(2, max(3, target_slots // courts + 2) + 1):
        spcd = max(1, min(SLOTS_PER_COURT_DAY_CAP, round(target_slots / (courts * days))))
        actual = courts * days * spcd
        err = abs(actual - target_slots)
        if best is None or err < best[0]:
            best = (err, days, spcd, actual)
    assert best is not None
    _, days, spcd, _actual = best
    return days, spcd


# --- board -----------------------------------------------------------------


@dataclass
class Fixture:
    id: str
    round_no: int
    home: str
    away: str
    division: str


@dataclass
class Slot:
    court: str
    start_ms: int

    @property
    def end_ms(self) -> int:
        return self.start_ms + MATCH_MIN * MIN_MS


@dataclass
class Existing:
    """An immovable row (build-encode.ts section 7 / build.ts's `existing`
    param). NOT part of `board.slots` — production removes an existing row's
    OWN court time from the lattice at `buildGrid` time; this bench does not
    bother re-deriving the lattice around it and instead lets section 7's
    per-slot forbid-list do the same job at the constraint layer, which is
    provably equivalent for every movable fixture's legality (it just leaves
    a lattice slot in `board.slots` that no movable fixture can ever actually
    take — inert, not wrong)."""

    id: str
    court: str
    start_ms: int
    end_ms: int
    entrants: list[str]


@dataclass
class Board:
    fixtures: list[Fixture]
    slots: list[Slot]
    days: int
    slots_per_court_day: int
    courts: list[str]
    per_entrant: int
    pool: int
    hard_rest_min: int
    day_cap: int
    not_after_entrants: int
    calendar_days_spanned: int
    dropped_by_blackout: int


def id_of(f: int) -> str:
    return f"f{f:04d}"


def entrant_of(e: int) -> str:
    return f"e{e:04d}"


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open overlap test — the same shape build-grid.ts's `admits()` uses
    for blackouts (calendar.ts's `intervalsOverlap`)."""
    return a_start < b_end and a_end > b_start


LUNCH_BREAK_MIN = (12 * 60, 13 * 60)
COFFEE_BREAK_MIN = (16 * 60, 17 * 60)


def build_board(
    n: int,
    courts_n: int,
    target_slots: int,
    per_entrant: int = 2,
    hard_rest_min: int = 80,
    not_after_entrants: int = 2,
    day_cap_override: int = 0,
    blackouts: bool = False,
    skip_weekends: bool = False,
) -> Board:
    courts = [f"C{i+1}" for i in range(courts_n)]
    days, spcd = choose_lattice(courts_n, target_slots)
    session_ms = spcd * MATCH_MIN * MIN_MS

    pool = max(4, math.ceil((2 * n) / per_entrant))
    per_round = max(1, pool // 2)
    day_cap = day_cap_override if day_cap_override > 0 else max(1, math.ceil(n / len(DIVISIONS) / days))

    fixtures: list[Fixture] = []
    for f in range(n):
        home = entrant_of((2 * f) % pool)
        away = entrant_of((2 * f + 1) % pool)
        division = DIVISIONS[f % len(DIVISIONS)]
        fixtures.append(
            Fixture(id=id_of(f), round_no=f // per_round + 1, home=home, away=away, division=division)
        )

    def day_admits(calendar_day: int) -> bool:
        if not skip_weekends:
            return True
        return (calendar_day % 7) < 5

    def slot_admits(day_open: int, start: int) -> bool:
        if not blackouts:
            return True
        end = start + MATCH_MIN * MIN_MS
        for lo_min, hi_min in (LUNCH_BREAK_MIN, COFFEE_BREAK_MIN):
            b_from = day_open - OPEN_MS + lo_min * MIN_MS
            b_to = day_open - OPEN_MS + hi_min * MIN_MS
            if intervals_overlap(start, end, b_from, b_to):
                return False
        return True

    slots: list[Slot] = []
    dropped_by_blackout = 0
    calendar_day = 0
    session_days_used = 0
    while session_days_used < days:
        if day_admits(calendar_day):
            day_open = EPOCH_MS + calendar_day * DAY_MS + OPEN_MS
            for court in courts:
                # `spcd` sizes the day in matchMinutes-sized chunks (unchanged
                # sizing math from the first pass); each chunk is materialised
                # at the true STEP_MIN tick granularity so the turnaround-gap
                # constraint (section 3) has real 50-minute-multiple starts to
                # choose from instead of being confined to 40-minute ones —
                # see the module docstring's "lattice re-spacing" note.
                for k in range(spcd * TICKS_PER_COARSE_SLOT):
                    start = day_open + k * STEP_MIN * MIN_MS
                    if slot_admits(day_open, start):
                        slots.append(Slot(court=court, start_ms=start))
                    else:
                        dropped_by_blackout += 1
            session_days_used += 1
        calendar_day += 1
    calendar_days_spanned = calendar_day

    slots.sort(key=lambda s: (s.court, s.start_ms))

    return Board(
        fixtures=fixtures,
        slots=slots,
        days=days,
        slots_per_court_day=spcd,
        courts=courts,
        per_entrant=per_entrant,
        pool=pool,
        hard_rest_min=hard_rest_min,
        day_cap=day_cap,
        not_after_entrants=not_after_entrants,
        calendar_days_spanned=calendar_days_spanned,
        dropped_by_blackout=dropped_by_blackout,
    )


def existing_rows_for(board: Board) -> list[Existing]:
    """A handful of pinned/immovable rows (build-encode.ts section 7). Two
    share entrants with the movable pool (exercise the rest-window half of
    section 7); one shares none but sits on a board court (exercises the
    gap-only half in isolation). Entrant ids reused from the low end of the
    pool, which every board hits early and often via the `(2f)%pool` /
    `(2f+1)%pool` round robin, so these actually bind several fixtures."""
    dur_ms = MATCH_MIN * MIN_MS
    day0_open = EPOCH_MS + OPEN_MS
    step_ms = MATCH_MIN * MIN_MS
    court_a = board.courts[0]
    court_b = board.courts[min(1, len(board.courts) - 1)]
    return [
        Existing(
            id="x-rest-a",
            court=court_a,
            start_ms=day0_open,
            end_ms=day0_open + dur_ms,
            entrants=[entrant_of(0), entrant_of(1)],
        ),
        Existing(
            id="x-rest-b",
            court=court_b,
            start_ms=day0_open + 3 * step_ms,
            end_ms=day0_open + 3 * step_ms + dur_ms,
            entrants=[entrant_of(2), entrant_of(3)],
        ),
        Existing(
            id="x-court-only",
            court=court_a,
            start_ms=day0_open + 10 * step_ms,
            end_ms=day0_open + 10 * step_ms + dur_ms,
            entrants=["e-pinned-x", "e-pinned-y"],
        ),
    ]


def dependency_pairs_for(board: Board, k: int = 3) -> list[tuple[str, str]]:
    """A few order-dependency pairs (build-encode.ts section 8): fixture at
    an odd index depends on the fixture immediately before it. Arbitrary but
    deterministic — this is exercising the CONSTRAINT FAMILY's cost, not
    modeling a real group-stage-before-knockout structure.

    Returned as (DEPENDENT, FEEDER) — note this is the reverse of
    `proto/scheduler.proto`'s `OrderPair(before_fixture_id, after_fixture_id)`,
    which is (FEEDER, DEPENDENT). `production_board()` flips them; the bench's
    own `build_model` consumes this order directly."""
    n = len(board.fixtures)
    pairs: list[tuple[str, str]] = []
    for idx in range(k):
        dep_idx = 2 * idx + 1
        feed_idx = 2 * idx
        if dep_idx < n:
            pairs.append((board.fixtures[dep_idx].id, board.fixtures[feed_idx].id))
    return pairs


# --- service-shaped rendering of the sweep's production point ---------------


def day_index_of(start_ms: int) -> int:
    """This corpus's calendar day for a tick, as `Slot.day_index` carries it.

    `placement.model` no longer derives the day-cap bucket from the timestamp —
    production caps are governed by the org's own timezone and the caller sends
    a resolved integer. The corpus is a UTC corpus, so the derivation that used
    to live in the model lives here instead, unchanged: the boards below mean
    exactly the same days they always did, and
    `test_the_corpus_epoch_is_utc_midnight_aligned` still guards it — it is now
    guarding THIS function rather than the solver.
    """
    return start_ms // DAY_MS

# The sweep point this mirrors: SweepPoint("prod-37x77k@8s", n=37, courts=5,
# target_slots=2081, wall_s=8.0). Kept as named constants rather than inlined
# so a change to the sweep point and a change here are visibly the same edit.
PROD_FIXTURES = 37
PROD_COURTS = 5
PROD_TARGET_SLOTS = 2081


def production_board():
    """The sweep's `prod-37x77k` board, rendered into `placement.model.build_model`'s
    plain-Python parameter shape:

        (fixtures, courts, grid_slots, step_minutes, constraints, existing, dependencies)

    where
      fixtures     = [(fixture_id, [entrant_id, ...], division_id), ...]
      courts       = [court, ...]
      grid_slots   = [(court, start_at_ms, day_index), ...]
      step_minutes = int
      constraints  = {match_minutes, gap_minutes, rest_by_division, day_cap_by_division}
      existing     = [(fixture_id, court, start_at_ms), ...]
      dependencies = [(before_fixture_id, after_fixture_id), ...]

    This is exactly what `proto/scheduler.proto`'s `SolveBuildRequest` can
    carry — no more. Two consequences, both deliberate and both worth knowing
    before reading the numbers this board produces:

    * `not_after_entrants=0`, unlike the sweep's default of 2. The sweep pins
      two entrants' fixtures to the first tick (build-encode.ts section 5's
      start-window family). `SolveBuildRequest.Fixture` carries only
      (fixture_id, entrant_ids, division_id) — there is NO per-fixture start
      window on the wire — so a board WITH the pin could not be sent to the
      real service, and a test using one would be testing a code path
      production can never reach. The pin is therefore dropped rather than
      faked.
    * `existing` rows lose their `entrants` list for the same reason: the wire
      type is `Assignment (fixture_id, court, start_at_ms)`. Only section 7's
      court-turnaround half survives the round trip.
    """
    board = build_board(
        n=PROD_FIXTURES,
        courts_n=PROD_COURTS,
        target_slots=PROD_TARGET_SLOTS,
        not_after_entrants=0,
    )
    existing = existing_rows_for(board)
    dependencies = dependency_pairs_for(board)

    fixtures = [(fx.id, [fx.home, fx.away], fx.division) for fx in board.fixtures]
    courts = list(board.courts)
    grid_slots = [(s.court, s.start_ms, day_index_of(s.start_ms)) for s in board.slots]
    constraints = {
        "match_minutes": MATCH_MIN,
        "gap_minutes": GAP_MIN,
        # The sweep applies one hard_rest_min and one day_cap to every
        # division; the wire keys both by division, so fan the single value
        # out rather than inventing per-division numbers the bench never
        # measured.
        "rest_by_division": {d: board.hard_rest_min for d in DIVISIONS},
        "day_cap_by_division": {d: board.day_cap for d in DIVISIONS},
    }
    existing_rows = [(e.id, e.court, e.start_ms) for e in existing]
    # (dependent, feeder) -> (before, after), i.e. proto `OrderPair` order.
    order_pairs = [(feeder, dependent) for dependent, feeder in dependencies]

    return fixtures, courts, grid_slots, STEP_MIN, constraints, existing_rows, order_pairs


# --- purpose-built probe boards ---------------------------------------------
#
# `production_board()` is a realistic board and is BLIND in two specific ways
# that four rounds of review did not surface, both measured:
#
#   * its three pins never contend. 3 pinned rows against 8 320 grid slots and
#     37 movable fixtures means no fixture ever WANTS a pinned (court, tick),
#     so the whole `existing` constraint family (`placement.model`'s pinned-row
#     fold) can be deleted with the suite still green. Adding an assertion to
#     the production board cannot fix that — the family is inert on it, not
#     merely unasserted.
#   * its court-imbalance tier can only be pinned through a multi-second
#     four-tier chain, so a T3 mutation that survives on the merits dies (or
#     does not) by wall clock instead. Measured: the same T3 mutation was green
#     at a 22 s wall and red at 44 s.
#
# Both boards below are tiny and prove all four tiers in milliseconds, so the
# assertions they carry are about the CODE and never about the machine's load.
# They are here rather than inline in `tests/` so there is exactly one place
# that answers "what boards does the service get tested against".

#: `match_minutes` / `gap_minutes` for `pin_contended_board`. Same 40/10 the
#: production board uses, so the pinned-row width is the production width.
PIN_MATCH_MIN = 40
PIN_GAP_MIN = 10
#: The one (court, tick) `pin_contended_board` leaves free, and therefore the
#: number of movable fixtures it can legally place.
PIN_BOARD_FREE_SLOTS = 1
PIN_BOARD_FIXTURES = 3


def pin_contended_board():
    """A board whose pins genuinely CONTEND, in `build_model`'s parameter shape.

    Two courts offer three ticks each, spaced exactly `match + gap` apart so
    consecutive ticks are legal neighbours. Five of those six (court, tick)
    pairs carry an immovable row; the sixth is free. Three movable fixtures
    with pairwise-disjoint entrants and no day cap then compete for it, so the
    ONLY thing standing between them and the pinned slots is the pinned-row
    fold in `placement.model.build_model`.

    Therefore: with that constraint, exactly ONE fixture can be placed, on the
    single free slot. Without it, all three place — two of them on top of a
    match that is already being played, reported `OPTIMAL`. That gap is the
    test's teeth, and it does not exist on `production_board()`.
    """
    dur_ms = PIN_MATCH_MIN * MIN_MS
    width_ms = (PIN_MATCH_MIN + PIN_GAP_MIN) * MIN_MS
    courts = ["C1", "C2"]
    ticks = [EPOCH_MS + OPEN_MS + k * width_ms for k in range(3)]
    grid_slots = [(court, tick, day_index_of(tick)) for court in courts for tick in ticks]

    # Pin everything except (C2, ticks[2]).
    free_slot = (courts[1], ticks[2])
    existing = [
        (f"pin-{court}-{k}", court, tick)
        for court in courts
        for k, tick in enumerate(ticks)
        if (court, tick) != free_slot
    ]

    # Disjoint entrants: nothing here should be decided by participant rest or
    # by an order dependency, so the pinned-row family is measured alone.
    fixtures = [(f"m{i:02d}", [f"pe{2 * i:02d}", f"pe{2 * i + 1:02d}"], "d1") for i in range(PIN_BOARD_FIXTURES)]
    constraints = {
        "match_minutes": PIN_MATCH_MIN,
        "gap_minutes": PIN_GAP_MIN,
        # Rest 0 is legitimate ("no minimum rest") and is deliberately chosen
        # so the participant-rest family cannot mask the pinned-row family.
        "rest_by_division": {"d1": 0},
        # Uncapped: a division absent from the dict is uncapped by contract.
        "day_cap_by_division": {},
    }
    del dur_ms
    return fixtures, courts, grid_slots, PIN_MATCH_MIN + PIN_GAP_MIN, constraints, existing, []


#: `gap_contended_board`'s match/gap and lattice. Deliberately distinct from
#: both the production board (40/10) and `imbalance_probe_board` (25/5), so no
#: count or duration here can collide with a number another test already uses.
GAP_PROBE_MATCH_MIN = 20
GAP_PROBE_GAP_MIN = 10
GAP_PROBE_STEP_MIN = 10
GAP_PROBE_COURTS = 2
GAP_PROBE_TICKS = 13  # 0, 10, ... 120 minutes after the day opens
#: Starts must be >= match+gap = 30 min apart, so one court fits
#: {0, 30, 60, 90, 120} = 5, and two courts fit 10.
GAP_PROBE_CAPACITY = 10
#: Drop the gap and the width becomes match alone = 20 min, so one court fits
#: {0, 20, 40, 60, 80, 100, 120} = 7, and two courts fit 14.
GAP_PROBE_CAPACITY_WITHOUT_GAP = 14
#: Enough fixtures for the ungapped board to reach its full capacity — that
#: headroom is the entire measurement.
GAP_PROBE_FIXTURES = GAP_PROBE_CAPACITY_WITHOUT_GAP


def gap_contended_board():
    """A board where the court TURNAROUND GAP is the binding constraint.

    Two courts, thirteen ticks each at a 10-minute lattice, and more fixtures
    than either version of the board can seat. The gap is then the only thing
    deciding how many fit:

        width = match + gap = 30 min  ->  5 per court, GAP_PROBE_CAPACITY = 10
        width = match       = 20 min  ->  7 per court, ..._WITHOUT_GAP  = 14

    so dropping `gap_ms` from the court interval width changes T0's answer by
    four placements. That is a COUNT, decided by T0 in milliseconds, and it
    does not depend on the wall, on which tiers completed, or on the day cap.

    Why this board has to exist. On `production_board()` the same mutation is
    only visible by luck: with 8 320 slots, 37 fixtures and a day cap of one
    per division per day, almost nothing is ever forced onto the same court
    close together, so a narrower court interval usually changes no placement
    at all. Re-measured across six runs at the production 8 s wall, the mutant
    was caught **2 times in 6** — and the kills correlated with the chain
    NOT completing, i.e. with a less-optimised board, not with a longer solve.
    A guard that fires two times in six is a coin flip that reads as a
    regression in CI, and it was the strongest evidence for the (incorrect)
    claim that a longer wall makes this family more detectable.

    Entrants are pairwise disjoint and rest is 0 so participant rest cannot
    bind; there is no day cap and no dependency. The court gap is measured
    alone.
    """
    dur_gap_ms = (GAP_PROBE_MATCH_MIN + GAP_PROBE_GAP_MIN) * MIN_MS
    del dur_gap_ms
    courts = [f"C{i + 1}" for i in range(GAP_PROBE_COURTS)]
    ticks = [EPOCH_MS + OPEN_MS + k * GAP_PROBE_STEP_MIN * MIN_MS for k in range(GAP_PROBE_TICKS)]
    grid_slots = [(court, tick, day_index_of(tick)) for court in courts for tick in ticks]
    fixtures = [
        (f"g{i:02d}", [f"ge{2 * i:02d}", f"ge{2 * i + 1:02d}"], "d1") for i in range(GAP_PROBE_FIXTURES)
    ]
    constraints = {
        "match_minutes": GAP_PROBE_MATCH_MIN,
        "gap_minutes": GAP_PROBE_GAP_MIN,
        "rest_by_division": {"d1": 0},
        "day_cap_by_division": {},
    }
    return fixtures, courts, grid_slots, GAP_PROBE_STEP_MIN, constraints, [], []


#: `rest_contended_board`'s shape. Match/gap/rest again distinct from every
#: other board here, and the capacities (5 and 7) collide with no other count
#: asserted in the suite.
REST_PROBE_MATCH_MIN = 15
REST_PROBE_GAP_MIN = 0  # legitimate, and keeps court turnaround out of the way
REST_PROBE_REST_MIN = 45
REST_PROBE_STEP_MIN = 15
REST_PROBE_TICKS = 17  # 0, 15, ... 240 minutes after the day opens
#: One entrant plays every fixture, so their rest intervals (match + rest =
#: 60 min) may not overlap: {0, 60, 120, 180, 240} = 5.
REST_PROBE_CAPACITY = 5
#: Enough courts and fixtures that, with participant rest gone, every fixture
#: seats on its own court at any tick.
REST_PROBE_FIXTURES = 7
REST_PROBE_COURTS = 7


def rest_contended_board():
    """A board where PARTICIPANT REST is the binding constraint.

    Seven fixtures all share one entrant, on seven courts, so court
    exclusivity can never bind — every fixture could sit on its own court at
    the same tick. The only thing spacing them out is the per-entrant rest
    interval:

        rest enforced (width match + rest = 60 min)  ->  REST_PROBE_CAPACITY
        rest constraint removed, or rest zeroed      ->  REST_PROBE_FIXTURES

    so deleting the participant-rest `AddNoOverlap`, or zeroing `rest_ms`,
    changes T0's proved count from 5 to 7.

    Why this board has to exist. On `production_board()` the participant-rest
    mutant is caught only most of the time: re-measured over six runs it was
    RED **5 of 6** at the production 8 s wall (and 3 of 3 at a 3 s wall — the
    wall is not the variable). One miss in six is a flake that reads as a
    regression, and it is the same weakness the court-gap family has in a
    milder form: on a board with 8 320 slots and a day cap of one per division
    per day, participants are naturally spread out and a missing rest window
    usually changes no placement.

    Like `gap_contended_board`, this is a T0-only question and is asserted as
    one.
    """
    courts = [f"C{i + 1}" for i in range(REST_PROBE_COURTS)]
    ticks = [EPOCH_MS + OPEN_MS + k * REST_PROBE_STEP_MIN * MIN_MS for k in range(REST_PROBE_TICKS)]
    grid_slots = [(court, tick, day_index_of(tick)) for court in courts for tick in ticks]
    # `re00` is in every fixture; the partner is unique so nothing else couples.
    fixtures = [(f"r{i:02d}", ["re00", f"rp{i:02d}"], "d1") for i in range(REST_PROBE_FIXTURES)]
    constraints = {
        "match_minutes": REST_PROBE_MATCH_MIN,
        "gap_minutes": REST_PROBE_GAP_MIN,
        "rest_by_division": {"d1": REST_PROBE_REST_MIN},
        "day_cap_by_division": {},
    }
    return fixtures, courts, grid_slots, REST_PROBE_STEP_MIN, constraints, [], []


#: `imbalance_probe_board`'s match/gap. Deliberately NOT 40/10: every derived
#: value on this board (durations, makespan, imbalance) must be distinct from
#: the production board's, so a mutant that hardcodes a production number
#: cannot pass here by collision.
PROBE_MATCH_MIN = 25
PROBE_GAP_MIN = 5
PROBE_COURTS = 3
PROBE_FIXTURES = 4
#: The proved T3 optimum on this board: four fixtures over three courts in two
#: rows is (2,1,1), a spread of one match. Maximising instead gives (2,2,0), a
#: spread of two — `PROBE_WORST_IMBALANCE_MS`.
PROBE_OPTIMAL_IMBALANCE_MS = 1 * PROBE_MATCH_MIN * MIN_MS
PROBE_WORST_IMBALANCE_MS = 2 * PROBE_MATCH_MIN * MIN_MS
#: T1b's proved optimum here: two rows, so one tick-width plus one match.
#:
#: Named `MAKESPAN` until 2026-08-13, when the service's whole-board span term
#: was retired for the day-aware rungs. The NUMBER is unchanged and needs no
#: re-baseline — this board's two ticks are on one calendar day, so its summed
#: per-day span and its whole-board span are the same quantity. Renamed anyway,
#: because a constant that keeps a retired term's name is how a stale number
#: gets asserted against a tier that no longer means what it says.
PROBE_OPTIMAL_DAY_SPAN_MS = (PROBE_MATCH_MIN + PROBE_GAP_MIN) * MIN_MS + PROBE_MATCH_MIN * MIN_MS


def imbalance_probe_board():
    """A board that pins T3's optimisation DIRECTION in milliseconds.

    Three courts, two ticks each, four movable fixtures with pairwise-disjoint
    entrants. The shape is chosen so every tier above T3 is forced and leaves
    T3 real slack:

      * T0 places all four (six slots, four fixtures).
      * T1's minimal makespan is two rows — four fixtures cannot share three
        courts in one tick — so both remaining layouts survive its freeze.
      * T2 is trivially 0: disjoint entrants means no participant has two
        matches, so there are no idle-gap pairs at all.
      * T3 then chooses between (2,1,1) — a spread of ONE match — and (2,2,0),
        a spread of TWO. Minimising gives `PROBE_OPTIMAL_IMBALANCE_MS`;
        maximising gives `PROBE_WORST_IMBALANCE_MS`. Both prove instantly.

    That last property is the point. On the production board a maximising T3
    is caught only by failing to finish inside the wall, which makes the kill a
    property of the box; here the two directions return different VALUES from a
    solve that costs milliseconds at any load.
    """
    width_ms = (PROBE_MATCH_MIN + PROBE_GAP_MIN) * MIN_MS
    courts = [f"C{i + 1}" for i in range(PROBE_COURTS)]
    ticks = [EPOCH_MS + OPEN_MS + k * width_ms for k in range(2)]
    grid_slots = [(court, tick, day_index_of(tick)) for court in courts for tick in ticks]
    fixtures = [(f"q{i:02d}", [f"qe{2 * i:02d}", f"qe{2 * i + 1:02d}"], "d1") for i in range(PROBE_FIXTURES)]
    constraints = {
        "match_minutes": PROBE_MATCH_MIN,
        "gap_minutes": PROBE_GAP_MIN,
        "rest_by_division": {"d1": 0},
        "day_cap_by_division": {},
    }
    return fixtures, courts, grid_slots, PROBE_MATCH_MIN + PROBE_GAP_MIN, constraints, [], []
