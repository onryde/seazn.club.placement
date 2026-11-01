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
        wall_seconds=20.0,
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


def test_ruling_255_empty_case_first_no_flag_byte_identical_model():
    """Ruling 255 empty case first: no flag -> byte-identical model.

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
    move.

    The branch's schema was also diffed against origin/main's on a
    400-request corpus during the task (see the commit body); that needs a
    historical copy of the module, so it cannot live here."""
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


def test_ruling_255_accepted_slots_are_logged_once_per_request_with_a_count():
    """On `person_only_fixture_accepted`'s and `round_end_barrier_received`'s
    precedent: one line per request, counts only, so the stg rollout can be
    confirmed rather than assumed. A request with no flag logs nothing."""
    with structlog.testing.capture_logs() as captured:
        request_to_model_input(_four_player_swiss(unpaired_round_2=True))
    events = [e for e in captured if e["event"] == "unpaired_round_slot_accepted"]
    assert len(events) == 1, captured
    assert events[0]["request_id"] == "r255"
    assert events[0]["unpaired_round_slots"] == 2

    with structlog.testing.capture_logs() as captured:
        request_to_model_input(_four_player_swiss(unpaired_round_2=False))
    assert [e for e in captured if e["event"] == "unpaired_round_slot_accepted"] == [], captured


# --- the counted parity sweep ---------------------------------------------------
#
# An independent checker, written from the contract and nothing else, run over
# fixed-seed Swiss-shaped boards: one or two divisions, each a field drawn for
# its first k rounds and unpaired after, per-court windows that open at
# different ticks, and a random court set on some boards. Every placed board
# is checked against its court's window, its court set, every other board on
# its court, and -- in END divisions -- every board of every other round.
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
            if fi["barrier"] != END:
                continue
            (a, sa), (b, sb) = ((i, si), (j, sj)) if fi["round"] < fj["round"] else ((j, sj), (i, si))
            unpaired = not fixtures[a]["entrants"] or not fixtures[b]["entrants"]
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
    output satisfies courts, windows, court sets and the END barrier, checked
    pair by pair by an independent checker.

    Anti-vacuity, each counted and each failing at zero: instances checked
    (>= 30), window checks, court pairs, END pairs with an unpaired side,
    rounds whose unpaired slots ran side by side, and BITES -- one-division
    boards whose barrier-free solve breaks the END rule on an unpaired pair,
    i.e. boards on which the barrier had something to do for an unpaired
    slot."""
    total = Counter()
    instances = bites = 0
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
    print(f"ruling 255 parity: instances={instances} bites={bites} {dict(total)}")
    assert instances >= _SWEEP_MIN_INSTANCES, f"only {instances} instances checked"
    for key in ("windows", "court_sets", "court_pairs", "end_pairs", "unpaired_end_pairs"):
        assert total[key] > 0, f"{key}: nothing checked"
    assert total["parallel_rounds"] > 0, "no round's unpaired slots ever ran side by side"
    assert bites > 0, "no board ever needed the barrier for an unpaired slot"
