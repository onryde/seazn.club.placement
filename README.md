# Placement scheduling service

gRPC service wrapping Google OR-Tools CP-SAT for the BUILD/POLISH scheduler.
`packages/engine/src/scheduling/build.ts` asks an injected solver, which in
production is `placementSolver` from `@seazn/scheduler` (`packages/scheduler/src/solver.ts`);
that adapter calls the service through `placement-client.ts`, the TS
anti-corruption layer in the same package — see
[`docs/superpowers/specs/2026-08-07-placement-scheduler-design.md`](https://github.com/onryde/seazn.club/blob/placement-split-base/docs/superpowers/specs/2026-08-07-placement-scheduler-design.md)
for the design and [`DEPLOY.md`](./DEPLOY.md) for the production runbook.
[`bench/README.md`](./bench/README.md) covers the offline sweep this
service's own board generator (`bench/placement_bench_boards.py`) is shared with.

## Local dev (for integration tests)

Start the service:

```bash
# from the repo root
# First time only, or if venv/ does not exist yet. Plain `python3 -m venv`
# will NOT work here: this repo's system Python is 3.9.6 and this package
# declares `requires-python = ">=3.11"` (pyproject.toml) — use `uv` to get a
# venv on a Python new enough, downloading one if it has to:
uv venv --python 3.14 venv   # CI and the image run 3.14
uv pip install --python venv/bin/python -e ".[dev]"   # uv venvs ship no pip
PLACEMENT_SERVICE_SECRET=dev-secret PLACEMENT_PORT=50051 venv/bin/python3 -m placement.main
```

It logs `placement service listening on :50051 (max_workers=4)` and then blocks
in the foreground — leave that terminal open, or background it yourself
(`... &`, noting the PID) if you need the terminal back.

In another terminal, **in a seazn.club checkout** (not this repo), run the TS
integration suite against it. The suite lives in `packages/scheduler` (the gRPC client moved there out of the engine, engine
split 1a), which holds TWO live test files, and there is no `test:integration`
script in `packages/scheduler/package.json` — this is the direct `vitest`
invocation, naming both:

```bash
# in a seazn.club checkout
cd packages/scheduler
PLACEMENT_SERVICE_HOST=localhost:50051 PLACEMENT_SERVICE_SECRET=dev-secret \
  npx vitest run src/__tests__/placement-integration.test.ts \
                 src/__tests__/repair-decompose-cpsat-integration.test.ts
```

Without `PLACEMENT_SERVICE_HOST` set, those same suites SKIP instead of dialing
the production default (`placement.flycast:50051`, unreachable from a dev
machine) — that is the correct, expected behaviour, not a failure:

```bash
# in a seazn.club checkout
cd packages/scheduler
npx vitest run src/__tests__/placement-integration.test.ts \
               src/__tests__/repair-decompose-cpsat-integration.test.ts
```

### Bind address (`PLACEMENT_BIND_HOST`)

The service listens on `<PLACEMENT_BIND_HOST>:<PLACEMENT_PORT>`. Unset, the host
is `[::]`, the dual-stack wildcard the service has always bound and the one Fly's
private IPv6 network reaches it through; leave it unset in `fly.toml`.

On a laptop, `PLACEMENT_BIND_HOST=127.0.0.1` keeps the service off the LAN. It is
then an IPv4-only listener, so a client that dials `::1` is refused.

- A bare IPv6 literal is bracketed for you: `::1` and `[::1]` both bind `[::1]`.
- Surrounding whitespace is ignored.
- Set but empty or blank is refused at startup, naming the variable. It is not
  read as unset.

The listening log line carries the `bind_host` it bound.

### Stop it when you're done

This is a plain foreground (or backgrounded) process on your machine, not a
container — nothing stops it for you, and a copy left listening on `:50051`
is stale the moment you next edit `src/placement/**`: a later run (yours, or
someone else's) that dials `localhost:50051` reaches that stale process and
reports a false green against code that has since changed, which is worse
than a connection refused. `Ctrl-C` the foreground terminal, or if you
backgrounded it:

```bash
kill %1                     # if it's a job in the current shell, or:
lsof -t -i :50051 | xargs kill

# confirm the port is free before trusting your next run:
lsof -t -i :50051            # prints nothing once it's actually down
```

If `lsof -t -i :50051` prints a PID *before* you start your own server, that
port is already occupied by something else's process — do not assume it is
safe to reuse; find out whose it is first.

## Installing buf

`tests/test_buf_breaking.py` runs the real `buf` and FAILS (never skips) when it
is missing or is not the pinned version. `BUF_VERSION` in
`.github/workflows/ci.yml` is the only place that version is written. Download
that release's binary into a directory outside the repo, verify it against the
release's `sha256.txt`, and put the directory on `PATH` for the test run only:

```bash
# from the repo root; nothing on your machine's PATH changes
BUF_VERSION=$(sed -n 's/^  BUF_VERSION: "\(.*\)"$/\1/p' .github/workflows/ci.yml)
BUF_DIR=$(mktemp -d)
ASSET="buf-$(uname -s)-$(uname -m)"   # buf-Darwin-arm64, buf-Darwin-x86_64, buf-Linux-x86_64, ...
RELEASE="https://github.com/bufbuild/buf/releases/download/v$BUF_VERSION"
curl -fsSL -o "$BUF_DIR/$ASSET" "$RELEASE/$ASSET"
curl -fsSL -o "$BUF_DIR/sha256.txt" "$RELEASE/sha256.txt"
(cd "$BUF_DIR" && grep " $ASSET\$" sha256.txt | shasum -a 256 -c -)
mv "$BUF_DIR/$ASSET" "$BUF_DIR/buf" && chmod +x "$BUF_DIR/buf"
"$BUF_DIR/buf" --version   # must print $BUF_VERSION
```

Then run pytest from the same shell (or after `export BUF_DIR=...`) with
`PATH="${BUF_DIR:+$BUF_DIR:}$PATH"`, as shown in `AGENTS.md`. Keep the
`${BUF_DIR:+...}` form: a bare `$BUF_DIR:` prefix, with `BUF_DIR` unset, adds an
empty entry (the current directory) to `PATH`.

To bump the version, edit `BUF_VERSION` in `ci.yml` (newest buf release that is
at least two weeks old: `gh release list -R bufbuild/buf --limit 5`) and reinstall.
