"""Structlog configuration for the placement service.

Deliberately INDEPENDENT of stdlib `logging` rather than routed through it.
`serve()` keeps its own `logging.basicConfig(...)` call, unchanged, for
whatever third-party code still emits through stdlib `logging`; this module
configures structlog as a second, parallel logger rather than unifying the
two via `structlog.stdlib.ProcessorFormatter`.

Why independent: nothing else in this codebase emits a stdlib log worth
unifying with — `main.py`'s own call sites are the only application code that
logs at all, and this change is converting them AWAY from `logging`, not
toward it. There is no second emitter here that justifies the extra
machinery. Routing through `ProcessorFormatter` would also complicate the
testing story for no benefit: `caplog` (pytest's stdlib capture) and
`structlog.testing.capture_logs()` (what this service's own tests use) would
have to be kept in agreement about one shared pipeline instead of each
reading its own logger cleanly. Simplicity wins while there is only one real
application emitter.
"""

from __future__ import annotations

import sys

import structlog


def configure_structlog(level: int) -> None:
    """Configure structlog standalone, at `level`.

    `level` is an `int` such as `logging.INFO` — in practice always the
    return value of `placement.main.resolve_log_level`, so `PLACEMENT_LOG_LEVEL`
    governs structlog's own filtering exactly as it governs stdlib logging's,
    from the one place `main.py` resolves it.

    `wrapper_class=make_filtering_bound_logger(level)` does structlog's level
    filtering itself, independently of stdlib `logging`'s. The processor chain
    ends in `JSONRenderer` rather than structlog's default human-readable
    console renderer: this is a deployed Fly.io service, and JSON lines are
    what a log aggregator wants.

    Deliberately NOT `cache_logger_on_first_use=True`: structlog's own docs
    warn that a cached logger is invisible to `structlog.testing.capture_logs()`
    (it patches the logger FACTORY, which a cached logger never calls again),
    so the first test to exercise `configure_structlog()` would make every
    OTHER `capture_logs()`-based test in the same process start silently
    seeing empty captures. A handful of log calls per RPC is nowhere near
    CP-SAT's own cost, so there is nothing here worth that footgun.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    )
