"""`configure_structlog(level)` -> structlog's own filtering actually obeys it.

WHY THIS IS TESTED AT ALL. `serve()` resolves `PLACEMENT_LOG_LEVEL` ONCE and
hands the same `level` to both `logging.basicConfig` and `configure_structlog`
(see `main.py`'s `serve()`) specifically so the two loggers cannot drift apart.
`test_log_level.py` already proves `resolve_log_level` maps the env var to the
right int; this proves the OTHER half — that passing that int into
`configure_structlog` actually gates which structlog events get emitted, not
merely that the call does not raise. Without this, `configure_structlog` could
hardcode a level, or read `PLACEMENT_LOG_LEVEL` a second, independently
drifting way, and nothing would catch it: an operator raising the env var to
DEBUG mid-incident would see stdlib logs change and structlog's stay silent,
or vice versa.
"""

import logging

import structlog

from placement._logging import configure_structlog


def test_configure_structlog_level_gates_which_events_emit():
    old_config = structlog.get_config()
    try:
        configure_structlog(logging.WARNING)
        with structlog.testing.capture_logs() as captured_at_warning:
            log = structlog.get_logger("test")
            log.info("should_be_filtered_out")
            log.warning("should_pass_through")

        configure_structlog(logging.DEBUG)
        with structlog.testing.capture_logs() as captured_at_debug:
            log = structlog.get_logger("test")
            log.info("should_pass_now_the_level_dropped")
    finally:
        structlog.configure(**old_config)

    events_at_warning = [entry["event"] for entry in captured_at_warning]
    assert events_at_warning == ["should_pass_through"], captured_at_warning

    events_at_debug = [entry["event"] for entry in captured_at_debug]
    assert "should_pass_now_the_level_dropped" in events_at_debug, captured_at_debug
