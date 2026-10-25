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
