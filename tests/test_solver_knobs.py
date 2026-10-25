"""The solver knobs are READ FROM THE ENVIRONMENT, and a solve says what it did.

THIS FILE IS THE REGRESSION TEST FOR A REAL PRODUCTION INCIDENT, 2026-08-10.

`NUM_SEARCH_WORKERS` was set in `fly.toml`'s `[env]` and had **no effect
whatsoever**: it was a hardcoded module constant, and `config.py` read only
PLACEMENT_SERVICE_SECRET / _MAX_WORKERS / _WALL_SECONDS_MAX / _PORT. Four
production deploys were then spent varying the wall (10s -> 30s) and the
machine (shared-cpu-2x -> performance-8x/16gb) while the one variable everybody
believed was being tuned never moved off 8. Every run returned a byte-identical
board.

A knob that silently does nothing is worse than no knob: it makes an experiment
look conclusive when it never ran.

--- what changed under this file, and why the shape of the tests changed too ---

The knobs were first made settable by turning them into `os.environ.get(...)`
module constants in `model.py`. That fixed the incident and broke the layering:
`_RULES.md` §2.1 forbids `os.environ` in the domain. They now live as a
`SolverKnobs` VALUE — defaults in the domain, environment read in `config.py`,
joined in `main.py` — so these tests no longer reload modules to observe an
import-time read. Nothing here monkeypatches a module attribute and asserts it
changed; that shape passes even when nothing ever reads the environment, which
is exactly the vacuity this file exists to prevent.

The chain has four links and each one is asserted separately, because the
incident was a break at ONE of them with the other three intact:

    environment -> Settings -> SolverKnobs -> CpSolver.parameters

and the last test walks the whole chain through the real servicer, which is
the only assertion that fails if `main.py` builds the knobs correctly and then
forgets to pass them to `solve()`. That failure mode is live: `solve(model,
wall_seconds)` still has a default `knobs` argument so domain tests can call it
with two arguments, and a defaulted argument nobody passes is precisely how
2026-08-10 happened the first time.
"""

from __future__ import annotations

import time

import pytest
import structlog

import placement.objective
from placement.config import Settings
from placement.model import DEFAULT_SOLVER_KNOBS, SolverKnobs

KNOB_ENV = (
    "PLACEMENT_NUM_SEARCH_WORKERS",
    "PLACEMENT_SYMMETRY_LEVEL",
    "PLACEMENT_PROBING_LEVEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """A process environment with no knob set and a usable secret.

    The secret matters: `Settings.from_env()` refuses to build without one, so
    a test that forgot it would fail for a reason that has nothing to do with
    the knobs.
    """
    for key in KNOB_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    return monkeypatch


# --- link 1: the domain owns the defaults -----------------------------------


def test_the_knobs_have_their_documented_defaults():
    """Unset environment must reproduce the shipped behaviour exactly.

    These three defaults are what every measurement in this service's docs was
    taken against, so a change here silently invalidates the record.
    """
    assert DEFAULT_SOLVER_KNOBS == SolverKnobs(
        num_search_workers=8,
        symmetry_level=0,
        cp_model_probing_level=0,
    )


def test_an_unset_environment_changes_no_default(clean_env):
    """The anti-drift assertion, and the reason `Settings` carries `None`.

    `config.py` may not import the domain, so if it defaulted these itself
    there would be two copies of `8` — and the copy that drifted would drift
    silently, changing the shipped solver behaviour with nothing to catch it.
    `None` means "not overridden", so there is exactly one copy of every
    default and this composition is the identity.
    """
    settings = Settings.from_env()

    assert (settings.num_search_workers, settings.symmetry_level, settings.cp_model_probing_level) == (
        None,
        None,
        None,
    )
    assert (
        DEFAULT_SOLVER_KNOBS.with_overrides(
            num_search_workers=settings.num_search_workers,
            symmetry_level=settings.symmetry_level,
            cp_model_probing_level=settings.cp_model_probing_level,
        )
        == DEFAULT_SOLVER_KNOBS
    )


# --- link 2: the environment reaches Settings -------------------------------


def test_the_knobs_are_actually_read_from_the_environment(clean_env):
    """THE regression test. This fails against a hardcoded constant.

    Not asserting "the field can be set" — that is true of any dataclass field
    and proves nothing. Asserting that a value in the ENVIRONMENT reaches the
    settings object, which is the thing that was false in production.
    """
    clean_env.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "3")
    clean_env.setenv("PLACEMENT_SYMMETRY_LEVEL", "2")
    clean_env.setenv("PLACEMENT_PROBING_LEVEL", "1")

    settings = Settings.from_env()

    assert settings.num_search_workers == 3
    assert settings.symmetry_level == 2
    assert settings.cp_model_probing_level == 1


def test_an_unparseable_value_is_refused_rather_than_read_as_unset(clean_env):
    """A typo'd knob must stop the process, not fall back to the default.

    Falling back is the incident in miniature: the operator sees the variable
    set in `fly.toml`, the service runs, and the number in effect is not the
    one on the screen.
    """
    clean_env.setenv("PLACEMENT_SYMMETRY_LEVEL", "2 ")  # trailing space is fine
    assert Settings.from_env().symmetry_level == 2

    clean_env.setenv("PLACEMENT_SYMMETRY_LEVEL", "high")
    with pytest.raises(ValueError, match="PLACEMENT_SYMMETRY_LEVEL must be an integer"):
        Settings.from_env()


def test_a_negative_worker_count_is_refused_but_zero_is_not(clean_env):
    """Zero is a legitimate request and must survive to the solver.

    CP-SAT reads `num_search_workers = 0` as "choose for me", which is exactly
    what an operator wants on an unfamiliar box. Rejecting it — or, worse,
    treating it as unset — would take a deliberate instruction and quietly
    substitute 8. A negative count is not a request the solver can refuse
    loudly, so it is refused here.
    """
    clean_env.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "0")
    settings = Settings.from_env()
    assert settings.num_search_workers == 0
    assert DEFAULT_SOLVER_KNOBS.with_overrides(num_search_workers=0).num_search_workers == 0

    clean_env.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "-1")
    with pytest.raises(ValueError, match="PLACEMENT_NUM_SEARCH_WORKERS must be >= 0"):
        Settings.from_env()


# --- link 3: Settings reaches SolverKnobs, and the solver ------------------


def test_with_overrides_keeps_a_zero_and_ignores_only_none():
    """`None` and `0` are different answers and must not collapse.

    Both presolve knobs default to 0, so a naive falsy check would look correct
    on the shipped configuration and be wrong for exactly the operator who
    turned one on and then wanted it back off.
    """
    base = SolverKnobs(num_search_workers=4, symmetry_level=3, cp_model_probing_level=2)

    assert base.with_overrides(symmetry_level=0) == SolverKnobs(
        num_search_workers=4, symmetry_level=0, cp_model_probing_level=2
    )
    assert base.with_overrides(symmetry_level=None) == base


def test_with_overrides_refuses_a_knob_it_does_not_have():
    """A misspelled knob raises instead of being silently dropped — a silent
    drop is the same class of defect as the one this file is named for."""
    with pytest.raises(TypeError):
        DEFAULT_SOLVER_KNOBS.with_overrides(num_serch_workers=2)


def test_the_tier_solver_applies_the_knobs_it_is_given():
    """The knobs reaching the SOLVER, not merely a dataclass.

    A knob carried in a value nothing passes to CP-SAT is the same defect in a
    different place, so this asserts against `solver.parameters` — the object
    the search actually uses.
    """
    knobs = SolverKnobs(num_search_workers=2, symmetry_level=1, cp_model_probing_level=2)

    # A deadline far enough out that `max_time_in_seconds` is not the floor.
    solver = placement.objective._tier_solver(time.perf_counter() + 30.0, knobs)

    assert solver.parameters.num_search_workers == 2
    assert solver.parameters.symmetry_level == 1
    assert solver.parameters.cp_model_probing_level == 2


# --- link 4: the whole chain, through the real servicer --------------------


def _solvable_request():
    """Same shape and same constant as `test_server.py`'s `_solvable_request`:
    one fixture, one court, one slot, and a real epoch-ms timestamp rather than
    0 — the boundary rejects a slot at epoch 0, and a zero-based corpus is what
    made "unset" and "a test value" the same number for four review rounds.
    Small enough that nothing here is timing-sensitive."""
    from placement.generated import scheduler_pb2  # noqa: PLC0415

    slot_ms = 1_767_225_600_000 + 8 * 3_600_000
    return scheduler_pb2.SolveBuildRequest(
        request_id="knobs-1",
        court_names=["Court 1"],
        entrant_count=2,
        division_count=1,
        fixtures=[scheduler_pb2.Fixture(entrant_indices=[0, 1], division_index=0)],
        slots=[scheduler_pb2.Slot(court_index=0, start_at_ms=slot_ms, day_index=0)],
        step_minutes=10,
        constraints=scheduler_pb2.BuildConstraints(match_minutes=30, gap_minutes=10),
        wall_seconds=2.0,
    )


def test_the_service_carries_its_env_knobs_all_the_way_into_the_search(clean_env):
    """environment -> Settings -> SolverKnobs -> every tier's CpSolver.

    THE test that fails if `main.py` resolves the knobs correctly and then
    calls `solve(model, wall_seconds=wall)` without them. `solve` has a default
    `knobs` argument, so that call compiles, runs, returns a good board, and
    logs the right numbers while the search uses 8/0/0 — the 2026-08-10
    incident, reproduced exactly, one layer along.

    The spy delegates rather than replacing: a stub returning a fake solver
    would prove the argument arrived and nothing about the solve still working.
    """
    from placement.main import SchedulerServicer  # noqa: PLC0415

    clean_env.setenv("PLACEMENT_NUM_SEARCH_WORKERS", "2")
    clean_env.setenv("PLACEMENT_SYMMETRY_LEVEL", "1")
    clean_env.setenv("PLACEMENT_PROBING_LEVEL", "2")

    seen: list[SolverKnobs] = []
    real_tier_solver = placement.objective._tier_solver

    def spy(deadline, knobs=DEFAULT_SOLVER_KNOBS):
        seen.append(knobs)
        return real_tier_solver(deadline, knobs)

    clean_env.setattr(placement.objective, "_tier_solver", spy)

    servicer = SchedulerServicer(Settings.from_env())
    response = servicer._solve_build(_solvable_request())

    assert seen, "no tier ran, so this asserted nothing"
    expected = SolverKnobs(num_search_workers=2, symmetry_level=1, cp_model_probing_level=2)
    assert all(knobs == expected for knobs in seen), seen
    # The board still comes back. A solve that reached the solver with the
    # right settings and then failed would satisfy every assertion above.
    assert len(response.assignments) == 1


def test_a_solve_logs_one_line_saying_what_it_did():
    """The absence of this line cost four production deploys.

    The service logged only refusals, so the only visible timing was TS-side
    `elapsed_ms` — which covers greedy seed, grid, encode, RPC and verification,
    and cannot separate "the solver used its whole budget" from "the solver
    finished in 3s and something else took the rest".

    Asserted on the FIELDS, not the prose: `granted_wall_seconds` is the wall
    after clamping (so a clamp is visible rather than inferred),
    `solver_elapsed_ms` is the solver's own time, and the three knobs are
    echoed because a knob you cannot see is a knob you cannot trust.

    `structlog.testing.capture_logs()`, not `caplog`: `main.py` now logs this
    through structlog, configured standalone (see `placement._logging`) rather
    than routed through stdlib `logging`, so the structured fields never reach
    `record.getMessage()` and a substring match against a rendered line no
    longer sees anything. `capture_logs()` yields the events as plain dicts —
    one per call, each carrying `event` plus every kwarg passed — so the
    fields below are asserted as dict keys instead of substrings of a line.
    """
    from placement.main import SchedulerServicer  # noqa: PLC0415

    settings = Settings(
        port=50051,
        max_workers=1,
        shared_secret="test-secret",
        wall_seconds_max=5.0,
    )
    servicer = SchedulerServicer(settings)

    with structlog.testing.capture_logs() as captured:
        servicer._solve_build(_solvable_request())

    events = [entry for entry in captured if entry["event"] == "solve_completed"]
    assert len(events) == 1, f"expected exactly one per-solve event, got {captured}"
    entry = events[0]
    for field in (
        "status",
        "placed",
        "fixtures",
        "tiers_completed",
        "tiers_total",
        "granted_wall_seconds",
        "solver_elapsed_ms",
        "workers",
        "symmetry",
        "probing",
    ):
        assert field in entry, f"{field!r} missing from the solve_completed event: {entry}"
    # The values, not just the keys: `Settings` above overrides nothing, so the
    # event must echo the shipped defaults. An event that carried workers=0 for
    # every solve would satisfy the loop and tell an operator nothing.
    assert (entry["workers"], entry["symmetry"], entry["probing"]) == (8, 0, 0)
    assert entry["request_id"] == "knobs-1"
