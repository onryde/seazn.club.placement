# seazn.club.placement

The CP-SAT placement gRPC service (Python 3.14, OR-Tools). It is consumed only
by `onryde/seazn.club`'s `packages/scheduler`, over Fly private networking
(`placement.flycast:50051`). It is an internal service: never reachable from the
public internet. The repo and its one image are public, because nothing in
either is secret; "internal" governs the network, not visibility.

Layout, all at the repo root: `Dockerfile`, `fly.toml`, `pyproject.toml`,
`src/placement/**`, `tests/**`, `bench/**`, `proto/scheduler.proto`.

## History

Pre-cutover history lives in onryde/seazn.club:
`git log placement-split-base -- services/placement proto` (tag at
`093ad1d19ef068950e72571309320e5861dd3255`). This repo starts with one import
commit and is never rewritten.

## Paths in comments

A path that starts with `packages/` or `apps/` (and the occasional `docs/`) in a
comment or a doc here names a **seazn.club** file, not a file in this repo. Do not
"fix" those references.

## The proto rule

`proto/scheduler.proto` is additive only, enforced by `buf breaking` in this
repo's CI, against `main`. Never renumber, repurpose or remove a field.

Even a comment edit is a contract change downstream: ts-proto copies proto
comments into seazn.club's generated TS stubs, so any byte change to the proto
drifts them.

The order for a contract change is:

1. placement PR (this repo), merged;
2. deploy;
3. re-vendor in seazn.club with
   `pnpm --filter @seazn/scheduler vendor:proto <this checkout>`, then `gen:proto`.

## Deploys

- stg (`placement-stg`) on every push to `main`.
- prod (`placement`) on a `v*.*.*` tag.
- The only artifact is the public image `ghcr.io/onryde/seazn-placement:<sha>`,
  published from `main`. Tags are immutable commit shas: no `latest`, no `main`.
- The only secret is `FLY_TOKEN`.
- No public IP, ever: the apps are reached over private IPv6 only.

## Tests

```bash
uv venv --python 3.14 venv && uv pip install --python venv/bin/python -e ".[dev]"
PATH="$BUF_DIR:$PATH" venv/bin/python -m pytest tests/ -q \
  --deselect tests/test_model.py::test_production_board_meets_the_stated_acceptance_criterion
```

- The deselected test asserts `OPTIMAL` on the production board inside an 8s wall,
  so it is load-sensitive on purpose. CI re-runs it alone as an advisory
  (non-blocking) step.
- The generated stubs under `src/placement/generated/` are tracked.
  `tests/test_proto_compiles.py` byte-compares a fresh compile against them.
- Local tests need the pinned `buf` on `PATH` (`BUF_DIR` above): the `buf breaking`
  tests are added with the CI workflow.
- There is no container runtime on the developer machine, so the image is built
  and run only in CI.
