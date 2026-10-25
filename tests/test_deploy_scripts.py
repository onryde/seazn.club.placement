"""Behaviour of the three scripts the deploy workflows (stg.yml, prod.yml) run.

Each script is run for real. The only thing faked is the outside world: a
``docker`` and a ``flyctl`` on PATH that log every call and answer from the test's
own flags (the pattern tests/test_publish_image.py uses), and a local bare git
repository standing in for ``origin``.

Every sweep reports how many cases it ran and zero is a FAILURE (seazn.club's
TEST-STRATEGY.md, see AGENTS.md, "Paths in comments": anti-vacuity). The expected
values come from the rules the scripts exist for (Fly's address types, the image
naming rule, "main moved on"), never from the scripts.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
REQUIRE_IMAGE = SCRIPTS / "require-image.sh"
REQUIRE_PRIVATE_IPS = SCRIPTS / "require-private-ips.py"
REQUIRE_MAIN_TIP = SCRIPTS / "require-main-tip.sh"

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
IMAGE = f"ghcr.io/onryde/seazn-placement:{SHA}"
DIGEST = "sha256:" + "ab" * 32

# Fly's own address types (flyctl's `ips list --json`, key "Type"). A Flycast
# address is private_v6 and lives under fdaa::/16.
PRIVATE = {"Address": "fdaa:0:1234:0:1::2", "Type": "private_v6", "Region": "global"}
PUBLIC_V4 = {"Address": "66.241.124.10", "Type": "v4", "Region": "global"}
SHARED_V4 = {"Address": "137.66.1.2", "Type": "shared_v4", "Region": "global"}
PUBLIC_V6 = {"Address": "2a09:8280:1::1:2345", "Type": "v6", "Region": "global"}
EGRESS_V6 = {"Address": "2a09:8280:1::9", "Type": "egress_v6", "Region": "iad"}
EGRESS_V4 = {"Address": "198.51.100.7", "Type": "egress_v4", "Region": "iad"}
# Every value of flyctl's IPAddress "Type" (v0.4.115, read from its source on 2026-10-25).
# The sweep below must cover each of them, so a type Fly adds to this list is a type
# the table has to answer for.
FLY_ADDRESS_TYPES = {"v4", "v6", "shared_v4", "private_v6", "egress_v4", "egress_v6"}

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "${FAKE_INSPECT:-present}" in
  present)
    printf 'Name:      %s\\nMediaType: application/vnd.oci.image.index.v1+json\\nDigest:    %s\\n' "$4" "$FAKE_DIGEST"
    ;;
  nodigest)
    printf 'Name:      %s\\n' "$4"
    ;;
  absent)
    echo "ERROR: $4: not found" >&2
    exit 1
    ;;
esac
"""

FAKE_FLYCTL = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
echo "update available: run flyctl version upgrade" >&2
cat "$FAKE_STDOUT"
if [ -n "${FAKE_EXIT:-}" ]; then
  echo "Error: could not find app" >&2
  exit "$FAKE_EXIT"
fi
"""


def _install(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path):
    """A PATH with a logging docker and flyctl in front, and the log they write."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _install(fakes, "docker", FAKE_DOCKER)
    _install(fakes, "flyctl", FAKE_FLYCTL)
    log = tmp_path / "calls.log"
    log.write_text("")
    stdout = tmp_path / "flyctl-stdout.json"
    env = dict(os.environ)
    env.pop("GITHUB_OUTPUT", None)
    env.update(PATH=f"{fakes}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_DIGEST=DIGEST, FAKE_STDOUT=str(stdout), FAKE_INSPECT="present")
    return {"env": env, "log": log, "stdout": stdout, "dir": tmp_path}


def _calls(world):
    return world["log"].read_text().splitlines()


def _run(command, world, cwd=None, **env):
    merged = dict(world["env"])
    merged.update(env)
    return subprocess.run(command, env=merged, cwd=cwd or world["dir"], capture_output=True, text=True)


# --- require-image.sh -------------------------------------------------------------


def _require_image(world, *args, **env):
    return _run(["bash", str(REQUIRE_IMAGE), *args], world, **env)


def test_require_image_passes_for_a_published_image_and_prints_its_digest(world):
    result = _require_image(world, IMAGE)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert DIGEST in result.stdout and IMAGE in result.stdout, result.stdout
    # The registry is asked once, anonymously: no login, no pull, nothing else.
    assert _calls(world) == [f"buildx imagetools inspect {IMAGE}"], _calls(world)


def test_require_image_fails_when_the_image_is_not_published(world):
    result = _require_image(world, IMAGE, FAKE_INSPECT="absent")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout
    # The message names the image and says the two things it can mean, so a
    # failing run explains itself: not published yet, or not public.
    assert IMAGE in result.stdout and "public" in result.stdout, result.stdout
    assert _calls(world) == [f"buildx imagetools inspect {IMAGE}"], _calls(world)


def test_require_image_fails_when_the_registry_reports_no_digest(world):
    # A reply with no Digest line proves nothing about the image.
    result = _require_image(world, IMAGE, FAKE_INSPECT="nodigest")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout


BAD_IMAGES = {
    "latest": "ghcr.io/onryde/seazn-placement:latest",
    "main": "ghcr.io/onryde/seazn-placement:main",
    "empty-tag": "ghcr.io/onryde/seazn-placement:",
    "no-tag": "ghcr.io/onryde/seazn-placement",
    "short-sha": f"ghcr.io/onryde/seazn-placement:{SHA[:12]}",
    "long-sha": f"ghcr.io/onryde/seazn-placement:{SHA}0",
    "upper-case-sha": f"ghcr.io/onryde/seazn-placement:{SHA.upper()}",
    "digest-only": f"ghcr.io/onryde/seazn-placement@{DIGEST}",
    "other-owner": f"ghcr.io/someone/seazn-placement:{SHA}",
    "other-registry": f"registry.fly.io/placement:{SHA}",
    "suffix": f"ghcr.io/onryde/seazn-placement:{SHA}-dirty",
    "prefix": f"x-ghcr.io/onryde/seazn-placement:{SHA}",
    "newline": f"ghcr.io/onryde/seazn-placement:{SHA}\n",
}


@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_require_image_refuses_anything_but_the_sha_tag_before_asking_the_registry(world, name):
    result = _require_image(world, BAD_IMAGES[name])
    assert result.returncode == 1, (name, result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout
    assert _calls(world) == [], f"{name}: the registry was asked about a malformed reference: {_calls(world)}"


# The rule a reference must satisfy is ghcr.io / onryde / seazn-placement : <40 lowercase
# hex>, the whole string. Each clause below names the cases that break only it.
REFERENCE_CLAUSES = {
    "registry is ghcr.io": {"other-registry", "prefix"},
    "owner is onryde": {"other-owner"},
    "a tag is present": {"no-tag", "empty-tag", "digest-only"},
    "the tag is a commit, not a name": {"latest", "main"},
    "the commit is all 40 characters": {"short-sha", "long-sha"},
    "the commit is lowercase": {"upper-case-sha"},
    "nothing follows the commit": {"suffix", "newline"},
}


def test_require_image_bad_reference_table_covers_every_clause_of_the_rule():
    # A refusal sweep over an empty or accidentally-valid table proves nothing, so the
    # floor is the rule: every clause has a case, and every case belongs to a clause.
    assert all(cases for cases in REFERENCE_CLAUSES.values())
    assert set().union(*REFERENCE_CLAUSES.values()) == set(BAD_IMAGES)
    assert IMAGE not in BAD_IMAGES.values()


@pytest.mark.parametrize("args", [(), ("",), (IMAGE, "extra")])
def test_require_image_wants_exactly_one_reference(world, args):
    result = _require_image(world, *args)
    assert result.returncode == 2, (args, result.stdout, result.stderr)
    assert "usage" in result.stderr.lower(), result.stderr
    assert _calls(world) == [], _calls(world)


# --- require-private-ips.py -----------------------------------------------------------


def _require_ips(world, addresses, app="placement-stg", raw=None, **env):
    world["stdout"].write_text(raw if raw is not None else json.dumps(addresses))
    return _run([sys.executable, str(REQUIRE_PRIVATE_IPS), app], world, **env)


def test_private_ips_pass_with_one_private_v6(world):
    result = _require_ips(world, [PRIVATE])
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "::error::" not in result.stdout, result.stdout
    # Exactly this command: the app, JSON output, nothing that changes anything.
    assert _calls(world) == ["ips list --app placement-stg --json"], _calls(world)


def test_private_ips_pass_with_two_private_v6(world):
    # Two Flycast addresses (e.g. two 6PN networks) are still all private.
    other = {"Address": "fdaa:0:1234:0:2::3", "Type": "private_v6", "Region": "global"}
    result = _require_ips(world, [PRIVATE, other])
    assert result.returncode == 0, (result.stdout, result.stderr)


# Every way an address can be reachable from the internet, alone and next to the
# private v6 the service needs. The check is "ALL private", so a public address
# fails however many private ones sit beside it.
PUBLIC_CASES = {
    "public-v4": [PUBLIC_V4],
    "shared-v4": [SHARED_V4],
    "public-v6": [PUBLIC_V6],
    "egress-v6": [EGRESS_V6],
    "egress-v4": [EGRESS_V4],
    "private-then-v4": [PRIVATE, PUBLIC_V4],
    "v4-then-private": [PUBLIC_V4, PRIVATE],
    "private-then-shared-v4": [PRIVATE, SHARED_V4],
    "private-then-v6": [PRIVATE, PUBLIC_V6],
    "private-between-publics": [PUBLIC_V4, PRIVATE, PUBLIC_V6],
}


@pytest.mark.parametrize("name", sorted(PUBLIC_CASES))
def test_private_ips_fail_on_any_address_that_is_not_a_private_v6(world, name):
    result = _require_ips(world, PUBLIC_CASES[name])
    assert result.returncode == 1, (name, result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout
    offenders = [a for a in PUBLIC_CASES[name] if a is not PRIVATE]
    for offender in offenders:
        assert offender["Address"] in result.stdout, (name, offender, result.stdout)
    assert PRIVATE["Address"] not in result.stdout, "the private address is not an offender"


def test_private_ips_public_case_table_covers_every_address_type_fly_has():
    # The floor is the type list, not a number: each type except the private v6 is
    # refused on its own, and the private v6 appears in the mixed cases.
    assert all(any(a is not PRIVATE for a in case) for case in PUBLIC_CASES.values())
    alone = {case[0]["Type"] for case in PUBLIC_CASES.values() if len(case) == 1}
    assert alone == FLY_ADDRESS_TYPES - {PRIVATE["Type"]}, alone
    assert {a["Type"] for case in PUBLIC_CASES.values() for a in case} == FLY_ADDRESS_TYPES


def test_private_ips_fail_when_there_is_no_address_at_all(world):
    # The empty list answers "no" to every "is there a public one" question, so
    # it must be stated: no private v6 means .flycast does not resolve and every
    # solve silently goes greedy (DEPLOY.md, step 2).
    result = _require_ips(world, [])
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout and "private" in result.stdout, result.stdout


def test_private_ips_need_both_the_type_and_the_prefix(world):
    # A private type on a public-looking address, and a Flycast prefix under a
    # public type, are both unproven: each half alone must not be enough.
    lying_type = {"Address": "2a09:8280:1::1:2345", "Type": "private_v6"}
    lying_prefix = {"Address": "fdaa:0:1234:0:1::2", "Type": "v6"}
    for entry in (lying_type, lying_prefix):
        result = _require_ips(world, [PRIVATE, entry])
        assert result.returncode == 1, (entry, result.stdout, result.stderr)
        assert entry["Address"] in result.stdout, result.stdout


@pytest.mark.parametrize(
    "raw",
    ["not json at all", "", '{"Address": "fdaa::1", "Type": "private_v6"}', "null", '"private_v6"',
     '[{"address": "fdaa::1", "type": "private_v6"}]', '[{"Address": "fdaa::1"}]', '[{"Type": "private_v6"}]',
     "[1]", '[null]', '["fdaa::1"]', '[[]]'],
)
def test_private_ips_fail_closed_on_anything_it_cannot_read(world, raw):
    result = _require_ips(world, None, raw=raw)
    assert result.returncode == 1, (raw, result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout


def test_private_ips_fail_when_flyctl_fails(world):
    # flyctl printed a clean list and THEN failed: a non-zero exit is not "no public addresses".
    world["stdout"].write_text(json.dumps([PRIVATE]))
    result = _run([sys.executable, str(REQUIRE_PRIVATE_IPS), "placement"], world, FAKE_EXIT="1")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout
    assert _calls(world) == ["ips list --app placement --json"], _calls(world)


@pytest.mark.parametrize("how", ["missing", "not-executable"])
def test_private_ips_fail_cleanly_when_flyctl_cannot_run_at_all(world, how):
    # No traceback: a missing or unrunnable flyctl is an annotated failure like any other.
    # (FileNotFoundError and PermissionError are two OSErrors; both must be caught.)
    bin_dir = world["dir"] / "no-flyctl-here"
    bin_dir.mkdir()
    if how == "not-executable":
        (bin_dir / "flyctl").write_text("#!/bin/sh\n")
    result = _run([sys.executable, str(REQUIRE_PRIVATE_IPS), "placement"], world, PATH=str(bin_dir))
    assert result.returncode == 1, (how, result.stdout, result.stderr)
    assert "::error::" in result.stdout and "flyctl" in result.stdout, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    assert _calls(world) == [], "the fake flyctl was on PATH: the case did not remove it"


def test_private_ips_ignore_noise_flyctl_writes_to_stderr(world):
    # The fake prints an update notice on stderr; only stdout is the JSON.
    result = _require_ips(world, [PRIVATE])
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_private_ips_check_the_app_it_is_given(world):
    for app in ("placement", "placement-stg"):
        result = _require_ips(world, [PRIVATE], app=app)
        assert result.returncode == 0, (app, result.stdout, result.stderr)
    assert _calls(world) == ["ips list --app placement --json", "ips list --app placement-stg --json"]


@pytest.mark.parametrize("args", [(), ("",), ("placement", "placement-stg")])
def test_private_ips_want_exactly_one_app(world, args):
    result = _run([sys.executable, str(REQUIRE_PRIVATE_IPS), *args], world)
    assert result.returncode == 2, (args, result.stdout, result.stderr)
    assert "usage" in result.stderr.lower(), result.stderr
    assert _calls(world) == [], _calls(world)


# --- require-main-tip.sh -----------------------------------------------------------------


def _git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare ``origin`` with ``main`` at one commit, and a clone of it (the job's checkout)."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "checkout"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    _git(tmp_path, "init", "--initial-branch=main", str(seed))
    (seed / "a.txt").write_text("a\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "first")
    _git(seed, "push", str(bare), "main")
    _git(tmp_path, "clone", str(bare), str(clone))
    return {"bare": bare, "seed": seed, "clone": clone, "first": _git(seed, "rev-parse", "HEAD")}


def _advance_main(origin):
    seed = origin["seed"]
    (seed / "b.txt").write_text("b\n")
    _git(seed, "add", "b.txt")
    _git(seed, "commit", "-m", "second")
    _git(seed, "push", str(origin["bare"]), "main")
    return _git(seed, "rev-parse", "HEAD")


def _tip(world, origin, sha, output=True):
    out = world["dir"] / "github-output"
    out.write_text("")
    env = {"GITHUB_OUTPUT": str(out)} if output else {}
    result = _run(["bash", str(REQUIRE_MAIN_TIP), sha], world, cwd=origin["clone"], **env)
    return result, out.read_text().splitlines()


def test_main_tip_is_current_when_main_has_not_moved(world, origin):
    result, written = _tip(world, origin, origin["first"])
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert written == ["current=true"], written
    assert "::notice::" not in result.stdout, result.stdout


def test_main_tip_is_superseded_after_main_moves_on(world, origin):
    # Two pushes to main, and the older one's CI finishes last: it must not be
    # deployed over the newer one. Not an error: the newer commit deploys itself.
    second = _advance_main(origin)
    assert second != origin["first"]
    result, written = _tip(world, origin, origin["first"])
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert written == ["current=false"], written
    assert "::notice::" in result.stdout and second in result.stdout, result.stdout
    # The newer commit's run can itself be cancelled while pending (concurrency keeps
    # one pending run), so the notice says what to do then.
    assert "re-run the cancelled stg run" in result.stdout, result.stdout
    # The same script, asked about the new tip, says current: the answer follows
    # the remote, not a constant.
    result, written = _tip(world, origin, second)
    assert result.returncode == 0 and written == ["current=true"], (written, result.stdout, result.stderr)


def test_main_tip_answers_for_the_remote_not_the_local_checkout(world, origin):
    # The clone still has the first commit as origin/main until it fetches. A
    # guard that read the local ref would say "current" for a stale sha.
    _advance_main(origin)
    local = _git(origin["clone"], "rev-parse", "origin/main")
    assert local == origin["first"]
    result, written = _tip(world, origin, local)
    assert written == ["current=false"], (written, result.stdout, result.stderr)


def test_main_tip_is_an_error_when_origin_cannot_be_read(world, origin):
    _git(origin["clone"], "remote", "set-url", "origin", str(world["dir"] / "no-such-repo.git"))
    result, written = _tip(world, origin, origin["first"])
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout and "could not read origin" in result.stdout, result.stdout
    # An unreadable remote must never read as "superseded" (skip) or "current".
    assert written == [], written


def test_main_tip_is_an_error_when_origin_has_no_main(world, origin):
    _git(origin["bare"], "symbolic-ref", "HEAD", "refs/heads/other")
    _git(origin["bare"], "branch", "-m", "main", "other")
    result, written = _tip(world, origin, origin["first"])
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "::error::" in result.stdout and "no refs/heads/main" in result.stdout, result.stdout
    assert written == [], written


@pytest.mark.parametrize("sha", ["", "main", "HEAD", "abc123", "0123456789ABCDEF0123456789ABCDEF01234567", "x" * 40])
def test_main_tip_wants_a_full_lowercase_sha(world, origin, sha):
    result, written = _tip(world, origin, sha)
    assert result.returncode == 1, (sha, result.stdout, result.stderr)
    assert "::error::" in result.stdout, result.stdout
    assert written == [], written


def test_main_tip_without_a_github_output_just_prints(world, origin):
    result, _ = _tip(world, origin, origin["first"], output=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert origin["first"] in result.stdout, result.stdout


def test_main_tip_wants_exactly_one_argument(world, origin):
    for args in ([], [origin["first"], "extra"]):
        result = _run(["bash", str(REQUIRE_MAIN_TIP), *args], world, cwd=origin["clone"])
        assert result.returncode == 2, (args, result.stdout, result.stderr)
        assert "usage" in result.stderr.lower(), result.stderr


# --- the scripts as files -----------------------------------------------------------------


def test_the_deploy_scripts_exist_and_pass_a_syntax_check():
    checked = 0
    for script in (REQUIRE_IMAGE, REQUIRE_MAIN_TIP):
        assert script.is_file(), script
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (script.name, result.stderr)
        checked += 1
    assert REQUIRE_PRIVATE_IPS.is_file(), REQUIRE_PRIVATE_IPS
    compile(REQUIRE_PRIVATE_IPS.read_text(), str(REQUIRE_PRIVATE_IPS), "exec")
    checked += 1
    assert checked == 3
