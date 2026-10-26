# Deploying the Placement service — owner runbook

**Setup is by hand; routine deploys run in CI.** Sections 1–4 (the apps, the
private IPv6, the secrets, and the two things CI needs from you) are one-time owner
steps. After that,
`.github/workflows/ci.yml` tests and publishes the image on every push to `main`,
and two more workflows deploy that published image without rebuilding it:
`.github/workflows/stg.yml` (staging, once `ci` has passed) and
`.github/workflows/prod.yml` (production, on a `v*.*.*` tag) — see §5. No agent in
this programme runs `fly deploy`.

Run every command from the repository root. The repository root is the build
context: the `Dockerfile` copies `pyproject.toml` and `src/` relative to
itself.

Two names are load-bearing and must not be changed:

| | |
|---|---|
| Service app | `placement` — Fly's private DNS derives from the app name, and `packages/scheduler/src/placement-client.ts`'s `DEFAULT_HOST` is the literal `placement.flycast:50051` |
| Web app | `seazn-club-prod` (seazn.club's root `fly.toml`) |

Rename the service app and every solve dials a host that does not exist: the
gRPC call fails, `build.ts` falls back to greedy, and **nothing surfaces
why**. That is the failure this runbook most wants to avoid.

---

## 1. Create the app

```bash
fly apps create placement        # add --org <org> if you belong to more than one
```

## 2. Allocate the PRIVATE IPv6 that makes `.flycast` resolve

**Do not skip this, and do not confuse it with a public IP.** `fly.toml` sets
`auto_stop_machines = "suspend"`, so the machine spends most of its life
suspended. Autostart is a **Fly Proxy** feature: `<app>.internal` resolves
straight to machine IPs and bypasses the proxy, so a suspended machine on an
`.internal` host is simply unreachable. `.flycast` routes through the proxy,
which is what wakes the machine on the first request.

Fly's own docs, on Flycast: *"unlike private networking using `.internal`
addresses you don't need to keep Machines running for the app to be
reachable."*

```bash
fly ips allocate-v6 --private --app placement
```

Skip it and the symptom is not an error — it is **greedy boards, silently**,
for every organiser, presenting as "placement got worse".

## 3. Confirm it has NO *public* IP

The private v6 above is expected and required. What must not exist is a
public address. The design's security model is that 6PN is already
WireGuard-encrypted and not internet-reachable, which is *why* a shared
secret substitutes for mTLS. A public IP invalidates that assumption.

```bash
fly ips list --app placement     # expect the private v6 ONLY — no public v4/v6
```

If Fly allocated a public one anyway, release it before deploying:

```bash
fly ips release <address> --app placement
```

## 4. Set the shared secret on BOTH apps

`PLACEMENT_SERVICE_SECRET` is new. **Never reuse `CRON_SECRET`** — a shared value
would let either service authenticate as the other.

Generate it once and set the *same* value on both apps. Do not paste it into
a chat, a commit, or a ticket.

```bash
SECRET=$(openssl rand -base64 32)
fly secrets set PLACEMENT_SERVICE_SECRET="$SECRET" --app placement
fly secrets set PLACEMENT_SERVICE_SECRET="$SECRET" --app seazn-club-prod
unset SECRET
```

Both sides are required. `build.ts:1442` reads
`process.env.PLACEMENT_SERVICE_SECRET ?? ""`, and the service rejects an empty
secret as `UNAUTHENTICATED` — which `placement-client.ts:507` maps to a failure
and `build.ts` turns into a greedy board. A missing secret on the web app
therefore looks exactly like "placement is slow", not like an auth error.

### CI needs two more things from you, once

CI can do neither of these for itself.

1. **Set the `FLY_TOKEN` repository secret** (GitHub: Settings, Secrets and variables,
   Actions) to a Fly token that can deploy both `placement` and `placement-stg`. It is
   the only GitHub secret the deploy workflows use (§5).
2. **Make the GHCR package `seazn-placement` public, once, after its first publish.**
   `ci` creates the package the first time it publishes an image on `main`, and GitHub
   creates it private. Fly pulls the image anonymously, so a private package cannot be
   deployed (GitHub: Packages, `seazn-placement`, Package settings, Change visibility,
   Public). It stays public after that.

**The first `stg` run is expected to go RED**, at *Require the image ci published for
this commit*, until step 2 is done: that check inspects the image anonymously, exactly
as Fly will, so a private package fails it. That red is the check working, not a
defect. Once the package is public, re-run that `stg` run (Actions, `stg`, the red run,
*Re-run failed jobs*); nothing in the repository needs to change.

### If the publish step is denied: `permission_denied: write_package`

The `publish` job pushes with the repository's own `GITHUB_TOKEN`, and GHCR lets that
token write only a package this repository has been granted. The image's
`org.opencontainers.image.source` label links a package to a repository when the
package is *created*; it grants nothing on one that already exists. So a denial at
`docker push` means the package exists and this repository has no Actions access to it
(for example it was first created from another repository). Look at it:

```bash
gh api orgs/onryde/packages/container/seazn-placement   # needs the read:packages scope
```

`repository` names the source repository the package is linked to (the UI's *Repository
source*) and `visibility` should read `public`. Today `repository` reads
`onryde/seazn.club.placement`: the package was deleted and recreated on 2026-10-25, by this
repository's own `publish` job, so it carries this repository as its source. If `repository`
ever names another repository, the package was created from there and Actions access to it
must be granted by hand. Do that in the package's settings (GitHub: Packages,
`seazn-placement`, Package settings):

1. Untick *Inherit access from source repository*.
2. Under *Manage Actions access*, add `seazn.club.placement` with the role *Write*.
3. Check that the visibility is *Public* (step 2 above).

Then re-run the failed `ci` run; nothing in the repository needs to change.

## 4b. Staging gets its OWN service, `placement-stg` (owner decision 2026-08-11)

**This supersedes the 2026-08-10 decision to share.** Sharing was correct while
the contract was frozen; it stops being correct the moment a contract revision
is due, and #21 is due.

Why it had to change, in one sentence: with one service behind both web apps,
**deploying a proto revision to staging IS deploying it to production**, so the
rehearsal and the performance are the same event. The two accepted consequences
recorded under the old decision — staging stealing machines from the pool of
ten, and no way to validate a contract first — are both closed by the split.

### There is no `fly.stg.toml`, deliberately

Staging deploys the **same** `fly.toml` with the app name overridden:

```bash
fly deploy . --config fly.toml --app placement-stg --no-public-ips
```

`--no-public-ips` on every manual deploy, as in CI: a first `fly deploy` on an app that
holds no address allocates public ones (and the placement service must never have one).
Step 2 gives the app its private v6 first, so allocation would be skipped anyway, but
the flag makes that independent of the order you did things in.

A second TOML would be ~200 lines duplicated, and every line that drifted would
make staging a worse and worse rehearsal for production. The point of this app
is to answer "will the real service behave like this?", and it can only answer
that while its machine size, concurrency limits, health checks and solver knobs
are the production ones. Everything that legitimately differs — the secret, the
machine count, the web app's host override — is a `fly secrets` / `fly scale`
call below, not file content.

If a future change genuinely needs staging-only configuration, add it as an
`[env]` override on the `fly secrets set`/`fly scale` line rather than forking
this file.

### Standing it up (once)

```bash
fly apps create placement-stg
fly ips allocate-v6 --private --app placement-stg
fly ips list --app placement-stg        # expect the private v6 ONLY
```

Steps 2 and 3's reasoning applies unchanged and is not optional: without the
private v6 the `.flycast` host does not resolve, and the symptom is not an
error but **greedy boards, silently**.

### Its own secret — not production's

```bash
STG_SECRET=$(openssl rand -base64 32)
fly secrets set PLACEMENT_SERVICE_SECRET="$STG_SECRET" --app placement-stg
fly secrets set PLACEMENT_SERVICE_SECRET="$STG_SECRET" --app seazn-club-stg
unset STG_SECRET
```

A **different** value from production's, which is the second thing the split
buys. Under the old shared arrangement one secret's blast radius covered both
environments; now a staging leak costs staging.

### Point the staging web app at it

```bash
fly secrets set PLACEMENT_SERVICE_HOST="placement-stg.flycast:50051" --app seazn-club-stg
```

This is the whole code-side story. `placement-client.ts` reads
`PLACEMENT_SERVICE_HOST` and falls back to `DEFAULT_HOST =
"placement.flycast:50051"` only when it is unset, so the override already
exists and nothing ships to make this work.

**Verify it took, and do not skip this.** If the variable is missing or
misspelled, `seazn-club-stg` silently keeps calling **production** — which is
exactly the state the split exists to end, and it looks identical to success
from the outside. Check the value is set, then confirm a staging solve appears
in `placement-stg`'s log and not in `placement`'s:

```bash
fly secrets list --app seazn-club-stg | grep PLACEMENT_SERVICE_HOST
fly logs --app placement-stg    # run a staging Auto-schedule; expect one `solve request=` line
fly logs --app placement        # the same run must add NOTHING here
```

That per-solve log line is the one added after four production deploys measured
nothing; this is the second job it does.

### Debug-level logging, staging only

`fly.toml`'s `[env]` block leaves `PLACEMENT_LOG_LEVEL` commented out
deliberately: that file is shared between `placement` and `placement-stg`
(§[1] above), so uncommenting it there would set DEBUG on production too. A
`fly secrets set` on `placement-stg` alone is the same mechanism already used
for its own secret and its own web-app host above — per-app, without forking
the TOML:

```bash
fly secrets set PLACEMENT_LOG_LEVEL="DEBUG" --app placement-stg
fly secrets list --app placement-stg | grep PLACEMENT_LOG_LEVEL
fly secrets list --app placement     | grep PLACEMENT_LOG_LEVEL   # expect nothing
```

`resolve_log_level` treats an unknown name as INFO rather than refusing to
boot (`main.py`'s own docstring: it changes only what the service PRINTS), so
there is no risk of a typo here taking staging down — only of it silently
staying at INFO, which the `grep` above catches.

### Scale

```bash
fly scale count 2 --app placement-stg
```

Two, not ten. `hard_limit = 2` means two solves per machine (raised from 1 on
2026-08-11, in lockstep with `PLACEMENT_MAX_WORKERS` — see `fly.toml` §[3]),
and staging does not have ten concurrent organisers — but one machine is not
enough either, since a third tester would get `SOLVER_BUSY` and a greedy
board, and mistake it for a
regression in the thing they were testing.

### Deploying a contract change — the reason all of the above exists

Staging and production deploy from different triggers (step 5), so they can run
different revisions. `stg.yml` deploys the tip of `main` as soon as `ci` has passed
and published its image; `prod.yml` moves only when someone pushes a `v*.*.*` tag.
The gap between "merged" and "tagged" is the rehearsal window, and it is why the
two must not deploy from the same trigger: they would always be the same revision
and there would be no window to validate anything in.

So a contract revision goes:

1. Merge the PR. `ci` publishes `ghcr.io/onryde/seazn-placement:<commit sha>` and
   `stg.yml` deploys that image to `placement-stg`.
2. Exercise it on `stg.seazn.club` — a real Auto-schedule on a real board, and
   confirm the per-solve log line comes from `placement-stg`.
3. Only then push the release tag, letting `prod.yml` deploy the same image to
   production.

Between 1 and 3 the two services run **different contracts**, which is the
point. Both sides must therefore still tolerate the other's messages for that
window — proto3 makes added fields safe in both directions, but a **renumbered
or repurposed field number is not**, and no runbook step can save you from one.

## 5. Deploy

### Before the FIRST production tag: the stg Auto-schedule check (O7) is a hard prerequisite

Owner ruling. Do not push the first `v*.*.*` tag until every box below is ticked. Nothing in
`prod.yml` enforces it (a tag deploys whatever image `ci` published for that commit), so this
list is the gate. It is the same rehearsal the contract-change sequence in step 4b describes,
made mandatory once: production has never run an image from this repository, and the first tag is
the first time the whole chain (web app, Flycast, this service) is exercised on its own deploy
path.

- [ ] The commit you mean to tag is the tip of `main`, `ci` is green for it, and the `stg` run
  for it is green: `placement-stg` runs that commit's published image.
- [ ] `seazn-club-stg` really calls `placement-stg`, not production: `PLACEMENT_SERVICE_HOST` is
  `placement-stg.flycast:50051` (step 4b).
- [ ] Run one real Auto-schedule on `stg.seazn.club`, on a **multi-court** board. The result strip
  reports the `optimized` engine, not the greedy fallback (step 7).
- [ ] Attribute that solve by log: `placement-stg` shows exactly one `solve request=` line for it
  and `placement` shows nothing for it.
- [ ] Record the commit sha, the date and who ran the check in the tag's release notes.

```bash
fly secrets list --app seazn-club-stg | grep PLACEMENT_SERVICE_HOST
fly logs --app placement-stg    # expect one `solve request=` line for the run
fly logs --app placement        # the same run must add NOTHING here
```

Any box that cannot be ticked is a stop: fix it on `stg` first. Later tags follow the ordinary
rehearsal window (step 4b, "Deploying a contract change").

**Deploys run in CI, from the published image. Nothing rebuilds it.** On every
push to `main`, `.github/workflows/ci.yml` builds the image once, smoke-tests it in
its `docker` job, and — after every gate — its `publish` job pushes that same image
as `ghcr.io/onryde/seazn-placement:<commit sha>` (public; an immutable sha tag,
never `latest`). The two deploy workflows ship exactly that image, so what runs is
the artifact the `docker` job proved starts and accepts a TCP connection, not a
second build.

| Workflow | Job | Runs | Deploys |
|---|---|---|---|
| `.github/workflows/stg.yml` | `deploy-stg` | when `ci` succeeds for a push to `main`, while that commit is still the tip of `main` | that commit's image to `placement-stg` |
| `.github/workflows/prod.yml` | `deploy` | on a `v*.*.*` tag | the tagged commit's image to `placement` |

- There is no paths filter: the whole repository is the service.
- Both fail before deploying if the commit has no published image (or its package is
  not public, because Fly pulls it anonymously). For `prod.yml` that is the gate: a
  tag does not run `ci`, so a tag on a commit that never passed on `main`, or one whose
  `ci` is still running, stops there.
- Both need the `FLY_TOKEN` GitHub secret, pass `--no-public-ips` to `flyctl deploy`,
  and then fail the job if `flyctl ips list` shows any address that is not a private
  v6 (§3). No workflow allocates an address.
- `stg.yml` skips a commit that `main` has already moved past, so staging follows the
  tip of `main`. The newer commit's own `stg` run deploys it, with one gap: the `stg`
  concurrency group keeps one running run and one *pending* run, and a newer pending
  run cancels the older pending one even though `cancel-in-progress` is `false`. If the
  cancelled run was the one for the tip of `main`, staging stays behind until you
  re-run the cancelled stg run (Actions, `stg`, the cancelled run, *Re-run all jobs*)
  or the next merge lands.

The standing rule that **no agent runs `fly deploy`** is unchanged; none
does. CI is a different actor, gated on checks this service cannot run
anywhere else — the developer machine has no container runtime, so the
`docker` job is the only place this image is ever built.

The manual path below remains valid for a first deploy or a rollback. It builds on
Fly's builder from your checkout:

```bash
fly deploy . --app placement --no-public-ips
```

To roll back to an image CI already tested, deploy that sha instead (the same flags
the workflow uses):

```bash
fly deploy --config fly.toml --app placement --no-public-ips \
  --image ghcr.io/onryde/seazn-placement:<commit sha>
```

`fly.toml` pins `auto_stop_machines = "suspend"` with
`min_machines_running = 0`, so an idle machine scales to zero between solves
and **resumes from a memory snapshot** on the first request through the
proxy — much faster than the 1-3s cold start a full `stop` would cost, which
matters against an 8-10s wall budget.

```bash
fly scale count 10 --app placement
```

This call **is** needed now (Task 12 — an earlier draft of this step said
otherwise). `fly.toml`'s `[services.concurrency]` sets `hard_limit = 2`, so
Fly Proxy treats a machine as full at TWO connections: the third concurrent
organiser's solve is routed to a different machine, autostarting it if it is
suspended, rather than queuing behind the first two. The service admits the
same number (`PLACEMENT_MAX_WORKERS = 2`); if that pair ever disagrees, the
proxy hands a machine work the servicer refuses and the caller silently gets
a greedy board. `fly scale count 10` is
what gives the proxy nine more machines to fan out to — without it there is
still only one machine to route to no matter how the proxy load-balances.
Ten matches the owner's stated concurrent-organiser target; it is not a
worst-case pad.

Nine of those ten sit `suspended` almost all the time, and the expectation is
that this is cheap — but **check the first invoice rather than trusting this
paragraph.** Fly's pricing page documents rootfs storage billing at $0.15/GB
per 30 days for **`stopped`** machines. It does **not** document the
suspended state, which also holds a memory snapshot; whether that is billed,
and at what rate, is not stated anywhere we could find. `fly.toml` carries
the same caveat beside `auto_stop_machines`.

The expected cost either way is cents against machine-hours that are not, so
this remains a latency choice rather than a cost one — and `"stop"` is a
one-word change costing only a 1-3s cold start if an invoice disagrees. What
is NOT in doubt is that ten idle machines do not cost ten machine-hours;
autostop is what makes `fly scale count 10` affordable at all.

This is the setting that makes step 2 non-optional. Suspended plus
`.internal` equals unreachable.

## 6. Verify before touching the web app

```bash
fly status --app placement       # 10 machines (after step 5's fly scale count 10), tcp checks passing
fly logs --app placement         # expect a bind on 50051, no tracebacks
```

Expect most of the ten machines to report `suspended` shortly after deploy
once traffic stops — with `hard_limit = 2`, only as many machines as there
are PAIRS of concurrent solves ever need to be running at once. That is the
configuration working, not a fault.

The health check is a TCP accept, not a gRPC `Health/Check`, on purpose —
a gRPC probe once shared the solve thread pool and would kill the machine
mid-solve. A passing check proves the port accepts, not that a solve works.
Step 7 is what proves that.

## 7. Prove the cutover is actually live

The one assertion that matters: run a real Auto-schedule on a **multi-court**
board and confirm the result strip reports the `optimized` engine, not the greedy
fallback. A board that simply "looks fine" proves nothing — greedy also
returns a plausible board, and the entire cutover once shipped inert while
every happy-path test passed (`_INDEX.md`, "The cutover nearly shipped
INERT").

If it says greedy, work through, in order: **private v6 allocated so
`.flycast` resolves (step 2)** — the most likely cause now that the machine
suspends — then secret set on both apps (step 4), app named exactly `placement`
(step 1), machine actually deployed (step 5), and — new as of Task 12 —
**every solve landing on one machine because the caller reused a channel**:
`placement-client.ts` opens a fresh gRPC client per solve and closes it when that
solve settles specifically so Fly Proxy sees ten separate connections to fan
out across ten machines; a regression back to a cached, shared client would
pin every solve to whichever one machine holds that connection, which reads
as "placement lost on merit under load" rather than as a wiring bug.

## 8. Owed immediately after this deploy

**The server halves the caller's wall budget, and it will do so in
production on day one.** `PLACEMENT_WALL_SECONDS_MAX` defaults to **10**
(`src/placement/config.py:39`) and `main.py:142` clamps every request to
`min(parsed.wall_seconds, wall_seconds_max)`. The web app asks for up to
**20 s** (`AUTO_SOLVER_WALL_MS`, `apps/web/src/server/usecases/schedule.ts`),
so half of it is discarded on every board. Measured 2026-08-10: a generated
21-fixture round-robin stalls at `tiers_completed: 1/4` because of this, which
is why Task 11's E2E uses a smaller hand-built board.

This is **not** a placement failure — `placed` stays full. It truncates how
much of the T0→T3 chain is PROVEN inside the wall, which is the thing this
service exists to deliver over greedy. Left unset deliberately rather than
raised to 20 here: see the commented line in `fly.toml`'s `[env]`, and decide
it together with the re-measurement below rather than changing two variables
at once.

**Re-measure the search worker count.** It defaults to 8
(`model.SolverKnobs`, the one place that number exists), measured on a
6-physical-core dev box. This shape is `shared-cpu-2x` — 2 vCPU, so 4x
oversubscribed — and a dev-box A/B already found 4 beating both 8 and 12 under
load. It cannot be measured anywhere but the real machine shape.

Since 2026-08-11 this is an `[env]` change, not a code change:
`PLACEMENT_NUM_SEARCH_WORKERS` in `fly.toml` (already present, set to 8), plus
`PLACEMENT_SYMMETRY_LEVEL` / `PLACEMENT_PROBING_LEVEL` for the two presolve
knobs. Every solve echoes what it actually used —
`workers=N symmetry=N probing=N` on the per-solve log line — so confirm the
number moved before believing the run measured anything. That confirmation is
the whole point: four production deploys were spent on a knob that was set and
had no effect.

Two prior task reports already recommended this and nothing changed, because
they recorded it in the git-ignored SDD workspace. It is in `_INDEX.md` too,
which is committed.

Watch `tiers_completed`, not `placed`. Across 24 paired runs on the
production board, `placed` was 37 of 37 every time regardless of load — what
degrades under contention is how much of the T0-T3 proof chain completes
inside the wall. **A run whose `placed` is not 37 is a real bug, not a sizing
tradeoff.**
