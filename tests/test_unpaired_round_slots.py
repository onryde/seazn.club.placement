"""Ruling 255 (owner, 2026-11-01, option A): unpaired round slots.

A Swiss stage mints every future round at Generate as shells: boards that
exist and need a court and a time, but whose players are not drawn yet.
seazn.club sends each one with `Fixture.unpaired_round_slot` set and both
index lists empty. Until this field existed the service refused the whole
request ("has neither entrant_indices nor person_indices set"), so every
Swiss auto-schedule fell back to the app's greedy placer and the round end
barrier (X-SW-31) never reached the solver for Swiss.

The rule, from the ruling and nothing else:

  * an unpaired slot carries no person constraint: it joins no participant
    group, so the slots of one round may run side by side on spare courts;
  * every other constraint applies to it exactly as to any fixture of its
    round and division: court capacity, the per-court window, the court set,
    day caps, the round order and the round barrier;
  * the flag with any index set is refused by name; a fixture with neither
    list set and no flag is refused exactly as before.

NOT "everyone in the field": that encoding makes every shell of a round
clash with every other one and serialises the round onto one court at a
time. `test_ruling_255_unpaired_slots_of_one_round_run_in_parallel_on_spare_
courts` shows that encoding doing exactly that, beside the real one.

Every expected time is derived by hand in the test's own docstring, never
read back from the model. Solves go through the real servicer
(`SchedulerServicer`, so schema -> `main.py` -> model -> response) on ONE
search worker; forced-feasibility probes build the model `main.py` builds.
"""

import random
from collections import Counter

import grpc
import grpc_testing
import pytest
import structlog.testing
from ortools.sat.python import cp_model

from placement.config import Settings
from placement.generated import scheduler_pb2 as pb
from placement.main import SchedulerServicer
from placement.model import DAY_MS, MIN_MS, ROUND_BARRIER_END, build_model
from placement.schema import InvalidRequestError, request_to_model_input

SERVICE = pb.DESCRIPTOR.services_by_name["SchedulerService"]
SOLVE_BUILD = SERVICE.methods_by_name["SolveBuild"]
AUTH = (("x-internal-secret", "test-secret"),)

#: A real epoch-ms timestamp (2026-01-01T08:00:00Z), the same anchor
#: `test_server.py`/`test_schema.py` call `SLOT_MS`. Every tick below is
#: `T0 + k * 10 min`.
T0 = 1_767_225_600_000 + 8 * 3_600_000
MATCH_MIN = 30
DUR = MATCH_MIN * MIN_MS
STEP_MIN = 10
END = pb.ROUND_BARRIER_END
START = pb.ROUND_BARRIER_START


def _t(minutes: int) -> int:
    return T0 + minutes * MIN_MS


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", "20")
    # One search worker: the same board every run, so the sweep's counts are
    # stable. The wall is far above an idle solve, so a loaded box does not
    # cut a tier short either.
    monkeypatch.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "1")
    return grpc_testing.server_from_dictionary(
        {SERVICE: SchedulerServicer(Settings.from_env())}, grpc_testing.strict_real_time()
    )


def _paired(entrants, round_, division=0, barrier=END, **kw) -> pb.Fixture:
    return pb.Fixture(
        entrant_indices=entrants, division_index=division, round=round_, round_barrier=barrier, **kw
    )


def _slot(round_, division=0, barrier=END, **kw) -> pb.Fixture:
    """An unpaired round slot: the shell the app sends for an undrawn board."""
    return pb.Fixture(
        division_index=division,
        round=round_,
        round_barrier=barrier,
        unpaired_round_slot=True,
        **kw,
    )


def _slots(courts, ticks: int, *, first: int = 0, day: int = 0, base: int = T0) -> list[pb.Slot]:
    """Ten-minute ticks `first..ticks-1` from `base` on each of `courts`."""
    return [
        pb.Slot(court_index=c, start_at_ms=base + k * STEP_MIN * MIN_MS, day_index=day)
        for c in courts
        for k in range(first, ticks)
    ]


def _request(
    fixtures,
    courts: int,
    *,
    entrant_count: int = 0,
    person_count: int = 0,
    division_count: int = 1,
    ticks: int = 18,
    slots=None,
    rule_groups=(),
    existing=(),
) -> pb.SolveBuildRequest:
    return pb.SolveBuildRequest(
        request_id="r255",
        court_names=[f"Court {c + 1}" for c in range(courts)],
        entrant_count=entrant_count,
        division_count=division_count,
        person_count=person_count,
        fixtures=fixtures,
        slots=slots if slots is not None else _slots(range(courts), ticks),
        step_minutes=STEP_MIN,
        constraints=pb.BuildConstraints(match_minutes=MATCH_MIN, gap_minutes=0),
        rule_groups=list(rule_groups),
        existing=list(existing),
        wall_seconds=20.0,
    )


def _pin(court: int, at_minutes: int, round_: int, barrier=END) -> pb.PinnedRow:
    """A played (or locked) board in `existing`, carrying its round the way
    the app forwards a played round of the one sequence on the board."""
    return pb.PinnedRow(
        court_index=court, start_at_ms=_t(at_minutes), round=round_, round_barrier=barrier
    )


def _invoke(server, req):
    response, _, code, _ = server.invoke_unary_unary(SOLVE_BUILD, AUTH, req, None).termination()
    assert code == grpc.StatusCode.OK
    return response


def _solve(server, req, statuses=(pb.SOLVE_STATUS_OPTIMAL,)) -> dict[int, tuple[int, int]]:
    """fixture index -> (court index, start), through the real servicer."""
    response = _invoke(server, req)
    assert response.status in statuses, response
    return {a.fixture_index: (a.court_index, a.start_at_ms) for a in response.assignments}


def _model(req):
    """The model `main.py` builds for `req`: the schema's output handed to
    `build_model` in `main.py`'s own argument order."""
    p = request_to_model_input(req)
    return build_model(
        p.fixtures, p.courts, p.grid_slots, p.step_minutes, p.constraints, p.existing,
        p.dependencies, p.rule_groups, p.pinned_rule_group_indices, p.pinned_entrant_indices,
        p.fixture_rounds, p.pinned_round, p.person_indices, p.allowed_courts,
        fixture_round_barriers=p.fixture_round_barriers,
        existing_round_barriers=p.pinned_round_barriers,
        fixture_round_gap_minutes=p.fixture_round_gap_minutes,
        fixture_rounds_per_day_caps=p.fixture_rounds_per_day_caps,
    )


def _feasible(model) -> bool:
    status = cp_model.CpSolver().Solve(model)
    assert status != cp_model.MODEL_INVALID
    return status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def _force(model, i: int, start_ms: int) -> None:
    fv = model.fixture_vars
    model.Add(fv.placed[i] == 1)
    model.Add(fv.start[i] == start_ms)


def _no_overlaps(model) -> int:
    return sum(1 for c in model.Proto().constraints if c.has_no_overlap())


def _four_player_swiss(unpaired_round_2: bool) -> pb.SolveBuildRequest:
    """Four players, two courts, END. Round 1: 0-1, 2-3. Round 2: 0-2, 1-3
    once drawn, or two unpaired slots before."""
    round_2 = (
        [_slot(2), _slot(2)] if unpaired_round_2 else [_paired([0, 2], 2), _paired([1, 3], 2)]
    )
    return _request([_paired([0, 1], 1), _paired([2, 3], 1), *round_2], 2, entrant_count=4)


# --- the empty case first -----------------------------------------------------


def test_ruling_255_empty_case_first_no_flag_is_one_wire_shape_and_builds_the_hand_model():
    """Ruling 255 empty case first: no flag is ONE wire shape, and a no-flag
    board parses to the hand tuples and builds exactly the model the domain
    builds from them.

    What this does NOT prove, named so nobody reads it in: that the no-flag
    model is byte-identical to origin/main's. That comparison needs main's
    module beside this one, so it ran out of suite -- a 400-request no-flag
    corpus (drawn, person-only, "neither", duplicate, round/barrier/court-set
    mixes) diffed line for line on origin/main 27200db and on this branch:
    400/400 identical, 124 built (ModelInput and sha256 of the model text) and
    276 refused (173 of them "neither", text included). Re-run at every
    branch commit; the counts are in the commit bodies and the report.

    One "no flag" shape, not two: a proto3 `bool` has no presence, so a caller
    that never heard of the field and one that sends `false` put the SAME
    bytes on the wire (none). Pinned first, because an `optional bool` would
    split it into two shapes that could be read differently.

    Then the board main already solves -- the same Swiss stage with round 2
    drawn, no flag anywhere -- parses to the hand-written domain tuples below
    and builds, byte for byte, the model the domain builds from them. Not
    vacuous: that model holds two court lists AND four entrant groups (each
    player is in two boards), six NoOverlaps by hand; the unpaired twin holds
    the two court lists only (no player is in two boards), so the count can
    move."""
    plain = pb.Fixture(entrant_indices=[0, 1], division_index=0, round=1)
    explicit_false = pb.Fixture(
        entrant_indices=[0, 1], division_index=0, round=1, unpaired_round_slot=False
    )
    assert explicit_false.SerializeToString() == plain.SerializeToString()
    flagged = pb.Fixture(division_index=0, round=1, unpaired_round_slot=True)
    assert flagged.SerializeToString() != pb.Fixture(division_index=0, round=1).SerializeToString()

    req = _four_player_swiss(unpaired_round_2=False)
    parsed = request_to_model_input(req)
    hand_fixtures = [([0, 1], 0), ([2, 3], 0), ([0, 2], 0), ([1, 3], 0)]
    assert parsed.fixtures == hand_fixtures
    assert parsed.person_indices == [[], [], [], []]
    assert parsed.fixture_rounds == [1, 1, 2, 2]
    assert parsed.fixture_round_barriers == [ROUND_BARRIER_END] * 4

    hand_grid = [(c, _t(10 * k), 0) for c in range(2) for k in range(18)]
    direct = build_model(
        hand_fixtures, 2, hand_grid, STEP_MIN, {"match_minutes": 30, "gap_minutes": 0}, [], [],
        fixture_rounds=[1, 1, 2, 2],
        person_indices=[[], [], [], []],
        fixture_round_barriers=[ROUND_BARRIER_END] * 4,
    )
    assert str(_model(req).Proto()) == str(direct.Proto())
    assert _no_overlaps(direct) == 2 + 4
    assert _no_overlaps(_model(_four_player_swiss(unpaired_round_2=True))) == 2


# --- solved, not refused --------------------------------------------------------


def test_ruling_255_a_swiss_stage_with_round_1_paired_and_round_2_as_unpaired_slots_is_solved_not_refused(
    server,
):
    """Ruling 255: a Swiss stage with round 1 paired and round 2 as unpaired
    slots is solved, not refused -- through the real servicer.

    By hand. Eight players: round 1 = four drawn boards, round 2 = four
    unpaired slots; four courts, END. Eight 30-minute boards on four courts,
    so every court hosts two and `day_span` >= 60. The barrier puts every
    round-2 board at or after round 1's last end, so span 60 is reachable
    only one way: round 1 on all four courts at T0, round 2 on all four at
    T0+30 (start == end, legal). Any round-1 board later than T0 pushes round
    2 to T0+60 or later, span 90:

        round 1: T0 x4        round 2: T0+30 x4, on four distinct courts."""
    fixtures = [_paired([2 * k, 2 * k + 1], 1) for k in range(4)] + [_slot(2) for _ in range(4)]
    board = _solve(server, _request(fixtures, 4, entrant_count=8))
    assert sorted(board) == list(range(8))
    assert [board[i][1] for i in range(4)] == [T0] * 4
    assert [board[i][1] for i in range(4, 8)] == [_t(30)] * 4
    assert sorted(board[i][0] for i in range(4, 8)) == [0, 1, 2, 3]


def test_ruling_255_unpaired_slots_of_one_round_run_in_parallel_on_spare_courts(server):
    """Ruling 255: unpaired slots of one round run in parallel on spare courts
    (no person clash).

    By hand. Three unpaired round-1 slots, three courts, and a rule group
    giving all three a 60-minute rest. They have no players, so no
    participant group holds them and the rest interval binds nothing:
    `day_span` is minimised at 30, all three at T0, one per court.

    The contrast is the encoding the ruling rejects: every shell carrying the
    whole field (six players) as people. Every pair of shells then shares a
    person, their 90-minute rest intervals (30 + 60) may not overlap, and the
    round serialises -- T0, T0+90, T0+180, the span rung pulling each onto the
    previous one's rest end and `day_start` putting the first at T0."""
    rest = [pb.RuleGroup(fixture_indices=[0, 1, 2], min_rest_minutes=60)]
    board = _solve(server, _request([_slot(1) for _ in range(3)], 3, ticks=24, rule_groups=rest))
    assert [board[i][1] for i in range(3)] == [T0] * 3
    assert sorted(board[i][0] for i in range(3)) == [0, 1, 2]

    whole_field = [
        pb.Fixture(division_index=0, round=1, round_barrier=END, person_indices=range(6))
        for _ in range(3)
    ]
    serial = _solve(server, _request(whole_field, 3, person_count=6, ticks=24, rule_groups=rest))
    assert sorted(start for _court, start in serial.values()) == [T0, _t(90), _t(180)]


# --- the round barrier ----------------------------------------------------------


@pytest.mark.parametrize(
    "barrier", [END, START, pb.ROUND_BARRIER_UNSPECIFIED], ids=["end", "start", "absent"]
)
def test_ruling_255_with_round_barrier_end_unpaired_round_2_starts_after_round_1s_last_board_ends(
    server, barrier
):
    """Ruling 255: with round_barrier END, unpaired round 2 starts after round
    1's last board ends.

    By hand -- X-SW-31's spare board with round 2 undrawn. Three courts;
    round 1 = four drawn boards with disjoint players, round 2 = two unpaired
    slots. Six 30-minute boards on three courts: `day_span` >= 60.

      START or absent (the control): span 60 is reachable. A round-2 slot at
      T0 would need all four round-1 boards at T0 on three courts, so both
      slots sit at T0+30 beside round 1's fourth board -- round 1 =
      T0,T0,T0,T0+30, round 2 = T0+30,T0+30, starting while round 1 plays.

      END: round 1 is four boards on three courts, so its last board ends
      >= T0+60; both slots start at or after that end, span 90, and the span
      rung pulls them onto T0+60 exactly."""
    fixtures = [_paired([2 * k, 2 * k + 1], 1, barrier=barrier) for k in range(4)] + [
        _slot(2, barrier=barrier) for _ in range(2)
    ]
    board = _solve(server, _request(fixtures, 3, entrant_count=8))
    round_1 = sorted(board[i][1] for i in range(4))
    round_2 = sorted(board[i][1] for i in (4, 5))
    if barrier == END:
        assert min(round_1) == T0
        assert max(round_1) + DUR == _t(60)
        assert round_2 == [_t(60), _t(60)]
    else:
        assert round_1 == [T0, T0, T0, _t(30)]
        assert round_2 == [_t(30), _t(30)]
        assert min(round_2) < max(round_1) + DUR  # the overlap END forbids


@pytest.mark.parametrize(
    ("barrier", "slot_at", "feasible"),
    [
        # The drawn round-1 board is forced to T0 and ends T0+30.
        (END, 30, True),    # start == end: legal
        (END, 20, False),   # ten minutes before round 1 has ended
        (START, 20, True),  # the same probe under the start floor
    ],
    ids=["end-tie-legal", "end-early-refused", "start-early-legal"],
)
def test_ruling_255_an_unpaired_slot_obeys_the_end_barrier_to_the_minute(barrier, slot_at, feasible):
    """Two courts, so a court clash is never the reason."""
    req = _request(
        [_paired([0, 1], 1, barrier=barrier), _slot(2, barrier=barrier)], 2, entrant_count=2
    )
    model = _model(req)
    _force(model, 0, T0)
    _force(model, 1, _t(slot_at))
    assert _feasible(model) is feasible


# --- the start floor and the pins (review I1) -----------------------------------
#
# Forced probes, each paired with a probe that differs only in the thing under
# test, so a model that refuses everything cannot pass. Two courts throughout,
# so a court clash is never the reason: the forced boards always leave one
# court free.

_NO_BARRIER = pb.ROUND_BARRIER_UNSPECIFIED


@pytest.mark.parametrize("barrier", [START, _NO_BARRIER], ids=["start", "absent"])
@pytest.mark.parametrize(
    ("slot_at", "feasible"),
    [
        (0, False),   # 20 minutes before round 1's board starts
        (20, True),   # the same tick: ties are legal under the start floor
    ],
    ids=["before-round-1-refused", "tie-legal"],
)
def test_ruling_255_the_start_floor_binds_an_unpaired_slot_under_start_or_no_barrier(
    barrier, slot_at, feasible
):
    """Ruling 255: the round order still binds an unpaired slot when the
    sequence is START or carries no barrier. By hand: the drawn round-1 board
    is forced to T0+20. The start floor puts a round-2 board at or after
    T0+20 on the same day; the shell at T0 (court 1 free) breaks it, the shell
    at T0+20 (court 1) does not."""
    req = _request(
        [_paired([0, 1], 1, barrier=barrier), _slot(2, barrier=barrier)], 2, entrant_count=2
    )
    model = _model(req)
    _force(model, 0, _t(20))
    _force(model, 1, _t(slot_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize("barrier", [START, _NO_BARRIER], ids=["start", "absent"])
@pytest.mark.parametrize(
    ("slot_at", "feasible"),
    [
        (0, False),   # before the played round-1 board started
        (20, True),   # the same tick, on the other court
    ],
    ids=["before-pin-refused", "tie-legal"],
)
def test_ruling_255_the_start_floor_binds_an_unpaired_slot_to_a_played_round_in_existing(
    barrier, slot_at, feasible
):
    """Ruling 255, the production reschedule shape under START/absent: round
    1 was played and arrives in `existing` with its round; round 2 is a shell.
    By hand: the pin sits on court 0 at T0+20, on day 0. The start floor puts
    the shell at or after T0+20; at T0 on court 1 (free, so no court clash)
    it breaks that, at T0+20 on court 1 it does not."""
    req = _request([_slot(2, barrier=barrier)], 2, existing=[_pin(0, 20, 1, barrier)])
    model = _model(req)
    _force(model, 0, _t(slot_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("barrier", "slot_at", "feasible"),
    [
        # The round-1 pin is on court 0 at T0 and ends T0+30.
        (END, 20, False),    # ten minutes before the played board has ended
        (END, 30, True),     # start == end: legal
        (START, 20, True),   # the same probe under the start floor
    ],
    ids=["end-early-refused", "end-tie-legal", "start-early-legal"],
)
def test_ruling_255_the_end_barrier_binds_an_unpaired_slot_after_an_earlier_round_pin(
    barrier, slot_at, feasible
):
    """Ruling 255: an earlier-round pin FLOORS a shell's start at the pin's
    end. The shell can always use court 1."""
    req = _request([_slot(2, barrier=barrier)], 2, existing=[_pin(0, 0, 1, barrier)])
    model = _model(req)
    _force(model, 0, _t(slot_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("barrier", "slot_at", "feasible"),
    [
        # The round-3 pin is on court 0 at T0+60. A round-2 shell must END by
        # then, i.e. start by T0+30.
        (END, 40, False),    # ends T0+70, after the round-3 board has started
        (END, 30, True),     # ends T0+60 == the pin's start: legal
        (START, 40, True),   # start floor only: T0+40 <= T0+60 on one day
        (START, 70, False),  # start floor: T0+70 is after the round-3 board's start
    ],
    ids=["end-late-refused", "end-tie-legal", "start-late-legal", "start-after-the-pin-refused"],
)
def test_ruling_255_the_end_barrier_binds_an_unpaired_slot_before_a_later_round_pin(
    barrier, slot_at, feasible
):
    """Ruling 255: a later-round pin CAPS a shell's end at the pin's start.
    The shell can always use court 1."""
    req = _request([_slot(2, barrier=barrier)], 2, existing=[_pin(0, 60, 3, barrier)])
    model = _model(req)
    _force(model, 0, _t(slot_at))
    assert _feasible(model) is feasible


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    "names",
    [
        {"entrant_indices": [0, 1]},
        {"person_indices": [0]},
        {"entrant_indices": [0], "person_indices": [1]},
    ],
    ids=["entrants", "people", "both"],
)
def test_ruling_255_the_flag_with_indices_set_is_refused_by_name(server, names):
    """Ruling 255: the flag with indices set is refused by name. A slot that
    names a player is not unpaired, and accepting it would drop that
    player's clash and rest rules without a word. Fixture 1 carries it, so
    the message must name `fixtures[1]`, not the first fixture."""
    req = _request(
        [_paired([0, 1], 1), _slot(2, **names)], 2, entrant_count=2, person_count=2
    )
    with pytest.raises(InvalidRequestError, match=r"fixtures\[1\]\.unpaired_round_slot"):
        request_to_model_input(req)

    response = _invoke(server, req)
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert "fixtures[1].unpaired_round_slot" in response.error.message
    assert list(response.assignments) == []


def test_ruling_255_an_unpaired_slot_without_a_round_is_refused_by_name(server):
    """Ruling 255 follow-up (controller, 2026-11-01): an unpaired slot must
    carry `round`. With no round nothing orders it -- section 10 and the END
    barrier read only round-bearing fixtures -- so it would float free of
    the round it belongs to. Refused by name, at the schema and through the
    RPC.

    The positive pair, so a guard that refuses too much cannot pass:
    `round = 0` is a real round (presence, not value: `round` is an opaque
    ordering key and 0 is legal), and a DRAWN fixture without a round is
    accepted exactly as before -- the rule is the flag's, not every
    fixture's."""
    roundless = pb.Fixture(division_index=0, round_barrier=END, unpaired_round_slot=True)
    req = _request([_paired([0, 1], 1), roundless], 2, entrant_count=2)
    with pytest.raises(InvalidRequestError, match=r"fixtures\[1\]\.unpaired_round_slot requires round"):
        request_to_model_input(req)
    response = _invoke(server, req)
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert "fixtures[1].unpaired_round_slot requires round" in response.error.message

    round_zero = request_to_model_input(_request([_slot(0)], 1))
    assert round_zero.fixture_rounds == [0]
    drawn_roundless = request_to_model_input(
        _request([pb.Fixture(entrant_indices=[0, 1], division_index=0)], 1, entrant_count=2)
    )
    assert drawn_roundless.fixture_rounds == [None]


#: The refusal exactly as origin/main words it (27200db), copied from that
#: source, not from this branch: "unchanged" means byte for byte.
_NEITHER_REFUSAL = (
    "fixtures[{i}] has neither entrant_indices nor person_indices set. A fixture "
    "joining no participant group at all -- by entrant OR by person -- skips the "
    "participant-rest NoOverlap and every T2 idle-gap term. Measured (string "
    "contract, same mechanism): two fixtures sharing one player placed CONCURRENTLY, "
    "reported OPTIMAL."
)


def test_ruling_255_neither_indices_and_no_flag_is_still_refused_unchanged(server):
    """Ruling 255: neither indices and no flag is still refused (unchanged).
    The same undrawn round-2 board as everywhere above, minus the flag."""
    req = _request(
        [_paired([0, 1], 1), pb.Fixture(division_index=0, round=2, round_barrier=END)],
        2,
        entrant_count=2,
    )
    with pytest.raises(InvalidRequestError) as refused:
        request_to_model_input(req)
    assert str(refused.value) == _NEITHER_REFUSAL.format(i=1)

    response = _invoke(server, req)
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert response.error.message == _NEITHER_REFUSAL.format(i=1)


# --- every other constraint still binds -----------------------------------------


def test_ruling_255_a_day_cap_still_binds_an_unpaired_slot(server):
    """By hand. Two days (T0.. and T0+1d.., six ticks each), two courts, two
    unpaired round-1 slots.

      uncapped (the control): the `days` rung uses ONE day, `day_span` puts
      both on one tick (one per court) and `day_start` on that day's first
      tick. No rung prefers day 0 to day 1 -- each is the same span and the
      same zero offset -- so the control asserts the shape, not the day: one
      start, either T0 or T0+1d, shared by both.
      a rule group capping both at one per day: two days are forced, one slot
      on each, each on its day's first tick -- T0 and T0+1d exactly."""
    slots = _slots(range(2), 6) + _slots(range(2), 6, day=1, base=T0 + DAY_MS)

    free = _solve(server, _request([_slot(1), _slot(1)], 2, slots=slots))
    starts = sorted(start for _court, start in free.values())
    assert len(starts) == 2 and starts[0] == starts[1] and starts[0] in (T0, T0 + DAY_MS)

    cap = [pb.RuleGroup(fixture_indices=[0, 1], max_fixtures_per_day=1)]
    capped = _solve(server, _request([_slot(1), _slot(1)], 2, slots=slots, rule_groups=cap))
    assert sorted(start for _court, start in capped.values()) == [T0, T0 + DAY_MS]


#: Court 0 offers T0..T0+50, court 1 offers T0+60..T0+110: two windows that
#: do not touch.
_SPLIT_WINDOWS = _slots([0], 6) + _slots([1], 12, first=6)


def test_ruling_255_court_windows_still_bind_an_unpaired_slot(server):
    """By hand. Three unpaired round-1 slots on `_SPLIT_WINDOWS`. Court 0
    fits at most two 30-minute boards, both starting by T0+50; court 1's
    first tick is T0+60. `day_span` (latest end - earliest start) first:

      one slot on court 1 (ends >= T0+90): court 0's two boards are 30
      apart and the later starts by T0+50, so the earlier starts by T0+20 --
      span >= 70, met by court 0 = T0+20, T0+50 and court 1 = T0+60.
      two on court 1 (T0+60, T0+90, ends T0+120) and one on court 0 by T0+50:
      span >= 70, met by court 0 = T0+50.
      three on court 1: T0+60..T0+150, span 90.

    Span 70 either way; `day_start` (earliest start minus the grid's first
    tick, T0) then takes 20 over 50. So court 0 = T0+20, T0+50 and court 1 =
    T0+60 -- every board inside its own court's window, which is the point."""
    board = _solve(server, _request([_slot(1) for _ in range(3)], 2, slots=_SPLIT_WINDOWS))
    on_court = {c: sorted(s for cc, s in board.values() if cc == c) for c in (0, 1)}
    assert on_court == {0: [_t(20), _t(50)], 1: [_t(60)]}


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ((), (0, T0)),          # unconstrained: court 0's first tick
        ((1,), (1, _t(60))),    # court 1 only: court 1's first tick
    ],
    ids=["any-court", "court-1-only"],
)
def test_ruling_255_the_court_set_still_binds_an_unpaired_slot(server, allowed, expected):
    """By hand, one unpaired slot on `_SPLIT_WINDOWS`: the earliest tick it
    may take is T0 on court 0, unless its court set leaves only court 1."""
    req = _request([_slot(1, allowed_court_indices=allowed)], 2, slots=_SPLIT_WINDOWS)
    assert _solve(server, req) == {0: expected}


# --- rollout signal -------------------------------------------------------------


def _accepted_lines(server, req) -> tuple[list[dict], pb.SolveBuildResponse]:
    with structlog.testing.capture_logs() as captured:
        response = _invoke(server, req)
    return [e for e in captured if e["event"] == "unpaired_round_slot_accepted"], response


def test_ruling_255_accepted_slots_are_logged_once_per_request_with_a_count(server):
    """On `person_only_fixture_accepted`'s and `round_end_barrier_received`'s
    precedent: one line per request, counts only, so the stg rollout can be
    confirmed rather than assumed. A request with no flag logs nothing."""
    events, response = _accepted_lines(server, _four_player_swiss(unpaired_round_2=True))
    assert response.status == pb.SOLVE_STATUS_OPTIMAL, response
    assert len(events) == 1, events
    assert events[0]["request_id"] == "r255"
    assert events[0]["unpaired_round_slots"] == 2

    events, response = _accepted_lines(server, _four_player_swiss(unpaired_round_2=False))
    assert response.status == pb.SOLVE_STATUS_OPTIMAL, response
    assert events == []


def _refused_late_by_the_schema(req):
    # A rule group naming fixture 99: refused by `_validated_rule_groups`,
    # which runs AFTER the fixtures (and their flags) were read and accepted.
    req.rule_groups.append(pb.RuleGroup(fixture_indices=[99]))
    return req


def _refused_by_the_model(req):
    # One row of division 0 ordered START against the others' END: the schema
    # accepts it and `build_model` refuses it ("mixes round barriers").
    req.fixtures[0].round_barrier = START
    return req


@pytest.mark.parametrize(
    ("refuse", "reason"),
    [(_refused_late_by_the_schema, "rule_groups[0]"), (_refused_by_the_model, "mixes round barriers")],
    ids=["schema-after-the-fixtures", "build-model"],
)
def test_ruling_255_a_request_refused_after_its_slots_were_read_logs_no_acceptance(
    server, refuse, reason
):
    """Review M3: "accepted" means the whole request was -- the line is
    emitted only after every validation has passed, the schema's AND
    `build_model`'s, never on a request that is then refused."""
    events, response = _accepted_lines(server, refuse(_four_player_swiss(unpaired_round_2=True)))
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert reason in response.error.message
    assert events == []


# --- the counted parity sweep ---------------------------------------------------
#
# An independent checker, written from the contract and nothing else, run over
# fixed-seed Swiss-shaped boards: one or two divisions, each a field drawn for
# its first k rounds and unpaired after, per-court windows that open at
# different ticks, and a random court set on some boards. Every placed board
# is checked against its court's window, its court set, every other board on
# its court, and every board of every other round of its division: the start
# floor in every division, the END rule in END divisions.
# Solved through the real servicer on ONE search worker, so the counts are the
# same on every run.

_SWEEP_SEEDS = range(48)
_SWEEP_MIN_INSTANCES = 30


def _sweep_instance(seed: int) -> dict:
    rng = random.Random(seed)
    courts = rng.randint(2, 4)
    # In ten-minute ticks: each court opens 0-4 ticks late and stays open
    # 36-42 ticks.
    windows = {}
    for c in range(courts):
        first = rng.randint(0, 4)
        windows[c] = range(first, first + rng.randint(36, 42))
    fixtures = []
    player = 0
    divisions = rng.choice([1, 1, 2])
    for d in range(divisions):
        barrier = END if d == 0 else rng.choice([END, START])
        # Sized so every board proves OPTIMAL far inside the wall on one
        # worker: measured, 12+ boards on two divisions or two courts ran the
        # tier chain into the 20 s wall, which makes a sweep slow and its
        # counts clock-dependent.
        boards = rng.randint(1, 3 if divisions == 1 else 2)
        rounds = rng.randint(2, 3)
        drawn = rng.randint(1, rounds - 1)
        field = list(range(player, player + 2 * boards))
        player += 2 * boards
        for r in range(1, rounds + 1):
            order = field[:]
            rng.shuffle(order)
            for b in range(boards):
                allowed = []
                if rng.random() < 0.25:
                    allowed = sorted(rng.sample(range(courts), rng.randint(1, courts)))
                fixtures.append(
                    dict(
                        division=d,
                        round=r,
                        barrier=barrier,
                        allowed=allowed,
                        entrants=order[2 * b : 2 * b + 2] if r <= drawn else [],
                    )
                )
    return dict(
        courts=courts, windows=windows, fixtures=fixtures, divisions=divisions, players=player
    )


def _sweep_request(instance: dict, barrier_override=None) -> pb.SolveBuildRequest:
    fixtures = []
    for f in instance["fixtures"]:
        barrier = barrier_override if barrier_override is not None else f["barrier"]
        kw = dict(division=f["division"], barrier=barrier, allowed_court_indices=f["allowed"])
        fixtures.append(
            _paired(f["entrants"], f["round"], **kw) if f["entrants"] else _slot(f["round"], **kw)
        )
    slots = [
        pb.Slot(court_index=c, start_at_ms=_t(STEP_MIN * k), day_index=0)
        for c, ticks in instance["windows"].items()
        for k in ticks
    ]
    return _request(
        fixtures,
        instance["courts"],
        entrant_count=instance["players"],
        division_count=instance["divisions"],
        slots=slots,
    )


def _check(instance: dict, board: dict[int, tuple[int, int]]) -> dict:
    """The contract, checked pair by pair. Returns counts and breaches."""
    fixtures = instance["fixtures"]
    ticks = {c: {_t(STEP_MIN * k) for k in r} for c, r in instance["windows"].items()}
    out = Counter()
    breaches = []
    for i, (court, start) in board.items():
        out["windows"] += 1
        if start not in ticks[court]:
            breaches.append(("window", i))
        if fixtures[i]["allowed"]:
            out["court_sets"] += 1
            if court not in fixtures[i]["allowed"]:
                breaches.append(("court set", i))
    placed = sorted(board.items())
    for x, (i, (ci, si)) in enumerate(placed):
        for j, (cj, sj) in placed[x + 1 :]:
            fi, fj = fixtures[i], fixtures[j]
            if ci == cj:
                out["court_pairs"] += 1
                if abs(si - sj) < DUR:
                    breaches.append(("court", i, j))
            if fi["division"] != fj["division"] or fi["round"] == fj["round"]:
                continue
            (a, sa), (b, sb) = ((i, si), (j, sj)) if fi["round"] < fj["round"] else ((j, sj), (i, si))
            unpaired = not fixtures[a]["entrants"] or not fixtures[b]["entrants"]
            # The start floor binds in EVERY division, START or END (every
            # board here is on day 0, so it is the plain `sb >= sa`).
            out["floor_pairs"] += 1
            out["unpaired_floor_pairs"] += unpaired
            if sb < sa:
                breaches.append(("floor", a, b, "unpaired" if unpaired else "drawn"))
            if fi["barrier"] != END:
                out["start_division_unpaired_floor_pairs"] += unpaired
                continue
            out["end_pairs"] += 1
            out["unpaired_end_pairs"] += unpaired
            if sb < sa + DUR:
                breaches.append(("end", a, b, "unpaired" if unpaired else "drawn"))
    # A round whose unpaired slots share a start: the parallelism the ruling
    # exists for, counted so a model that serialises them reads as zero.
    starts = Counter(
        (fixtures[i]["division"], fixtures[i]["round"], start)
        for i, (_court, start) in board.items()
        if not fixtures[i]["entrants"]
    )
    out["parallel_rounds"] = len({(d, r) for (d, r, _s), n in starts.items() if n >= 2})
    out["unpaired_placed"] = sum(1 for i in board if not fixtures[i]["entrants"])
    out["unplaced"] = len(fixtures) - len(board)
    return {"counts": out, "breaches": breaches}


def test_ruling_255_counted_parity_sweep_unpaired_slots_satisfy_courts_windows_and_the_barrier(server):
    """Ruling 255 parity: over >= 30 fixed-seed Swiss boards, the solver's own
    output satisfies courts, windows, court sets, the start floor and the END
    barrier, checked pair by pair by an independent checker; so does each
    one-division board's START twin, for the start floor.

    Anti-vacuity, each counted and each failing at zero: instances checked
    (>= 30), window checks, court pairs, floor pairs with an unpaired side
    (in START divisions too, and in the START twins), END pairs with an
    unpaired side, rounds whose unpaired slots ran side by side, and BITES --
    one-division
    boards whose barrier-free solve breaks the END rule on an unpaired pair,
    i.e. boards on which the barrier had something to do for an unpaired
    slot."""
    total = Counter()
    instances = bites = free_floor_pairs = 0
    for seed in _SWEEP_SEEDS:
        instance = _sweep_instance(seed)
        statuses = (pb.SOLVE_STATUS_OPTIMAL, pb.SOLVE_STATUS_FEASIBLE)
        result = _check(instance, _solve(server, _sweep_request(instance), statuses))
        assert result["breaches"] == [], f"seed {seed}: {result['breaches']}"
        if result["counts"]["unpaired_end_pairs"] == 0:
            continue
        instances += 1
        total.update(result["counts"])
        if instance["divisions"] == 1:
            # The barrier-free twin, on one-division boards only: measured,
            # a two-division board with no barrier is the one shape whose
            # tier chain runs into the 20 s wall on one worker.
            free = _check(instance, _solve(server, _sweep_request(instance, START), statuses))
            bites += any(b[0] == "end" and b[3] == "unpaired" for b in free["breaches"])
            # The START twin still owes the start floor, shells included.
            floor = [b for b in free["breaches"] if b[0] != "end"]
            assert floor == [], f"seed {seed} (START twin): {floor}"
            free_floor_pairs += free["counts"]["unpaired_floor_pairs"]
    print(
        f"ruling 255 parity: instances={instances} bites={bites} "
        f"free_floor_pairs={free_floor_pairs} {dict(total)}"
    )
    assert instances >= _SWEEP_MIN_INSTANCES, f"only {instances} instances checked"
    for key in (
        "windows", "court_sets", "court_pairs", "end_pairs", "unpaired_end_pairs",
        "unpaired_floor_pairs", "start_division_unpaired_floor_pairs",
    ):
        assert total[key] > 0, f"{key}: nothing checked"
    assert free_floor_pairs > 0, "the START twin checked no unpaired floor pair"
    assert total["parallel_rounds"] > 0, "no round's unpaired slots ever ran side by side"
    assert bites > 0, "no board ever needed the barrier for an unpaired slot"


# === ruling 256 ===================================================================
#
# Ruling 256 (owner, 2026-11-01, "apply rec"): unpaired Swiss rounds keep the
# player limits as ROUND limits. An unpaired slot has no people, so person rest
# and per-person day caps never reach it; the sequence carries both instead:
#
#   * `round_gap_minutes` -- under round_barrier END, every board of round r
#     starts no earlier than the last END of round r-1 in the same sequence
#     plus this value. 0/absent = PR #4's END exactly. It binds pins
#     (`existing`) as the barrier does.
#   * `rounds_per_day_cap` -- in one sequence, the number of distinct rounds
#     with at least one board on a calendar day is at most this value.
#     0/absent = no cap. Paired and unpaired boards alike; pins count.
#   * both are one value per sequence, like `round_barrier`; mixed values are
#     refused by name.
#
# Every expectation below is derived by hand from that contract.

#: Day 1 starts 24 hours after T0. `_t(_DAY_1 + m)` is day 1's T0+m.
_DAY_1 = 24 * 60


def _two_days(courts, ticks_day_0: int, ticks_day_1: int) -> list[pb.Slot]:
    return _slots(courts, ticks_day_0) + _slots(courts, ticks_day_1, day=1, base=T0 + DAY_MS)


def _limits_board(barrier=END, **kw) -> pb.SolveBuildRequest:
    """Two days, two courts: round 1 drawn, rounds 2-3 unpaired, a played
    round-1 pin on day 0 and a round-4 pin on day 1 -- every path ruling 256
    touches (movable pairs, both pin directions, the day buckets)."""
    fixtures = [
        _paired([0, 1], 1, barrier=barrier, **kw),
        _paired([2, 3], 1, barrier=barrier, **kw),
        _slot(2, barrier=barrier, **kw),
        _slot(2, barrier=barrier, **kw),
        _slot(3, barrier=barrier, **kw),
    ]
    pins = [_pin(0, 0, 1, barrier), _pin(1, _DAY_1 + 50, 4, barrier)]
    return _request(
        fixtures, 2, entrant_count=4, slots=_two_days(range(2), 12, 12), existing=pins
    )


def _pre_256_model(req):
    """The model `main.py` built before ruling 256: the same schema output,
    handed to `build_model` WITHOUT the two new lists."""
    p = request_to_model_input(req)
    return build_model(
        p.fixtures, p.courts, p.grid_slots, p.step_minutes, p.constraints, p.existing,
        p.dependencies, p.rule_groups, p.pinned_rule_group_indices, p.pinned_entrant_indices,
        p.fixture_rounds, p.pinned_round, p.person_indices, p.allowed_courts,
        fixture_round_barriers=p.fixture_round_barriers,
        existing_round_barriers=p.pinned_round_barriers,
    )


# --- the empty case first ---------------------------------------------------------


def test_ruling_256_empty_case_first_zero_or_absent_is_one_wire_shape_and_reaches_nothing():
    """Ruling 256 empty case first: 0 and absent are ONE wire shape for both
    fields, the schema hands the domain all-zero lists, and those lists build
    exactly the model the pre-256 call builds -- on a board that has every
    path the ruling touches (movable round pairs, an earlier-round and a
    later-round pin, two days).

    Not vacuous: on the same board a gap of 10 and a cap of 2 each change the
    model text, so the comparison can fail.

    What this does NOT prove, named so nobody reads it in: that the model is
    byte-identical to 8dd851c's. That needs 8dd851c's module beside this one,
    so it ran out of suite -- an 800-request corpus (origin/main's 400
    shapes, plus 400 ruling-255 shapes: unpaired slots, END/START/absent,
    round-bearing pins on and off the lattice, one to three days, rule-group
    day caps) diffed line for line on 8dd851c and on this tree, with this
    tree's `build_model` called the way `main.py` now calls it. The counts
    are in the commit body and the report."""
    absent = pb.Fixture(division_index=0, round=2, unpaired_round_slot=True)
    zeros = pb.Fixture(
        division_index=0, round=2, unpaired_round_slot=True,
        round_gap_minutes=0, rounds_per_day_cap=0,
    )
    assert zeros.SerializeToString() == absent.SerializeToString()
    for field in ("round_gap_minutes", "rounds_per_day_cap"):
        one = pb.Fixture(division_index=0, round=2, unpaired_round_slot=True, **{field: 1})
        assert one.SerializeToString() != absent.SerializeToString()

    req = _limits_board()
    parsed = request_to_model_input(req)
    assert parsed.fixture_round_gap_minutes == [0] * 5
    assert parsed.fixture_rounds_per_day_caps == [0] * 5
    zero = str(_model(req).Proto())
    assert zero == str(_pre_256_model(req).Proto())

    assert str(_model(_limits_board(round_gap_minutes=10)).Proto()) != zero
    assert str(_model(_limits_board(rounds_per_day_cap=2)).Proto()) != zero


def test_ruling_256_a_gap_on_a_start_sequence_reaches_nothing_and_a_cap_still_binds():
    """The gap is defined against the previous round's END, so it is an END
    rule: under START (or no barrier) it reaches nothing and the model is the
    gap-free one, byte for byte. It is accepted, not refused -- a refusal
    would send the whole request to the greedy fallback (the ruling-255
    precedent for START). The cap is not tied to a barrier: under START it
    still changes the model."""
    for barrier in (START, _NO_BARRIER):
        plain = str(_model(_limits_board(barrier)).Proto())
        assert str(_model(_limits_board(barrier, round_gap_minutes=30)).Proto()) == plain
        assert str(_model(_limits_board(barrier, rounds_per_day_cap=2)).Proto()) != plain


# --- the round gap ------------------------------------------------------------------


@pytest.mark.parametrize("gap", [0, 10, 20, 40])
def test_ruling_256_the_gap_pushes_round_2_by_exactly_the_gap(server, gap):
    """Ruling 256, through the real servicer. By hand -- ruling 255's
    eight-player board with a gap: four courts, round 1 = four drawn boards,
    round 2 = four unpaired slots, END. Every round-2 start is at or after
    round 1's last end plus the gap, so `day_span` >= 30 + gap + 30, reached
    only with round 1 all at T0 (any later round-1 board raises the floor)
    and round 2 all at exactly T0 + 30 + gap."""
    fixtures = [_paired([2 * k, 2 * k + 1], 1, round_gap_minutes=gap) for k in range(4)] + [
        _slot(2, round_gap_minutes=gap) for _ in range(4)
    ]
    board = _solve(server, _request(fixtures, 4, entrant_count=8))
    assert sorted(board) == list(range(8))
    assert [board[i][1] for i in range(4)] == [T0] * 4
    assert [board[i][1] for i in range(4, 8)] == [_t(MATCH_MIN + gap)] * 4


@pytest.mark.parametrize("second", ["unpaired", "drawn"])
@pytest.mark.parametrize(
    ("barrier", "gap", "slot_at", "feasible"),
    [
        # The drawn round-1 board is forced to T0 and ends T0+30.
        (END, 20, 40, False),   # ten minutes inside the gap
        (END, 20, 50, True),    # round 1's end + the gap, exactly
        (END, 0, 30, True),     # no gap: PR #4's END
        (START, 20, 0, True),   # an END rule: the start floor's tie is still legal
    ],
    ids=["end-inside-gap-refused", "end-gap-exact-legal", "end-no-gap-legal", "start-gap-unread"],
)
def test_ruling_256_a_round_2_board_obeys_the_gap_to_the_minute(
    barrier, gap, slot_at, feasible, second
):
    """Two courts, so a court clash is never the reason, and round 2's drawn
    board has players disjoint from round 1's, so participant rest is never
    the reason either: the gap binds drawn and unpaired boards alike."""
    kw = dict(barrier=barrier, round_gap_minutes=gap)
    later = _slot(2, **kw) if second == "unpaired" else _paired([2, 3], 2, **kw)
    req = _request([_paired([0, 1], 1, **kw), later], 2, entrant_count=4)
    model = _model(req)
    _force(model, 0, T0)
    _force(model, 1, _t(slot_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("pin_at", "pin_round", "slot_at", "feasible"),
    [
        # A round-1 pin on court 0 at T0 ends T0+30; + gap 20 = T0+50.
        (0, 1, 40, False),   # ten minutes inside the gap after the pin
        (0, 1, 50, True),    # the pin's end + the gap, exactly
        # A round-3 pin at T0+80: the round-2 shell's end + 20 must be <= it,
        # so the shell starts by T0+30.
        (80, 3, 40, False),  # ends T0+70, + 20 = T0+90 > T0+80
        (80, 3, 30, True),   # ends T0+60, + 20 == the pin's start
    ],
    ids=["after-pin-inside-gap-refused", "after-pin-exact-legal",
         "before-pin-inside-gap-refused", "before-pin-exact-legal"],
)
def test_ruling_256_pins_bind_the_gap_in_both_directions(pin_at, pin_round, slot_at, feasible):
    """The gap rides on the sequence (the movable fixtures' division); a pin
    joins that sequence through its round, as it does for the barrier. The
    shell can always use court 1."""
    req = _request(
        [_slot(2, round_gap_minutes=20)], 2, existing=[_pin(0, pin_at, pin_round)]
    )
    model = _model(req)
    _force(model, 0, _t(slot_at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("slot_at", "feasible"),
    [
        (40, False),  # round 1 ends T0+30; + gap 20 = T0+50
        (50, True),   # exactly round 1's end + the gap
    ],
    ids=["inside-gap-refused", "gap-exact-legal"],
)
def test_ruling_256_the_gap_runs_from_every_earlier_round_not_only_r_minus_1(slot_at, feasible):
    """T10c alignment, point 1: the gap is measured from the last end of
    EVERY earlier round of the sequence -- the same pair set as the END
    barrier (full r < r'), never r-1 only. The witness is a sequence with no
    round-2 board: round 3 still keeps the gap after round 1. (Where round 2
    has a board, round 1's end + gap is always below round 2's, so only a
    missing round tells the two readings apart.)"""
    req = _request(
        [_paired([0, 1], 1, round_gap_minutes=20), _slot(3, round_gap_minutes=20)],
        2,
        entrant_count=2,
    )
    model = _model(req)
    _force(model, 0, T0)
    _force(model, 1, _t(slot_at))
    assert _feasible(model) is feasible


# --- rounds per day -----------------------------------------------------------------

#: Day 0 offers T0..T0+110 on two courts; day 1 offers ONE tick, T0+1d, on two
#: courts -- room for exactly one round of two boards.
_CAP_DAYS = _two_days(range(2), 12, 1)


@pytest.mark.parametrize(
    ("cap", "round_3"),
    [(0, [_t(60)] * 2), (3, [_t(60)] * 2), (2, [_t(_DAY_1)] * 2)],
    ids=["uncapped", "cap-3", "cap-2"],
)
def test_ruling_256_cap_2_moves_round_3_to_day_2(server, cap, round_3):
    """Ruling 256, through the real servicer. Two courts, END, `_CAP_DAYS`;
    round 1 = two drawn boards, rounds 2 and 3 = two unpaired slots each.

      uncapped, or cap 3 (= the round count): the `days` rung keeps one day
      (day 1 cannot hold three rounds, day 0 can), and `day_span` packs the
      rounds back to back: T0, T0+30, T0+60.
      cap 2: every board still places -- rounds 1-2 on day 0 and round 3 on
      day 1 is the ONLY way (round 1 alone on day 0 leaves rounds 2 and 3 to
      share day 1's single tick, which END forbids). Day 0's span rung then
      puts rounds 1-2 at T0, T0+30, and round 3 takes day 1's tick on both
      courts.

    Round 1 is drawn and rounds 2-3 are not, so a cap that counted only one
    kind of board would leave round 3 on day 0."""
    kw = dict(rounds_per_day_cap=cap)
    fixtures = [
        _paired([0, 1], 1, **kw), _paired([2, 3], 1, **kw),
        _slot(2, **kw), _slot(2, **kw),
        _slot(3, **kw), _slot(3, **kw),
    ]
    board = _solve(server, _request(fixtures, 2, entrant_count=4, slots=_CAP_DAYS))
    assert sorted(board) == list(range(6))
    assert [board[i][1] for i in (0, 1)] == [T0] * 2
    assert [board[i][1] for i in (2, 3)] == [_t(30)] * 2
    assert [board[i][1] for i in (4, 5)] == round_3


@pytest.mark.parametrize("kind", ["unpaired", "drawn"])
@pytest.mark.parametrize(
    ("cap", "rounds", "starts", "feasible"),
    [
        (1, (1, 1), (0, 0), True),          # two boards of ONE round: one round
        (1, (1, 2), (0, 30), False),        # two rounds on day 0, cap 1
        (1, (1, 2), (0, _DAY_1), True),     # one round per day
        (2, (1, 2), (0, 30), True),         # two rounds on day 0, cap 2
    ],
    ids=["one-round-two-boards", "two-rounds-cap-1-refused", "a-day-each", "two-rounds-cap-2"],
)
def test_ruling_256_the_cap_counts_distinct_rounds_of_drawn_or_unpaired_boards(
    cap, rounds, starts, feasible, kind
):
    """Forced probes, two courts, two days of twelve ticks, END. Each pair of
    rows differs only in the thing under test, so a model that refuses
    everything (or nothing) cannot pass."""
    def board(i):
        return _slot(rounds[i], rounds_per_day_cap=cap) if kind == "unpaired" else _paired(
            [2 * i, 2 * i + 1], rounds[i], rounds_per_day_cap=cap
        )

    req = _request([board(0), board(1)], 2, entrant_count=4, slots=_two_days(range(2), 12, 12))
    model = _model(req)
    _force(model, 0, _t(starts[0]))
    _force(model, 1, _t(starts[1]))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("cap", "pins", "shells", "feasible"),
    [
        # An earlier-round pin on day 0 takes day 0's one round.
        (1, [(0, 0, 1)], [(2, 30)], False),
        (1, [(0, 0, 1)], [(2, _DAY_1)], True),
        (2, [(0, 0, 1)], [(2, 30)], True),
        # A later-round pin on day 1 takes day 1's one round.
        (1, [(0, _DAY_1 + 60, 3)], [(2, _DAY_1)], False),
        (1, [(0, _DAY_1 + 60, 3)], [(2, 0)], True),
        # Pins ALREADY over the cap on day 0 (rounds 1 and 2, cap 1): a board
        # of a round already there adds no round; a new round may not join;
        # and the board stays solvable rather than INFEASIBLE.
        (1, [(0, 0, 1), (0, 30, 2)], [(2, 30), (3, _DAY_1)], True),
        (1, [(0, 0, 1), (0, 30, 2)], [(2, 30), (3, 60)], False),
        # A pin OFF the lattice (T0+180: after day 0's last tick T0+110,
        # before day 1) has no day, so it counts against no day's cap -- the
        # day caps' rule (`_day_of_pin`). The same pin ON the lattice does.
        (1, [(0, 180, 3)], [(2, 0)], True),
        (1, [(0, 100, 3)], [(2, 0)], False),
    ],
    ids=[
        "earlier-pin-same-day-refused", "earlier-pin-next-day-legal", "earlier-pin-cap-2-legal",
        "later-pin-same-day-refused", "later-pin-day-before-legal",
        "over-cap-pins-same-round-legal", "over-cap-pins-new-round-refused",
        "off-lattice-pin-uncounted", "on-lattice-pin-counted",
    ],
)
def test_ruling_256_pins_bind_the_cap_in_both_directions(cap, pins, shells, feasible):
    """Forced probes, END, two courts, two days of twelve ticks. Pins are
    (court, minutes from T0, round); shells are (round, minutes from T0). A
    shell at a pin's time uses the other court, and every shell is placed
    where the END barrier allows it, so the cap is the only reason a row is
    refused."""
    req = _request(
        [_slot(r, rounds_per_day_cap=cap) for r, _at in shells],
        2,
        slots=_two_days(range(2), 12, 12),
        existing=[_pin(c, at, r) for c, at, r in pins],
    )
    model = _model(req)
    for i, (_r, at) in enumerate(shells):
        _force(model, i, _t(at))
    assert _feasible(model) is feasible


@pytest.mark.parametrize(
    ("second_day_index", "feasible"),
    [(1, True), (0, False)],
    ids=["two-caller-days-legal", "one-caller-day-refused"],
)
def test_ruling_256_the_cap_buckets_by_the_callers_day_index_never_utc(second_day_index, feasible):
    """T10c alignment, point 2: the cap's day is the caller's `Slot.day_index`
    -- the org's local calendar day, resolved by the app in the org's zone --
    the same bucketing as the rule-group day caps (section 9). The service
    has no zone field and does no UTC arithmetic. Cap 1, round 1 at T0 and
    round 2 at T0+12h, the SAME UTC date: legal when the caller labels them
    two days, refused when it labels them one. A UTC bucketing would refuse
    both. (T0 is 08:00Z and T0+12h 20:00Z: for an org at UTC+5, 13:00 and
    01:00 the next local day.)"""
    slots = _slots(range(2), 6) + _slots(
        range(2), 6, day=second_day_index, base=T0 + 12 * 3_600_000
    )
    req = _request(
        [_slot(1, rounds_per_day_cap=1), _slot(2, rounds_per_day_cap=1)], 2, slots=slots
    )
    model = _model(req)
    _force(model, 0, T0)
    _force(model, 1, T0 + 12 * 3_600_000)
    assert _feasible(model) is feasible


def test_ruling_256_a_capped_request_with_a_slot_missing_its_day_is_refused(server):
    """T10c alignment, point 2: with no caller day there is nothing to bucket
    by, and the service never falls back to UTC -- a slot without
    `day_index` is refused by name, as for every request (the field carries
    the org's zone, resolved per slot)."""
    req = _request([_slot(1, rounds_per_day_cap=1), _slot(2, rounds_per_day_cap=1)], 2)
    req.slots[0].ClearField("day_index")
    response = _invoke(server, req)
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert "slots[0].day_index must be set" in response.error.message


def test_ruling_256_pins_alone_over_the_cap_leave_the_board_solvable(server):
    """T10c alignment, point 3: a day already over the cap through pins alone
    is not an error. Pins are never constrained; the cap only limits the
    rounds MOVABLE boards add. By hand: cap 1, played rounds 1 and 2 pinned
    on day 0 (two rounds, over the cap), two round-3 shells. Day 0 takes no
    new round, so both shells go to day 1: one day used, one tick, both
    courts, at day 1's first tick. OPTIMAL, not INFEASIBLE."""
    req = _request(
        [_slot(3, rounds_per_day_cap=1), _slot(3, rounds_per_day_cap=1)],
        2,
        slots=_two_days(range(2), 12, 12),
        existing=[_pin(0, 0, 1), _pin(0, 30, 2)],
    )
    board = _solve(server, req)
    assert sorted(board) == [0, 1]
    assert [board[i][1] for i in (0, 1)] == [_t(_DAY_1)] * 2


# --- one value per sequence -------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"), [("round_gap_minutes", 20), ("rounds_per_day_cap", 2)]
)
def test_ruling_256_mixed_values_in_one_sequence_are_refused_by_name(server, field, value):
    """Like `round_barrier`, each value is one per sequence. Fixture 3 of
    division 0 carries a different value from fixtures 0-2: refused, by name,
    through the real servicer."""
    req = _four_player_swiss(unpaired_round_2=True)
    setattr(req.fixtures[3], field, value)
    response = _invoke(server, req)
    assert response.status == pb.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert f"division 0 mixes {field} 0 and {value} (fixture 3)" in response.error.message


def test_ruling_256_one_value_per_sequence_not_per_board_and_a_round_less_row_is_skipped(server):
    """Not refused: two divisions with different values (each one sequence),
    and a round-less row in division 0 carrying values of its own (a row with
    no round is in no sequence, exactly as for `round_barrier`).

    By hand, three courts. Division 0: round 1 drawn (f0), round 2 a shell
    (f1), gap 20, cap 2; plus f4, drawn with no round. Division 1: round 1
    drawn (f2), round 2 a shell (f3), no gap, no cap. Division 0 alone makes
    `day_span` >= 30 + 20 + 30 = 80, met only with f1 exactly 50 after f0
    and nothing starting before f0. Then the forced probe: division 1's round
    2 right at its round 1's end is legal (its own gap is 0), division 0's is
    not."""
    fixtures = [
        _paired([0, 1], 1, round_gap_minutes=20, rounds_per_day_cap=2),
        _slot(2, round_gap_minutes=20, rounds_per_day_cap=2),
        _paired([2, 3], 1, division=1),
        _slot(2, division=1),
        pb.Fixture(
            entrant_indices=[4, 5], division_index=0, round_barrier=END,
            round_gap_minutes=50, rounds_per_day_cap=1,
        ),
    ]
    req = _request(fixtures, 3, entrant_count=6, division_count=2)
    board = _solve(server, req)
    assert sorted(board) == list(range(5))
    assert board[1][1] - board[0][1] == 50 * MIN_MS

    for i, j, gap_ms, feasible in [(2, 3, DUR, True), (0, 1, DUR, False)]:
        model = _model(req)
        _force(model, i, T0)
        _force(model, j, T0 + gap_ms)
        assert _feasible(model) is feasible


@pytest.mark.parametrize("kwarg", ["fixture_round_gap_minutes", "fixture_rounds_per_day_caps"])
def test_ruling_256_a_negative_round_limit_is_refused_by_the_model(kwarg):
    """The wire carries `uint32`, so only a direct caller can send a negative
    value; the domain refuses it rather than build a confidently wrong board
    (a negative gap would let round 2 start before round 1 ends; a negative
    cap would forbid every round on every day, OPTIMAL with nothing placed)."""
    grid = [(0, _t(10 * k), 0) for k in range(6)]
    with pytest.raises(ValueError, match=r"fixture 1 .* must be >= 0, got -10"):
        build_model(
            [([0, 1], 0), ([], 0)], 1, grid, STEP_MIN, {"match_minutes": MATCH_MIN}, [], [],
            fixture_rounds=[1, 2],
            person_indices=[[], []],
            fixture_round_barriers=[ROUND_BARRIER_END] * 2,
            **{kwarg: [0, -10]},
        )


# --- the counted parity sweep (ruling 256) ----------------------------------------
#
# An independent checker, written from the contract and nothing else, over
# fixed-seed multi-day Swiss-shaped boards: one or two sequences, each with its
# own barrier, gap and cap, drawn for its first k rounds and unpaired after.
# Every placed board is checked against its court, and every pair of boards
# of different rounds of one sequence against the round order, the END
# barrier plus the gap; every (sequence, day) against the cap. Two days, and a
# capped sequence has at most two days' worth of rounds, so every board must
# place.
#
# Solved on production's EIGHT search workers (fly.toml sets
# PLACEMENT_NUM_SEARCH_WORKERS = 8), not on the one worker the rest of this
# file uses. Measured: on one worker, multi-day boards stall on the `day_span`
# rung for the whole 20 s wall -- START or END, with or without a gap or cap
# (a three-fixture START board on four days does it with no ruling-256 field
# at all) -- while eight workers prove every board of this sweep OPTIMAL
# through all six tiers (80 solves, 15.6 s). The checks are of the contract,
# so they hold for any optimal board; the counts can move between runs, and
# each is asserted only to be nonzero.

@pytest.fixture
def production_workers_server(monkeypatch):
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", "20")
    monkeypatch.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "8")
    return grpc_testing.server_from_dictionary(
        {SERVICE: SchedulerServicer(Settings.from_env())}, grpc_testing.strict_real_time()
    )


_SWEEP_256_SEEDS = range(40)
_SWEEP_256_MIN_INSTANCES = 30


def _sweep_256_instance(seed: int) -> dict:
    rng = random.Random(25_600 + seed)
    courts = rng.randint(2, 3)
    divisions = rng.choice([1, 1, 2])
    max_rounds = 4 if divisions == 1 else 3
    fixtures, sequences = [], {}
    player = 0
    for d in range(divisions):
        sequences[d] = dict(
            barrier=START if rng.random() < 0.25 else END,
            gap=rng.choice([0, 10, 20, 30]),
            cap=rng.choice([0, 1, 2, 2, 3]),
        )
        boards = rng.randint(1, 2 if divisions == 1 else 1)
        cap = sequences[d]["cap"]
        rounds = rng.randint(2, min(max_rounds, 2 * cap) if cap else max_rounds)
        drawn = rng.randint(0, rounds - 1)
        field = list(range(player, player + 2 * boards))
        player += 2 * boards
        for r in range(1, rounds + 1):
            order = field[:]
            rng.shuffle(order)
            for b in range(boards):
                entrants = order[2 * b : 2 * b + 2] if r <= drawn else []
                fixtures.append(dict(division=d, round=r, entrants=entrants))
    # Two days, each open 10-14 ticks on every court: room for two rounds a
    # day at the largest gap.
    windows = {day: rng.randint(10, 14) for day in range(2)}
    return dict(
        courts=courts, divisions=divisions, players=player,
        fixtures=fixtures, sequences=sequences, windows=windows,
    )


def _sweep_256_request(instance: dict, free: bool = False) -> pb.SolveBuildRequest:
    """`free` drops the gap and the cap (the barrier stays): the twin that
    shows whether the limits had anything to do on this board."""
    fixtures = []
    for f in instance["fixtures"]:
        seq = instance["sequences"][f["division"]]
        kw = dict(division=f["division"], barrier=seq["barrier"])
        if not free:
            kw.update(round_gap_minutes=seq["gap"], rounds_per_day_cap=seq["cap"])
        fixtures.append(
            _paired(f["entrants"], f["round"], **kw) if f["entrants"] else _slot(f["round"], **kw)
        )
    slots = [
        pb.Slot(
            court_index=c, start_at_ms=T0 + day * DAY_MS + k * STEP_MIN * MIN_MS, day_index=day
        )
        for day, ticks in instance["windows"].items()
        for c in range(instance["courts"])
        for k in range(ticks)
    ]
    return _request(
        fixtures,
        instance["courts"],
        entrant_count=instance["players"],
        division_count=instance["divisions"],
        slots=slots,
    )


def _check_256(instance: dict, board: dict[int, tuple[int, int]]) -> dict:
    """The contract, checked pair by pair and day by day."""
    fixtures, sequences = instance["fixtures"], instance["sequences"]

    def day_of(start: int) -> int:
        return (start - T0) // DAY_MS  # every tick sits inside its own day's window

    out = Counter()
    breaches = []
    placed = sorted(board.items())
    for x, (i, (ci, si)) in enumerate(placed):
        for j, (cj, sj) in placed[x + 1 :]:
            fi, fj = fixtures[i], fixtures[j]
            if ci == cj:
                out["court_pairs"] += 1
                if abs(si - sj) < DUR:
                    breaches.append(("court", i, j))
            if fi["division"] != fj["division"] or fi["round"] == fj["round"]:
                continue
            (a, sa), (b, sb) = ((i, si), (j, sj)) if fi["round"] < fj["round"] else ((j, sj), (i, si))
            seq = sequences[fi["division"]]
            unpaired = not fixtures[a]["entrants"] or not fixtures[b]["entrants"]
            same_day = day_of(sa) == day_of(sb)
            out["floor_pairs"] += 1
            if day_of(sb) < day_of(sa) or (same_day and sb < sa):
                breaches.append(("floor", a, b))
            if seq["barrier"] != END:
                continue
            if seq["gap"]:
                out["gap_pairs"] += 1
                out["unpaired_gap_pairs"] += unpaired
                out["same_day_gap_pairs"] += same_day
            if sb < sa + DUR + seq["gap"] * MIN_MS:
                breaches.append(("gap" if seq["gap"] else "end", a, b, unpaired))
    rounds_on_day: dict[tuple[int, int], set[int]] = {}
    drawn_on_day: dict[tuple[int, int], set[bool]] = {}
    for i, (_court, start) in board.items():
        key = (fixtures[i]["division"], day_of(start))
        rounds_on_day.setdefault(key, set()).add(fixtures[i]["round"])
        drawn_on_day.setdefault(key, set()).add(bool(fixtures[i]["entrants"]))
    for (d, day), rounds in rounds_on_day.items():
        cap = sequences[d]["cap"]
        if not cap:
            continue
        out["cap_days"] += 1
        out["cap_days_at_the_cap"] += len(rounds) == cap
        out["cap_days_drawn_and_unpaired"] += len(drawn_on_day[(d, day)]) == 2
        if len(rounds) > cap:
            breaches.append(("cap", d, day, sorted(rounds)))
    out["unplaced"] = len(fixtures) - len(board)
    return {"counts": out, "breaches": breaches}


def test_ruling_256_counted_parity_sweep_boards_satisfy_the_gap_and_the_cap(
    production_workers_server,
):
    """Ruling 256 parity: over >= 30 fixed-seed multi-day Swiss boards that
    carry a gap or a cap, the solver's own output places every board and
    satisfies courts, the round order, the END barrier plus the gap, and the
    rounds-per-day cap, checked by an independent checker.

    Anti-vacuity, each counted and each failing at zero: instances checked
    (>= 30), gap pairs (with an unpaired side, and on one day -- across days
    the gap is free), capped (sequence, day)s, days AT the cap, days holding
    drawn and unpaired boards together, and BITES -- boards whose twin
    without the gap (or without the cap) breaks it, i.e. boards on which the
    rule had something to do."""
    server = production_workers_server
    total = Counter()
    instances = gap_bites = cap_bites = 0
    statuses = (pb.SOLVE_STATUS_OPTIMAL, pb.SOLVE_STATUS_FEASIBLE)
    for seed in _SWEEP_256_SEEDS:
        instance = _sweep_256_instance(seed)
        result = _check_256(instance, _solve(server, _sweep_256_request(instance), statuses))
        assert result["breaches"] == [], f"seed {seed}: {result['breaches']}"
        assert result["counts"]["unplaced"] == 0, f"seed {seed}: a board went unplaced"
        limited = any(
            s["cap"] or (s["gap"] and s["barrier"] == END) for s in instance["sequences"].values()
        )
        if not limited:
            continue
        instances += 1
        total.update(result["counts"])
        free = _check_256(instance, _solve(server, _sweep_256_request(instance, free=True), statuses))
        gap_bites += any(b[0] == "gap" and b[3] for b in free["breaches"])
        cap_bites += any(b[0] == "cap" for b in free["breaches"])
    print(
        f"ruling 256 parity: instances={instances} gap_bites={gap_bites} "
        f"cap_bites={cap_bites} {dict(total)}"
    )
    assert instances >= _SWEEP_256_MIN_INSTANCES, f"only {instances} instances checked"
    for key in (
        "court_pairs", "floor_pairs", "gap_pairs", "unpaired_gap_pairs", "same_day_gap_pairs",
        "cap_days", "cap_days_at_the_cap", "cap_days_drawn_and_unpaired",
    ):
        assert total[key] > 0, f"{key}: nothing checked"
    assert gap_bites > 0, "no board ever needed the gap for an unpaired slot"
    assert cap_bites > 0, "no board ever needed the cap"
