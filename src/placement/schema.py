"""The anti-corruption layer between the wire and the domain.

This module is the ONLY place a proto message is read, and the ONLY place a
plain-Python solve result is turned back into one — both directions, one file.
`placement.model` and `placement.objective` are written against tuples, dicts and
floats and must never import `scheduler_pb2`; `placement.main` orchestrates
(authenticate, translate in, compute, translate out) but holds no domain logic
and constructs no proto message of its own. That boundary is the design's, not
a style preference — it is what lets the solver be tested, benched and reasoned
about without a gRPC runtime.

It is also where degenerate requests are rejected. Proto3 has no way to say
"absent" for a non-optional scalar or for a message field, so a field the
caller simply failed to set arrives as `0` / empty and is indistinguishable
from a deliberate zero — and each of the four below makes the solver return a
confidently WRONG board with a healthy-looking status rather than an error:

  * `constraints.match_minutes == 0` — zero-width intervals, so NoOverlap
    constrains nothing and every fixture stacks on one tick. OPTIMAL.
  * a division's `max_fixtures_per_day` of `0` — that division may not be
    placed at all, and the solve reports OPTIMAL having dropped all of its
    fixtures.
  * `wall_seconds == 0` — the tier chain stops before T0, returning UNKNOWN
    with no assignments, which is exactly what an impossible board returns.
  * `slots` empty — every fixture is forced onto tick 0, and one fixture per
    court is placed and proved OPTIMAL while the rest are silently dropped.
    Measured through the real server: 8 fixtures, 2 courts, no slots -> a
    board proved OPTIMAL, `error` unset, most fixtures silently unplaced.

`build_model` and `run_tier_chain` guard the first two themselves and raise
`ValueError`. Those guards stay: they protect the bench and any future caller.
But the wire is where the mistake is actually made, so it is caught here first
and as an `InvalidRequestError`, which `placement.main` turns into a
`SOLVE_STATUS_ERROR` response carrying the reason — a well-formed answer the
caller can act on, rather than an exception crossing the RPC boundary.

--- round 6: identity is POSITIONAL, and `_require_id` is gone -------------

`_require_id` went through three rounds of character rules — whitespace,
then invisible Unicode, then homoglyphs — and never terminated, because every
rule was a guess about what a caller MEANT by two strings that render
identically. The service never actually misbehaved: given two distinct
strings it saw two courts and placed one fixture on each, correctly. The
wrongness only ever appeared when a human rendered the board — and this
context never renders (`docs/superpowers/plans/2026-08-07-placement-service-
prompts/_RULES.md` section 1: this is a PLACER only). So the fix is not a
fourth character rule; it is to stop comparing strings at all.

A fixture's identity is its position in `fixtures`. A court's is its position
in `court_names`. A division's or entrant's identity is a caller-assigned
index, bounded by a declared count (`division_count` / `entrant_count`)
rather than inferred from the largest index actually used — an inferred
bound cannot distinguish "entrant 7" from a typo, a declared one can.

In `_require_id`'s place, ONE rule, in two parts:

  * every index must be IN RANGE for the list or count it points into.
    Out-of-range is a hard reject naming the field, the value, and the bound.
  * every SINGULAR (non-repeated) index field must be explicitly SET. Proto3
    has no way to say "absent" for a non-optional scalar, and 0 is always a
    legitimate index — so without presence tracking, a caller who forgot to
    set (say) `Fixture.division_index` would silently join division 0's
    rest/cap rules rather than being refused. That is the SAME "unset vs.
    legitimately zero" trap `gap_minutes` and `Slot.day_index` already close
    with proto3 `optional`, applied to every field whose entire job is naming
    one specific entity. `entrant_indices` is exempt from this second part —
    it is REPEATED, and a repeated field's elements carry no presence
    ambiguity of their own: every element that exists was explicitly
    appended, so only its range is checked.

`tests/test_schema.py`'s completeness gate enumerates every `uint32` field
straight out of `SolveBuildRequest.DESCRIPTOR` and asserts each one either
reaches `_require_index_range` (and, where singular, `_require_index_present`)
or sits on a named exemption list — the same shape `_require_id`'s gate had,
retargeted from string fields to index fields.

Two things this rule does NOT need to reproduce from the string contract,
because the defect they closed cannot occur positionally any more:

  * canonicality ("is this the same id as that one, once whitespace/invisible
    characters/homoglyphs are accounted for") — there is no id to canonicalise.
  * `courts` uniqueness/blankness — `court_names` is now display-only, never
    compared, never required unique. Two entries that render identically are
    simply two distinct, correctly-disambiguated indices.

Most of what the old referential-integrity pass enforced — a pinned row on a
court nobody declared, a dependency naming an unresolvable fixture, a rule
naming an undeclared division — survives, now as a range check instead of a
dict-membership check. `model.py`'s own tolerance of an out-of-range index
(skip rather than crash) is UNCHANGED, for the same reason it was unchanged
in the string contract: the wire is where the mistake is caught, not the
domain, and moving the check there would take away the domain's own ability
to be fed a partial board directly (by tests, or any future caller).

--- task C2: the per-court coverage refusal is GONE, not renamed --------------

Two shapes used to be refused here by `_validate_court_slot_coverage`, which
no longer exists: a court declared without slots, and courts offering
different tick sets from one another. Both were a REFUSAL standing in for a
missing capability — `model.py` unioned every court's admissible starts into
one shared domain, so a per-court blackout could not be expressed and the
whole request was bounced back to the caller's own greedy fallback instead of
risking a fixture placed on a tick its court never actually offered.

`model.py` now enforces each court's own tick set directly (see its
docstring, "per-court grids"), so neither shape needs a refusal any more —
one is simply modelled correctly (a per-fixture domain restriction, court by
court), and the other (a court no slot mentions at all — a legitimate way to
configure a court blacked out for the whole horizon) is handled by forcing
that court's `presence_court[i][c]` to 0 for every fixture, in the domain,
rather than by rejecting the request at the ACL. The RULE survives; only the
REFUSAL does not.

--- #21: rule_groups / PinnedRow.rule_group_indices / PinnedRow.entrant_indices --

Three more fields this module owns: C1/C4/C6 of the contract revision that
generalised past `division_rules`' division-only scope -- that field (proto
field 10) has since been retired outright; see `request_to_model_input`'s own
docstring. `RuleGroup` is a rule
and the exact MOVABLE fixture set it binds — the caller resolves any scope
(competition, division, pool, entrant, person) via its own `scopeCoversFixture`
and sends the RESULT, so this service still never learns what a division, pool,
entrant or person IS (`_RULES.md` section 1). `PinnedRow.rule_group_indices` is
which of those groups a pin counts against, and `PinnedRow.entrant_indices` is
who is playing in it.

All three are parsed and validated here exactly like every other field in this
module — every index in range, every value where "unset" and "legitimately
zero" could collide presence-tracked — and, like every other validated field,
they land on `ModelInput`. What happens next has PARTIALLY changed since this
was first written: task C4 taught `build_model` to enforce a rule group's DAY
CAP (`max_fixtures_per_day`) against the fixtures and pins it covers, and task
C6 taught it to fold a pin into its entrants' participant-rest groups,
resolving that pin's own rest as the max `min_rest_minutes` over the rule
groups it counts against (`pinned_rule_group_indices`) — so `main.py` calls
`build_model` with (as of the round-order design's own C1, 2026-08-12)
TWELVE positional arguments, not seven: `rule_groups`,
`pinned_rule_group_indices`, `pinned_entrant_indices`, `fixture_rounds` and
`pinned_round` all join the original seven. A rule group's `min_rest_minutes`
applying directly to its own MOVABLE fixtures (rather than only to the pins
that count against it) is still unread and a separate, later task. See
`placement.model`'s own module docstring ("A FIFTH is CLOSED as of task C4" /
"A SIXTH is CLOSED as of task C6") for both halves' mechanics.

--- task C10 (2026-08-16): Fixture.person_indices / SolveBuildRequest.person_count --

A fourth field, and the first to touch `Fixture` itself since round 6: a
person is a DIFFERENT participant channel from an entrant, not a rename of
one. The gap this closes: an undecided knockout slot (a final, a third-place
playoff, before its feeders resolve) has no entrant yet, but has people --
the players who can still reach it -- and this service could not see that at
all before this task, because person identity never crossed the wire. The
refusal `_validated_fixtures` raises for `fixtures[i]` narrows from "entrants
must not be empty" to "neither entrants nor people may be empty"; see that
function's own comment for why the refusal survives at all rather than being
deleted.

`person_indices` is a SEPARATE namespace from `entrant_indices` throughout —
own declared bound (`person_count`, mirroring `entrant_count`), own range/
duplicate checks, own `ModelInput.person_indices` parallel list, own
`by_person` grouping in `placement.model` (module docstring, "AN EIGHTH") —
never merged into the entrant tables at any layer, matching
`packages/engine/src/scheduling/repair-domain.ts`'s `sharesParticipant`,
which already compares the two families in separate, non-crossing loops.
`main.py` now calls `build_model` with THIRTEEN positional arguments:
`person_indices` joins the twelve above, last.

Only `Fixture` was widened, not `PinnedRow`: `PinnedRow.entrant_indices` (C6)
has no `person_indices` counterpart on the wire this round, so a pin can
still only be attributed entrants, never people, for participant-rest
purposes. Deliberate scope, not an oversight -- see the PR body.

--- #622: Fixture.allowed_court_indices ------------------------------------

Round-scoped required court tags gave a fixture a candidate court set of its
own, narrower than the request-wide `court_names`. `Fixture.allowed_court_
indices` carries it, `ModelInput.allowed_courts` is its parallel list, and
`placement.model`'s section 1c is the only reader. `main.py` now calls
`build_model` with FOURTEEN positional arguments: `allowed_courts` joins the
thirteen above, last.

Empty on the wire means UNCONSTRAINED (the same reading `candidateCourts`
gives an empty required-tag list), and `_validated_fixtures` normalises that
to `None` so `build_model` never has to know the wire convention -- and so an
empty `set()` reaching it stays readable as the caller bug it would be,
rather than as "every court".
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from google.protobuf import unknown_fields

from placement.generated import scheduler_pb2
from placement.model import SolveOutcome
from placement.objective import MIN_TIER_SECONDS

log = structlog.get_logger(__name__)

# `SolveOutcome.status` is CP-SAT's own vocabulary, passed through verbatim by
# the domain. Anything not listed — MODEL_INVALID, or a future OR-Tools status
# — maps to ERROR rather than being guessed at. It must NOT fall through to the
# proto default, which is SOLVE_STATUS_UNSPECIFIED (0) and would read as fine.
STATUS_MAP = {
    "OPTIMAL": scheduler_pb2.SOLVE_STATUS_OPTIMAL,
    "FEASIBLE": scheduler_pb2.SOLVE_STATUS_FEASIBLE,
    "INFEASIBLE": scheduler_pb2.SOLVE_STATUS_INFEASIBLE,
    "UNKNOWN": scheduler_pb2.SOLVE_STATUS_UNKNOWN,
}


class InvalidRequestError(ValueError):
    """The request cannot be turned into a solvable model.

    A `ValueError` subclass on purpose: `build_model`/`solve` raise plain
    `ValueError` for the same conditions, and `placement.main` maps both onto one
    response shape. Anything that catches `ValueError` therefore cannot miss
    this, whichever layer noticed first.
    """


@dataclass(frozen=True)
class ModelInput:
    """Exactly `build_model`'s arguments plus the wall budget, in domain types.

    Field order and shapes mirror `placement.model.build_model`'s signature so the
    call site in `main.py` is a straight unpack with nothing to get wrong.
    `main.py` is out of scope for this round and reads these fields by NAME
    (`parsed.courts`, `parsed.step_minutes`, ...) — every name below is
    unchanged from the string contract even though several TYPES are not.

    The six fields after `wall_seconds` were, when this was written, an
    exception to "exactly `build_model`'s arguments": the #21 contract
    programme's own C1/C4/C6 tasks (a different "C1" from the one below —
    the #21 programme numbered rule_groups C1; the round-order design reused
    the label independently, three weeks later), validated here like
    everything else but not among the seven `main.py` originally passed to
    `build_model`. All six now flow: #21's C4 added `rule_groups` and
    `pinned_rule_group_indices`, #21's C6 added `pinned_entrant_indices`, the
    round-order design's C1 (2026-08-12) added `fixture_rounds` and
    `pinned_round`, and task C10 (2026-08-16) added `person_indices` —
    `main.py` now passes THIRTEEN arguments.
    """

    courts: int  # the COUNT of courts (len(court_names)); names never reach the domain
    fixtures: list[tuple[list[int], int]]  # (entrant_indices, division_index)
    grid_slots: list[tuple[int, int, int]]  # (court_index, start_at_ms, day_index)
    step_minutes: int
    constraints: dict
    existing: list[tuple[int, int]]  # (court_index, start_at_ms) -- no fixture identity
    dependencies: list[tuple[int, int]]  # (before_index, after_index) -- fixture positions
    wall_seconds: float
    # C1 -- one rule and the MOVABLE fixture indices it binds, plus its two
    # value fields (each `None` when unset -- presence-tracked the same way
    # the retired `DivisionRule`'s were). Validated, unread by `build_model`
    # this round; see the module docstring, "#21".
    rule_groups: list[tuple[list[int], int | None, int | None]]
    # (fixture_indices, min_rest_minutes, max_fixtures_per_day)
    # C4 -- parallel to `existing` (same length, same order; `existing[k]`'s
    # entry is `pinned_rule_group_indices[k]`). Kept OFF `existing`'s own tuple
    # shape rather than widening it: `existing` is passed to `build_model`
    # UNCHANGED, and `model.py`'s `for k, (existing_court, existing_start) in
    # enumerate(existing)` unpacks it as a bare 2-tuple -- a 4-tuple there is a
    # crash, not a behaviour change.
    pinned_rule_group_indices: list[list[int]]
    # C6 -- parallel to `existing` the same way.
    pinned_entrant_indices: list[list[int]]
    # C1 (2026-08-12 round-ordering design) -- parallel to `fixtures` (same
    # length, same order): `fixture_rounds[i]` is `fixtures[i]`'s round, or
    # `None` when the wire left it unset. Kept off `fixtures`'s own tuple
    # shape for the same reason `pinned_rule_group_indices` is kept off
    # `existing`'s: `model.py` unpacks `fixtures` as a bare 2-tuple
    # (`for i, (entrant_indices, _division) in enumerate(fixtures)`), so a
    # 3-tuple there is a crash, not a behaviour change.
    fixture_rounds: list[int | None]
    # C1 -- parallel to `existing` the same way: `pinned_round[k]` is
    # `existing[k]`'s round, or `None` when unset (the common case -- most
    # pins carry no round at all). `None` is also what a caller sends for a
    # pin that is genuinely round-less (a bracket/stepladder pin), so this
    # module does not and cannot distinguish "no round" from "the caller
    # chose not to attribute this pin to the movable round-robin sequence" --
    # both read identically as "this pin is unconstrained by round order",
    # which is the only interpretation `model.py`'s pair scan needs.
    pinned_round: list[int | None]
    # C10 (2026-08-16, wire person indices design) -- parallel to `fixtures`
    # the same way `fixture_rounds` is: `person_indices[i]` is `fixtures[i]`'s
    # person indices. Kept off `fixtures`'s own tuple shape for the identical
    # reason `fixture_rounds` is -- `model.py` unpacks `fixtures` as a bare
    # 2-tuple, so widening it there is a crash, not a behaviour change.
    #
    # Only `fixtures` carries one: `existing` (a pin) has no `person_indices`
    # field on the wire in this round -- `PinnedRow` was not widened, only
    # `Fixture` was (see this module's own module docstring and the PR body
    # for why that is a deliberate scope line, not an oversight).
    person_indices: list[list[int]]
    # #622 (round-scoped required court tags) -- parallel to `fixtures` the
    # same way `fixture_rounds` and `person_indices` are, and kept off
    # `fixtures`'s own tuple shape for the identical reason: `model.py`
    # unpacks it as a bare 2-tuple.
    #
    # `allowed_courts[i]` is the set of court indices `fixtures[i]` may be
    # placed on, or `None` when the wire sent none. `None`, not an empty set:
    # the proto reads an empty `allowed_court_indices` as UNCONSTRAINED, and
    # an empty `set()` here would read as "no court at all" and make the
    # fixture silently unplaceable -- the exact "unset vs. legitimately
    # empty" trap this module exists to close, on a repeated field instead of
    # a singular one. Normalised to `None` at parse time so `build_model` can
    # test one thing (`is None`) and never has to know the wire convention.
    allowed_courts: list[set[int] | None]


def _require_index_present(has_field: bool, where: str) -> None:
    """Half of the chokepoint every SINGULAR index field passes through.

    0 is always a legitimate index, so an unset singular `uint32` field is
    indistinguishable from a deliberate 0 by value alone — the caller who
    forgot to set `where` gets silently routed to index 0 instead of being
    refused. Same defence proto3 `optional` already gives `gap_minutes` and
    `Slot.day_index`, applied here to every field whose whole job is naming
    one entity. Repeated fields (`entrant_indices`) do not call this — an
    element that exists in a repeated field was explicitly appended, so
    there is no presence ambiguity to close.
    """
    if not has_field:
        raise InvalidRequestError(
            f"{where} must be set. 0 is a legitimate index, so an unset field cannot be told from "
            "a deliberately-chosen 0 by its value alone — and would otherwise silently resolve to "
            "index 0 rather than being refused, joining the wrong group instead of erroring."
        )


def _require_index_range(value: int, bound: int, where: str) -> int:
    """The other half: every index must be IN RANGE for the list or count it
    points into. Out-of-range is a hard reject naming the field, the value,
    and the bound — the complete replacement for `_require_id`'s canonicality
    checks, because there is no id left to canonicalise."""
    if not (0 <= value < bound):
        raise InvalidRequestError(
            f"{where} = {value} is out of range: every index here must satisfy 0 <= {where} < "
            f"{bound}. An inferred bound (the largest index actually used) cannot distinguish a "
            f"real high index from a typo; {bound} is the request's OWN declared bound."
        )
    return value


def _validated_fixtures(
    proto_fixtures, entrant_count: int, division_count: int, person_count: int, num_courts: int
) -> tuple[
    list[tuple[list[int], int]], list[int | None], list[list[int]], list[set[int] | None]
]:
    """The movable fixtures, with every index they carry checked for range,
    plus their C1 round numbers and C10 person indices as SEPARATE parallel
    lists (see `ModelInput.fixture_rounds`/`ModelInput.person_indices`'s own
    comments for why neither is folded onto the tuple).

    Runs first because everything after it resolves against what it returns:
    the dependency endpoints resolve against `len(fixtures)`.

    `round` needs no range check unlike every other index field here: it is
    not a position into any list this request declares (there is no
    `round_count`), just an opaque ordering key the caller assigns within
    its own round-robin sequence -- any `uint32` value is a legitimate round.
    """
    fixtures: list[tuple[list[int], int]] = []
    fixture_rounds: list[int | None] = []
    fixture_people: list[list[int]] = []
    fixture_allowed_courts: list[set[int] | None] = []
    for i, f in enumerate(proto_fixtures):
        # C10 (2026-08-16, wire person indices design) -- narrowed from "must
        # not be empty" to "neither entrants nor people": a genuinely
        # undecided knockout slot (a final, a third-place playoff, before its
        # feeders have resolved) has no entrants yet but has PEOPLE -- the
        # players who can still reach it -- and section 6's participant-rest
        # NoOverlap and the T2 idle-gap term now join a fixture through
        # EITHER family (`placement.model`'s `by_entrant`/`by_person`). The
        # refusal survives for the case neither family can rescue: a fixture
        # that names no participant AT ALL joins no group either way, so the
        # measured hazard below still applies to it unchanged.
        if len(f.entrant_indices) == 0 and len(f.person_indices) == 0:
            raise InvalidRequestError(
                f"fixtures[{i}] has neither entrant_indices nor person_indices set. A fixture "
                "joining no participant group at all -- by entrant OR by person -- skips the "
                "participant-rest NoOverlap and every T2 idle-gap term. Measured (string "
                "contract, same mechanism): two fixtures sharing one player placed CONCURRENTLY, "
                "reported OPTIMAL."
            )
        entrant_indices = [
            _require_index_range(e, entrant_count, f"fixtures[{i}].entrant_indices[{j}]")
            for j, e in enumerate(f.entrant_indices)
        ]
        if len(set(entrant_indices)) != len(entrant_indices):
            # Not cosmetic: a repeated entrant puts this fixture's index in its
            # own `by_entrant` group TWICE, and the per-entrant AddNoOverlap
            # then requires its rest interval not to overlap ITSELF — which is
            # unsatisfiable, so `placed[i]` is forced to 0. Same mechanism the
            # string contract measured 6/6: entrant_ids=["e1","e1"] gave
            # OPTIMAL/placed=0/error unset where ["e1","e2"] placed it.
            raise InvalidRequestError(
                f"fixtures[{i}].entrant_indices repeats an entrant, got {list(entrant_indices)!r}. "
                "The duplicate makes the fixture overlap itself in its own participant-rest group, "
                "so it becomes silently UNPLACEABLE and the board comes back OPTIMAL without it."
            )

        # C10 -- the person-namespace mirror of the two checks just above,
        # range then duplicate. A SEPARATE table from `entrant_indices`
        # throughout (`person_count`, never `entrant_count`): an entrant id
        # and a person id are different namespaces that happen to both be
        # small integers, and `placement.model`'s `by_entrant`/`by_person`
        # must never be built from one shared map -- see this module's own
        # module docstring and `Fixture.person_indices`'s proto comment.
        person_indices = [
            _require_index_range(p, person_count, f"fixtures[{i}].person_indices[{j}]")
            for j, p in enumerate(f.person_indices)
        ]
        if len(set(person_indices)) != len(person_indices):
            # The identical hazard the entrant duplicate check above closes,
            # one namespace over: a repeated person would put this fixture's
            # index in its own `by_person` group twice, forcing its rest
            # interval to not overlap itself -- unsatisfiable.
            raise InvalidRequestError(
                f"fixtures[{i}].person_indices repeats a person, got {list(person_indices)!r}. "
                "The duplicate makes the fixture overlap itself in its own participant-rest group, "
                "so it becomes silently UNPLACEABLE and the board comes back OPTIMAL without it."
            )

        _require_index_present(f.HasField("division_index"), f"fixtures[{i}].division_index")
        division_index = _require_index_range(
            f.division_index, division_count, f"fixtures[{i}].division_index"
        )
        # #622 -- the courts this fixture may use. Range-checked against
        # `court_names` like every other index, and de-duplicated into a set
        # rather than refused on a repeat: unlike `entrant_indices`/
        # `person_indices` above, where a duplicate silently makes the fixture
        # overlap ITSELF in a NoOverlap group and become unplaceable, a
        # repeated court index states the same permission twice and is
        # harmless. A refusal there would be a behaviour difference with no
        # hazard behind it.
        allowed_courts = {
            _require_index_range(c, num_courts, f"fixtures[{i}].allowed_court_indices[{j}]")
            for j, c in enumerate(f.allowed_court_indices)
        }
        fixtures.append((entrant_indices, division_index))
        fixture_rounds.append(f.round if f.HasField("round") else None)
        fixture_people.append(person_indices)
        # Empty on the wire == unconstrained, normalised to `None` here. See
        # `ModelInput.allowed_courts`.
        fixture_allowed_courts.append(allowed_courts if allowed_courts else None)
    return fixtures, fixture_rounds, fixture_people, fixture_allowed_courts


def _validated_rule_groups(
    groups, num_fixtures: int
) -> list[tuple[list[int], int | None, int | None]]:
    """C1 -- one rule and the exact MOVABLE fixture set it binds, resolved by
    the CALLER (`build.ts`'s `scopeCoversFixture`) and sent as the result
    rather than as a scope this service would have to understand itself — see
    `RuleGroup`'s own comment in the proto. Validated here exactly like every
    other field; `build_model` reads the `max_fixtures_per_day` half as of
    task C4 (module docstring, "#21") — the `min_rest_minutes` half is still
    unread, a separate task's job.

    Runs right after `_validated_fixtures`, before anything else, because
    `existing[].rule_group_indices` (validated in `_validated_existing`)
    resolves against `len(rule_groups)` -- the same "bounds before the things
    that reference them" ordering `request_to_model_input`'s own docstring
    already follows for `fixtures`/`dependencies`.
    """
    out: list[tuple[list[int], int | None, int | None]] = []
    for i, g in enumerate(groups):
        # An EMPTY `fixture_indices` is legal, not a caller error -- a
        # competition-scoped rule on an all-pinned board resolves to exactly
        # this, and the rule still has to reach the service because a pinned
        # row's `rule_group_indices` references it by position in THIS list.
        fixture_indices = [
            _require_index_range(idx, num_fixtures, f"rule_groups[{i}].fixture_indices[{j}]")
            for j, idx in enumerate(g.fixture_indices)
        ]

        min_rest_minutes: int | None = None
        if g.HasField("min_rest_minutes"):
            min_rest_minutes = g.min_rest_minutes
            if min_rest_minutes < 0:
                raise InvalidRequestError(
                    f"rule_groups[{i}].min_rest_minutes must be >= 0, got {min_rest_minutes!r}. "
                    "0 is a legitimate 'no minimum rest' rule, distinct from this group carrying "
                    "no rest rule at all -- a negative value has no meaning either way."
                )

        max_fixtures_per_day: int | None = None
        if g.HasField("max_fixtures_per_day"):
            max_fixtures_per_day = g.max_fixtures_per_day
            if max_fixtures_per_day <= 0:
                raise InvalidRequestError(
                    f"rule_groups[{i}].max_fixtures_per_day must be > 0, got "
                    f"{max_fixtures_per_day!r}. A cap of 0 forbids the whole group from being "
                    "placed at all, which is never what 'no cap' means -- omit the field instead "
                    "of sending 0."
                )

        out.append((fixture_indices, min_rest_minutes, max_fixtures_per_day))
    return out


def _validated_slots(proto_slots, num_courts: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i, s in enumerate(proto_slots):
        _require_index_present(s.HasField("court_index"), f"slots[{i}].court_index")
        court_index = _require_index_range(s.court_index, num_courts, f"slots[{i}].court_index")
        if s.start_at_ms <= 0:
            raise InvalidRequestError(
                f"slots[{i}].start_at_ms must be > 0, got {s.start_at_ms!r}. Epoch 0 is 1970 and "
                "is never a legitimate court time; an unset start is a legal tick that fixtures are "
                "then placed on and proved OPTIMAL."
            )
        if not s.HasField("day_index"):
            raise InvalidRequestError(
                f"slots[{i}].day_index must be set. Day 0 is a legitimate value, so an unset "
                "field cannot be told from a real one by its value — and unset puts EVERY slot on "
                "day 0, collapsing the whole lattice into one day-cap bucket. Resolve each slot to "
                "the org's local calendar day; the solver never reasons about time zones."
            )
        if s.day_index < 0:
            raise InvalidRequestError(f"slots[{i}].day_index must be >= 0, got {s.day_index!r}.")
        out.append((court_index, s.start_at_ms, s.day_index))
    return out


def _validated_existing(
    rows, num_courts: int, num_rule_groups: int, entrant_count: int
) -> tuple[list[tuple[int, int]], list[list[int]], list[list[int]], list[int | None]]:
    """`existing`, plus its three C4/C6/C1 companions -- kept as SEPARATE
    parallel lists (same length and order as the returned `existing`) rather
    than widened onto its `(court_index, start_at_ms)` tuple. `existing` is
    `build_model`'s argument, UNCHANGED, and `model.py` unpacks it as a bare
    2-tuple (`for k, (existing_court, existing_start) in enumerate(existing)`)
    -- a 4-tuple there is a crash, not a behaviour change.
    """
    out: list[tuple[int, int]] = []
    rule_group_indices_by_row: list[list[int]] = []
    entrant_indices_by_row: list[list[int]] = []
    round_by_row: list[int | None] = []
    for i, a in enumerate(rows):
        _require_index_present(a.HasField("court_index"), f"existing[{i}].court_index")
        court_index = _require_index_range(a.court_index, num_courts, f"existing[{i}].court_index")
        if a.start_at_ms <= 0:
            raise InvalidRequestError(
                f"existing[{i}].start_at_ms must be > 0, got {a.start_at_ms!r}. An unset start "
                "builds the blocking interval at epoch 0, which overlaps nothing real, so the pin "
                "is silently ignored and a movable fixture takes the pinned slot."
            )
        out.append((court_index, a.start_at_ms))

        # C4 -- which rule groups this pin counts against. EMPTY IS A REAL
        # ANSWER ("counts against nothing"), not a missing one -- see
        # `RuleGroup.rule_group_indices`'s own field comment in the proto --
        # so there is no not-empty guard here, only range.
        rule_group_indices_by_row.append(
            [
                _require_index_range(
                    idx, num_rule_groups, f"existing[{i}].rule_group_indices[{j}]"
                )
                for j, idx in enumerate(a.rule_group_indices)
            ]
        )
        # C6 -- who is playing in this pinned match.
        entrant_indices_by_row.append(
            [
                _require_index_range(idx, entrant_count, f"existing[{i}].entrant_indices[{j}]")
                for j, idx in enumerate(a.entrant_indices)
            ]
        )
        # C1 -- this pin's round, or `None` when the wire left it unset (the
        # common case). No range check: same reasoning as `Fixture.round` in
        # `_validated_fixtures` -- not a position into anything declared.
        round_by_row.append(a.round if a.HasField("round") else None)
    return out, rule_group_indices_by_row, entrant_indices_by_row, round_by_row


def _validated_dependencies(pairs, num_fixtures: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for i, d in enumerate(pairs):
        _require_index_present(d.HasField("before_index"), f"dependencies[{i}].before_index")
        before = _require_index_range(d.before_index, num_fixtures, f"dependencies[{i}].before_index")
        _require_index_present(d.HasField("after_index"), f"dependencies[{i}].after_index")
        after = _require_index_range(d.after_index, num_fixtures, f"dependencies[{i}].after_index")
        out.append((before, after))
    return out


def _validated_constraints(proto_constraints) -> dict:
    """The `build_model` constraints dict, validated.

    `match_minutes`/`gap_minutes` guards are unchanged from the string
    contract — this round is about identity, and neither field is id-shaped.

    Used to also validate and fold in `rest_by_division`/`day_cap_by_division`
    (derived from `division_rules`, proto field 10, via the now-deleted
    `_validated_division_rules`). That field was retired 2026-08-12: the
    `rule_groups` path (see `request_to_model_input`'s own docstring) was
    already authoritative in production, so this dict carries only
    `match_minutes`/`gap_minutes` now, and `placement.model.build_model` no
    longer reads a `rest_by_division`/`day_cap_by_division` key at all.
    """
    if not proto_constraints.HasField("gap_minutes"):
        raise InvalidRequestError(
            "constraints.gap_minutes must be set. 0 is a legitimate value ('no court turnaround'), "
            "so an unset field cannot be told from a deliberate one by its value — which is why the "
            "field carries proto3 `optional` and is checked for presence rather than for range. "
            "Unset silently books matches back-to-back and reports OPTIMAL."
        )

    constraints = {
        "match_minutes": proto_constraints.match_minutes,
        "gap_minutes": proto_constraints.gap_minutes,
    }

    if constraints["match_minutes"] <= 0:
        raise InvalidRequestError(
            f"constraints.match_minutes must be > 0, got {constraints['match_minutes']!r}. "
            "A zero-length match makes every court and rest interval zero-width, so the board "
            "comes back OPTIMAL with every fixture stacked on one tick."
        )
    if constraints["gap_minutes"] < 0:
        raise InvalidRequestError(
            f"constraints.gap_minutes must be >= 0, got {constraints['gap_minutes']!r}. The court "
            "interval is match_minutes + gap_minutes wide, so at gap == -match_minutes it is "
            "zero-width and two matches overlap on one court."
        )

    return constraints


#: Observability for the `division_rules` (proto field 10) deploy window: a
#: not-yet-upgraded `build.ts` deployed alongside a Placement version that has
#: already reserved the field sends it anyway, and proto3 preserves it as a
#: silent, uninterpreted unknown field rather than raising -- which is exactly
#: what the wire-compat test protects, and exactly why nothing would otherwise
#: tell an operator it is happening. Kept as a set literal rather than
#: `{FIELD_NUMBER_DIVISION_RULES}` alone: any legacy field belongs here, not
#: only the one retired most recently.
_LEGACY_WIRE_FIELD_NUMBERS = frozenset({10})


def _log_legacy_wire_fields(req) -> None:
    """One structured log line per request that still carries a legacy field.

    Lives here, not in `placement.main`: this module is documented as "the
    ONLY place a proto message is read" (module docstring above), and reading
    `UnknownFields()` is reading the proto message -- `main.py` deliberately
    never imports `scheduler_pb2` at all (its own docstring: "constructs no
    proto message of its own"). `google.protobuf.unknown_fields.
    UnknownFieldSet(req)` is used rather than `req.UnknownFields()` because
    the latter raises `NotImplementedError` under this repo's protobuf
    build (the `upb` implementation) -- confirmed by hand, not assumed.

    O(1)-ish per request (one pass over however many unknown fields exist,
    normally zero) and logs field NUMBERS only, never field CONTENTS: a
    reserved field's bytes are not this service's to read or repeat into a
    log, only its presence is.
    """
    numbers = sorted({f.field_number for f in unknown_fields.UnknownFieldSet(req)})
    legacy = [n for n in numbers if n in _LEGACY_WIRE_FIELD_NUMBERS]
    if legacy:
        log.info("legacy_wire_field_ignored", request_id=req.request_id, field_numbers=legacy)


def _log_person_only_fixtures(
    request_id: str, fixtures: list[tuple[list[int], int]], fixture_people: list[list[int]]
) -> None:
    """Observability for the C10 (2026-08-16) deploy window -- the mirror
    image of `_log_legacy_wire_fields` above, for an ADDITIVE field rather
    than a retired one.

    This field number (`Fixture.person_indices` = 4) cannot be detected via
    `UnknownFields()` the way a stale caller's field 10 can: an old, not-yet-
    upgraded `build.ts`/`packages/engine` simply never emits it, so there is
    no unknown-field byte to see arrive -- unlike C0's case, there is nothing
    to probe FOR on this side. What IS observable, and what an operator
    actually needs during this rollout, is the other direction: whether the
    NEW acceptance path this task adds is being exercised at all -- a
    fixture that reaches `build_model` with entrants empty and only people
    carrying it, the shape the pre-C10 refusal always rejected outright.

    Deploy-order note (see the PR body for the full analysis): an OLDER
    service -- this module's code before this change -- rejects such a
    fixture unconditionally regardless of what `person_indices` carries, so
    a caller upgraded ahead of this service sees EXACTLY today's behaviour,
    not a new failure mode; conversely a caller not yet upgraded sends an
    empty `person_indices`/`person_count`, so a NEW service's narrowed
    refusal reduces to the OLD unconditional one. Recommended order is still
    service-first, mirroring `_log_legacy_wire_fields`'s own "reader
    upgrades before writer" discipline -- but nothing here is a hard gate,
    because neither ordering can silently ship a WORSE board than the one
    that ships today; see `build.ts`'s `rejectedBlockingConflicts`
    (`person_overlap` is a blocking reason there), which already catches a
    person-blind solver's mistake and falls back to greedy rather than
    trust it. This log exists so a rollout can be CONFIRMED rather than
    assumed, not because the transition is unsafe without it.

    O(1)-ish per request, logs INDICES only (never any id -- this module
    never had ids to begin with, round 6) -- one line per request, not per
    fixture, so a board with many undecided bracket slots costs one log
    line, not one per slot.
    """
    indices = [
        i
        for i, ((entrant_indices, _division), people) in enumerate(zip(fixtures, fixture_people))
        if len(entrant_indices) == 0 and len(people) > 0
    ]
    if indices:
        log.info("person_only_fixture_accepted", request_id=request_id, fixture_indices=indices)


def request_to_model_input(req) -> ModelInput:
    """Translate a `SolveBuildRequest` into domain types, or reject it.

    ONE referential-integrity pass, before any domain object exists: every
    index the request carries must be IN RANGE for the list or count it
    points into. Order matters — `court_names`/`entrant_count`/`division_count`
    are read first because they are the BOUNDS everything else resolves
    against; `fixtures` next because `len(fixtures)` is the bound
    `dependencies` (and `rule_groups[].fixture_indices`) resolve against; then
    `rule_groups` itself, because `existing[].rule_group_indices` (C4) resolves
    against `len(rule_groups)` in turn.

    Unconditionally logs (`_log_legacy_wire_fields`) before any of that, win
    or lose: a request that also gets rejected below for an unrelated reason
    should still surface that it carried a legacy field, and observability
    must not depend on the rest of validation succeeding.

    Args:
        req: a `scheduler_pb2.SolveBuildRequest`. Untyped in the signature so
            this module's *callers* need the proto import and its *consumers*
            do not — the boundary runs through here in one direction only.

    Raises:
        InvalidRequestError: on an empty board, on any degenerate scalar
            enumerated in the module docstring, or on any index that is
            unset or out of range.
    """
    _log_legacy_wire_fields(req)

    if len(req.fixtures) == 0:
        raise InvalidRequestError("fixtures must not be empty")
    if len(req.court_names) == 0:
        raise InvalidRequestError("court_names must not be empty")
    if len(req.slots) == 0:
        raise InvalidRequestError(
            "slots must not be empty. With no admissible ticks the model falls back to a single "
            "start of 0, forces every fixture onto it, and proves ONE fixture per court OPTIMAL "
            "while silently dropping the rest."
        )
    if not req.HasField("constraints"):
        raise InvalidRequestError(
            "the BuildConstraints message must be set. It is a proto3 message field, so an omitted "
            "one arrives fully default-valued and every constraint family it carries disappears at "
            "once."
        )

    num_courts = len(req.court_names)
    entrant_count = req.entrant_count
    division_count = req.division_count
    person_count = req.person_count

    fixtures, fixture_rounds, fixture_people, fixture_allowed_courts = _validated_fixtures(
        req.fixtures, entrant_count, division_count, person_count, num_courts
    )
    _log_person_only_fixtures(req.request_id, fixtures, fixture_people)
    # C1. Must run before `existing` below: `existing[].rule_group_indices`
    # resolves against `len(rule_groups)`.
    rule_groups = _validated_rule_groups(req.rule_groups, len(fixtures))

    grid_slots = _validated_slots(req.slots, num_courts)
    # No per-court coverage refusal here any more (task C2). This used to be
    # `_validate_court_slot_coverage(num_courts, grid_slots)`, rejecting both
    # a declared-but-slotless court and courts whose tick sets merely
    # differed — `placement.model.build_model` now enforces each court's OWN
    # tick set directly (a per-fixture domain restriction, and a forced
    # `presence_court[i][c] == 0` on a court no slot mentions), so a
    # per-court blackout is placed correctly instead of the whole request
    # bouncing the caller back to its own greedy fallback. See that module's
    # docstring, "per-court grids" / "task C2".
    existing, pinned_rule_group_indices, pinned_entrant_indices, pinned_round = _validated_existing(
        req.existing, num_courts, len(rule_groups), entrant_count
    )
    dependencies = _validated_dependencies(req.dependencies, len(fixtures))
    constraints = _validated_constraints(req.constraints)

    if req.wall_seconds <= 0:
        raise InvalidRequestError(
            f"wall_seconds must be > 0, got {req.wall_seconds!r}. A 0 budget stops the tier chain "
            "before T0 runs and returns UNKNOWN with no assignments — the same answer an impossible "
            "board gives, so an unset field would read as a solver verdict about the request."
        )

    return ModelInput(
        courts=num_courts,
        fixtures=fixtures,
        grid_slots=grid_slots,
        step_minutes=req.step_minutes,
        constraints=constraints,
        existing=existing,
        dependencies=dependencies,
        wall_seconds=req.wall_seconds,
        rule_groups=rule_groups,
        pinned_rule_group_indices=pinned_rule_group_indices,
        pinned_entrant_indices=pinned_entrant_indices,
        fixture_rounds=fixture_rounds,
        pinned_round=pinned_round,
        person_indices=fixture_people,
        allowed_courts=fixture_allowed_courts,
    )


def error_response(code: str, message: str) -> scheduler_pb2.SolveBuildResponse:
    """A rejected request is a SUCCESSFUL RPC carrying a reason, not a gRPC
    error. The caller (`build.ts`) can fall back on its own heuristic placer
    given a reason; it can do nothing useful with a transport-level failure.

    One constructor for every rejection path — the wire-boundary one and the
    domain's own `ValueError` backstop — so whichever layer noticed first, the
    caller sees exactly one shape.
    """
    return scheduler_pb2.SolveBuildResponse(
        status=scheduler_pb2.SOLVE_STATUS_ERROR,
        error=scheduler_pb2.SolveError(code=code, message=message),
    )


def outcome_to_response(
    outcome: SolveOutcome, wall_seconds: float
) -> scheduler_pb2.SolveBuildResponse:
    """Translate a solve result back onto the wire.

    Args:
        outcome: the domain's `SolveOutcome`, in plain Python. `outcome.
            assignments` is now `(fixture_index, court_index, start_at_ms)`
            triples of ints, matching `Assignment`'s new shape directly.
        wall_seconds: the budget the solve was actually GIVEN — the clamped
            value, not whatever the request asked for. `wall_exhausted` is
            meaningless against a budget that was never applied.
    """
    # An unmapped status is the one SOLVE_STATUS_ERROR path reached WITHOUT an
    # exception having been raised — the domain returned a status vocabulary
    # this layer has no entry for (`MODEL_INVALID`, or a future OR-Tools
    # status), so there is no `str(exc)` to hand on. It has to build its own
    # `SolveError`, or it becomes the only error response in the service that
    # tells the caller nothing about why. `error=None` leaves the field unset,
    # which is what every mapped status wants.
    #
    # This branch still maps `assignments` through, which is safe only because
    # of a guarantee that lives in another module: `objective._chain_status`
    # returns a raw solver status name (the only way to get a name that is not
    # in STATUS_MAP) exclusively on its `if not assignments` path — a mapped
    # status is returned whenever there IS a board. So an unmapped status
    # cannot arrive carrying assignments, and this response cannot ship a board
    # alongside an ERROR. Stated here because it is not visible from this file,
    # and a future change to `_chain_status` would break it silently.
    status = STATUS_MAP.get(outcome.status)
    error = None
    if status is None:
        status = scheduler_pb2.SOLVE_STATUS_ERROR
        error = scheduler_pb2.SolveError(
            code="INTERNAL_ERROR",
            message=(
                f"solver returned unmapped status {outcome.status!r}; expected one of "
                f"{sorted(STATUS_MAP)}. Treat this response as an error, not as a board."
            ),
        )

    return scheduler_pb2.SolveBuildResponse(
        assignments=[
            scheduler_pb2.Assignment(fixture_index=fi, court_index=ci, start_at_ms=start)
            for fi, ci, start in outcome.assignments
        ],
        status=status,
        error=error,
        tiers_completed=outcome.tiers_completed,
        # Sliced to the PROVED tiers. `objective_values` can carry one more
        # entry than `tiers_completed`: when the clock cuts a tier short
        # mid-solve the outcome still records that tier's last-known value,
        # deliberately, without counting it as completed. Publishing that entry
        # would tell the caller a value had been proved optimal when it is only
        # the best thing seen before the budget ran out.
        objective_values=[
            scheduler_pb2.Tier(name=name, value=value)
            for name, value in outcome.objective_values[: outcome.tiers_completed]
        ],
        elapsed_ms=outcome.elapsed_ms,
        # Against the wall MINUS the tier loop's own proactive-break margin,
        # not against the raw wall. `run_tier_chain` stops when
        # `deadline - now <= MIN_TIER_SECONDS` rather than starting a tier it
        # cannot finish, so a chain genuinely cut short by the budget reports
        # an `elapsed_ms` up to 50ms UNDER the wall. Comparing against the raw
        # wall calls that `False` and tells the caller the budget was not the
        # limiting factor — the one thing this flag exists to say. Derived from
        # `MIN_TIER_SECONDS` rather than a literal so the two move together.
        wall_exhausted=outcome.elapsed_ms >= int((wall_seconds - MIN_TIER_SECONDS) * 1000),
    )
