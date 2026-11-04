"""The `day_rank` rung: the earliest-day tie-break (owner ruling, 2026-11-04).

    placed -> days -> day_span -> day_start -> idle_gap -> imbalance -> day_rank

THE DEFECT. Every rung above `day_rank` is unchanged when a whole board slides
by one calendar day: `days` counts days, `day_span` and `day_start` measure
inside a day, `idle_gap` and `imbalance` never look at the day at all. So when
the caller's lattice holds a second, EMPTY calendar day — seazn.club's
`boundSolverWindow` adds a whole day of slack after greedy's last end whenever
the organiser set no end date — the board at 00:00 on day 0 and the same board
at 00:00 on day 1 are BOTH proved optimal on all six rungs, and CP-SAT returns
whichever one a search worker finds first. With the shipped 8 workers that is
day 1 in ~15-60% of runs, depending on the machine (seazn.club W3 X-SW-29:
9/60 on a 4-vCPU CI-sized container; this file's scratch probe, before the
rung: 18/30 on the Swiss board below, 14/30 on the two-fixture one). An
organiser who sets a start date gets round 1 at midnight of the NEXT day.

THE RUNG. `day_rank` = the sum, over every match on the board that has a day,
of that day's position among the grid's days in time order (0 = the grid's
first day). Minimised LAST, so it decides only between boards every earlier
rung already calls equal, and it can never buy anything an earlier rung
measures: each earlier rung is frozen as a hard bound before it runs.

Expected values below are derived by hand in each test's docstring, never read
back from the model. The determinism sweeps run the SHIPPED worker count (8),
because a single worker is deterministic by construction and could not witness
the race the rung exists to settle.
"""

import grpc
import grpc_testing
import pytest

from placement.config import Settings
from placement.generated import scheduler_pb2 as pb
from placement.main import SchedulerServicer
from placement.model import DAY_MS, MIN_MS, ROUND_BARRIER_END, SolverKnobs, build_model, solve
from placement.objective import (
    TIER_COUNT,
    TIER_DAY_RANK,
    TIER_DAY_SPAN,
    TIER_DAY_START,
    TIER_DAYS,
    TIER_IDLE_GAP,
    TIER_IMBALANCE,
    TIER_ORDER,
    TIER_PLACED,
)

#: 2030-08-01T00:00:00Z, X-SW-29's start day. Midnight, because seazn.club's
#: `applyWindow` floors the window at the start day's local midnight.
_ANCHOR = 1_911_686_400_000

_MATCH_MINUTES = 30
_DUR_MS = _MATCH_MINUTES * MIN_MS
_STEP_MS = 30 * MIN_MS
_CONSTRAINTS = {"match_minutes": _MATCH_MINUTES, "gap_minutes": 0}
_WALL_SECONDS = 20.0

#: The shipped worker count (`SolverKnobs.num_search_workers`, and fly.toml's
#: `PLACEMENT_NUM_SEARCH_WORKERS`). Stated, not read from the default, so a
#: change to the default cannot quietly turn these sweeps single-worker.
_SHIPPED_WORKERS = 8
_KNOBS = SolverKnobs(num_search_workers=_SHIPPED_WORKERS)

#: Runs per determinism sweep. Before the rung the day-1 rate on these boards
#: was 14/30 and 18/30 here (and 9/60 on CI-sized hardware), so 30 straight
#: day-0 boards without the rung is a ~1e-6 to ~1e-2 event; with it, every run
#: is a proof, so 30/30 is the only passing count.
_RUNS = 30


def _reported(outcome):
    return dict(outcome.objective_values)


def _day_of(start_ms: int) -> int:
    return (start_ms - _ANCHOR) // DAY_MS


def _two_day_grid(courts: int, day_0_ticks: int, day_1_ticks: int) -> list[tuple[int, int, int]]:
    """`day_0_ticks` half-hour ticks from day 0's midnight and `day_1_ticks`
    from day 1's, on every court. Day 1's ticks are the same clock times, so
    the two days offer IDENTICAL boards to every rung above `day_rank`."""
    return [
        (c, _ANCHOR + day * DAY_MS + k * _STEP_MS, day)
        for c in range(courts)
        for day, ticks in ((0, day_0_ticks), (1, day_1_ticks))
        for k in range(ticks)
    ]


def _swiss_board() -> dict:
    """X-SW-29's shape, at the model's own arguments.

    A Swiss of five on two courts: round 1 drawn (0-1, 2-3; player 4 has the
    bye, which takes no slot), round 2 two UNPAIRED shells (ruling 255: no
    entrants), both rounds under the END barrier. The window is X-SW-29's:
    day 0 the whole day from midnight, day 1 from midnight to greedy's end
    (10:00) — `boundSolverWindow`'s 24 h slack.

    Every rung above `day_rank` ties a day-0 board against the same board on
    day 1: four matches, two per round side by side, round 2 straight after
    round 1 — `days` 1, `day_span` 2 * dur, `day_start` 0, `idle_gap` 0,
    `imbalance` 0, on either day.
    """
    return {
        "fixtures": [([0, 1], 0), ([2, 3], 0), ([], 0), ([], 0)],
        "num_courts": 2,
        "grid_slots": _two_day_grid(2, 48, 21),
        "step_minutes": 30,
        "constraints": _CONSTRAINTS,
        "existing": [],
        "dependencies": [],
        "fixture_rounds": [1, 1, 2, 2],
        "fixture_round_barriers": [ROUND_BARRIER_END] * 4,
    }


def _league_board() -> dict:
    """A round robin of four on two courts, the same two-day window. T11
    measured the flip on a league too (3/30 on main's image), so the defect is
    not a Swiss one and neither is the sweep."""
    return {
        "fixtures": [([0, 1], 0), ([2, 3], 0), ([0, 2], 0), ([1, 3], 0), ([0, 3], 0), ([1, 2], 0)],
        "num_courts": 2,
        "grid_slots": _two_day_grid(2, 48, 21),
        "step_minutes": 30,
        "constraints": _CONSTRAINTS,
        "existing": [],
        "dependencies": [],
    }


# --- the empty case first ------------------------------------------------------


def test_a_one_day_grid_reports_day_rank_zero_and_the_full_ladder():
    """EMPTY CASE FIRST: a grid with ONE day has nothing to rank. Every match
    is on the grid's first day, so the sum is 0 by definition, and the rung
    must prove it (7 of 7, not 6 + an unproved one) — a rung that cannot prove
    on the trivial board would cost every one-day request its
    `tiers_completed == TIER_COUNT`, which is seazn.club's `already_optimal`
    and its whole proof gate."""
    grid_slots = [(0, _ANCHOR + k * _STEP_MS, 0) for k in range(4)]
    fixtures = [([0, 1], 0), ([2, 3], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )
    detail = f"status={outcome.status} values={outcome.objective_values}"

    assert len(outcome.assignments) == 2, detail
    assert outcome.tiers_completed == TIER_COUNT, detail
    assert _reported(outcome)[TIER_DAY_RANK] == 0, detail


def test_an_unplaceable_board_reports_day_rank_zero_not_a_negative():
    """Nothing placed: the sum has no terms, so 0. The self-dependency makes
    the sole fixture unplaceable (the same cheap route
    `test_day_objective.py`'s empty-day clamp test takes), on a TWO-day grid,
    so a rung that counted unplaced fixtures, or ranked an unused day, would
    read non-zero here."""
    grid_slots = _two_day_grid(1, 2, 2)
    fixtures = [([0, 1], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], [(0, 0)]),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )

    assert outcome.assignments == [], "the self-dependency should make fixture 0 unplaceable"
    assert _reported(outcome)[TIER_PLACED] == 0
    assert _reported(outcome)[TIER_DAY_RANK] == 0, outcome.objective_values


# --- the ladder ------------------------------------------------------------------


def test_day_rank_is_the_seventh_and_last_rung():
    """Position by INDEX, not membership: the rung is a tie-break ONLY because
    it is last, and a set-membership check passes for any permutation. The
    name is the wire string seazn.club's `TIER_NAMES` must equal exactly
    (`provedOurLadder` compares names in order)."""
    assert TIER_ORDER == (
        TIER_PLACED,
        TIER_DAYS,
        TIER_DAY_SPAN,
        TIER_DAY_START,
        TIER_IDLE_GAP,
        TIER_IMBALANCE,
        TIER_DAY_RANK,
    )
    assert TIER_DAY_RANK == "day_rank"
    assert TIER_COUNT == 7


# --- the defect, swept ---------------------------------------------------------


@pytest.mark.parametrize("board", [_swiss_board, _league_board], ids=["swiss-x-sw-29", "league"])
def test_a_board_every_earlier_rung_ties_lands_on_the_first_day_every_time(board):
    """THE DETERMINISM SWEEP. `_RUNS` solves at the shipped 8 workers; every
    one must put every match on day 0, and must PROVE all seven rungs (a
    day-0 board that came back FEASIBLE would be luck, not the rung).

    Expected by hand, from each board's docstring: the earlier rungs tie
    between a day-0 board and its day-1 copy, so `day_rank` decides, and the
    day-0 copy scores 0 against the day-1 copy's one-per-match. The strict
    optimum is therefore 0, and a proved 0 means every match is on day 0.
    """
    checked = 0
    on_day_0 = 0
    values = set()
    for _ in range(_RUNS):
        kw = board()
        outcome = solve(build_model(**kw), wall_seconds=_WALL_SECONDS, knobs=_KNOBS)
        assert len(outcome.assignments) == len(kw["fixtures"]), outcome
        assert outcome.tiers_completed == TIER_COUNT, outcome
        checked += 1
        if {_day_of(start) for _f, _c, start in outcome.assignments} == {0}:
            on_day_0 += 1
        values.add(tuple(outcome.objective_values))

    # Anti-vacuity: the sweep ran, and every run counted.
    assert checked == _RUNS > 0
    assert on_day_0 == checked, f"{checked - on_day_0} of {checked} boards landed on day 1"
    # One proved ladder, run after run, and its last rung is the day-0 value.
    assert len(values) == 1, values
    assert dict(next(iter(values)))[TIER_DAY_RANK] == 0


def test_the_swiss_board_keeps_round_1_on_the_start_day_and_round_2_after_it():
    """X-SW-29's own assertion, at the model: round 1's boards on the start
    day, round 2's shells after round 1 has ENDED (the END barrier), on the
    same day. Hand-derived: two courts, so both round-1 boards at 00:00 and
    both shells at 00:30."""
    kw = _swiss_board()
    outcome = solve(build_model(**kw), wall_seconds=_WALL_SECONDS, knobs=_KNOBS)
    start_of = {i: start for i, _c, start in outcome.assignments}

    assert sorted(start_of) == [0, 1, 2, 3], outcome
    assert [start_of[i] for i in (0, 1)] == [_ANCHOR, _ANCHOR]
    assert [start_of[i] for i in (2, 3)] == [_ANCHOR + _DUR_MS, _ANCHOR + _DUR_MS]


# --- the rung never overrides an earlier one ---------------------------------------


def test_days_still_wins_when_it_prefers_the_later_day():
    """`days` prefers day 1 here, and `day_rank` must not overrule it.

    One court. Day 0 has ONE tick, day 1 has two. Two fixtures, no shared
    entrant:

        both on day 1          days 1   day_rank 1 + 1 = 2
        one on each day        days 2   day_rank 0 + 1 = 1

    `days` ranks above and picks the first, so the board is both on day 1 and
    the rung reports 2 — the WORSE value of its own metric. A rung ranked above
    `days` would report 1 and use two days.
    """
    grid_slots = _two_day_grid(1, 1, 2)
    fixtures = [([0, 1], 0), ([2, 3], 0)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert sorted(_day_of(s) for _f, _c, s in outcome.assignments) == [1, 1], detail
    assert reported[TIER_DAYS] == 1, detail
    assert reported[TIER_DAY_RANK] == 2, detail
    assert outcome.tiers_completed == TIER_COUNT, detail


def test_day_start_still_wins_when_it_prefers_the_later_day():
    """The nearest DAY rung, and the one most like this one: `day_start`
    prefers day 1, and `day_rank` must not overrule it.

    One court, one fixture. Each day offers 00:00 and 00:30. An off-lattice pin
    on court 0 runs 23:50 (the evening before day 0) to 00:20, so day 0's 00:00
    tick is blocked — and, being on no day (`_day_of_pin` -> None), the pin
    pulls no day's span or start. So:

        day 0 at 00:30    days 1   day_span dur   day_start step   day_rank 0
        day 1 at 00:00    days 1   day_span dur   day_start 0      day_rank 1

    `days` and `day_span` tie, `day_start` picks day 1, and the rung reports
    the 1 it could have saved.
    """
    grid_slots = _two_day_grid(1, 2, 2)
    fixtures = [([0, 1], 0)]
    existing = [(0, _ANCHOR - 10 * MIN_MS)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, []),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )
    detail = f"status={outcome.status} assignments={sorted(outcome.assignments)}"
    reported = _reported(outcome)

    assert [s for _f, _c, s in outcome.assignments] == [_ANCHOR + DAY_MS], detail
    assert reported[TIER_DAY_START] == 0, detail
    # The off-lattice pin has no day, so it adds nothing: the movable's 1 only.
    assert reported[TIER_DAY_RANK] == 1, detail
    assert outcome.tiers_completed == TIER_COUNT, detail


# --- pins -----------------------------------------------------------------------


def test_a_pin_on_a_grid_day_counts_toward_day_rank():
    """A pin is a match on the board, so its day counts, the way seazn.club's
    `boardMetrics` measures its incumbent (pins included).

    One court. Day 0 offers 00:00; day 1 offers 00:00 and 00:30, and a pin sits
    on day 1 at 00:00. The one movable:

        day 0 at 00:00    days 2 (the pin forces day 1)
        day 1 at 00:30    days 1

    `days` puts it on day 1, so the board is two matches on day 1:
    `day_rank` = 1 (movable) + 1 (pin) = 2. A rung that skipped pins would
    report 1.
    """
    grid_slots = _two_day_grid(1, 1, 2)
    fixtures = [([0, 1], 0)]
    existing = [(0, _ANCHOR + DAY_MS)]

    outcome = solve(
        build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, []),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )
    detail = f"status={outcome.status} values={outcome.objective_values}"

    assert [s for _f, _c, s in outcome.assignments] == [_ANCHOR + DAY_MS + _STEP_MS], detail
    assert _reported(outcome)[TIER_DAY_RANK] == 2, detail


def test_a_pin_whose_caller_day_is_given_counts_on_that_day():
    """Follow-up 2's caller day (`PinnedRow.day_index`) is the pin's day for
    every per-day rule, and for this rung too. The pin sits OFF the lattice
    (day 1, 23:00 — after day 1's last tick), so the slot range would give it
    no day; the caller says day 1. The movable lands on day 0 or day 1 with
    every earlier rung tied except `days`, which wants it on day 1 with the
    pin. Expected: day_rank = 1 (movable) + 1 (pin).

    Day 0 offers 00:00, day 1 offers 00:00: one tick each, one court.
    """
    grid_slots = _two_day_grid(1, 1, 1)
    fixtures = [([0, 1], 0)]
    existing = [(0, _ANCHOR + DAY_MS + 23 * 60 * MIN_MS)]

    outcome = solve(
        build_model(
            fixtures, 1, grid_slots, 30, _CONSTRAINTS, existing, [], existing_day_indices=[1]
        ),
        wall_seconds=_WALL_SECONDS,
        knobs=_KNOBS,
    )
    detail = f"status={outcome.status} values={outcome.objective_values}"

    assert [s for _f, _c, s in outcome.assignments] == [_ANCHOR + DAY_MS], detail
    assert _reported(outcome)[TIER_DAY_RANK] == 2, detail


# --- rank, not label ------------------------------------------------------------------


def test_the_rank_is_time_order_not_the_callers_label():
    """`day_rank` ranks days by WHERE THEY ARE ON THE TIMELINE, so it cannot
    depend on what integers the caller chose to label them with. seazn.club
    labels them densely in time order (`buildDayIndexOf`), where rank and
    label coincide; this board labels day 0 as 7 and day 1 as 3, so a rung
    summing labels would prefer the LATER day (3 < 7) and land the board on
    day 1.

    Expected: day 0 (rank 0), reported 0.
    """
    grid_slots = [
        (0, _ANCHOR, 7),
        (0, _ANCHOR + DAY_MS, 3),
    ]
    fixtures = [([0, 1], 0)]

    for _ in range(_RUNS):
        outcome = solve(
            build_model(fixtures, 1, grid_slots, 30, _CONSTRAINTS, [], []),
            wall_seconds=_WALL_SECONDS,
            knobs=_KNOBS,
        )
        detail = f"status={outcome.status} values={outcome.objective_values}"
        assert [s for _f, _c, s in outcome.assignments] == [_ANCHOR], detail
        assert _reported(outcome)[TIER_DAY_RANK] == 0, detail


# --- the seam: proto -> schema -> main.py -> model -> response -------------------


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", "20")
    monkeypatch.setenv("PLACEMENT_NUM_SEARCH_WORKERS", str(_SHIPPED_WORKERS))
    service = pb.DESCRIPTOR.services_by_name["SchedulerService"]
    return service, grpc_testing.server_from_dictionary(
        {service: SchedulerServicer(Settings.from_env())}, grpc_testing.strict_real_time()
    )


def _swiss_request() -> pb.SolveBuildRequest:
    """`_swiss_board()` on the wire, the way seazn.club sends X-SW-29."""
    end = pb.ROUND_BARRIER_END

    def paired(entrants):
        return pb.Fixture(entrant_indices=entrants, division_index=0, round=1, round_barrier=end)

    def shell():
        return pb.Fixture(division_index=0, round=2, round_barrier=end, unpaired_round_slot=True)

    return pb.SolveBuildRequest(
        request_id="day-rank",
        court_names=["Court 1", "Court 2"],
        entrant_count=4,
        division_count=1,
        fixtures=[paired([0, 1]), paired([2, 3]), shell(), shell()],
        slots=[
            pb.Slot(court_index=c, start_at_ms=s, day_index=d) for c, s, d in _two_day_grid(2, 48, 21)
        ],
        step_minutes=30,
        constraints=pb.BuildConstraints(match_minutes=_MATCH_MINUTES, gap_minutes=0),
        wall_seconds=20.0,
    )


def test_the_servicer_proves_day_rank_and_answers_day_0_every_time(server):
    """The whole seam at the shipped worker count: the reply names all seven
    rungs in order (what seazn.club's `provedOurLadder` compares) and every
    board is on day 0. A `main.py` that drove a shorter ladder, or a schema
    that sliced `objective_values` wrongly, fails on the names; the flip
    fails on the day."""
    service, test_server = server
    method = service.methods_by_name["SolveBuild"]
    checked = 0
    for _ in range(10):
        response, _, code, _ = test_server.invoke_unary_unary(
            method, (("x-internal-secret", "test-secret"),), _swiss_request(), None
        ).termination()
        assert code == grpc.StatusCode.OK
        assert response.status == pb.SOLVE_STATUS_OPTIMAL, response
        assert response.tiers_completed == TIER_COUNT, response
        assert [t.name for t in response.objective_values] == list(TIER_ORDER)
        assert response.objective_values[-1].value == 0
        assert len(response.assignments) == 4
        assert {_day_of(a.start_at_ms) for a in response.assignments} == {0}, response
        checked += 1
    assert checked == 10
