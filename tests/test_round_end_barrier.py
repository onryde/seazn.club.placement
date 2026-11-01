"""X-SW-31 (W3 Swiss): the round END barrier.

The contract is the engine's, not this repo's: `solver-port.ts`'s
`SolveBuildInput.fixtures[].roundBarrier` doc comment (onryde/seazn.club.engine,
`src/scheduling/solver-port.ts`), and its verifier `roundOrderConflicts`, kind
`round_order_end`, in `src/scheduling/calendar.ts`. In short:

  * a fixture or a pin may carry a barrier, "start" | "end"; absent is
    "start", C1's start floor, and must stay byte-identical;
  * the sequence on the wire is the division;
  * in an "end" sequence every board of round j starts no earlier than every
    board of an earlier round i ENDS: `start_j >= start_i + match_minutes`.
    Equality is legal;
  * pins bind in both directions (an earlier-round pin floors a later round's
    start, a later-round pin caps an earlier round's end); a pin-pin pair is
    exempt.

Every expected time below is derived BY HAND from that rule and written in the
test's own comment, never read back from the model. Free solves assert the
starts the objective is forced to (each derivation says why the optimum is
unique in starts); forced probes check one combination for feasibility and are
always paired with a probe that differs only in the thing under test, so a
model that refuses everything cannot pass.

One premise of the brief does not hold here, as it did not in the engine's
placer either (T10b report, false premise 2): every match is `match_minutes`
long, so there is no "long board". The board that ends last is the round-1
board pushed into a second wave because round 1 has more boards than there
are courts.
"""

import random

import pytest
from ortools.sat.python import cp_model

from placement.model import (
    DAY_MS,
    MIN_MS,
    ROUND_BARRIER_END,
    ROUND_BARRIER_START,
    SolverKnobs,
    build_model,
    solve,
)

#: Arbitrary anchor, above INT32_MAX (same reasoning as `test_model.py`'s
#: `_ROUND_DAY0_T0`). Every tick below is `T0 + k * 10 min`.
T0 = 1_900_000_000_000
MATCH_MIN = 30
DUR = MATCH_MIN * MIN_MS
STEP_MIN = 10
END = ROUND_BARRIER_END
START = ROUND_BARRIER_START


def _t(minutes: int) -> int:
    return T0 + minutes * MIN_MS


def _grid(courts: int, ticks: int = 18, day: int = 0, base: int = T0) -> list[tuple[int, int, int]]:
    """`ticks` ten-minute ticks from `base` on every court, one day index."""
    return [(c, base + k * STEP_MIN * MIN_MS, day) for c in range(courts) for k in range(ticks)]


def _constraints() -> dict:
    return {"match_minutes": MATCH_MIN, "gap_minutes": 0}


def _feasible(model) -> bool:
    status = cp_model.CpSolver().Solve(model)
    assert status != cp_model.MODEL_INVALID
    return status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def _force(model, i: int, start_ms: int) -> None:
    fv = model.fixture_vars
    model.Add(fv.placed[i] == 1)
    model.Add(fv.start[i] == start_ms)


# --- the free-solve pair: the control first, then the barrier ---------------
#
# Division 0, three courts, every board's entrants disjoint (no participant
# rest binds). Round 1 = fixtures 0-3 (four boards), round 2 = fixtures 4-5.

_SPARE_FIXTURES = [([2 * k, 2 * k + 1], 0) for k in range(6)]
_SPARE_ROUNDS = [1, 1, 1, 1, 2, 2]


def _solve_spare_board(barriers):
    model = build_model(
        _SPARE_FIXTURES, 3, _grid(3), STEP_MIN, _constraints(), [], [],
        fixture_rounds=_SPARE_ROUNDS,
        fixture_round_barriers=barriers,
    )
    outcome = solve(model, wall_seconds=10.0)
    assert outcome.status == "OPTIMAL", outcome
    assert len(outcome.assignments) == 6, outcome.assignments
    start = {i: s for i, _court, s in outcome.assignments}
    return sorted(start[i] for i in range(4)), sorted(start[i] for i in (4, 5))


def test_x_sw_31_empty_case_first_no_barrier_rounds_may_overlap_on_spare_courts():
    """X-SW-31 empty case first: no barrier → rounds may overlap on spare courts.

    By hand. Six 30-minute boards on three courts: every court hosts two, so
    `day_span` >= 60, reachable only as three boards at T0 and three at T0+30
    (each court back to back). The start floor wants every round-1 start <=
    every round-2 start, so round 2 cannot sit in the T0 wave while a round-1
    board sits in the T0+30 wave; round 1 has four boards and three courts, so
    one of them IS in the T0+30 wave, and both round-2 boards join it:

        round 1: T0, T0, T0, T0+30   (the last one ends T0+60)
        round 2: T0+30, T0+30        (30 minutes before round 1 has ended)

    Legal under the start floor. This is the differential the barrier test
    below must move."""
    round1, round2 = _solve_spare_board(None)
    assert round1 == [_t(0), _t(0), _t(0), _t(30)]
    assert round2 == [_t(30), _t(30)]
    assert min(round2) < max(round1) + DUR  # the overlap the barrier forbids


def test_x_sw_31_explicit_start_on_the_spare_board_is_the_control():
    """Same board, every row explicitly "start": the identical answer."""
    round1, round2 = _solve_spare_board([START] * 6)
    assert round1 == [_t(0), _t(0), _t(0), _t(30)]
    assert round2 == [_t(30), _t(30)]


def test_x_sw_31_end_barrier_round_2_never_starts_before_round_1s_last_board_ends():
    """X-SW-31 end barrier: round 2 never starts before round 1's last board ends.

    By hand, same board, every row "end". Round 1 is four boards on three
    courts, so some court plays two round-1 boards back to back: round 1's
    last board ends >= first start + 60. Every round-2 board starts at or
    after that end, so round 2 ends >= first start + 90: `day_span` = 90, with
    the first start at T0 (`day_start`). That pins both ends of it:

        round 1's last board ends exactly T0+60;
        both round-2 boards start exactly T0+60 (start == end, legal; the
        span rung pulls them onto it).

    Round 2 moved from T0+30 (the control above) to T0+60."""
    round1, round2 = _solve_spare_board([END] * 6)
    assert min(round1) == _t(0)
    assert max(round1) + DUR == _t(60)
    assert round2 == [_t(60), _t(60)]
    for s2 in round2:
        for s1 in round1:
            assert s2 >= s1 + DUR


# --- forced probes ----------------------------------------------------------


def _two_round_model(barrier, courts: int = 2):
    """Fixture 0 = round 1, fixture 1 = round 2, division 0, disjoint
    entrants, two courts (so a court clash can never be the reason)."""
    return build_model(
        [([0, 1], 0), ([2, 3], 0)], courts, _grid(courts), STEP_MIN, _constraints(), [], [],
        fixture_rounds=[1, 2],
        fixture_round_barriers=None if barrier is None else [barrier, barrier],
    )


@pytest.mark.parametrize(
    ("barrier", "round2_at", "feasible"),
    [
        # Round 1 at T0 ends T0+30.
        (END, 30, True),    # start == end: legal
        (END, 20, False),   # 10 minutes before round 1 ends
        (None, 20, True),   # the same probe without the barrier: the start floor allows it
        (START, 20, True),  # explicit start: identical to absent
    ],
)
def test_x_sw_31_tie_start_equals_end_is_legal(barrier, round2_at, feasible):
    """X-SW-31 tie start == end is legal."""
    model = _two_round_model(barrier)
    _force(model, 0, _t(0))
    _force(model, 1, _t(round2_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("barrier", "movable_at", "feasible"),
    [
        # Pin = round 2 at T0+40. Movable = round 1, so it must END by T0+40,
        # i.e. start by T0+10.
        (END, 10, True),    # ends T0+40 == the pin's start: legal
        (END, 20, False),   # ends T0+50, after the round-2 pin has started
        (None, 20, True),   # start floor only: T0+20 <= T0+40 on one day
    ],
)
def test_x_sw_31_pin_movable_constrained_a_later_round_pin_caps_the_end(
    barrier, movable_at, feasible
):
    """X-SW-31 pin-pin exempt; pin-movable constrained (upper side).

    The pin sits on court 0; the movable can always use court 1, so a court
    clash is never the reason."""
    model = build_model(
        [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [(0, _t(40))], [],
        fixture_rounds=[1], existing_rounds=[2],
        fixture_round_barriers=None if barrier is None else [barrier],
        existing_round_barriers=None if barrier is None else [barrier],
    )
    _force(model, 0, _t(movable_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("barrier", "movable_at", "feasible"),
    [
        # Pin = round 1 at T0 (ends T0+30). Movable = round 2.
        (END, 30, True),    # starts exactly when the pin ends
        (END, 20, False),   # 10 minutes before the round-1 pin ends
        (None, 20, True),   # start floor only: T0 <= T0+20
    ],
)
def test_x_sw_31_pin_movable_constrained_an_earlier_round_pin_floors_the_start(
    barrier, movable_at, feasible
):
    """X-SW-31 pin-pin exempt; pin-movable constrained (lower side)."""
    model = build_model(
        [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [(0, _t(0))], [],
        fixture_rounds=[2], existing_rounds=[1],
        fixture_round_barriers=None if barrier is None else [barrier],
        existing_round_barriers=None if barrier is None else [barrier],
    )
    _force(model, 0, _t(movable_at))
    assert _feasible(model) is feasible


def test_x_sw_31_pin_pin_exempt_pin_movable_constrained():
    """X-SW-31 pin-pin exempt; pin-movable constrained.

    Two pins of one "end" sequence, OUT of order: P1 = round 1 on court 0 at
    T0+60 (ends T0+90), P2 = round 2 on court 1 at T0 (ends T0+30). Neither can
    move, so the pair is exempt and must not make the board infeasible. The
    one movable fixture is round 3: both pins are earlier rounds, so both
    floor it. By hand: start >= max(T0+90, T0+30) = T0+90, and the solve puts
    it on that floor (`day_start`: the earliest start it is allowed).

    Without the pin floors it could start at T0+60 (the start floor: >= both
    pins' starts, court 1 free from T0+30), so T0+90 is the barrier's doing."""
    model = build_model(
        [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [(0, _t(60)), (1, _t(0))], [],
        fixture_rounds=[3], existing_rounds=[1, 2],
        fixture_round_barriers=[END], existing_round_barriers=[END, END],
    )
    outcome = solve(model, wall_seconds=5.0)
    assert outcome.status == "OPTIMAL", outcome
    assert [(i, s) for i, _c, s in outcome.assignments] == [(0, _t(90))]


def test_x_sw_31_an_off_lattice_pin_still_binds_the_end_barrier():
    """The end barrier compares INSTANTS, so unlike the start floor's day rows
    it does not need the pin to fall on a grid day (the engine's verifier runs
    it with or without a zone, for the same reason). Day 0 offers T0..T0+60,
    day 1 offers T0+1d..T0+1d+60; the round-1 pin at T0+120 falls on neither
    (`_day_of_pin` is None), and ends T0+150. The round-2 movable at T0 starts
    before that end: refused with the barrier; the start floor skips an
    off-lattice pin entirely, so without it the same probe is feasible. On
    day 1 it is after the pin's end either way."""
    grid = _grid(2, ticks=7) + _grid(2, ticks=7, day=1, base=T0 + DAY_MS)

    def probe(barrier, at):
        model = build_model(
            [([0, 1], 0)], 2, grid, STEP_MIN, _constraints(), [(0, _t(120))], [],
            fixture_rounds=[2], existing_rounds=[1],
            fixture_round_barriers=None if barrier is None else [barrier],
            existing_round_barriers=None if barrier is None else [barrier],
        )
        _force(model, 0, at)
        return _feasible(model)

    assert probe(END, _t(0)) is False
    assert probe(None, _t(0)) is True
    assert probe(END, T0 + DAY_MS) is True


def test_x_sw_31_two_sequences_are_independent():
    """X-SW-31 two sequences are independent.

    Division 0: fixture 0 (round 1), fixture 1 (round 2). Division 1:
    fixture 2 (round 1), fixture 3 (round 2). Both "end", four courts.
    Forced by hand: div-0 round 1 at T0+30 (ends T0+60), div-1 round 1 at T0
    (ends T0+30), div-1 round 2 at T0+30 -- on its own division's end, legal,
    although div 0's round 1 is still playing. Div-0 round 2 at T0+30 starts
    before its own round 1 ends: refused. The same probe with all four in ONE
    division is refused too, so the division is what made the first legal."""

    def probe(divisions, div0_round2_at=None):
        fixtures = [([2 * k, 2 * k + 1], d) for k, d in enumerate(divisions)]
        model = build_model(
            fixtures, 4, _grid(4), STEP_MIN, _constraints(), [], [],
            fixture_rounds=[1, 2, 1, 2], fixture_round_barriers=[END] * 4,
        )
        _force(model, 0, _t(30))
        _force(model, 2, _t(0))
        _force(model, 3, _t(30))
        if div0_round2_at is not None:
            _force(model, 1, div0_round2_at)
        return _feasible(model)

    assert probe([0, 0, 1, 1]) is True
    assert probe([0, 0, 1, 1], div0_round2_at=_t(30)) is False
    assert probe([0, 0, 1, 1], div0_round2_at=_t(60)) is True
    assert probe([0, 0, 0, 0]) is False


def test_x_sw_31_a_start_division_beside_an_end_division_keeps_its_overlap():
    """X-SW-31 two sequences are independent: a START division in the same
    build as an END division is NOT barred (review I1). A joint build can
    carry a Swiss division and a round-robin one side by side.

    Division 0 = fixtures 0 (round 1), 1 (round 2), "end". Division 1 =
    fixtures 2 (round 1), 3 (round 2), "start". Four courts. By hand:

      * div 1: round 1 at T0, round 2 at T0+10 on a spare court. The start
        floor allows it (T0 <= T0+10), and the end barrier would not
        (T0+10 < T0+30). Feasible as built; refused once div 1 is "end" too,
        so feasibility is the START reading's doing.
      * div 0, same build: round 1 at T0, round 2 at T0+10 is refused, and
        round 2 at T0+30 (start == end) is legal."""

    def probe(barriers, forced):
        model = build_model(
            [([2 * k, 2 * k + 1], d) for k, d in enumerate([0, 0, 1, 1])],
            4, _grid(4), STEP_MIN, _constraints(), [], [],
            fixture_rounds=[1, 2, 1, 2], fixture_round_barriers=barriers,
        )
        for i, at in forced.items():
            _force(model, i, _t(at))
        return _feasible(model)

    mixed = [END, END, START, START]
    assert probe(mixed, {2: 0, 3: 10}) is True
    assert probe([END] * 4, {2: 0, 3: 10}) is False
    assert probe(mixed, {0: 0, 1: 10}) is False
    assert probe(mixed, {0: 0, 1: 30}) is True
    # Both at once: div 1 overlaps while div 0 holds its barrier.
    assert probe(mixed, {0: 0, 1: 30, 2: 0, 3: 10}) is True


# --- absent == start, byte for byte -------------------------------------------

_BOARD = dict(
    fixtures=[([0, 1], 0), ([2, 3], 0), ([4, 5], 0), ([0, 2], 1), ([1, 3], 1), ([4, 0], 1)],
    num_courts=3,
    grid_slots=_grid(3) + _grid(3, day=1, base=T0 + DAY_MS),
    existing=[(0, _t(0)), (1, _t(40)), (2, T0 - DAY_MS)],
    fixture_rounds=[1, 2, 2, 1, 2, None],
    existing_rounds=[1, 3, None],
)


def _board_model(fixture_barriers, existing_barriers):
    b = _BOARD
    return build_model(
        b["fixtures"], b["num_courts"], b["grid_slots"], STEP_MIN, _constraints(),
        b["existing"], [],
        fixture_rounds=b["fixture_rounds"], existing_rounds=b["existing_rounds"],
        fixture_round_barriers=fixture_barriers, existing_round_barriers=existing_barriers,
    )


def test_x_sw_31_explicit_start_equals_absent_byte_identical_output():
    """X-SW-31 explicit start == absent (byte-identical output).

    A round-bearing board with pins on and off the lattice, two divisions, and
    round-less rows. The CP-SAT model is compared as text, byte for byte; the
    solve is compared on one search worker, which is deterministic."""
    absent = _board_model(None, None)
    explicit = _board_model([START] * 6, [START] * 3)
    assert str(explicit.Proto()) == str(absent.Proto())

    knobs = SolverKnobs(num_search_workers=1)
    a = solve(absent, wall_seconds=10.0, knobs=knobs)
    e = solve(explicit, wall_seconds=10.0, knobs=knobs)
    assert a.status == "OPTIMAL", a
    assert (e.assignments, e.status, e.tiers_completed, e.objective_values) == (
        a.assignments, a.status, a.tiers_completed, a.objective_values,
    )


def test_x_sw_31_end_beside_no_round_is_inert():
    """A barrier is read only beside a round, as the engine skips a round-less
    row. "end" on exactly the round-less rows (fixture 5, pin 2) builds the
    absent model; "end" on a round-bearing division does not (so the
    comparison can fail)."""
    absent = str(_board_model(None, None).Proto())
    on_roundless = _board_model([START] * 5 + [END], [START, START, END])
    assert str(on_roundless.Proto()) == absent
    single = build_model(
        [([0, 1], 0), ([2, 3], 0)], 2, _grid(2), STEP_MIN, _constraints(), [], [],
        fixture_rounds=[1, 2], fixture_round_barriers=[END, END],
    )
    assert str(single.Proto()) != str(_two_round_model(None).Proto())


# --- guards: what the engine refuses, refused here too ------------------------


def test_x_sw_31_a_division_that_mixes_barriers_is_refused():
    """The engine refuses a mixed sequence CONFIG_INVALID before asking. If one
    reaches the model anyway, reading it either way is a guess."""
    with pytest.raises(ValueError, match="mixes"):
        build_model(
            [([0, 1], 0), ([2, 3], 0)], 2, _grid(2), STEP_MIN, _constraints(), [], [],
            fixture_rounds=[1, 2], fixture_round_barriers=[END, START],
        )


def test_x_sw_31_a_pin_whose_barrier_disagrees_with_its_sequence_is_refused():
    """A round-bearing pin is compared with every round-bearing movable
    fixture (C1: the caller forwards a pin's round only when there is one
    sequence), so its barrier must be that sequence's."""
    with pytest.raises(ValueError, match="disagrees"):
        build_model(
            [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [(0, _t(0))], [],
            fixture_rounds=[2], existing_rounds=[1],
            fixture_round_barriers=[END], existing_round_barriers=[START],
        )


def test_x_sw_31_an_end_pin_with_no_round_bearing_movable_is_inert():
    """Review M2. With no round-bearing movable fixture there is no sequence
    for an END pin to disagree with and nothing for it to bind, so it is
    accepted and builds the absent model. The engine cannot send this shape
    (`pinRoundOf` forwards a pin's round only beside one round-bearing free
    sequence); this pins the tolerance, not a production path."""

    def model_text(pin_barriers):
        return str(
            build_model(
                [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [(0, _t(0))], [],
                fixture_rounds=[None], existing_rounds=[1],
                fixture_round_barriers=[START], existing_round_barriers=pin_barriers,
            ).Proto()
        )

    assert model_text([END]) == model_text(None)


def test_x_sw_31_a_barrier_outside_start_and_end_is_refused():
    with pytest.raises(ValueError, match="round barrier"):
        build_model(
            [([0, 1], 0)], 2, _grid(2), STEP_MIN, _constraints(), [], [],
            fixture_rounds=[1], fixture_round_barriers=["END"],
        )


# --- the counted parity sweep ---------------------------------------------------
#
# An independent checker, written from the contract and nothing else, run over
# fixed-seed random boards. It checks the END divisions only: a start-floor
# division sits beside an "end" one on some boards, but the checker never
# inspects it, so this sweep cannot see a barrier leaking ONTO a start
# division -- `test_x_sw_31_a_start_division_beside_an_end_division_keeps_its_
# overlap` is what witnesses that. Pins appear only on single-division boards:
# the caller forwards a pin's round only when there is one round sequence.
# Solved on ONE search worker, so the counts are the same on every run.

_PARITY_SEEDS = range(64)
_PARITY_MIN_INSTANCES = 50


def _end_rule_breaches(instance, assignments) -> tuple[int, int]:
    """(pairs checked, pairs breached) of the end rule, from the contract."""
    start = {i: s for i, _court, s in assignments}
    fixtures, rounds, barriers = instance["fixtures"], instance["rounds"], instance["barriers"]
    pairs = breaches = 0
    end_divisions = {d for (_e, d), b in zip(fixtures, barriers) if b == END}
    for i in start:
        di = fixtures[i][1]
        if di not in end_divisions:
            continue
        for j in start:
            if fixtures[j][1] == di and rounds[i] < rounds[j]:
                pairs += 1
                breaches += start[j] < start[i] + DUR
        for (court, pin_start), pin_round, pin_barrier in zip(
            instance["existing"], instance["existing_rounds"], instance["existing_barriers"]
        ):
            del court
            if pin_barrier != END:
                continue
            if pin_round < rounds[i]:
                pairs += 1
                breaches += start[i] < pin_start + DUR
            elif rounds[i] < pin_round:
                pairs += 1
                breaches += start[i] + DUR > pin_start
    return pairs, breaches


def _random_instance(seed: int) -> dict:
    rng = random.Random(seed)
    courts = rng.randint(2, 3)
    divisions = rng.choice([1, 1, 2])
    fixtures, rounds, barriers = [], [], []
    entrant = 0
    for d in range(divisions):
        barrier = END if d == 0 else rng.choice([END, START])
        for r in range(1, rng.randint(2, 3) + 1):
            for _ in range(rng.randint(1, 3)):
                if rng.random() < 0.5 or entrant < 2:
                    pair = [entrant, entrant + 1]
                    entrant += 2
                else:
                    pair = rng.sample(range(entrant), 2)
                fixtures.append((pair, d))
                rounds.append(r)
                barriers.append(barrier)
    existing, existing_rounds = [], []
    if divisions == 1 and rng.random() < 0.6:
        # At most one pin per court, so two pins can never overlap each
        # other on a court (that would be the caller's infeasible board).
        for court in rng.sample(range(courts), rng.randint(1, courts - 1)):
            existing.append((court, _t(10 * rng.randint(0, 12))))
            existing_rounds.append(rng.randint(1, max(rounds) + 1))
    return dict(
        fixtures=fixtures, rounds=rounds, barriers=barriers, courts=courts,
        existing=existing, existing_rounds=existing_rounds,
        existing_barriers=[END] * len(existing),
        entrants=entrant,
    )


def _solve_instance(instance, with_barrier: bool):
    model = build_model(
        instance["fixtures"], instance["courts"], _grid(instance["courts"], ticks=24),
        STEP_MIN, _constraints(), instance["existing"], [],
        fixture_rounds=instance["rounds"], existing_rounds=instance["existing_rounds"],
        fixture_round_barriers=instance["barriers"] if with_barrier else None,
        existing_round_barriers=instance["existing_barriers"] if with_barrier else None,
    )
    # One worker is deterministic; the wall is set far above an idle solve
    # (well under a second) so a loaded CI box does not cut a tier short and
    # make the counts depend on the clock.
    outcome = solve(model, wall_seconds=20.0, knobs=SolverKnobs(num_search_workers=1))
    assert outcome.status in ("OPTIMAL", "FEASIBLE"), outcome
    return outcome.assignments


def test_x_sw_31_counted_parity_sweep_the_solver_output_satisfies_the_end_rule():
    """X-SW-31 parity: over >= 50 fixed-seed boards the solver's own output
    satisfies the end rule, checked pair by pair by an independent checker.

    Anti-vacuity, all three counted and all three failing at zero: instances
    checked (>= 50), pairs checked, and BITES -- boards whose barrier-free
    solve breaks the rule, i.e. boards on which the barrier had something to
    do. A family where the rule never bites would pass with the barrier
    deleted."""
    instances = pairs = bites = 0
    for seed in _PARITY_SEEDS:
        instance = _random_instance(seed)
        checked, breached = _end_rule_breaches(instance, _solve_instance(instance, True))
        assert breached == 0, f"seed {seed}: {breached} of {checked} end-rule pairs breached"
        if checked == 0:
            continue
        instances += 1
        pairs += checked
        _free_checked, free_breached = _end_rule_breaches(instance, _solve_instance(instance, False))
        bites += free_breached > 0
    print(f"X-SW-31 parity: instances={instances} pairs={pairs} bites={bites}")
    assert instances >= _PARITY_MIN_INSTANCES, f"only {instances} instances checked"
    assert pairs > 0
    assert bites > 0, "no board ever needed the barrier: the sweep cannot see it"
