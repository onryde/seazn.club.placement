"""Process configuration, read once from the environment.

Deliberately a frozen dataclass built by an explicit `from_env()` rather than
module-level `os.environ` reads: the servicer takes its `Settings` as a
constructor argument, so a test can build one directly and never has to reason
about import-time side effects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _optional_int(name: str) -> int | None:
    """An env var that may be absent, parsed as an int.

    `None` means UNSET, and the caller keeps its own default — which is why
    this returns `None` rather than taking a default here. The solver knobs'
    defaults live on `model.SolverKnobs`, in the domain, and `config.py` may
    not import the domain (`_RULES.md` §2.1), so repeating them here is the one
    thing this function exists to avoid. Two copies of `8` would be two copies
    to keep in step, and the one that drifts would drift silently.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:  # a typo'd value must not be read as "unset"
        raise ValueError(
            f"{name} must be an integer, got {raw!r}. Treating an unparseable value as unset would "
            "put the process back in the 2026-08-10 failure mode: a knob that appears to be set, "
            "reads as the default, and makes an experiment look conclusive when it never ran."
        ) from exc


@dataclass(frozen=True)
class Settings:
    port: int
    max_workers: int
    shared_secret: str
    wall_seconds_max: float

    # --- the CP-SAT search knobs. `None` means "not overridden here", and the
    # --- domain's own default applies. They are typed as plain ints rather
    # --- than a `SolverKnobs` because this module may not import the domain;
    # --- `main.py` is the layer that owns both sides and does the joining.
    num_search_workers: int | None = None
    symmetry_level: int | None = None
    cp_model_probing_level: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        # No default, and the process refuses to start without it. An empty or
        # missing secret would otherwise make `secret != shared_secret` compare
        # two empty strings and authenticate every unauthenticated caller —
        # failing OPEN, which is the one failure mode auth may not have.
        secret = os.environ.get("PLACEMENT_SERVICE_SECRET")
        if not secret:
            raise ValueError("PLACEMENT_SERVICE_SECRET is required")

        # CONCURRENT SOLVES, not the RPC thread pool's size. `placement.main`
        # sizes the pool at this plus `HEALTH_RESERVE_THREADS` and holds the
        # solver to this number with a semaphore, so a saturated solver still
        # has threads left to answer the liveness probe.
        max_workers = int(os.environ.get("PLACEMENT_MAX_WORKERS", "4"))
        # The server's own ceiling on a caller-supplied `wall_seconds`. A
        # request may ask for less; it may not ask for more.
        wall_seconds_max = float(os.environ.get("PLACEMENT_WALL_SECONDS_MAX", "10"))

        # --- degenerate configuration. Same zero-scalar family `schema.py`
        # --- guards on the wire, one layer further out — and worse, because a
        # --- bad request is one caller's problem while a bad env var is every
        # --- caller's problem and the service reports SERVING throughout.
        if wall_seconds_max <= 0:
            raise ValueError(
                f"PLACEMENT_WALL_SECONDS_MAX must be > 0, got {wall_seconds_max!r}. Every request's "
                "budget is clamped to this ceiling, so a non-positive value takes a well-formed "
                "request asking for 8.0 down to 0.0, the tier chain refuses it, and the caller is "
                "told `wall_seconds must be > 0` about a field they set correctly."
            )
        if max_workers <= 0:
            raise ValueError(
                f"PLACEMENT_MAX_WORKERS must be > 0, got {max_workers!r}. It caps CONCURRENT SOLVES: "
                "the servicer admits that many at once and refuses the rest with SOLVER_BUSY, and "
                "the RPC thread pool is sized this plus a fixed reserve so the health check cannot "
                "be starved by a full solver."
            )

        # A negative worker count is rejected; ZERO is not, and the difference
        # is CP-SAT's, not ours — `num_search_workers = 0` means "choose for
        # me", which is a legitimate thing to ask for on an unfamiliar box.
        num_search_workers = _optional_int("PLACEMENT_NUM_SEARCH_WORKERS")
        if num_search_workers is not None and num_search_workers < 0:
            raise ValueError(
                f"PLACEMENT_NUM_SEARCH_WORKERS must be >= 0, got {num_search_workers!r}. It is the "
                "number of parallel CP-SAT search workers per solve; 0 asks the solver to pick, and "
                "a negative value is not a request the solver can refuse loudly."
            )

        return cls(
            port=int(os.environ.get("PLACEMENT_PORT", "50051")),
            max_workers=max_workers,
            shared_secret=secret,
            wall_seconds_max=wall_seconds_max,
            num_search_workers=num_search_workers,
            symmetry_level=_optional_int("PLACEMENT_SYMMETRY_LEVEL"),
            cp_model_probing_level=_optional_int("PLACEMENT_PROBING_LEVEL"),
        )
