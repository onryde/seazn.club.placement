"""The servicer, driven in-process — no socket, no port, no real channel.

`grpc_testing.server_from_dictionary` runs the real `SchedulerServicer` behind
the real service descriptor, so metadata, status codes and response
serialisation are exercised exactly as a live server would exercise them,
while the test stays deterministic and instant.

Three things here are guarding known ways this handler can go wrong rather
than just describing what it does:

  * auth is checked BEFORE any work happens, so an unauthenticated caller can
    never make the box solve anything (`test_rejects_*` + the spy);
  * a `ValueError` raised inside the domain becomes a SOLVE_STATUS_ERROR
    response, never an unhandled exception crossing the RPC boundary;
  * only PROVED tiers are published in `objective_values` — the outcome can
    carry one more entry than `tiers_completed` when the clock cuts a tier
    short, and that unproven value must not go out on the wire.

Round 6: identity on the wire is positional (`Assignment.fixture_index` /
`.court_index`, not `.fixture_id` / `.court`). `_solvable_request` below
builds a one-fixture, one-court request the same way it always did; only the
field names and types changed.
"""

import contextlib
import dataclasses
import threading

import grpc
import grpc_testing
import pytest
import structlog.testing
from grpc_health.v1 import health_pb2, health_pb2_grpc

from _board_positional import to_proto_request
from placement.config import Settings
from placement.generated import scheduler_pb2, scheduler_pb2_grpc
from placement.main import SchedulerServicer, build_health_servicer, build_server
from placement.model import SolveOutcome
from placement.objective import TIER_ORDER

SERVICE = scheduler_pb2.DESCRIPTOR.services_by_name["SchedulerService"]
SOLVE_BUILD = SERVICE.methods_by_name["SolveBuild"]
GOOD_AUTH = (("x-internal-secret", "test-secret"),)


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", "10")
    return Settings.from_env()


@pytest.fixture
def test_server(settings):
    return grpc_testing.server_from_dictionary(
        {SERVICE: SchedulerServicer(settings)},
        grpc_testing.strict_real_time(),
    )


#: A real epoch-ms timestamp (2026-01-01T08:00:00Z), never 0 — the boundary
#: rejects a slot at epoch 0 because 1970 is not a court time, and because a
#: zero-based corpus is what made "unset" and "a test value" the same number
#: for four review rounds. Same constant and same reasoning as
#: `test_schema.py`'s `SLOT_MS`.
SLOT_MS = 1_767_225_600_000 + 8 * 3_600_000


def _solvable_request(**overrides) -> scheduler_pb2.SolveBuildRequest:
    """One fixture, one court, one slot — small enough to prove OPTIMAL well
    inside the budget, so nothing here is timing-sensitive."""
    kwargs = dict(
        request_id="r1",
        court_names=["Court 1"],
        entrant_count=2,
        division_count=1,
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)],
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=0)],
        step_minutes=10,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=10),
        wall_seconds=2.0,
    )
    kwargs.update(overrides)
    return scheduler_pb2.SolveBuildRequest(**kwargs)


def _invoke(server, request, metadata=GOOD_AUTH):
    return server.invoke_unary_unary(SOLVE_BUILD, metadata, request, None).termination()


# --- auth ------------------------------------------------------------------


def test_rejects_missing_auth_metadata(test_server):
    req = scheduler_pb2.SolveBuildRequest(request_id="r1")
    _, _, code, _ = _invoke(test_server, req, metadata=())
    assert code == grpc.StatusCode.UNAUTHENTICATED


def test_rejects_wrong_secret(test_server):
    """The missing-metadata case alone would still pass if the comparison were
    `secret in (None, shared_secret)` or similar; this pins that a present but
    wrong value is rejected too."""
    _, _, code, _ = _invoke(
        test_server, _solvable_request(), metadata=(("x-internal-secret", "wrong-secret"),)
    )
    assert code == grpc.StatusCode.UNAUTHENTICATED


def test_rejects_before_the_solve_ever_runs(test_server, monkeypatch):
    """The acceptance criterion is 'rejected BEFORE the solve ever runs', which
    a status-code assertion cannot see. An unauthenticated caller must not be
    able to spend a single CPU-second of the box's budget."""
    calls: list[str] = []

    def _spy_build_model(*args, **kwargs):
        calls.append("build_model")
        raise AssertionError("build_model must not run for an unauthenticated request")

    monkeypatch.setattr("placement.main.build_model", _spy_build_model)

    _, _, code, _ = _invoke(test_server, _solvable_request(), metadata=())
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert calls == []


def test_auth_metadata_key_is_matched_case_insensitively(test_server):
    """HTTP/2 header names are case-insensitive and a real gRPC transport
    lower-cases them before the servicer sees them; the in-process harness does
    not. Normalising in the handler keeps the two behaving the same."""
    _, _, code, _ = _invoke(
        test_server, _solvable_request(), metadata=(("X-Internal-Secret", "test-secret"),)
    )
    assert code == grpc.StatusCode.OK


# --- happy path ------------------------------------------------------------


def test_accepts_valid_request_with_correct_secret(test_server):
    response, _, code, _ = _invoke(test_server, _solvable_request())
    assert code == grpc.StatusCode.OK
    assert response.status in (scheduler_pb2.SOLVE_STATUS_OPTIMAL, scheduler_pb2.SOLVE_STATUS_FEASIBLE)
    # Status alone is not evidence of a board: the chain reports OPTIMAL for an
    # EMPTY schedule just as readily, so assert the fixture actually landed.
    assert [(a.fixture_index, a.court_index, a.start_at_ms) for a in response.assignments] == [
        (0, 0, SLOT_MS)
    ]
    assert response.tiers_completed > 0
    assert response.elapsed_ms >= 0


# --- C1: round order, driven through the real RPC boundary -----------------
#
# `test_model.py`/`test_schema.py` already prove the constraint and the wire
# translation in isolation; this is the smoke test — proto in, real servicer,
# real solve, proto out — closing the gap neither of those two can: that a
# `round` field set on the wire survives `SolveBuild` end to end and actually
# shapes the returned board, not merely the model `build_model` is handed
# directly in a unit test.


def test_round_order_survives_the_full_rpc_round_trip(test_server):
    """Two round-robin fixtures, disjoint entrants, one court offering two
    ticks on the same day — round 2 listed FIRST (index 0) so a bug that
    silently used array order instead of `Fixture.round` would happen to
    still look right; only reading the round field correctly gets this test
    to pass either way."""
    req = _solvable_request(
        entrant_count=4,
        fixtures=[
            scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0, round=2),
            scheduler_pb2.Fixture(entrant_indices=[2, 3], division_index=0, round=1),
        ],
        slots=[
            scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS, day_index=0),
            scheduler_pb2.Slot(court_index=0, start_at_ms=SLOT_MS + 40 * 60_000, day_index=0),
        ],
        wall_seconds=5.0,
    )
    response, _, code, _ = _invoke(test_server, req)
    assert code == grpc.StatusCode.OK
    assert response.status in (scheduler_pb2.SOLVE_STATUS_OPTIMAL, scheduler_pb2.SOLVE_STATUS_FEASIBLE)
    assert len(response.assignments) == 2, response.assignments

    start_by_fixture = {a.fixture_index: a.start_at_ms for a in response.assignments}
    assert start_by_fixture[1] <= start_by_fixture[0], (
        f"round 1 (fixture index 1) must not start after round 2 (fixture index 0) on the "
        f"same day: {list(response.assignments)}"
    )


def test_clamps_wall_seconds_to_the_server_ceiling(test_server, monkeypatch):
    """`wall_seconds` is caller-supplied. Without the clamp one request can pin
    a worker thread for as long as it likes."""
    seen: list[float] = []

    def _spy_solve(model, wall_seconds, knobs):
        seen.append(wall_seconds)
        return SolveOutcome(
            assignments=[], status="OPTIMAL", tiers_completed=4, objective_values=[], elapsed_ms=1
        )

    monkeypatch.setattr("placement.main.solve", _spy_solve)

    _, _, code, _ = _invoke(test_server, _solvable_request(wall_seconds=999.0))
    assert code == grpc.StatusCode.OK
    assert seen == [10.0]  # PLACEMENT_WALL_SECONDS_MAX, not the 999 that was asked for


# --- invalid requests ------------------------------------------------------


def test_maps_invalid_request_to_error_response(test_server):
    """Rejected at the wire boundary by `schema.request_to_model_input`. The
    RPC still succeeds — the caller gets a well-formed answer saying why."""
    response, _, code, _ = _invoke(test_server, scheduler_pb2.SolveBuildRequest(request_id="r1"))
    assert code == grpc.StatusCode.OK
    assert response.status == scheduler_pb2.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert "fixtures" in response.error.message


def test_a_wire_refusal_is_logged_not_just_returned(test_server):
    """The refusal the caller sees must also be one an OPERATOR can find.

    `_solve_build` has two INVALID_REQUEST paths. The domain one
    (`build_model`/`solve` raising ValueError) logs `solve_rejected`. The WIRE
    one (`request_to_model_input` raising InvalidRequestError) used to return
    the same response shape and log NOTHING, which made it the only refusal on
    this path that was invisible on both sides of the wire: the TS caller reads
    a RESOLVED response, so `buildSchedule`'s catch never runs either.

    That asymmetry cost a bench wave. Suite 11 reported "the optimized
    scheduling path does not survive a real fixture count" against
    `solver_unavailable` with nothing in any log to contradict it, and a 30 s
    control run later showed the build returning in 849 ms against a 30_000 ms
    wall — never a timeout at all. The message this refusal already carried
    would have named the real fault on the first run.
    """
    with structlog.testing.capture_logs() as captured:
        response, _, code, _ = _invoke(
            test_server, scheduler_pb2.SolveBuildRequest(request_id="r-wire-refusal")
        )

    assert code == grpc.StatusCode.OK
    assert response.error.code == "INVALID_REQUEST"

    refusals = [e for e in captured if e.get("event") == "request_refused"]
    assert refusals, f"the wire refusal was not logged at all: {captured}"
    entry = refusals[0]
    # The request id is what ties this line back to the caller's own log.
    assert entry["request_id"] == "r-wire-refusal"
    # The REASON, not just the fact. A log that said only "refused" would leave
    # an operator exactly where the missing line did.
    assert "fixtures" in entry["error"], entry
    # Same severity as its sibling refusal, so one filter finds both.
    assert entry["log_level"] == "warning", entry


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"constraints": scheduler_pb2.BuildConstraints(match_minutes=0, gap_minutes=10)},
            id="match_minutes",
        ),
        pytest.param(
            # `division_rules` (proto field 10, `DivisionRule`) is retired;
            # `rule_groups` is the only wire path a day cap reaches the
            # domain through now. `fixture_indices=[0]` names
            # `_solvable_request`'s own lone fixture.
            {"rule_groups": [scheduler_pb2.RuleGroup(fixture_indices=[0], max_fixtures_per_day=0)]},
            id="day_cap",
        ),
        pytest.param({"wall_seconds": 0.0}, id="wall_seconds"),
    ],
)
def test_degenerate_scalars_come_back_as_error_not_a_wrong_board(test_server, overrides):
    """Each of these is an UNSET proto3 scalar arriving as 0, and each would
    otherwise produce a confidently wrong board reported as OPTIMAL. The wire
    boundary rejects all three, so none of them reaches the solver."""
    req = _solvable_request(**overrides)

    response, _, code, _ = _invoke(test_server, req)
    assert code == grpc.StatusCode.OK
    assert response.status == scheduler_pb2.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert len(response.assignments) == 0


@pytest.mark.parametrize("target", ["build_model", "solve"])
def test_domain_value_error_becomes_an_error_response(test_server, monkeypatch, target):
    """Defence in depth, and deliberately not dead code. `build_model` and
    `run_tier_chain` keep their own guards; if one of them ever fires for a
    request the wire boundary let through, the handler must still answer rather
    than let a raw exception escape as an UNKNOWN gRPC error."""

    def _raise(*args, **kwargs):
        raise ValueError("boom from the domain")

    monkeypatch.setattr(f"placement.main.{target}", _raise)

    response, _, code, _ = _invoke(test_server, _solvable_request())
    assert code == grpc.StatusCode.OK
    assert response.status == scheduler_pb2.SOLVE_STATUS_ERROR
    assert response.error.code == "INVALID_REQUEST"
    assert "boom from the domain" in response.error.message


# --- objective_values: only what was PROVED --------------------------------


def test_publishes_only_confirmed_objective_values(test_server, monkeypatch):
    """`SolveOutcome.objective_values` can be ONE longer than
    `tiers_completed`: when the wall budget cuts a tier short mid-solve the
    outcome still records that tier's last-known value without counting it as
    completed. Publishing it would tell the caller a value was proved when it
    was not."""

    def _cut_short_solve(model, wall_seconds, knobs):
        return SolveOutcome(
            assignments=[(0, 0, 0)],
            status="FEASIBLE",
            tiers_completed=1,
            # T1 (makespan) was cut short: recorded, but NOT proved.
            objective_values=[(TIER_ORDER[0], 1), (TIER_ORDER[1], 999)],
            elapsed_ms=2000,
        )

    monkeypatch.setattr("placement.main.solve", _cut_short_solve)

    response, _, code, _ = _invoke(test_server, _solvable_request())
    assert code == grpc.StatusCode.OK
    assert response.tiers_completed == 1
    assert [(t.name, t.value) for t in response.objective_values] == [(TIER_ORDER[0], 1)]


def test_publishes_every_objective_value_when_all_tiers_proved(test_server, monkeypatch):
    """The other side of the slice: a completed chain must not lose a tier to
    an off-by-one."""

    def _full_chain_solve(model, wall_seconds, knobs):
        return SolveOutcome(
            assignments=[(0, 0, 0)],
            status="OPTIMAL",
            tiers_completed=len(TIER_ORDER),
            objective_values=[(name, i) for i, name in enumerate(TIER_ORDER)],
            elapsed_ms=1000,
        )

    monkeypatch.setattr("placement.main.solve", _full_chain_solve)

    response, _, _, _ = _invoke(test_server, _solvable_request())
    assert response.tiers_completed == len(TIER_ORDER)
    assert [t.name for t in response.objective_values] == list(TIER_ORDER)


# --- health ----------------------------------------------------------------


def test_health_check_reports_serving():
    """The deploy's readiness probe. Built by the same function `serve()` calls,
    so a wiring change that stops setting SERVING fails here."""
    health_service = health_pb2.DESCRIPTOR.services_by_name["Health"]
    server = grpc_testing.server_from_dictionary(
        {health_service: build_health_servicer()},
        grpc_testing.strict_real_time(),
    )
    response, _, code, _ = server.invoke_unary_unary(
        health_service.methods_by_name["Check"], (), health_pb2.HealthCheckRequest(service=""), None
    ).termination()
    assert code == grpc.StatusCode.OK
    assert response.status == health_pb2.HealthCheckResponse.SERVING


# --- the real registration path --------------------------------------------


def test_build_server_registers_both_services_on_a_real_port(settings):
    """`grpc_testing` never binds a port and never calls
    `add_*Servicer_to_server`, so everything `serve()` actually does to stand
    the service up was untested — all three of its registration lines could be
    deleted with the suite still green.

    This drives `build_server` (the same function `serve()` calls) over a REAL
    channel: health must answer SERVING, the scheduler must be reachable, and
    auth must hold across a real transport — which, unlike the in-process
    harness, lower-cases metadata keys for us.

    Port 0 asks the OS for a free port and `add_insecure_port` returns the one
    it got, so this cannot collide with a squatted fixed port or with a
    concurrent agent's server.
    """
    server, port = build_server(dataclasses.replace(settings, port=0))
    assert port != 0, "add_insecure_port returned 0 — nothing is listening"
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=10)

        health = health_pb2_grpc.HealthStub(channel).Check(
            health_pb2.HealthCheckRequest(service=""), timeout=10
        )
        assert health.status == health_pb2.HealthCheckResponse.SERVING

        stub = scheduler_pb2_grpc.SchedulerServiceStub(channel)

        # Registered AND authenticated: without the servicer registration this
        # would be UNIMPLEMENTED, not UNAUTHENTICATED.
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.SolveBuild(_solvable_request(), timeout=10)
        assert excinfo.value.code() == grpc.StatusCode.UNAUTHENTICATED

        response = stub.SolveBuild(_solvable_request(), metadata=GOOD_AUTH, timeout=10)
        assert response.status == scheduler_pb2.SOLVE_STATUS_OPTIMAL
        assert [(a.fixture_index, a.court_index, a.start_at_ms) for a in response.assignments] == [
            (0, 0, SLOT_MS)
        ]
    finally:
        channel.close()
        server.stop(None)


# --- the brief's acceptance criterion, on the production board --------------


def test_production_board_solves_through_the_server(test_server):
    """The brief's acceptance criterion: "a well-formed, correctly-authenticated
    request against the production board shape returns OPTIMAL or FEASIBLE".

    Every other test here uses a one-fixture toy board, which never exercises
    `existing`, `dependencies` or a division-scoped rest/cap `rule_group`
    (`_board_positional.rule_groups_for`) through the wire mapping at all — a
    dropped field in `schema.py` would relax the board and go unnoticed. This
    drives the bench's own
    `production_board()` (37 fixtures, 5 courts, ~2081 slots) end to end,
    converted to the positional wire shape by `_board_positional.to_proto_request`
    (`bench/` is out of scope for round 6 and still returns string identity).

    Deliberately does NOT assert `tiers_completed == 4`: the T0-T3 chain is
    ~4.9s idle but 9-11s under load, so that assertion is a flake generator
    under parallel agents. What is asserted instead is that the BOARD is real —
    status alone is not evidence, since the chain reports OPTIMAL just as
    readily for an empty schedule.
    """
    from placement_bench_boards import production_board

    board = production_board()
    fixtures, courts, grid_slots, _step_minutes, _constraints, _existing, _dependencies = board

    req = to_proto_request(board, request_id="prod-37x77k", wall_seconds=8.0)

    response, _, code, _ = _invoke(test_server, req)

    assert code == grpc.StatusCode.OK
    assert response.status in (
        scheduler_pb2.SOLVE_STATUS_OPTIMAL,
        scheduler_pb2.SOLVE_STATUS_FEASIBLE,
    )
    assert not response.HasField("error")
    assert response.tiers_completed >= 1

    placed = [(a.fixture_index, a.court_index, a.start_at_ms) for a in response.assignments]
    assert len(placed) > 0

    # A board, not just a status. Each of these is a way the wire mapping could
    # be wrong while the solve still reports success.
    num_fixtures = len(fixtures)
    num_courts = len(courts)
    assert {fi for fi, _c, _s in placed} <= set(range(num_fixtures))
    assert len({fi for fi, _c, _s in placed}) == len(placed), "a fixture was placed twice"
    assert {court for _f, court, _s in placed} <= set(range(num_courts))
    # The lattice actually reached the model: every (court, start) has to be a
    # real grid point. An empty or ignored slot list collapses every start onto
    # tick 0 instead — precisely the empty-slots failure this round also fixed.
    court_index_of = {name: i for i, name in enumerate(courts)}
    legal_points = {(court_index_of[court], start) for court, start, _day in grid_slots}
    assert {(court, start) for _f, court, start in placed} <= legal_points
    assert len({start for _f, _c, start in placed}) > 1, "every fixture landed on one tick"


# --- the health check may not be starved by in-flight solves ----------------


def _saturating_solve(started: threading.Semaphore, release: threading.Event):
    """A stand-in for `placement.main.solve` that occupies its thread on command.

    The real solver is replaced so that solve SPEED is not a variable: this
    test is about the server's thread topology, and a test whose red depends on
    how fast CP-SAT is that afternoon says nothing about topology.
    """

    def _solve(model, wall_seconds, knobs):
        started.release()
        release.wait(timeout=20)
        return SolveOutcome(
            assignments=[], status="OPTIMAL", tiers_completed=4, objective_values=[], elapsed_ms=1
        )

    return _solve


@contextlib.contextmanager
def _server_with_solves_in_flight(settings, monkeypatch, count):
    """Start a real server on a real port and hold `count` solves in its pool.

    A real port, not `grpc_testing`: the in-process harness never builds the
    `ThreadPoolExecutor` at all, so the topology this file's last two tests
    are about is invisible to it.
    """
    started = threading.Semaphore(0)
    release = threading.Event()
    monkeypatch.setattr("placement.main.solve", _saturating_solve(started, release))

    server, port = build_server(settings)
    assert port != 0, "failed to bind a port"
    server.start()

    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = scheduler_pb2_grpc.SchedulerServiceStub(channel)
    callers = [
        stub.SolveBuild.future(_solvable_request(), metadata=GOOD_AUTH, timeout=30)
        for _ in range(count)
    ]
    try:
        for _ in range(count):
            assert started.acquire(timeout=10), "a solve never reached the solver"
        yield port
    finally:
        release.set()
        for call in callers:
            try:
                call.result(timeout=10)
            except Exception:  # noqa: BLE001 - teardown; the assertions are above
                pass
        channel.close()
        server.stop(None)


def test_health_check_answers_while_every_solve_slot_is_busy(settings, monkeypatch):
    """A saturated solver must not make the machine look dead.

    Measured over a real port before the fix, with `solve` replaced by a sleep
    so solver speed was not a variable:

        max_workers   health, idle       health, pool saturated (2s deadline)
        1             SERVING in 0.003s  DEADLINE_EXCEEDED after 2.005s
        4 (default)   SERVING in 0.002s  DEADLINE_EXCEEDED after 2.004s

    One `grpc.server` carried both servicers on one bounded pool, so with
    `max_workers` solves in flight there was no thread left to answer
    `Health/Check`. With the shipped defaults that is a ~10 s liveness blackout
    — long enough for a platform probe to kill the machine MID-SOLVE, taking
    every in-flight solve with it, including the ones about to succeed. Under
    load it is self-reinforcing: the kill drops capacity, which saturates the
    replacement faster.

    `main.py`'s own module docstring used to claim this topology was chosen
    partly because an event loop "would starve the health check". It starved it
    too. A comment asserting a bug is absent is how the bug survives review.
    """
    settings = dataclasses.replace(settings, port=0, max_workers=1)
    with _server_with_solves_in_flight(settings, monkeypatch, settings.max_workers) as port:
        channel = grpc.insecure_channel(f"localhost:{port}")
        try:
            health = health_pb2_grpc.HealthStub(channel)
            # 2 s, not the default: a deadline generous enough to outlast a
            # solve would pass with the bug still present.
            response = health.Check(health_pb2.HealthCheckRequest(), timeout=2)
            assert response.status == health_pb2.HealthCheckResponse.SERVING
        finally:
            channel.close()


def test_a_solve_beyond_the_configured_concurrency_is_refused_not_queued(settings, monkeypatch):
    """Past `max_workers` concurrent solves, the answer is SOLVER_BUSY at once.

    Reserving threads for the health check is only half the fix: without
    admission control an unbounded queue of solves eventually fills the reserve
    too, and every queued caller waits out a deadline it was always going to
    miss. `build.ts` can fall back on its own heuristic placer given a reason
    immediately; it can do nothing with a request that returns at T+30s. The
    design spec's `solver_busy` status is this case.

    A SUCCESSFUL RPC carrying SOLVE_STATUS_ERROR, like every other refusal in
    this service — not a gRPC status code, which would put transport
    vocabulary in front of a caller whose problem is not transport.
    """
    settings = dataclasses.replace(settings, port=0, max_workers=1)
    with _server_with_solves_in_flight(settings, monkeypatch, settings.max_workers) as port:
        channel = grpc.insecure_channel(f"localhost:{port}")
        try:
            stub = scheduler_pb2_grpc.SchedulerServiceStub(channel)
            response = stub.SolveBuild(_solvable_request(), metadata=GOOD_AUTH, timeout=5)
            assert response.status == scheduler_pb2.SOLVE_STATUS_ERROR
            assert response.error.code == "SOLVER_BUSY"
            assert len(response.assignments) == 0
        finally:
            channel.close()
