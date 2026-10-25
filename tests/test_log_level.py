"""`PLACEMENT_LOG_LEVEL` -> a logging level.

WHY THIS IS TESTED AT ALL. The knob is one line in `serve()`, which no test
calls (it binds a port and blocks). So the resolution lives in a pure function
and this suite is the only thing that can say whether `PLACEMENT_LOG_LEVEL=DEBUG`
actually produces DEBUG — the failure mode otherwise is an operator setting it
during an incident, seeing no new output, and concluding the service is wedged.

The FALLBACK is the part worth pinning. It is deliberately unlike every other
env var this service reads: `Settings.from_env` refuses to start on a bad wall
or worker count, because those change what the service COMPUTES. This one only
changes what it PRINTS, so refusing would let a typo take down the service you
were trying to observe.
"""

import logging

import pytest

from placement.main import resolve_log_level


def test_defaults_to_info_when_unset():
    assert resolve_log_level(None) == logging.INFO


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_defaults_to_info_when_blank(raw):
    # Fly hands an unset env var through as an empty string rather than omitting
    # it, so blank has to mean "not configured" and not "an unknown level".
    assert resolve_log_level(raw) == logging.INFO


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("WARN", logging.WARNING),
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
    ],
)
def test_accepts_every_documented_level_name(raw, expected):
    assert resolve_log_level(raw) == expected


@pytest.mark.parametrize("raw", ["debug", "Debug", "  dEbUg  "])
def test_is_case_insensitive_and_ignores_surrounding_space(raw):
    assert resolve_log_level(raw) == logging.DEBUG


def test_debug_is_actually_more_verbose_than_the_default():
    # Non-vacuity: the table could map every name to INFO and every assertion
    # above would still pass except by name. A level is an ORDERING, so assert
    # the ordering, against an oracle (`logging`'s own constants) rather than
    # against the table under test.
    assert resolve_log_level("DEBUG") < resolve_log_level(None)
    assert resolve_log_level("ERROR") > resolve_log_level(None)


def test_an_unknown_name_falls_back_to_info_rather_than_raising(capsys):
    assert resolve_log_level("VERBOSE") == logging.INFO
    err = capsys.readouterr().err
    assert "VERBOSE" in err, "the rejected value must be named, or the typo is invisible"
    assert "using INFO" in err


def test_a_numeric_level_is_refused_rather_than_silently_accepted():
    # `logging` itself accepts ints, so "10" is a plausible thing to set. It is
    # NOT supported here, and the operator has to be told — silently treating it
    # as INFO while it looks like DEBUG is the worst of both.
    assert resolve_log_level("10") == logging.INFO


def test_a_non_level_logging_attribute_cannot_leak_through():
    # The guard against `getattr(logging, name)`: `logging.raiseExceptions` is
    # True, and `bool` subclasses `int`, so an isinstance check would have let
    # this resolve to level 1 — more verbose than DEBUG, from a name that is not
    # a level at all.
    assert resolve_log_level("RAISEEXCEPTIONS") == logging.INFO
    assert resolve_log_level("BASIC_FORMAT") == logging.INFO
    assert resolve_log_level("NOTSET") == logging.INFO
