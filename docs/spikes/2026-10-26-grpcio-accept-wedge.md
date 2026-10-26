# grpcio listener wedge after a connection that closes before accept

Date 2026-10-26. Question: does a TCP connection that closes before the server
accepts it (what a Fly `tcp_checks` probe does) wedge the gRPC listener on
Linux, the way it did on the macOS dev service?

## Answer

**Not on Linux.** grpcio 1.83.0 and 1.85.0 (the newest release, out the same
day) both kept serving after 300 connect-and-close knocks each, in two modes. No
version bump, nothing pinned. The wedge is real on macOS, and it is **not fixed
in 1.85.0**, so there is no newer version to move to either.

## Method

A throwaway harness, deliberately not in the repo. It starts the real service
(`python -m placement.main`) on a free port, proves `Health/Check` is SERVING,
sends 100 knocks (TCP connect, then close, nothing written), and probes again
with two independent checks: `Health/Check` through grpcio on a fresh channel,
and a raw HTTP/2 preface that only an accepting listener answers with a SETTINGS
frame. A repeat is **wedged** when either probe fails. Two modes:

- `tight`: knocks back to back against a running server. Whether a knock dies
  before accept is a race.
- `stopped`: the server is SIGSTOPped, the knocks queue and die in the kernel's
  accept backlog, then it is SIGCONTed. Every knock is closed-before-accepted.

The knocks go over loopback from inside the same container, so no userland port
proxy (Docker's, colima's) sits between knock and listener.

Linux side, the repo's own image, unmodified (no Docker daemon was running, so a
colima VM was started for this):

```bash
colima start grpcwedge --cpu 2 --memory 3 --disk 20 --vm-type vz --runtime docker
export DOCKER_HOST=unix://$HOME/.colima/grpcwedge/docker.sock
docker build -t placement-wedge:base .        # Dockerfile as is: resolves grpcio 1.85.0
# 1.83.0 = the same image with `pip install grpcio==1.83.0` on top (as root)
docker run --rm -i --init placement-wedge:<tag> python - --n 100 --repeats 3 --kinds fin < knock_harness.py
```

Environment: Linux 6.8.0-117 (colima VM), Docker 29.5.2, `python:3.14.6-slim-bookworm`
(glibc 2.36), grpcio-health-checking 1.81.1, protobuf 6.33.6, ortools 9.15.6755.
macOS: Darwin 25.6.0 x86_64, CPython 3.14.6, native venv, same dependency ranges.

## Result

| OS | grpcio | knock | mode | repeats x knocks | wedged |
|---|---|---|---|---|---|
| Linux (repo image) | 1.83.0 | connect+close | tight | 3 x 100 | 0 / 3 |
| Linux (repo image) | 1.83.0 | connect+close | stopped | 3 x 100 | 0 / 3 |
| Linux (repo image) | 1.85.0 | connect+close | tight | 3 x 100 | 0 / 3 |
| Linux (repo image) | 1.85.0 | connect+close | stopped | 3 x 100 | 0 / 3 |
| macOS | 1.83.0 | RST (`SO_LINGER` 0) | tight | 1 x 50 | **1 / 1** |
| macOS | 1.83.0 | RST | stopped | 1 x 50 | 0 / 1 |
| macOS | 1.85.0 | RST | tight, in the pytest below | 4 x 100 | **4 / 4** |
| macOS | 1.83.0 | connect+close | tight, stopped | 1 x 50 each | 0 / 2 |
| macOS | 1.85.0 | connect+close | tight, in the pytest below | 3 x 100 | 0 / 3 |

No `Invalid address` line was logged in any Linux run. The macOS harness wedge
logged `Invalid address: Empty address`, then `Health/Check` timed out on all
three attempts and the raw HTTP/2 probe got no SETTINGS frame, with the process
still alive.

Also seen before the matrix was cut, same harness: Linux 1.81.1 and 1.82.0, both
knock kinds (connect+close and RST), both modes, 3 x 100 each: 0 / 12 wedged
apiece. Linux 1.83.0 RST, 1 x 50 per mode: clean.

Limits: the macOS rows are small samples. The original report was a plain
connect+close (`nc -z`); that did **not** reproduce here, only the RST variant
did. All Linux knocks are loopback, not a probe from Fly's host to a Firecracker
VM, so a real machine's kernel is untested.

## Not what the image runs

`uv.lock` pins grpcio 1.83.0, but nothing reads it: `ci.yml` and the Dockerfile
both do `pip install` against `pyproject.toml`'s `grpcio>=1.81.1,<2`. The image
therefore carries whatever grpcio is newest on the day it is built (1.85.0 in the
build above). A pin in `uv.lock` would be inert; only `pyproject.toml` changes
what ships. This PR does not change it.

## What the PR adds

- `tests/test_accept_wedge.py`: 100 closed-before-accept knocks (connect+close,
  and RST) against a real in-process server on port 0, then both probes. It
  carries negative controls so the probes can say "down". The RST case is
  `xfail` on macOS only (it fails there 4 / 4; it turns XPASS the day grpcio
  fixes it). On Linux it is the guard against a future floating-grpcio release
  that wedges.
- `PLACEMENT_BIND_HOST` (README): default `[::]`, unchanged.

## Recommendation (Fly health check; `fly.toml` untouched)

Keep `tcp_checks` for now. On the evidence the Linux image does not wedge on the
connect-and-close a TCP check sends, and a gRPC-speaking check is untested under
load (`fly.toml` [4]). Know what it cannot see, though: in the macOS wedge the
kernel kept completing TCP handshakes, so a `tcp_checks` probe would have stayed
green over a dead listener. If a Linux release ever wedges, the first signal is
this PR's test going red in CI, before the image ships, not the Fly check.

If you want runtime detection anyway, it has to be a check that speaks HTTP/2 or
gRPC, not a TCP one; I did not re-read Fly's check types for this spike, so
confirm what Fly offers before planning it. Cheap interim: after the next stg
deploy, let a few check cycles pass and grep the stg logs for `Invalid address`.
