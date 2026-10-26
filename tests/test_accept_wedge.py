"""A TCP connection that closes before the server accepts it must not wedge
the listener (docs/spikes/2026-10-26-grpcio-accept-wedge.md).

Fly's `tcp_checks` connect to the service every 15 s and close without a word.
On macOS, grpcio logs `Invalid address: Empty address` for a connection that
dies before accept and the listener then never accepts again (grpcio 1.83.0 and
1.85.0). The spike found no such wedge on Linux, which is where the image runs
(1.83.0 and 1.85.0, 300 knocks each). This test is the guard on that finding:
`pyproject.toml` leaves grpcio floating (`>=1.81.1,<2`) and the image takes
whatever is newest at build time, so a release that did wedge on Linux would
otherwise ship.

Two knock kinds: a plain close (FIN, what `nc -z` and a Fly TCP check do) and an
abortive close (RST, `SO_LINGER` 0). Only the RST kind wedges macOS, so it is
marked xfail there; it turns XPASS the day grpcio fixes it.

The probe is a real server on port 0 and two independent checks after the
knocks: `Health/Check` through grpcio, and a raw HTTP/2 preface that only an
accepting listener answers (with a SETTINGS frame).
"""

import socket
import struct
import sys
import threading

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from placement.config import Settings
from placement.main import build_server

KNOCKS = 100
H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"
SETTINGS_FRAME_TYPE = 0x04


def _answers_h2(port: int, timeout: float = 5.0) -> bool:
    """True when the listener at `port` replies to an HTTP/2 preface with a
    SETTINGS frame; False for a socket that is listening but never serves."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        try:
            s.sendall(H2_PREFACE)
            header = s.recv(9)
        except OSError:  # timed out (a wedge), or reset (the peer hung up)
            return False
    return len(header) == 9 and header[3] == SETTINGS_FRAME_TYPE


def _serves_health(port: int) -> bool:
    # A fresh channel: a reused one sits in its own reconnect backoff.
    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        try:
            reply = health_pb2_grpc.HealthStub(channel).Check(
                health_pb2.HealthCheckRequest(), timeout=10
            )
        except grpc.RpcError:
            return False
    return reply.status == health_pb2.HealthCheckResponse.SERVING


def _knock(port: int, rst: bool) -> int:
    """Connect, then close without sending a byte. Returns the knock's own
    local port, so a count of knocks is a count of sockets that existed."""
    knocker = socket.create_connection(("127.0.0.1", port), timeout=5)
    local_port = knocker.getsockname()[1]
    if rst:
        knocker.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    knocker.close()
    return local_port


KNOCK_KINDS = [
    pytest.param(False, id="fin"),
    pytest.param(
        True,
        id="rst",
        marks=pytest.mark.xfail(
            sys.platform == "darwin",
            reason="macOS-only grpcio wedge: the listener stops accepting after an RST'd "
            "connection that was never accepted (see the spike doc)",
            strict=False,
        ),
    ),
]


@pytest.mark.parametrize("rst", KNOCK_KINDS)
def test_listener_still_serves_after_connections_that_close_before_accept(rst):
    settings = Settings(port=0, max_workers=1, shared_secret="test-secret", wall_seconds_max=5.0)
    server, port = build_server(settings)
    assert port != 0
    server.start()
    try:
        assert _serves_health(port) and _answers_h2(port), "control: healthy before the knocks"
        assert KNOCKS >= 50, "a handful of knocks proves nothing"
        knock_ports = [_knock(port, rst) for _ in range(KNOCKS)]
        assert len(knock_ports) == KNOCKS and all(knock_ports), "the knocks were not all sent"
        assert _answers_h2(port), "the listener stopped accepting after the knocks"
        assert _serves_health(port), "Health/Check failed after the knocks"
    finally:
        server.stop(None)


def test_the_probes_can_tell_a_dead_listener_from_a_serving_one():
    """Negative controls. Each shape below is what a wedge or a dead service
    looks like from outside, and each probe must call it down; without this the
    test above could pass against a probe that answers True."""
    # Listening, never accepting: the wedge itself.
    with socket.socket() as wedged:
        wedged.bind(("127.0.0.1", 0))
        wedged.listen(1)
        assert _answers_h2(wedged.getsockname()[1], timeout=1.0) is False

    # Accepting, then hanging up, or answering with something that is not a
    # SETTINGS frame.
    for reply in (b"", b"\x00" * 9):
        with socket.socket() as imposter:
            imposter.bind(("127.0.0.1", 0))
            imposter.listen(1)

            def _answer(listener=imposter, reply=reply):
                conn = listener.accept()[0]
                conn.sendall(reply)
                conn.close()

            acceptor = threading.Thread(target=_answer)
            acceptor.start()
            assert _answers_h2(imposter.getsockname()[1], timeout=1.0) is False
            acceptor.join(timeout=5)

    # Nothing listening: Health/Check cannot be SERVING.
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        gone = closed.getsockname()[1]
    assert _serves_health(gone) is False
