"""`PLACEMENT_BIND_HOST` reaches the socket: a real server, a real port.

`test_config.py` proves the env var becomes the right address string. This file
proves the string is what `build_server` binds, which no config test can see.
The default is the one that matters most: `[::]` is what Fly's private IPv6
network reaches, and it is what the service bound before the knob existed.

Every refusal assertion has a positive control beside it. "Connecting to ::1 was
refused" proves nothing on a host with no IPv6 loopback, where the same
connect fails for a different reason, so each test first shows the address is
reachable under the wildcard bind and asserts the exact exception class after.
"""

import contextlib
import socket

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from placement.config import Settings
from placement.main import build_server


def _settings(**overrides) -> Settings:
    return Settings(port=0, max_workers=1, shared_secret="test-secret", wall_seconds_max=5.0, **overrides)


@contextlib.contextmanager
def _serving(**overrides):
    server, port = build_server(_settings(**overrides))
    assert port != 0, "the server failed to bind"
    server.start()
    try:
        yield port
    finally:
        server.stop(None)


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _accepts(host: str, port: int) -> bool:
    """True if a TCP connect succeeds, False on ECONNREFUSED. Anything else
    (no route, address not available) is an error, not a refusal."""
    with socket.socket(_family(host)) as s:
        s.settimeout(3)
        try:
            s.connect((host, port))
        except ConnectionRefusedError:
            return False
        return True


def _serves_health(host: str, port: int) -> bool:
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    with grpc.insecure_channel(target) as channel:
        reply = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=10)
    return reply.status == health_pb2.HealthCheckResponse.SERVING


def _non_loopback_ipv4() -> str | None:
    """This host's routed IPv4 address, if it has one. A UDP connect sends
    nothing; it only asks the kernel which source address it would use."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def test_default_binds_both_loopbacks():
    with _serving() as port:
        assert _accepts("127.0.0.1", port) and _serves_health("127.0.0.1", port)
        assert _accepts("::1", port) and _serves_health("::1", port)


def test_an_ipv4_loopback_host_is_not_listening_on_ipv6():
    with _serving() as wildcard_port:
        assert _accepts("::1", wildcard_port), "control: this host has a usable ::1"
    with _serving(bind_host="127.0.0.1") as port:
        assert _accepts("127.0.0.1", port) and _serves_health("127.0.0.1", port)
        assert _accepts("::1", port) is False


def test_a_bare_ipv6_literal_is_bracketed_and_binds_only_that_address():
    with _serving() as wildcard_port:
        assert _accepts("127.0.0.1", wildcard_port), "control: 127.0.0.1 reaches the wildcard"
    with _serving(bind_host="::1") as port:
        assert _accepts("::1", port) and _serves_health("::1", port)
        assert _accepts("127.0.0.1", port) is False


def test_a_loopback_bind_does_not_answer_on_the_hosts_routed_address():
    routed = _non_loopback_ipv4()
    if routed is None:
        pytest.skip("this host has no non-loopback IPv4 address; the ::1 tests above carry the guard")
    with _serving(bind_host="0.0.0.0") as open_port:
        assert _accepts(routed, open_port), "control: the routed address reaches a 0.0.0.0 bind"
    with _serving(bind_host="127.0.0.1") as port:
        assert _accepts(routed, port) is False


# --- serve(): the operator can see which host was bound ---------------------


class _FakeServer:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True

    def wait_for_termination(self):
        return None


def test_serve_logs_the_bind_host_it_was_given(monkeypatch):
    """A knob you cannot see is a knob you cannot trust: the listening line
    names the host, so `fly logs` shows whether the setting took."""
    import structlog.testing  # noqa: PLC0415

    from placement import main  # noqa: PLC0415

    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_BIND_HOST", "::1")
    monkeypatch.setenv("PLACEMENT_PORT", "7777")
    server = _FakeServer()
    monkeypatch.setattr(main, "build_server", lambda settings: (server, 7777))
    # serve() reconfigures structlog for stdout JSON, which would bypass the capture.
    monkeypatch.setattr(main, "configure_structlog", lambda level: None)
    with structlog.testing.capture_logs() as captured:
        main.serve()
    listening = [e for e in captured if e["event"] == "service_listening"]
    assert server.started and len(listening) == 1, captured
    assert listening[0].get("bind_host") == "[::1]"
    assert listening[0]["port"] == 7777


def test_serve_names_the_whole_address_when_the_bind_fails(monkeypatch):
    from placement import main  # noqa: PLC0415

    monkeypatch.setenv("PLACEMENT_SERVICE_SECRET", "test-secret")
    monkeypatch.setenv("PLACEMENT_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("PLACEMENT_PORT", "7777")
    monkeypatch.setattr(main, "build_server", lambda settings: (_FakeServer(), 0))
    with pytest.raises(RuntimeError, match=r"failed to bind 127\.0\.0\.1:7777"):
        main.serve()
