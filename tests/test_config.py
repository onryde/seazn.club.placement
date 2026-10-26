"""Settings load from the environment, and refuse to come up without a secret.

`Settings.from_env()` is the only place the process reads its environment, so
every default that matters to a deploy is stated once, here, and is testable
without standing a server up.
"""

import pytest

from placement.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("PLACEMENT_PORT", "50051")
    monkeypatch.setenv("PLACEMENT_MAX_WORKERS", "4")
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", "10")
    s = Settings.from_env()
    assert s.port == 50051
    assert s.max_workers == 4
    assert s.shared_secret == "test-secret"
    assert s.wall_seconds_max == 10.0


def test_settings_requires_secret(monkeypatch):
    monkeypatch.delenv("PLACEMENT_SERVICE_SECRET", raising=False)
    with pytest.raises(ValueError, match="PLACEMENT_SERVICE_SECRET"):
        Settings.from_env()


# --- degenerate configuration ----------------------------------------------
# Same zero-scalar family the wire boundary guards, one layer further out. The
# difference that makes these worth catching at startup: a bad request is one
# caller's problem, a bad env var is EVERY caller's problem, and the service
# stays healthy while producing it.


@pytest.mark.parametrize("value", ["0", "-1"])
def test_settings_rejects_non_positive_wall_seconds_max(monkeypatch, value):
    """`wall_seconds_max` is the ceiling every request's budget is clamped to.
    At 0 the clamp takes every well-formed request down to 0.0, the tier chain
    refuses it, and the caller is told `wall_seconds must be > 0` about a
    request that correctly sent 8.0 — a server misconfiguration reported as the
    caller's fault, on every request, while health still says SERVING."""
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_WALL_SECONDS_MAX", value)
    with pytest.raises(ValueError, match="PLACEMENT_WALL_SECONDS_MAX"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_settings_rejects_non_positive_max_workers(monkeypatch, value):
    """`ThreadPoolExecutor(max_workers=0)` raises at startup and `-1` likewise;
    failing here names the env var instead."""
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_MAX_WORKERS", value)
    with pytest.raises(ValueError, match="PLACEMENT_MAX_WORKERS"):
        Settings.from_env()


def test_settings_accepts_the_documented_defaults(monkeypatch):
    """The guards must not reject a bare, correct environment — only the secret
    has no default."""
    for name in ("PLACEMENT_PORT", "PLACEMENT_MAX_WORKERS", "PLACEMENT_WALL_SECONDS_MAX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    s = Settings.from_env()
    assert (s.port, s.max_workers, s.wall_seconds_max) == (50051, 4, 10.0)


# --- PLACEMENT_BIND_HOST ----------------------------------------------------
# The listen address is `<host>:<port>`. The default is the dual-stack wildcard
# `[::]`, byte for byte what `build_server` used to hard-code: Fly's private
# IPv6 network reaches the service through it, so a default that drifted to
# `0.0.0.0` would take production off the network without a single test red.


def _env_with_secret(monkeypatch):
    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")


def test_bind_host_defaults_to_the_dual_stack_wildcard(monkeypatch):
    _env_with_secret(monkeypatch)
    monkeypatch.delenv("PLACEMENT_BIND_HOST", raising=False)
    s = Settings.from_env()
    assert s.bind_host == "[::]"
    assert s.listen_address == "[::]:50051"


# (raw value, the host part of the listen address). Expected values are the
# gRPC target grammar's, not the code's: an IPv6 literal is written in
# brackets, a name or an IPv4 address is not.
BIND_HOST_CASES = [
    ("127.0.0.1", "127.0.0.1"),
    ("0.0.0.0", "0.0.0.0"),
    ("localhost", "localhost"),
    ("::1", "[::1]"),  # a bare IPv6 literal gets its brackets
    ("::", "[::]"),
    ("fdaa:0:1::3", "[fdaa:0:1::3]"),
    ("[::1]", "[::1]"),  # already bracketed: not bracketed twice
    ("[::]", "[::]"),
    ("  127.0.0.1\n", "127.0.0.1"),  # surrounding whitespace is not part of the host
]


@pytest.mark.parametrize("raw,expected", BIND_HOST_CASES)
def test_bind_host_is_bracketed_only_when_it_is_a_bare_ipv6_literal(monkeypatch, raw, expected):
    _env_with_secret(monkeypatch)
    monkeypatch.setenv("PLACEMENT_BIND_HOST", raw)
    monkeypatch.setenv("PLACEMENT_PORT", "6000")
    s = Settings.from_env()
    assert s.bind_host == expected
    assert s.listen_address == f"{expected}:6000"


def test_bind_host_cases_are_not_vacuous():
    assert len(BIND_HOST_CASES) >= 8
    assert any(raw.count(":") > 1 and "[" not in raw for raw, _ in BIND_HOST_CASES)


@pytest.mark.parametrize("value", ["", " ", "   ", "\t", "\n"])
def test_bind_host_refuses_an_empty_or_blank_value(monkeypatch, value):
    """SET-but-blank is refused, and that is not the same as unset (unset takes
    the default). Left to gRPC, a blank host builds the address `:50051`, which
    grpcio 1.85 refuses at bind time with `host must not be empty` — a
    RuntimeError that never names the env var, from a process whose operator
    typed `PLACEMENT_BIND_HOST=` into a deploy file and expects loopback."""
    _env_with_secret(monkeypatch)
    monkeypatch.setenv("PLACEMENT_BIND_HOST", value)
    with pytest.raises(ValueError, match="PLACEMENT_BIND_HOST"):
        Settings.from_env()


def test_bind_host_survives_dataclasses_replace(monkeypatch):
    """The test suite builds `Settings` once and `replace()`s fields on it
    (`port=0`), which re-runs the normalisation on an already-normalised host."""
    import dataclasses  # noqa: PLC0415

    _env_with_secret(monkeypatch)
    monkeypatch.setenv("PLACEMENT_BIND_HOST", "::1")
    s = dataclasses.replace(Settings.from_env(), port=0)
    assert s.listen_address == "[::1]:0"
