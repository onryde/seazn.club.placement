"""The gRPC server: bootstrap, auth, health, and the SolveBuild handler.

Application/infrastructure layer. It orchestrates and nothing else: it
authenticates, asks `placement.schema` to translate the request into domain types,
asks the domain (`build_model` / `solve`) to compute, and asks `placement.schema`
to translate the plain-Python outcome back onto the wire. It decides nothing
about scheduling and — deliberately — constructs no proto message itself. Both
directions of the translation live in the one anti-corruption layer, which is
why this module imports `scheduler_pb2_grpc` (the generated servicer base and
its registration helper) but never `scheduler_pb2`.

Sync `grpcio` with a `ThreadPoolExecutor`, not `grpc.aio`: a solve is a
CPU-bound OR-Tools call that never yields, so an event loop buys nothing. Auth
is a shared secret on request metadata, not mTLS — 6PN is already
WireGuard-encrypted, and cert rotation would be cost against a threat model
nothing else in this repo defends against.

--- the health check gets its own threads, and solves are admission-controlled

An earlier version of this docstring justified the thread pool partly on the
grounds that an event loop "would starve the health check". The chosen
topology starved it too, and the claim is exactly why nobody looked: a comment
asserting a bug is absent is how the bug survives the next review.

Measured over a real port with `solve` replaced by a sleep, so solver speed was
not a variable:

    max_workers   health, idle       health, pool saturated (2 s deadline)
    1             SERVING in 0.003s  DEADLINE_EXCEEDED after 2.005s
    4 (default)   SERVING in 0.002s  DEADLINE_EXCEEDED after 2.004s

One `grpc.server` carried both servicers on one bounded pool, so `max_workers`
in-flight solves left no thread to answer `Health/Check`. With the shipped
defaults that is a ~10 s liveness blackout — long enough for a platform probe
to kill the machine MID-SOLVE, taking every in-flight solve with it. It is
self-reinforcing under load: the kill drops capacity, which saturates the
replacement faster.

Two changes, and both are needed:

  * the pool is `max_workers + HEALTH_RESERVE_THREADS`, so threads exist that
    a solve can never occupy. `PLACEMENT_MAX_WORKERS` now means CONCURRENT SOLVES
    rather than pool size.
  * `SolveBuild` takes a slot from a bounded semaphore and refuses at once
    with `SOLVER_BUSY` when there is none. Reserved threads alone would fill
    with QUEUED solves, and a queued caller waits out a deadline it was always
    going to miss — `build.ts` can fall back on its own placer given a reason,
    and can do nothing with an answer that arrives after it gave up.

One server on one port, deliberately: a second server for health would need a
second port in the deploy, and the reserve achieves the same isolation without
one.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from concurrent import futures

import grpc
import structlog
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from placement._logging import configure_structlog
from placement.config import Settings
from placement.generated import scheduler_pb2_grpc
from placement.model import (
    DEFAULT_SOLVER_KNOBS,
    build_model,
    solve,
)
from placement.schema import (
    InvalidRequestError,
    error_response,
    outcome_to_response,
    request_to_model_input,
)

log = structlog.get_logger(__name__)

AUTH_METADATA_KEY = "x-internal-secret"

#: Threads on the server's pool that no solve can ever occupy.
#:
#: Two, not one: one answers `Health/Check` while the pool is full, and the
#: second answers the `SOLVER_BUSY` refusals — which are instant, but still
#: need a thread to be instant ON. Sizing this from `max_workers` would defeat
#: it, since the whole point is a floor that does not move when a caller turns
#: the solve concurrency up.
HEALTH_RESERVE_THREADS = 2


class SchedulerServicer(scheduler_pb2_grpc.SchedulerServiceServicer):
    def __init__(self, settings: Settings):
        self._settings = settings
        # THIS LINE IS THE JOIN, and it is the whole reason the knobs moved out
        # of `model.py`. `config.py` reads the environment and may not import
        # the domain; the domain owns the defaults and may not read the
        # environment; `main.py` is the only layer that sees both, so it is the
        # only layer that can put them together.
        #
        # Resolved ONCE, at construction: these are process-level search
        # settings, and resolving them per request would let two solves on one
        # machine behave differently for no visible reason.
        self._knobs = DEFAULT_SOLVER_KNOBS.with_overrides(
            num_search_workers=settings.num_search_workers,
            symmetry_level=settings.symmetry_level,
            cp_model_probing_level=settings.cp_model_probing_level,
        )
        # Admission control. `BoundedSemaphore` rather than `Semaphore` so a
        # release without a matching acquire — the shape a future refactor of
        # the try/finally below would take — raises instead of silently
        # inflating the concurrency limit.
        self._solve_slots = threading.BoundedSemaphore(settings.max_workers)

    def _authenticated(self, context) -> bool:
        # Header names are case-insensitive on the wire and a real transport
        # lower-cases them before the servicer sees them; normalising here
        # means the in-process test harness and a live server agree.
        metadata = {key.lower(): value for key, value in (context.invocation_metadata() or ())}
        secret = metadata.get(AUTH_METADATA_KEY)
        if not isinstance(secret, str):
            return False
        # Constant-time. The comparison is cheap either way, and a shared
        # secret compared with `==` leaks its prefix through timing.
        return hmac.compare_digest(secret, self._settings.shared_secret)

    def SolveBuild(self, request, context):
        if not self._authenticated(context):
            # Before ANY work: an unauthenticated caller must not be able to
            # spend a worker thread, let alone a whole wall budget, on a solve.
            # `abort` raises, so nothing below runs.
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid secret")

        # Admission control, before the request is even parsed: a saturated
        # server should cost a caller a round trip, not a parse of 8 000 slots.
        # `blocking=False` is the whole mechanism — queueing here is what fills
        # the health check's reserved threads and turns a busy service into a
        # dead-looking one.
        if not self._solve_slots.acquire(blocking=False):
            log.warning("solver_busy", request_id=request.request_id)
            return error_response(
                "SOLVER_BUSY",
                f"all {self._settings.max_workers} solve slots are in use. Retry, or fall back to "
                "your own placer — this request was refused immediately rather than queued, "
                "because a queued solve returns after the caller's deadline has already passed.",
            )

        try:
            return self._solve_build(request)
        finally:
            self._solve_slots.release()

    def _solve_build(self, request):
        """The handler proper, holding a solve slot. Split out so the release
        is a single `finally` with no early-return path that can skip it."""
        try:
            parsed = request_to_model_input(request)
        except InvalidRequestError as exc:
            # LOG IT, at the same level and with the same shape as
            # `solve_rejected` below. These are the two INVALID_REQUEST paths
            # and they used to be asymmetric: the domain one logged, this WIRE
            # one returned the identical response and said nothing.
            #
            # That made it the only refusal here invisible on BOTH sides. The
            # caller receives a RESOLVED response, not an RPC error, so the TS
            # `buildSchedule` catch never runs either — and until its sibling
            # commit the resolved-ERROR arm logged nothing and dropped this
            # message. A fully-diagnosed refusal, with the reason already
            # written down, reached the operator as a bare
            # `solver_unavailable`.
            #
            # It cost a bench wave: suite 11 published "the optimized
            # scheduling path does not survive a real fixture count" against
            # exactly that evidence, and a 30 s control run later showed the
            # build returning in 849 ms against a 30_000 ms wall — never a
            # timeout. This line is what names the real fault on run one.
            log.warning(
                "request_refused", request_id=request.request_id, error=str(exc)
            )
            return error_response("INVALID_REQUEST", str(exc))

        wall = min(parsed.wall_seconds, self._settings.wall_seconds_max)

        # Defence in depth, NOT dead code. `request_to_model_input` already
        # rejects every degenerate scalar `build_model` and `run_tier_chain`
        # guard against, so in principle nothing here can raise. But the two
        # layers are independent by design — the domain guards protect the
        # bench and any future caller, and the wire guard protects the RPC —
        # and "in principle nothing raises" is exactly the assumption that ends
        # with a raw traceback going back to a caller as an opaque UNKNOWN.
        # Same response shape as the branch above: whichever layer noticed, the
        # caller sees one thing.
        try:
            model = build_model(
                parsed.fixtures,
                parsed.courts,
                parsed.grid_slots,
                parsed.step_minutes,
                parsed.constraints,
                parsed.existing,
                parsed.dependencies,
                parsed.rule_groups,
                parsed.pinned_rule_group_indices,
                parsed.pinned_entrant_indices,
                parsed.fixture_rounds,
                parsed.pinned_round,
                parsed.person_indices,
                parsed.allowed_courts,
            )
            solve_started = time.perf_counter()
            outcome = solve(model, wall_seconds=wall, knobs=self._knobs)
            solve_elapsed_ms = (time.perf_counter() - solve_started) * 1000.0
        except ValueError as exc:
            log.warning("solve_rejected", request_id=request.request_id, error=str(exc))
            return error_response("INVALID_REQUEST", str(exc))

        # ONE LINE PER SOLVE, and it is not decoration.
        #
        # Before this existed the service logged only refusals, so the only
        # visible timing was `elapsed_ms` on the TS side — which measures the
        # whole round trip (greedy seed, grid, encode, RPC, verification) and
        # cannot separate "the solver used its whole budget" from "the solver
        # finished in 3s and something else took the rest".
        #
        # That cost four production deploys on 2026-08-10 to learn nothing: the
        # wall went 10s -> 30s and the machine shared-cpu-2x -> performance-8x,
        # and every run returned a byte-identical board at tiers_completed 1/4.
        # With this line the first run would have said which of those numbers
        # was even moving.
        #
        # `granted` is the wall the solve actually got (`min(requested, max)`),
        # so a clamp is visible here rather than inferred from a stopwatch two
        # processes away. `workers`/`symmetry` are logged because both are now
        # env-overridable and a knob you cannot see is a knob you cannot trust —
        # `NUM_SEARCH_WORKERS` spent that same afternoon set in `fly.toml` while
        # having no effect whatsoever.
        log.info(
            "solve_completed",
            request_id=request.request_id or "-",
            status=outcome.status,
            placed=len(outcome.assignments),
            fixtures=len(parsed.fixtures),
            tiers_completed=outcome.tiers_completed,
            tiers_total=len(outcome.objective_values) or outcome.tiers_completed,
            granted_wall_seconds=wall,
            solver_elapsed_ms=solve_elapsed_ms,
            workers=self._knobs.num_search_workers,
            symmetry=self._knobs.symmetry_level,
            probing=self._knobs.cp_model_probing_level,
        )

        # `wall`, not `parsed.wall_seconds`: the budget the solve was actually
        # given is the one `wall_exhausted` has to be measured against.
        return outcome_to_response(outcome, wall_seconds=wall)


def build_health_servicer() -> health.HealthServicer:
    """The readiness probe's servicer, marked SERVING for the empty (whole
    server) service name. A function rather than four lines inside `serve()` so
    the wiring is testable without binding a port."""
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    return health_servicer


def build_server(settings: Settings) -> tuple[grpc.Server, int]:
    """Construct and register the server, bind its port, and return both it and
    the port actually bound. Does NOT start it.

    Split out of `serve()` so the registration path is reachable from a test.
    Every line here is load-bearing and none of it is observable from the
    in-process `grpc_testing` harness, which never calls
    `add_*Servicer_to_server` and never binds anything — all three registration
    lines could be deleted with the suite still green.

    Returns the bound port because `settings.port` may be 0, meaning "any free
    port": `add_insecure_port` then returns the one the OS picked, and 0 back
    means the bind FAILED.
    """
    # `max_workers + HEALTH_RESERVE_THREADS`, not `max_workers`. The servicer's
    # semaphore caps concurrent solves at `max_workers`, so the reserve is
    # threads a solve provably cannot take — which is what keeps `Health/Check`
    # answerable while the solver is saturated. Deleting the `+` here restores
    # the measured ~10 s liveness blackout, and no in-process test can see it.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.max_workers + HEALTH_RESERVE_THREADS)
    )
    scheduler_pb2_grpc.add_SchedulerServiceServicer_to_server(SchedulerServicer(settings), server)
    health_pb2_grpc.add_HealthServicer_to_server(build_health_servicer(), server)
    port = server.add_insecure_port(f"[::]:{settings.port}")
    return server, port


#: The level names `PLACEMENT_LOG_LEVEL` accepts. An explicit table rather than
#: `getattr(logging, name)`: that reads ANY module attribute, and `logging` has
#: public ones that are ints or int-like without being levels (`raiseExceptions`
#: is a bool, and `bool` is a subclass of `int`, so an `isinstance` guard does
#: not exclude it). A table can only ever return a level.
_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def resolve_log_level(raw: str | None) -> int:
    """`PLACEMENT_LOG_LEVEL` -> a logging level. Default INFO.

    Accepts a level NAME, case-insensitively and ignoring surrounding space.
    `NOTSET` is deliberately absent: on the root logger it does not mean "log
    everything", it defers to a parent that does not exist, and the effective
    level silently becomes WARNING — quieter than the default the operator was
    trying to change.

    UNKNOWN VALUES FALL BACK TO INFO with a complaint on stderr rather than
    raising, and the asymmetry with `Settings.from_env` — which refuses to start
    on a bad wall or worker count — is deliberate. Those change what the service
    COMPUTES; this changes only what it PRINTS. Taking the service down over a
    typo in an observability knob would mean the one setting you reach for while
    diagnosing an incident is also the one that can end it.
    """
    if raw is None or raw.strip() == "":
        return logging.INFO
    name = raw.strip().upper()
    level = _LOG_LEVELS.get(name)
    if level is None:
        print(
            f"PLACEMENT_LOG_LEVEL={raw!r} is not a known level name "
            f"({', '.join(sorted(_LOG_LEVELS))}); using INFO",
            file=sys.stderr,
        )
        return logging.INFO
    return level


def serve() -> None:
    level = resolve_log_level(os.environ.get("PLACEMENT_LOG_LEVEL"))
    logging.basicConfig(level=level)
    configure_structlog(level)
    settings = Settings.from_env()
    server, port = build_server(settings)
    if port == 0:
        raise RuntimeError(f"failed to bind port {settings.port}")
    server.start()
    log.info("service_listening", port=port, max_workers=settings.max_workers)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
