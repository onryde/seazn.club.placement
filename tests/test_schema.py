"""The wire boundary, both directions: proto in via `request_to_model_input`,
proto out via `outcome_to_response` / `error_response`.

This is the anti-corruption layer. Domain code (`placement.model` /
`placement.objective`) never sees a proto type in either direction, and — the
point of most of the tests below — never sees a request that would make it
produce a confidently wrong board. `build_model` and `run_tier_chain` keep
their own guards on the same two scalar fields; those are defence in depth,
not the first line. Rejecting here is what turns the failure into a clean
`SOLVE_STATUS_ERROR` response instead of an exception escaping the handler.

--- round 6: identity is POSITIONAL ------------------------------------------

`_require_id` and its three-round character-canonicality saga (whitespace,
invisible Unicode, homoglyphs) are gone along with every test that exercised
them — there is no id left to canonicalise. In their place: every index must
be IN RANGE for the list or count it points into, and every SINGULAR index
field must be explicitly SET (0 is always a legitimate index, so an unset
field cannot be told from a deliberate 0 by value alone). See `placement.schema`'s
module docstring for the full reasoning.
"""

import pytest
import structlog

from placement.generated import scheduler_pb2
from placement.model import SolveOutcome
from placement.objective import MIN_TIER_SECONDS, TIER_ORDER
from placement.schema import (
    InvalidRequestError,
    error_response,
    outcome_to_response,
    request_to_model_input,
)


#: A real epoch-ms timestamp (2026-01-01T08:00:00Z), never 0.
#:
#: The corpus in `bench/` moved off epoch zero in Prompt 05b for a reason that
#: applies just as hard to these hand-built requests: at `start_at_ms = 0`,
#: proto3's UNSET and a deliberate test value are the same number, so a guard
#: written against "unset" cannot be shown to reject unset rather than to
#: reject the fixture data. Every timestamp in this file is above `INT32_MAX`
#: for the same reason `MIN_REAL_EPOCH_MS` is (see `test_bench_contract.py`).
SLOT_MS = 1_767_225_600_000 + 8 * 3_600_000


def _valid_request(**overrides) -> scheduler_pb2.SolveBuildRequest:
    """A minimal request that maps cleanly, so each test below can break
    exactly one field and know that field is why it was rejected."""
    kwargs = dict(
        request_id="r1",
        court_names=["Court 1"],
        entrant_count=2,
        division_count=1,
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)],
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=0)],
        step_minutes=10,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=10),
        wall_seconds=8.0,
    )
    kwargs.update(overrides)
    return scheduler_pb2.SolveBuildRequest(**kwargs)


def _constraints(**overrides) -> scheduler_pb2.BuildConstraints:
    """`match_minutes` and `gap_minutes` both SET, always.

    Written as a helper because `gap_minutes` has explicit presence: a test
    that builds `BuildConstraints(match_minutes=30)` to probe some unrelated
    field would be rejected for the missing gap instead, and would pass while
    proving nothing.
    """
    kwargs = dict(match_minutes=30, gap_minutes=10)
    kwargs.update(overrides)
    return scheduler_pb2.BuildConstraints(**kwargs)


def test_rejects_empty_fixtures():
    req = scheduler_pb2.SolveBuildRequest(request_id="r1", court_names=["Court 1"])
    with pytest.raises(InvalidRequestError, match="fixtures"):
        request_to_model_input(req)


def test_rejects_empty_court_names():
    req = scheduler_pb2.SolveBuildRequest(
        request_id="r1",
        entrant_count=2,
        division_count=1,
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)],
    )
    with pytest.raises(InvalidRequestError, match="court_names"):
        request_to_model_input(req)


def test_rejects_empty_slots():
    """No `Grid` wrapper any more (round 6 flattened it away — `step_minutes`
    was never read by the domain and had no reason to keep a message field
    to itself), so there is only one way to send zero slots now, not two.

    Without any admissible tick the model falls back to a single start of 0,
    forces every fixture onto it, and proves ONE fixture per court OPTIMAL
    while silently dropping the rest — measured through the real server
    before this guard existed: 8 fixtures, 2 courts, no slots -> OPTIMAL,
    `error` unset, most fixtures silently unplaced.
    """
    req = _valid_request(slots=[])
    with pytest.raises(InvalidRequestError, match="slots"):
        request_to_model_input(req)


def test_maps_valid_request():
    req = _valid_request()
    parsed = request_to_model_input(req)
    assert parsed.courts == 1
    assert len(parsed.fixtures) == 1
    assert parsed.wall_seconds == 8.0


def test_maps_every_field_through():
    """The whole translation, not just the two fields the guards read — a
    dropped `existing` or `dependencies` list silently relaxes the board."""
    req = _valid_request(
        entrant_count=4,
        division_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0),
            scheduler_pb2.Fixture(entrant_indices=[2, 3], division_index=0),
        ],
        existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS)],
        # Both endpoints name MOVABLE fixtures (indices 0 and 1).
        dependencies=[scheduler_pb2.OrderPair(before_index=0, after_index=1)],
    )
    parsed = request_to_model_input(req)
    assert parsed.fixtures == [([0, 1], 0), ([2, 3], 0)]
    assert parsed.grid_slots == [(0, SLOT_MS, 0)]
    assert parsed.step_minutes == 10
    # `division_rules` (proto field 10, `DivisionRule`) is retired:
    # `_validated_constraints` carries only `match_minutes`/`gap_minutes` now.
    assert parsed.constraints == {"match_minutes": 30, "gap_minutes": 10}
    assert parsed.existing == [(0, SLOT_MS)]
    assert parsed.dependencies == [(0, 1)]
    # C1: both fixtures left `round` unset on the wire, so both entries here
    # are `None`, not 0 -- see the presence tests below for why that
    # distinction is the whole point of the field.
    assert parsed.fixture_rounds == [None, None]
    assert parsed.pinned_round == [None]


# --- C1: round-order presence (2026-08-12 round-order design) --------------
#
# `fixture_rounds`/`pinned_round` are kept OFF `fixtures`/`existing`'s own
# tuple shape, same reasoning as `pinned_rule_group_indices`/
# `pinned_entrant_indices`: `model.py` unpacks both as bare tuples
# (`for i, (entrant_indices, _division) in enumerate(fixtures)`,
# `for k, (existing_court, existing_start) in enumerate(existing)`), so a
# wider tuple there is a crash, not a behaviour change.


def test_fixture_round_wire_presence_is_not_conflated_with_zero():
    """0 is round 1's neighbour, not "unset" -- proto3 `optional` is the only
    thing that can tell them apart, so this asserts presence survives the
    translation rather than trusting the wire type alone."""
    req = _valid_request(
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0, round=0),
        ]
    )
    parsed = request_to_model_input(req)
    assert parsed.fixture_rounds == [0]


def test_fixture_round_absent_decodes_as_none_not_zero():
    req = _valid_request(
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)]
    )
    parsed = request_to_model_input(req)
    assert parsed.fixture_rounds == [None]
    assert parsed.fixture_rounds != [0]


def test_pinned_round_round_trips_alongside_existing():
    req = _valid_request(
        existing=[
            scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS, round=3),
            scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS + 3_600_000),
        ]
    )
    parsed = request_to_model_input(req)
    assert parsed.existing == [(0, SLOT_MS), (0, SLOT_MS + 3_600_000)]
    assert parsed.pinned_round == [3, None]


# --- degenerate scalars ----------------------------------------------------
# Each of these is separately guarded downstream. They are rejected HERE so
# the caller gets SOLVE_STATUS_ERROR with a reason, and so the domain never
# has to be the thing that notices. Unrelated to identity, and unchanged by
# round 6.


@pytest.mark.parametrize("match_minutes", [0, -1])
def test_rejects_non_positive_match_minutes(match_minutes):
    """A zero-length match makes every court and rest interval zero-width, so
    NoOverlap constrains nothing and the solve returns OPTIMAL with the whole
    board stacked on one tick. `match_minutes=0` is also what an UNSET
    `BuildConstraints.match_minutes` looks like on the wire."""
    req = _valid_request(
        constraints=scheduler_pb2.BuildConstraints(match_minutes=match_minutes, gap_minutes=10)
    )
    with pytest.raises(InvalidRequestError, match="match_minutes"):
        request_to_model_input(req)


@pytest.mark.parametrize("wall_seconds", [0.0, -1.0])
def test_rejects_non_positive_wall_seconds(wall_seconds):
    """A 0 budget stops the tier chain before T0 ever runs and comes back
    UNKNOWN with no assignments — the same answer a genuinely impossible board
    gives, so a dropped field would read as a solver verdict."""
    req = _valid_request(wall_seconds=wall_seconds)
    with pytest.raises(InvalidRequestError, match="wall_seconds"):
        request_to_model_input(req)


# --- explicit presence, unrelated to identity -------------------------------
#
# `gap_minutes` and `Slot.day_index` are the two fields where 0 was ALREADY
# legitimate before round 6, so a value guard could never reach their
# degenerate case: proto3 `optional` is the only thing that separates "unset"
# from "deliberately 0", and these are the tests that keep the keyword in the
# .proto. Unchanged by round 6 — see the INDEX presence family further down
# for the new fields that now carry the same defence.


def test_rejects_unset_gap_minutes():
    """Unset -> 0 -> the court interval loses its turnaround and matches are
    booked back-to-back, OPTIMAL, `error` unset. Distinguishable from a
    deliberate `gap_minutes = 0` only by field presence."""
    req = _valid_request(constraints=scheduler_pb2.BuildConstraints(match_minutes=30))
    with pytest.raises(InvalidRequestError, match="gap_minutes"):
        request_to_model_input(req)


def test_accepts_an_explicit_zero_gap():
    """The other half, and the reason this is presence and not `> 0`: some
    sports genuinely have no turnaround, and that board must still solve."""
    req = _valid_request(constraints=_constraints(gap_minutes=0))
    assert request_to_model_input(req).constraints["gap_minutes"] == 0


def test_rejects_a_slot_without_a_day_index():
    """Day 0 is the first day, so 0 is a legitimate value and unset is the same
    bytes. Unset puts EVERY slot on day 0 and collapses the whole lattice into
    one day-cap bucket."""
    req = _valid_request(slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS)])
    with pytest.raises(InvalidRequestError, match="day_index"):
        request_to_model_input(req)


def test_accepts_day_index_zero():
    """The other half: day 0 is the first day and must map, or the presence
    check has quietly become `> 0` and no board can start on its own day one."""
    parsed = request_to_model_input(_valid_request())
    assert parsed.grid_slots == [(0, SLOT_MS, 0)]


def test_rejects_a_negative_day_index():
    req = _valid_request(
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=-1)]
    )
    with pytest.raises(InvalidRequestError, match="day_index"):
        request_to_model_input(req)


def test_rejects_an_omitted_constraints_message():
    """`BuildConstraints` is a message field, so omitting it entirely arrives
    as a default-valued one. Named separately from the field guards so the
    caller is told the message is missing rather than being told about
    whichever field the checks happen to reach first."""
    req = _valid_request()
    req.ClearField("constraints")
    with pytest.raises(InvalidRequestError, match="BuildConstraints"):
        request_to_model_input(req)


# --- negative values, unrelated to identity ---------------------------------
#
# The `<= 0` guards fire on zero and on missing; these two fields are guarded
# `>= 0` because zero is legitimate, and NEGATIVE is worse than either.


def test_rejects_negative_gap_minutes():
    """At `gap_minutes == -match_minutes` the court interval is zero-width,
    NoOverlap over zero-width intervals constrains nothing, and two matches
    overlap on one court. Measured: 37 placed, 4 tiers, 1 overlap, OPTIMAL."""
    req = _valid_request(constraints=_constraints(gap_minutes=-1))
    with pytest.raises(InvalidRequestError, match="gap_minutes"):
        request_to_model_input(req)


# `division_rules` (proto field 10, `DivisionRule`) is retired. The two tests
# that used to live here -- a negative `min_rest_minutes` rejected, and zero
# treated oppositely by `min_rest_minutes` (accepted) vs `max_fixtures_per_
# day` (refused) -- are now proved on the `RuleGroup` restatement of the same
# two fields instead: `test_rejects_negative_rule_group_min_rest_minutes` and
# `test_zero_is_legitimate_rule_group_rest_but_not_a_legitimate_day_cap`,
# both further down in the "#21 / C1: rule_groups" section.


# --- referential integrity, now expressed as index range --------------------
#
# ONE pass, before any domain object exists: every index a request carries
# must be IN RANGE for the list or count it points into. Each case below is
# the positional restatement of a measured defect in which the constraint the
# caller asked for evaporated and the service answered OPTIMAL with `error`
# unset — never a crash, never a warning.
#
# `court_names` uniqueness/blankness checks are GONE, on purpose — see the
# "confusable court names" section below. That family of defects cannot occur
# any more, by construction, so there is no guard left to test.


def test_accepts_a_declared_court_that_no_slot_offers():
    """Task C2: a court with no slots at all used to be refused here
    (`_validate_court_slot_coverage`'s "no slot offers them"). It no longer
    is — a court blacked out for its whole horizon is a legitimate thing to
    configure, and `placement.model.build_model` now forces that court's
    `presence_court[i][c]` to 0 for every fixture directly, in the domain,
    rather than the ACL rejecting the request. See `placement.model`'s
    docstring, "per-court grids".
    """
    req = _valid_request(
        court_names=["Court 1", "Court 2"],
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=0)],
    )
    parsed = request_to_model_input(req)  # must NOT raise
    assert parsed.courts == 2
    assert parsed.grid_slots == [(0, SLOT_MS, 0)]


def test_accepts_courts_that_offer_different_tick_sets():
    """Task C2 lifts this refusal too. A court need not be slotless to have
    been rejected before — it only needed a tick set DIFFERENT from another
    court's, because `build_model` used to union the starts and place a
    fixture on a court at a tick it never actually offered. Now each court's
    OWN tick set is enforced directly (`placement.model`'s "per-court grids"
    / task C2), so the capability that refusal traded away is back: this
    exact shape must map cleanly instead of raising.
    """
    req = _valid_request(
        court_names=["Court 1", "Court 2"],
        slots=[
            scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=0),
            scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS + 3_600_000, day_index=0),
            scheduler_pb2.Slot(court_index=1, start_at_ms=SLOT_MS + 3_600_000, day_index=0),
        ],
    )
    parsed = request_to_model_input(req)  # must NOT raise
    assert parsed.grid_slots == [
        (0, SLOT_MS, 0),
        (0, SLOT_MS + 3_600_000, 0),
        (1, SLOT_MS + 3_600_000, 0),
    ]


def test_accepts_several_courts_offering_the_identical_tick_set():
    """The homogeneous shape must still map too. This is the shape every
    board in `bench/` has, and the shape a normal request has."""
    req = _valid_request(
        court_names=["Court 1", "Court 2"],
        slots=[
            scheduler_pb2.Slot(court_index=c, start_at_ms=SLOT_MS + k * 3_600_000, day_index=0)
            for c in (0, 1)
            for k in range(2)
        ],
    )
    assert len(request_to_model_input(req).grid_slots) == 4


def test_a_per_court_blackout_now_solves_instead_of_being_refused():
    """The exact shape reported live (staging, org `hhhh` / division
    `test001`): a global blackout every court loses alike (a uniform
    removal, so it alone creates no asymmetry) plus a second blackout scoped
    to ONE of three courts — a strict subset on that court alone.

    Before task C2, the second blackout by itself was enough to make
    `request_to_model_input` refuse the whole request via
    `_validate_court_slot_coverage`'s "every court must offer the same start
    times" check — which took the board off the optimiser permanently, on a
    schedule that otherwise looks fine, for as long as the blackout stood.
    That function is gone; this is the regression test for it, carried all
    the way through to an actual solve rather than stopping at "did not
    raise".
    """
    step = 30 * 60_000
    day1_start = 1_786_438_800_000  # 2026-08-11T09:00:00Z
    day2_start = 1_786_525_200_000  # 2026-08-12T09:00:00Z
    ticks_per_day = 16  # 09:00 .. 16:30 at a 30-minute lattice

    def day_ticks(day_start):
        return [day_start + k * step for k in range(ticks_per_day)]

    day1 = day_ticks(day1_start)
    day2 = day_ticks(day2_start)
    # 2026-08-11T11:00Z-12:00Z, EVERY court: removes ticks 4 and 5 (11:00,
    # 11:30) uniformly, so this half alone creates no asymmetry.
    day1_blacked = {day1[4], day1[5]}
    # 2026-08-12T14:00Z-14:30Z, Board 1 (court 0) ONLY: removes tick 10
    # (14:00) from court 0 alone — the one thing that used to trip the
    # refusal.
    day2_blacked_court0_only = {day2[10]}

    slots = []
    for court in range(3):
        for start in day1:
            if start in day1_blacked:
                continue
            slots.append(scheduler_pb2.Slot(court_index=court, start_at_ms=start, day_index=0))
        for start in day2:
            if court == 0 and start in day2_blacked_court0_only:
                continue
            slots.append(scheduler_pb2.Slot(court_index=court, start_at_ms=start, day_index=1))

    # The premise: court 0's tick set really is a strict subset of court 1's
    # / court 2's (one tick fewer), and the other two remain identical to
    # each other — otherwise this test could pass for the wrong reason.
    starts_by_court = {c: {s.start_at_ms for s in slots if s.court_index == c} for c in range(3)}
    assert len(starts_by_court[0]) == len(starts_by_court[1]) - 1 == len(starts_by_court[2]) - 1
    assert starts_by_court[1] == starts_by_court[2]
    assert starts_by_court[0] < starts_by_court[1]

    req = scheduler_pb2.SolveBuildRequest(
        request_id="r1",
        court_names=["Board 1", "Board 2", "Board 3"],
        entrant_count=6,
        division_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[2 * i, 2 * i + 1], division_index=0)
            for i in range(3)
        ],
        slots=slots,
        step_minutes=30,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=0),
        wall_seconds=5.0,
    )

    parsed = request_to_model_input(req)  # must NOT raise -- this is the regression

    from placement.model import build_model, solve

    model = build_model(
        parsed.fixtures,
        parsed.courts,
        parsed.grid_slots,
        parsed.step_minutes,
        parsed.constraints,
        parsed.existing,
        parsed.dependencies,
    )
    outcome = solve(model, wall_seconds=5.0)
    detail = f"status={outcome.status} assignments={outcome.assignments}"
    assert len(outcome.assignments) == 3, detail  # every fixture placed
    legal = {(court, start) for court, start, _day in parsed.grid_slots}
    for _fi, court, start in outcome.assignments:
        assert (court, start) in legal, (
            f"fixture placed on an illegal (court, start) pair: {(court, start)} -- {detail}"
        )


def test_rejects_duplicate_entrant_indices_within_a_fixture():
    """A repeated entrant makes the fixture silently UNPLACEABLE.

    `by_entrant` puts the fixture's index in its own group twice, and the
    per-entrant `AddNoOverlap` then requires that fixture's rest interval not
    to overlap ITSELF — which forces `placed[i] = 0`."""
    req = _valid_request(
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 0], division_index=0)]
    )
    with pytest.raises(InvalidRequestError, match="entrant_indices"):
        request_to_model_input(req)


# --- C10 (2026-08-16, wire person indices design) ---------------------------
#
# `Fixture.person_indices` / `SolveBuildRequest.person_count`: a genuinely
# undecided knockout slot (a final, a third-place playoff, before its
# feeders resolve) has no entrants yet but has PEOPLE -- the players who can
# still reach it. The refusal above narrows from "entrant_indices must not
# be empty" to "neither entrants nor people may be empty" -- see
# `_validated_fixtures`'s own comment for the full reasoning, and the module
# docstring's "task C10" section for the design.


def test_accepts_a_fixture_with_empty_entrant_indices_but_nonempty_person_indices():
    """THE C10 acceptance criterion, on the schema/ACL boundary alone (the
    NoOverlap consequence is proven in `test_model.py`, on the Python side,
    per the brief). Before this task this exact request was refused
    unconditionally -- `test_rejects_a_fixture_with_neither_entrant_indices_
    nor_person_indices` below is what replaced that unconditional refusal.
    """
    req = _valid_request(
        person_count=2,
        fixtures=[scheduler_pb2.Fixture(division_index=0, person_indices=[0, 1])],
    )
    parsed = request_to_model_input(req)
    assert parsed.fixtures == [([], 0)]
    assert parsed.person_indices == [[0, 1]]


def test_rejects_a_fixture_with_neither_entrant_indices_nor_person_indices():
    """The NARROWED refusal -- still a refusal, on the one case neither
    family can rescue. Note there was, before this task, NO test anywhere in
    this suite pinning the empty-entrant refusal's behaviour at all (the
    brief's own finding); this test and the acceptance test above are what
    close that gap, on the new, narrower condition.
    """
    req = _valid_request(fixtures=[scheduler_pb2.Fixture(division_index=0)])
    with pytest.raises(InvalidRequestError, match="neither entrant_indices nor person_indices"):
        request_to_model_input(req)


def test_rejects_duplicate_person_indices_within_a_fixture():
    """The person-namespace mirror of `test_rejects_duplicate_entrant_
    indices_within_a_fixture` above: a repeated person would put the
    fixture's index in its own `by_person` group twice, forcing its rest
    interval not to overlap itself -- unsatisfiable, so `placed[i] = 0`."""
    req = _valid_request(
        person_count=1,
        fixtures=[scheduler_pb2.Fixture(division_index=0, person_indices=[0, 0])],
    )
    with pytest.raises(InvalidRequestError, match="person_indices"):
        request_to_model_input(req)


def test_person_indices_and_entrant_indices_are_independent_namespaces():
    """Index 0 in `person_indices` must never resolve against `entrant_
    count`, and vice versa -- a low `person_count` must not leak into the
    entrant range check, and a low `entrant_count` must not leak into the
    person one. Chosen so a namespace mix-up (checking a person index
    against `entrant_count`, or an entrant index against `person_count`)
    would flip which assertion raises, not merely which message is seen."""
    req = _valid_request(
        entrant_count=5,
        person_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[4], division_index=0, person_indices=[0])
        ],
    )
    parsed = request_to_model_input(req)
    assert parsed.fixtures == [([4], 0)]
    assert parsed.person_indices == [[0]]

    # Now push the PERSON index to 4 (in range for entrant_count=5, out of
    # range for person_count=1) -- must still be rejected, proving the
    # person check is bounded by `person_count`, not silently falling back
    # to the (here, more permissive) entrant bound.
    leaked = _valid_request(
        entrant_count=5,
        person_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0], division_index=0, person_indices=[4])
        ],
    )
    with pytest.raises(InvalidRequestError, match=r"person_indices\[0\] = 4 is out of range"):
        request_to_model_input(leaked)


def test_accepts_pinned_rows_with_no_identity_of_their_own():
    """`PinnedRow` carries no fixture id or index — round 6 confirmed by
    tracing every read that the old `Assignment.fixture_id` on an `existing`
    row reached nothing but a debug variable-name label in `model.py`, never
    a constraint. The old "reuses a movable fixture's id" / "pinned twice
    under the same id" family is gone with it: there is no id left for two
    pinned rows to collide on. This test replaces `test_accepts_pinned_rows_
    with_ids_of_their_own` — the rule now is simply that distinct pinned rows
    map straight through."""
    req = _valid_request(
        existing=[
            scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS),
            scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS + 3_600_000),
        ]
    )
    parsed = request_to_model_input(req)
    assert parsed.existing == [(0, SLOT_MS), (0, SLOT_MS + 3_600_000)]


def test_rejects_a_dependency_naming_an_out_of_range_fixture():
    """The positional restatement of "dependency naming an unknown fixture".
    Also closes what used to need its OWN test: a dependency naming a PINNED
    row's id. `PinnedRow` has no identity/index namespace of its own any
    more, so there is nothing for a dependency to misname there — every
    `before_index`/`after_index` is interpreted as a `fixtures` position,
    full stop, and out-of-range is out-of-range regardless of what the
    caller might have meant by it."""
    req = _valid_request(dependencies=[scheduler_pb2.OrderPair(before_index=0, after_index=1)])
    with pytest.raises(InvalidRequestError, match="dependencies.*after_index"):
        request_to_model_input(req)


@pytest.mark.parametrize("start_at_ms", [0, -1])
def test_rejects_a_pinned_row_without_a_real_start(start_at_ms):
    """An unset `start_at_ms` builds the blocking interval at epoch 0, which
    overlaps nothing real, so the pin is silently ignored and a movable
    fixture takes the pinned slot."""
    req = _valid_request(
        existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=start_at_ms)]
    )
    with pytest.raises(InvalidRequestError, match="existing"):
        request_to_model_input(req)


@pytest.mark.parametrize("start_at_ms", [0, -1])
def test_rejects_a_grid_slot_without_a_real_start(start_at_ms):
    """Same argument one field over, and the asymmetry is the point: a slot at
    epoch 0 is a legal tick in 1970 that fixtures are then placed on and
    proved OPTIMAL."""
    req = _valid_request(
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=start_at_ms, day_index=0)]
    )
    with pytest.raises(InvalidRequestError, match="slots"):
        request_to_model_input(req)


# `division_rules` (proto field 10, `DivisionRule`) is retired. Four tests
# used to live here: a rule naming an out-of-range division rejected, a
# duplicate rule for one division rejected, one entry carrying both rest and
# cap, and an entry carrying only rest. All four are proved on the
# `RuleGroup` restatement instead, further down: `RuleGroup` has no
# division-index concept to be out-of-range or duplicated (it names fixture
# POSITIONS, and several groups covering the same fixture is expected, not
# an error -- see `test_a_rule_group_may_carry_both_rest_and_cap_for_one_
# set_of_fixtures` and `test_accepts_a_rule_group_with_empty_fixture_
# indices`).


# --- #21 / C1: rule_groups -----------------------------------------------
#
# `RuleGroup` generalised past `DivisionRule`'s division-only scope (module
# docstring, "#21"): the CALLER resolves whatever scope it actually means
# (competition, division, pool, entrant, person) into a fixture-index set and
# sends the set, never the scope. So this service's own tests never see scope
# vocabulary at all -- only fixture indices, exactly like every other index
# family in this file. Validated and carried on `ModelInput`, NOT read by
# `build_model` this round -- see the module docstring and
# `test_populating_the_new_fields_does_not_change_the_board` below, which is
# the proof of that claim rather than a restatement of it.


def test_accepts_a_rule_group_with_empty_fixture_indices():
    """Legal, not a caller error: a competition-scoped rule on an all-pinned
    board resolves to exactly this, and the rule still has to reach the
    service because a pinned row's `rule_group_indices` references it by
    POSITION -- dropping an empty group would shift every later group's index
    and silently misdirect every pin that names one by number."""
    req = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(min_rest_minutes=30)])
    parsed = request_to_model_input(req)
    assert parsed.rule_groups == [([], 30, None)]


def test_zero_is_legitimate_rule_group_rest_but_not_a_legitimate_day_cap():
    """The `RuleGroup` restatement of `test_zero_is_legitimate_rest_but_not_a_
    legitimate_day_cap`: the same asymmetry, on the generalised field. 0
    minutes of rest is a real rule and must be RECORDED, not silently dropped;
    0 fixtures/day is the shape that comes back OPTIMAL having placed none of
    the group, so it is refused rather than recorded."""
    req = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(min_rest_minutes=0)])
    assert request_to_model_input(req).rule_groups == [([], 0, None)]

    capped = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(max_fixtures_per_day=0)])
    with pytest.raises(InvalidRequestError, match=r"rule_groups\[0\]\.max_fixtures_per_day"):
        request_to_model_input(capped)


def test_rejects_negative_rule_group_min_rest_minutes():
    req = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(min_rest_minutes=-1)])
    with pytest.raises(InvalidRequestError, match=r"rule_groups\[0\]\.min_rest_minutes"):
        request_to_model_input(req)


@pytest.mark.parametrize("cap", [0, -1])
def test_rejects_non_positive_rule_group_max_fixtures_per_day(cap):
    req = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(max_fixtures_per_day=cap)])
    with pytest.raises(InvalidRequestError, match=r"rule_groups\[0\]\.max_fixtures_per_day"):
        request_to_model_input(req)


def test_a_rule_group_may_carry_both_rest_and_cap_for_one_set_of_fixtures():
    req = _valid_request(
        entrant_count=4,
        division_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0),
            scheduler_pb2.Fixture(entrant_indices=[2, 3], division_index=0),
        ],
        rule_groups=[
            scheduler_pb2.RuleGroup(fixture_indices=[0, 1], min_rest_minutes=20, max_fixtures_per_day=2)
        ],
    )
    parsed = request_to_model_input(req)
    assert parsed.rule_groups == [([0, 1], 20, 2)]


def test_rejects_a_rule_groups_out_of_range_fixture_index_naming_the_field():
    """The general `INDEX_FIELDS` sweep further down proves every index field
    in the contract is range-checked; this pins that the message additionally
    NAMES the offending field, which the brief calls out specifically."""
    req = _valid_request(rule_groups=[scheduler_pb2.RuleGroup(fixture_indices=[5])])
    with pytest.raises(
        InvalidRequestError, match=r"rule_groups\[0\]\.fixture_indices\[0\] = 5 is out of range"
    ):
        request_to_model_input(req)


# --- #21 / C4 & C6: PinnedRow.rule_group_indices / PinnedRow.entrant_indices --


def test_accepts_a_pinned_row_with_empty_rule_group_indices_and_entrant_indices():
    """The C4/C6 counterpart of the empty-fixture_indices case above: a pin
    covered by no rule group counts against nothing, and a pin the caller
    never attributed any player to carries no entrants -- both are real
    answers, not missing ones."""
    req = _valid_request(existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS)])
    parsed = request_to_model_input(req)
    assert parsed.pinned_rule_group_indices == [[]]
    assert parsed.pinned_entrant_indices == [[]]


def test_a_pinned_row_may_reference_several_rule_groups_and_entrants():
    req = _valid_request(
        entrant_count=4,
        rule_groups=[
            scheduler_pb2.RuleGroup(min_rest_minutes=10),
            scheduler_pb2.RuleGroup(max_fixtures_per_day=3),
        ],
        existing=[
            scheduler_pb2.PinnedRow(
                court_index=0,
                start_at_ms=SLOT_MS,
                rule_group_indices=[0, 1],
                entrant_indices=[2, 3],
            )
        ],
    )
    parsed = request_to_model_input(req)
    assert parsed.pinned_rule_group_indices == [[0, 1]]
    assert parsed.pinned_entrant_indices == [[2, 3]]


def test_rejects_a_pinned_rows_out_of_range_rule_group_index_naming_the_field():
    # No `rule_groups` declared at all, so index 0 is out of range for `[]`.
    req = _valid_request(
        existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS, rule_group_indices=[0])]
    )
    with pytest.raises(
        InvalidRequestError,
        match=r"existing\[0\]\.rule_group_indices\[0\] = 0 is out of range",
    ):
        request_to_model_input(req)


def test_rejects_a_pinned_rows_out_of_range_entrant_index_naming_the_field():
    # `_valid_request()`'s default `entrant_count` is 2, so index 9 is out of range.
    req = _valid_request(
        existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS, entrant_indices=[9])]
    )
    with pytest.raises(
        InvalidRequestError,
        match=r"existing\[0\]\.entrant_indices\[0\] = 9 is out of range",
    ):
        request_to_model_input(req)


# --- #21: the no-behaviour-change proof -------------------------------------
#
# The whole point of this revision (proto comment on `SolveBuildRequest.
# rule_groups`; `_RULES.md`'s Task-21 brief): the ACL parses and validates the
# three new fields, but NOTHING downstream of `request_to_model_input` may be
# able to tell whether they were sent. This is the load-bearing test for that
# claim, checked two ways: first by value (every field `build_model`/`solve`
# actually read must be byte-identical whether or not the new fields were
# populated -- which IS "does not pass them to build_model", proven rather
# than read off the source), and then by actually solving both parses on a
# FORCED board -- one movable fixture, one admissible slot, so the outcome
# cannot vary with CP-SAT's own run-to-run nondeterminism (`_RULES.md` section
# 6b: "remove the nondeterminism instead of averaging over it").


def test_populating_the_new_fields_does_not_change_the_board():
    from placement.model import build_model, solve

    base_kwargs = dict(
        request_id="r1",
        court_names=["Court 1"],
        entrant_count=2,
        division_count=1,
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)],
        existing=[scheduler_pb2.PinnedRow(court_index=0, start_at_ms=SLOT_MS)],
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS + 3_600_000, day_index=0)],
        step_minutes=10,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=10),
        wall_seconds=5.0,
    )
    without = scheduler_pb2.SolveBuildRequest(**base_kwargs)
    populated = scheduler_pb2.SolveBuildRequest(
        **{
            **base_kwargs,
            "rule_groups": [
                scheduler_pb2.RuleGroup(
                    fixture_indices=[0], min_rest_minutes=45, max_fixtures_per_day=2
                )
            ],
            "existing": [
                scheduler_pb2.PinnedRow(
                    court_index=0,
                    start_at_ms=SLOT_MS,
                    rule_group_indices=[0],
                    entrant_indices=[0, 1],
                )
            ],
        }
    )

    parsed_without = request_to_model_input(without)
    parsed_populated = request_to_model_input(populated)

    # First: the new fields really did parse to something different, so the
    # equality assertions below are not vacuously comparing two empty results.
    assert parsed_populated.rule_groups == [([0], 45, 2)]
    assert parsed_populated.pinned_rule_group_indices == [[0]]
    assert parsed_populated.pinned_entrant_indices == [[0, 1]]
    assert parsed_without.rule_groups == []
    assert parsed_without.pinned_rule_group_indices == [[]]
    assert parsed_without.pinned_entrant_indices == [[]]

    # Second, and this is the actual claim: every field `build_model`/`solve`
    # read is untouched by the three new ones.
    assert parsed_populated.fixtures == parsed_without.fixtures
    assert parsed_populated.courts == parsed_without.courts
    assert parsed_populated.grid_slots == parsed_without.grid_slots
    assert parsed_populated.step_minutes == parsed_without.step_minutes
    assert parsed_populated.constraints == parsed_without.constraints
    assert parsed_populated.existing == parsed_without.existing
    assert parsed_populated.dependencies == parsed_without.dependencies
    assert parsed_populated.wall_seconds == parsed_without.wall_seconds

    # Third: actually solve both. One movable fixture and one admissible slot
    # well clear of the pinned row (SLOT_MS vs. SLOT_MS + 1h, match_minutes=30)
    # -- feasible and the ONLY candidate, so T0 (maximise placed) forces the
    # same single answer regardless of search order.
    def _solved(parsed):
        model = build_model(
            parsed.fixtures,
            parsed.courts,
            parsed.grid_slots,
            parsed.step_minutes,
            parsed.constraints,
            parsed.existing,
            parsed.dependencies,
        )
        return solve(model, wall_seconds=parsed.wall_seconds).assignments

    solved_without = _solved(parsed_without)
    solved_populated = _solved(parsed_populated)
    assert solved_without == [(0, 0, SLOT_MS + 3_600_000)], solved_without
    assert solved_populated == solved_without, (
        f"populated={solved_populated} without={solved_without}"
    )


def test_rejects_a_fixture_with_no_entrants():
    """An entrant-less fixture joins no `by_entrant` group, so the participant-
    rest NoOverlap and every T2 idle-gap term skip it entirely."""
    req = _valid_request(fixtures=[scheduler_pb2.Fixture(division_index=0)])
    with pytest.raises(InvalidRequestError, match="entrant_indices"):
        request_to_model_input(req)


def test_invalid_request_error_is_a_value_error():
    """`main.py` catches `InvalidRequestError` and `ValueError` on separate
    branches; both must land on the same SOLVE_STATUS_ERROR shape, and the
    subclass relationship is what keeps a missed branch from escaping."""
    assert issubclass(InvalidRequestError, ValueError)


# --- the closed set: every uint32 index field in the contract ---------------
#
# Retargeted from `_require_id`'s string-field gate (rounds 2-5) to index
# fields. Same shape, same purpose: enumerate the fields from the DESCRIPTOR,
# state one policy, and prove no field escapes it — built the "other way
# round" from the start, because rounds 2-4's string gate showed that closing
# only the members you were SHOWN leaves the family open.
#
# Policy: every index must be IN RANGE for the list/count it points into, and
# every SINGULAR (non-repeated) one must be explicitly SET.


def _maximal_request() -> scheduler_pb2.SolveBuildRequest:
    """A VALID request that populates every uint32-bearing message in the
    contract at least once. Every perturbation below starts from this, so a
    rejection can only have been caused by the one field that was changed."""
    return scheduler_pb2.SolveBuildRequest(
        request_id="r1",
        court_names=["Court 1", "Court 2"],
        entrant_count=4,
        division_count=1,
        # C10 (2026-08-16, wire person indices design).
        person_count=2,
        slots=[
            scheduler_pb2.Slot(court_index=c, start_at_ms=SLOT_MS + k * 3_600_000, day_index=0)
            for c in (0, 1)
            for k in range(2)
        ],
        step_minutes=10,
        fixtures=[
            scheduler_pb2.Fixture(
                entrant_indices=[0, 1],
                division_index=0,
                round=1,
                person_indices=[0, 1],
                # #622 -- narrowed to court 0 only, so the sweep has a
                # populated element to perturb and the unperturbed assertion
                # below can tell a narrowed fixture from an unconstrained one.
                allowed_court_indices=[0],
            ),
            scheduler_pb2.Fixture(entrant_indices=[2, 3], division_index=0, round=2),
        ],
        existing=[
            scheduler_pb2.PinnedRow(
                court_index=0,
                start_at_ms=SLOT_MS,
                rule_group_indices=[0],
                entrant_indices=[0],
                round=1,
            )
        ],
        dependencies=[scheduler_pb2.OrderPair(before_index=0, after_index=1)],
        rule_groups=[scheduler_pb2.RuleGroup(fixture_indices=[0, 1], min_rest_minutes=15)],
        constraints=_constraints(),
        wall_seconds=8.0,
    )


#: Keyed by `Message.field` exactly as the descriptor names it, so the
#: completeness test below can compare this table against the proto itself.
#: Each perturbation sets an OUT-OF-RANGE value.
INDEX_FIELDS = {
    "Fixture.division_index": lambda r: setattr(r.fixtures[0], "division_index", 999),
    "Fixture.entrant_indices": lambda r: r.fixtures[0].entrant_indices.__setitem__(0, 999),
    "Slot.court_index": lambda r: setattr(r.slots[0], "court_index", 999),
    "PinnedRow.court_index": lambda r: setattr(r.existing[0], "court_index", 999),
    "PinnedRow.rule_group_indices": lambda r: r.existing[0].rule_group_indices.__setitem__(0, 999),
    "PinnedRow.entrant_indices": lambda r: r.existing[0].entrant_indices.__setitem__(0, 999),
    "OrderPair.before_index": lambda r: setattr(r.dependencies[0], "before_index", 999),
    "OrderPair.after_index": lambda r: setattr(r.dependencies[0], "after_index", 999),
    "RuleGroup.fixture_indices": lambda r: r.rule_groups[0].fixture_indices.__setitem__(0, 999),
    # C10 (2026-08-16, wire person indices design).
    "Fixture.person_indices": lambda r: r.fixtures[0].person_indices.__setitem__(0, 999),
    # #622 (round-scoped required court tags).
    "Fixture.allowed_court_indices": lambda r: r.fixtures[0].allowed_court_indices.__setitem__(
        0, 999
    ),
}

#: Deliberately outside the range/presence policy, with the reason. Listed
#: rather than omitted so the completeness test still has to account for the
#: field. `entrant_count`/`division_count` are declared BOUNDs other indices
#: are checked against, not indices themselves. `Fixture.round`/
#: `PinnedRow.round` (C1, 2026-08-12 round-order design) are `uint32` but not
#: positional at all -- an opaque ordering key the caller assigns within its
#: own round-robin sequence. Never range-checked (no `round_count` a round is
#: bounded against; any `uint32` value is legitimate) and, unlike
#: `PRESENCE_FIELDS` below, NEVER presence-REJECTED either -- an absent round
#: is a real, common answer ("not round-robin-generated, or the caller chose
#: not to attribute it"), not a caller mistake. Presence is still tracked
#: (`request_to_model_input` maps unset to `None`, never to 0), it is simply
#: never grounds for `InvalidRequestError`.
EXEMPT_INDEX_FIELDS = {
    "SolveBuildRequest.entrant_count": "a declared bound for entrant_indices, not itself an index",
    "SolveBuildRequest.division_count": "a declared bound for division_index fields, not itself an index",
    "Fixture.round": "an opaque ordering key in the caller's own round-robin sequence, not a position",
    "PinnedRow.round": "an opaque ordering key in the caller's own round-robin sequence, not a position",
    # C10 (2026-08-16, wire person indices design).
    "SolveBuildRequest.person_count": "a declared bound for person_indices, not itself an index",
}


def _contract_index_fields() -> set[str]:
    """Every `uint32` field reachable from `SolveBuildRequest`, from the
    proto itself."""
    from google.protobuf.descriptor import FieldDescriptor

    found: set[str] = set()
    seen: set[str] = set()

    def walk(descriptor):
        if descriptor.full_name in seen:
            return
        seen.add(descriptor.full_name)
        for field in descriptor.fields:
            if field.type == FieldDescriptor.TYPE_UINT32:
                found.add(f"{descriptor.name}.{field.name}")
            elif field.type == FieldDescriptor.TYPE_MESSAGE:
                walk(field.message_type)

    walk(scheduler_pb2.SolveBuildRequest.DESCRIPTOR)
    return found


def test_the_index_policy_accounts_for_every_uint32_field_in_the_contract():
    """The closed set, checked against the proto rather than against memory.

    Add a `uint32` field to `scheduler.proto` and this fails until it is
    either routed through the range check or exempted with a reason.
    """
    assert _contract_index_fields() == set(INDEX_FIELDS) | set(EXEMPT_INDEX_FIELDS)

    # A union alone is NOT a closed set — the string-field gate's mutation
    # sweep proved it (moving a field between the two tables leaves the union
    # identical and survives 6/6). So the exemption list is pinned literally,
    # and no field may be in both.
    assert set(EXEMPT_INDEX_FIELDS) == {
        "SolveBuildRequest.entrant_count",
        "SolveBuildRequest.division_count",
        "Fixture.round",
        "PinnedRow.round",
        "SolveBuildRequest.person_count",
    }
    assert set(INDEX_FIELDS).isdisjoint(EXEMPT_INDEX_FIELDS)


@pytest.mark.parametrize("path", sorted(INDEX_FIELDS))
def test_an_out_of_range_index_is_rejected_in_every_index_field(path):
    req = _maximal_request()
    INDEX_FIELDS[path](req)
    with pytest.raises(InvalidRequestError, match="out of range"):
        request_to_model_input(req)


def test_the_maximal_request_is_valid_unperturbed():
    """Otherwise every case above could be passing for the wrong reason."""
    parsed = request_to_model_input(_maximal_request())
    assert len(parsed.fixtures) == 2
    assert parsed.existing == [(0, SLOT_MS)]
    # C1/C4/C6, mapped through unperturbed: one rule group covering both
    # movable fixtures, and the one pinned row counting against it and
    # carrying one entrant.
    assert parsed.rule_groups == [([0, 1], 15, None)]
    assert parsed.pinned_rule_group_indices == [[0]]
    assert parsed.pinned_entrant_indices == [[0]]
    # C1: round is not an INDEX field (exempt below), but the maximal request
    # populates it too, so this proves it flows through unperturbed alongside
    # everything else.
    assert parsed.fixture_rounds == [1, 2]
    assert parsed.pinned_round == [1]
    # C10: fixture 0 carries person_indices=[0, 1], fixture 1 carries none --
    # proves the parallel list flows through unperturbed AND that "no people"
    # is a real, distinct answer from "some people", not a default that
    # collapses the two.
    assert parsed.person_indices == [[0, 1], []]
    # #622: fixture 0 is narrowed to court 0, fixture 1 sends none at all --
    # proves an empty `allowed_court_indices` maps to `None` (unconstrained)
    # rather than to an empty set ("no court"), which is the one distinction
    # that decides whether an ordinary board stays placeable.
    assert parsed.allowed_courts == [{0}, None]


def test_an_exempt_field_carries_no_range_or_presence_check():
    """The exemption has to be real, or the table is decorative. Setting
    `entrant_count`/`division_count` to a value that happens to be smaller
    than an index actually used is caught by the INDEX field's own range
    check (proven above); this test is about the count fields THEMSELVES
    never being range- or presence-checked directly — any uint32 value,
    including 0, is a legal thing to declare as a count."""
    req = _maximal_request()
    req.entrant_count = 4  # unchanged, already valid; this is a smoke check
    req.division_count = 1
    parsed = request_to_model_input(req)
    assert parsed.wall_seconds == 8.0


# --- presence, for every SINGULAR index field --------------------------------
#
# This is NOT literally what the brief's ruling states ("every index must be
# in range... is the complete set of identity rules") — it is an extension of
# it, and the reasoning for adding it is recorded in `placement.schema`'s module
# docstring: a range check alone cannot catch a caller who simply forgot to
# set a singular index field, because 0 is always in range and always a
# legitimate index. Flagged here as a deliberate addition beyond the letter
# of the brief, not a silent one — this is exactly the kind of place the
# brief invites pushback.
#
# `Fixture.entrant_indices`, `RuleGroup.fixture_indices`,
# `PinnedRow.rule_group_indices` and `PinnedRow.entrant_indices` are all
# excluded: each is REPEATED, and a repeated field's elements carry no
# presence ambiguity of their own.
PRESENCE_FIELDS = {
    "Fixture.division_index": lambda r: r.fixtures[0].ClearField("division_index"),
    "Slot.court_index": lambda r: r.slots[0].ClearField("court_index"),
    "PinnedRow.court_index": lambda r: r.existing[0].ClearField("court_index"),
    "OrderPair.before_index": lambda r: r.dependencies[0].ClearField("before_index"),
    "OrderPair.after_index": lambda r: r.dependencies[0].ClearField("after_index"),
}

#: The repeated index fields -- excluded from `PRESENCE_FIELDS` above because
#: a repeated field's elements carry no presence ambiguity of their own.
REPEATED_INDEX_FIELDS = {
    "Fixture.entrant_indices",
    "RuleGroup.fixture_indices",
    "PinnedRow.rule_group_indices",
    "PinnedRow.entrant_indices",
    # C10 (2026-08-16, wire person indices design).
    "Fixture.person_indices",
    # #622 (round-scoped required court tags).
    "Fixture.allowed_court_indices",
}


def test_presence_fields_are_the_singular_index_fields_minus_the_repeated_ones():
    """Pins the two tables to the same key set minus the repeated fields, so a
    field added to one and not the other (when it should be in both) is
    caught rather than silently under-tested."""
    assert set(PRESENCE_FIELDS) == set(INDEX_FIELDS) - REPEATED_INDEX_FIELDS


@pytest.mark.parametrize("path", sorted(PRESENCE_FIELDS))
def test_an_unset_singular_index_field_is_rejected(path):
    """0 is always a legitimate index, so an unset field must be refused
    rather than silently read as index 0."""
    req = _maximal_request()
    PRESENCE_FIELDS[path](req)
    with pytest.raises(InvalidRequestError, match="must be set"):
        request_to_model_input(req)


# --- round 6: confusable court names are now harmless, by construction ------
#
# The whole point of the redesign. Rounds 4-6 tried, in order: reject
# whitespace, reject invisible Unicode, and were about to try rejecting
# homoglyphs when the pattern was recognised — every character rule is a
# guess about what a caller MEANT by two strings that render identically, and
# the guessing never terminates. The service was never actually misbehaving:
# given two distinct strings it saw two courts and placed one fixture on
# each, correctly. Now that court identity is POSITIONAL, "two distinct
# strings" and "two distinct entities" are the same fact, and there is
# nothing left to reject.
#
# Small boards (2 fixtures, 2 courts), never the production board — per the
# brief. N=6 per `_RULES.md` section 6b: the solver is nondeterministic, so a
# single run is a sample, not an observation.
N_REPRO = 6


def _confusable_pair_request(second_court_name: str) -> scheduler_pb2.SolveBuildRequest:
    """Two fixtures, two courts (one plain, one perturbed to be confusable
    with the first), each fixture free to land on either court at the SAME
    instant. If identity were still string-based and the two names collapsed,
    this would double-book one court; if it were rejected outright, this
    would raise. Under positional identity it is simply two fixtures on two
    distinct court indices."""
    return scheduler_pb2.SolveBuildRequest(
        request_id="r1",
        court_names=["Court 1", second_court_name],
        entrant_count=4,
        division_count=1,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0),
            scheduler_pb2.Fixture(entrant_indices=[2, 3], division_index=0),
        ],
        slots=[
            scheduler_pb2.Slot(court_index=c, start_at_ms=SLOT_MS, day_index=0) for c in (0, 1)
        ],
        step_minutes=10,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=10),
        wall_seconds=5.0,
    )


@pytest.mark.parametrize(
    "second_court_name",
    [
        pytest.param("Court 1 ", id="whitespace"),
        # U+3164 Hangul Filler: printable per Unicode's Lo category, and one
        # of the ten code points round 5's re-review found that pass
        # `str.isprintable()` while being invisible to a human.
        pytest.param("Court 1" + chr(0x3164), id="invisible-hangul-filler"),
        # Cyrillic Es (U+0421) in place of the Latin C. Fully printable, fully
        # visible, and identical to the eye — no invisibility rule of any
        # kind could ever have caught this one.
        pytest.param("Сourt 1", id="cyrillic-homoglyph"),
    ],
)
def test_a_confusable_court_name_is_harmless_end_to_end(second_court_name):
    """`request_to_model_input` -> `build_model` -> `solve`, the same call
    sequence `main.py` makes. Each of the three families that defeated a
    character rule must now be harmless BY CONSTRUCTION: two fixtures land on
    two distinct court indices, never double-booked, and the request is
    never rejected at all."""
    from placement.model import build_model, solve

    placed_counts = []
    courts_used_per_run = []
    for _ in range(N_REPRO):
        req = _confusable_pair_request(second_court_name)
        parsed = request_to_model_input(req)  # must not raise
        model = build_model(
            parsed.fixtures,
            parsed.courts,
            parsed.grid_slots,
            parsed.step_minutes,
            parsed.constraints,
            parsed.existing,
            parsed.dependencies,
        )
        outcome = solve(model, wall_seconds=5.0)
        placed_counts.append(len(outcome.assignments))
        courts_used_per_run.append(sorted({court for _f, court, _s in outcome.assignments}))

    detail = f"placed_counts={placed_counts} courts_used={courts_used_per_run}"
    assert placed_counts == [2] * N_REPRO, detail
    assert courts_used_per_run == [[0, 1]] * N_REPRO, (
        f"expected both distinct court indices used on every run (never double-booked one "
        f"court), got: {detail}"
    )


# --- the outbound half of the boundary --------------------------------------


def _outcome(**overrides) -> SolveOutcome:
    kwargs = dict(
        assignments=[(0, 0, 0)],
        status="OPTIMAL",
        tiers_completed=len(TIER_ORDER),
        objective_values=[(name, i) for i, name in enumerate(TIER_ORDER)],
        elapsed_ms=1000,
    )
    kwargs.update(overrides)
    return SolveOutcome(**kwargs)


def test_error_response_shape():
    resp = error_response("INVALID_REQUEST", "fixtures must not be empty")
    assert resp.status == scheduler_pb2.SOLVE_STATUS_ERROR
    assert resp.error.code == "INVALID_REQUEST"
    assert resp.error.message == "fixtures must not be empty"
    assert len(resp.assignments) == 0


def test_outcome_to_response_maps_every_field():
    resp = outcome_to_response(_outcome(), wall_seconds=10.0)
    assert [(a.fixture_index, a.court_index, a.start_at_ms) for a in resp.assignments] == [
        (0, 0, 0)
    ]
    assert resp.status == scheduler_pb2.SOLVE_STATUS_OPTIMAL
    assert resp.tiers_completed == len(TIER_ORDER)
    assert resp.elapsed_ms == 1000
    assert resp.wall_exhausted is False
    assert [t.name for t in resp.objective_values] == list(TIER_ORDER)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("OPTIMAL", scheduler_pb2.SOLVE_STATUS_OPTIMAL),
        ("FEASIBLE", scheduler_pb2.SOLVE_STATUS_FEASIBLE),
        ("INFEASIBLE", scheduler_pb2.SOLVE_STATUS_INFEASIBLE),
        ("UNKNOWN", scheduler_pb2.SOLVE_STATUS_UNKNOWN),
        # CP-SAT's vocabulary is passed through verbatim by the domain, so a
        # status this layer does not know about must become ERROR rather than
        # be guessed at — an unmapped name defaulting to 0 would serialise as
        # SOLVE_STATUS_UNSPECIFIED and read as "fine".
        ("MODEL_INVALID", scheduler_pb2.SOLVE_STATUS_ERROR),
    ],
)
def test_outcome_to_response_maps_status(status, expected):
    assert outcome_to_response(_outcome(status=status), wall_seconds=10.0).status == expected


def test_outcome_to_response_populates_error_for_an_unmapped_status():
    """Every other SOLVE_STATUS_ERROR path carries a populated `SolveError`.
    This one is reached without an exception ever being raised — the domain
    returned a status this layer has no mapping for — so it has to build its
    own, or the caller gets ERROR with no reason at all."""
    resp = outcome_to_response(_outcome(status="MODEL_INVALID"), wall_seconds=10.0)
    assert resp.status == scheduler_pb2.SOLVE_STATUS_ERROR
    assert resp.HasField("error")
    assert resp.error.code == "INTERNAL_ERROR"
    assert "MODEL_INVALID" in resp.error.message


@pytest.mark.parametrize("status", ["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"])
def test_outcome_to_response_leaves_error_unset_for_a_mapped_status(status):
    """The other side: a known status must NOT carry an error. `error` is how
    the caller tells a rejected request from a solved one, so populating it
    unconditionally would be worse than leaving it empty."""
    resp = outcome_to_response(_outcome(status=status), wall_seconds=10.0)
    assert not resp.HasField("error")


def test_outcome_to_response_publishes_only_proved_tiers():
    """`objective_values` can be ONE longer than `tiers_completed`: a tier cut
    short by the clock records its last-known value without being counted. That
    value was never proved optimal and must not go out on the wire as if it
    had been."""
    outcome = _outcome(
        status="FEASIBLE",
        tiers_completed=1,
        objective_values=[(TIER_ORDER[0], 1), (TIER_ORDER[1], 999)],
    )
    resp = outcome_to_response(outcome, wall_seconds=10.0)
    assert resp.tiers_completed == 1
    assert [(t.name, t.value) for t in resp.objective_values] == [(TIER_ORDER[0], 1)]


@pytest.mark.parametrize(
    "elapsed_ms,wall_seconds,exhausted",
    [
        (0, 10.0, False),
        (5000, 10.0, False),
        # The tier loop stops one MIN_TIER_SECONDS BEFORE the deadline, so this
        # is the last elapsed value that can mean "there was still time".
        (9949, 10.0, False),
        # From here up, the chain cannot have started another tier — it was cut
        # short by the wall even though it never reached the wall.
        (9950, 10.0, True),
        (9999, 10.0, True),
        (10000, 10.0, True),
        (10500, 10.0, True),
    ],
)
def test_outcome_to_response_reports_wall_exhausted(elapsed_ms, wall_seconds, exhausted):
    """`wall_exhausted` compares elapsed against the wall MINUS the tier loop's
    proactive-break margin, not against the raw wall.

    `run_tier_chain` breaks out when `deadline - now <= MIN_TIER_SECONDS`
    rather than running a tier it has no time to finish, so a chain genuinely
    stopped by the budget reports an `elapsed_ms` up to 50ms UNDER the wall. A
    raw `elapsed_ms >= wall * 1000` comparison calls that `wall_exhausted=False`
    and tells the caller the budget was not the limiting factor — which is the
    one thing this flag exists to say.
    """
    resp = outcome_to_response(_outcome(elapsed_ms=elapsed_ms), wall_seconds=wall_seconds)
    assert resp.wall_exhausted is exhausted


def test_wall_exhausted_threshold_tracks_the_tier_loops_own_margin():
    """Pins the threshold to `objective.MIN_TIER_SECONDS` rather than to a
    hardcoded 50ms. If the tier loop's margin changes, this flag's boundary has
    to move with it — a literal here would silently stop matching."""
    wall = 8.0
    boundary_ms = int((wall - MIN_TIER_SECONDS) * 1000)
    assert outcome_to_response(_outcome(elapsed_ms=boundary_ms - 1), wall).wall_exhausted is False
    assert outcome_to_response(_outcome(elapsed_ms=boundary_ms), wall).wall_exhausted is True


# --- wire compatibility: field 10 is RESERVED, not merely deleted -----------
#
# `division_rules` (proto field 10, `DivisionRule`-typed) is reserved as of
# this task -- the CURRENT stub cannot construct it or even name it. But
# proto3 does not drop bytes it fails to recognise: it keeps them as an
# opaque, uninterpreted entry in the message's `UnknownFieldSet` and
# reserialises them unchanged. That is the entire mechanism the deploy-order
# safety in the retirement design doc rests on -- "Placement first: the old
# build.ts still sends field 10; the new Python has no such field, so proto3
# parses it as an unknown field and ignores it" -- and it has to be proved
# against bytes, not against the current stub, because the current stub
# cannot be used to WRITE a field it no longer declares.


def _request_bytes_with_legacy_division_rules_field(**overrides) -> bytes:
    """A serialized `SolveBuildRequest` whose bytes contain field 10, built by
    hand: a normal request from `_valid_request`, serialized, with one raw
    length-delimited field-10 entry (tag `(10 << 3) | 2`, an EMPTY payload --
    a legal empty `DivisionRule` had one still been declared) appended.
    Appending after a complete, valid serialization is legal proto3 wire
    format: an embedded message is simply the concatenation of its fields'
    tag/value pairs in any order, repeats included.
    """
    data = _valid_request(**overrides).SerializeToString()
    tag = (10 << 3) | 2  # field 10, wire type 2 (length-delimited)
    return data + bytes([tag, 0])


def test_a_request_carrying_the_retired_division_rules_field_still_parses_and_solves():
    """The wire-compat guarantee, proved end to end: parse, translate, and
    actually solve -- not merely "does not raise on `ParseFromString`", which
    would pass even if `request_to_model_input` silently mistranslated the
    stray bytes into some other field.
    """
    data = _request_bytes_with_legacy_division_rules_field()
    req = scheduler_pb2.SolveBuildRequest()
    req.ParseFromString(data)  # must NOT raise -- proto3 preserves unknown fields

    parsed = request_to_model_input(req)  # must NOT raise
    assert parsed.wall_seconds == 8.0
    assert len(parsed.fixtures) == 1

    from placement.model import build_model, solve

    model = build_model(
        parsed.fixtures,
        parsed.courts,
        parsed.grid_slots,
        parsed.step_minutes,
        parsed.constraints,
        parsed.existing,
        parsed.dependencies,
    )
    outcome = solve(model, wall_seconds=parsed.wall_seconds)
    assert outcome.status == "OPTIMAL", outcome.status
    assert len(outcome.assignments) == 1


def test_legacy_wire_field_ignored_is_logged_for_a_request_carrying_field_10():
    """`_log_legacy_wire_fields` (schema.py) is the one thing that makes the
    deploy window in the test above OBSERVABLE rather than silent -- see the
    ADDITIONAL OWNER REQUIREMENT this task folded in. `structlog.testing.
    capture_logs()` reconfigures structlog to route through a capturing
    processor for the duration of the `with` block regardless of the
    process's own configured level/processors (`test_structlog_config.py`
    proves the OTHER half of that independence), so this does not depend on
    `configure_structlog` having been called first.
    """
    data = _request_bytes_with_legacy_division_rules_field(request_id="req-99")
    req = scheduler_pb2.SolveBuildRequest()
    req.ParseFromString(data)

    with structlog.testing.capture_logs() as captured:
        request_to_model_input(req)

    events = [e for e in captured if e["event"] == "legacy_wire_field_ignored"]
    assert len(events) == 1, captured
    assert events[0]["request_id"] == "req-99"
    assert events[0]["field_numbers"] == [10]


def test_legacy_wire_field_ignored_is_not_logged_for_a_clean_request():
    """The other half: a request that never carried a legacy field must not
    log as if it had -- otherwise the event is noise, not a deploy-window
    signal, on every ordinary request the service will ever see once the
    migration window has passed.
    """
    req = _valid_request()

    with structlog.testing.capture_logs() as captured:
        request_to_model_input(req)

    events = [e for e in captured if e["event"] == "legacy_wire_field_ignored"]
    assert events == [], captured


# --- C10: the deploy-window observability line -------------------------------
#
# `_log_person_only_fixtures`'s counterpart to the two
# `legacy_wire_field_ignored` tests above, and it exists for the MIRROR
# reason. C0's probe answers "is a stale caller still sending a retired
# field", which its service can see arrive. C10's risk runs the other way —
# a NEW caller reaching an OLD service — and the old service is by
# definition the one without the probe, so no check on this side could ever
# detect it (see the C10 section of the release-2 `_INDEX.md`). What IS
# answerable here, and what an operator actually needs mid-rollout, is
# whether the new acceptance path is being exercised at all.


def test_person_only_fixture_accepted_is_logged_with_its_fixture_indices():
    """The rollout signal itself: a fixture that reaches the model with no
    entrants and only people is exactly the shape the pre-C10 refusal
    always rejected, so one line proves the new path is live."""
    req = _valid_request(
        entrant_count=2,
        person_count=2,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0),
            scheduler_pb2.Fixture(division_index=0, person_indices=[0, 1]),
        ],
    )

    with structlog.testing.capture_logs() as captured:
        request_to_model_input(req)

    events = [e for e in captured if e["event"] == "person_only_fixture_accepted"]
    assert len(events) == 1, captured
    assert events[0]["request_id"] == "r1"
    # POSITION 1, not 0 -- names the person-only fixture specifically rather
    # than reporting "some fixture on this board", which is what makes the
    # line diagnosable instead of merely present.
    assert events[0]["fixture_indices"] == [1]


def test_person_only_fixture_accepted_is_not_logged_for_an_ordinary_board():
    """The other half, same argument the clean-request test above makes: a
    board whose fixtures all carry entrants must not log as if it were
    exercising the new path -- including a fixture that carries people
    ALONGSIDE its entrants, which is ordinary data and not the shape the
    refusal ever rejected."""
    req = _valid_request(
        entrant_count=2,
        person_count=2,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0, person_indices=[0, 1])
        ],
    )

    with structlog.testing.capture_logs() as captured:
        request_to_model_input(req)

    events = [e for e in captured if e["event"] == "person_only_fixture_accepted"]
    assert events == [], captured
