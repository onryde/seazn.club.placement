"""Pins on the two deploy workflows (``stg.yml``, ``prod.yml``), the network shape
of ``fly.toml``, and the docs that describe them.

What is pinned, because each of these can drift with every test green:

* the deploys ship the image ``ci.yml`` tested (``--image``), never a rebuild;
* ``stg.yml`` deploys only a green ``push`` to ``main`` of THIS repository, the
  commit ``ci`` ran on, and only while it is still the tip of ``main``;
* ``prod.yml`` runs on a ``v*.*.*`` tag and refuses a commit with no published image;
* the placement service stays internal: no workflow allocates an address, every
  deploy passes ``--no-public-ips`` and is followed by the private-address check,
  and ``fly.toml`` stays a raw TCP service;
* the one credential is ``FLY_TOKEN``, the actions are sha-pinned, and no event
  value is interpolated into a ``run:`` block;
* a guard cannot be switched off from the side: no ``continue-on-error``, no
  ``shell:``, an exact list of steps with their exact ``if:``, ``with:`` and
  ``env:`` keys, exact job, workflow and trigger keys, an exact set of flyctl
  subcommands, and a stg step that reds the job if the tip check never answered.

These are text reads (there is no YAML library in this project's dependencies and
none is wanted) plus a few runs of a workflow's own shell. The helpers are
duplicated from ``tests/test_ci_wiring.py`` on purpose, so this file stands alone.
Comment lines are stripped first. Every sweep reports what it scanned and zero is a
FAILURE (seazn.club's TEST-STRATEGY.md, see AGENTS.md, "Paths in comments":
anti-vacuity). Expected values come from the rules in the owner's brief and Fly's
documented behaviour, never from the file under test.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPTS_DIR = REPO_ROOT / "scripts"
JOB_HEADER = re.compile(r"^  ([a-z0-9-]+):[ \t]*$", re.MULTILINE)

IMAGE_REPO = "ghcr.io/onryde/seazn-placement"
# superfly/flyctl-actions, tag v1 (the same commit as the tag 1.6). Resolved read-only
# on 2026-10-25 with `gh api repos/superfly/flyctl-actions/git/ref/tags/v1`, a
# lightweight tag (object.type == commit). The action's own `version` input defaults
# to the newest flyctl, which is why FLYCTL_VERSION is pinned too.
SETUP_FLYCTL = "superfly/flyctl-actions/setup-flyctl"
SETUP_FLYCTL_SHA = "ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1"

DEPLOY_WORKFLOWS = ("stg.yml", "prod.yml")
EXPECTED_JOB = {"stg.yml": "deploy-stg", "prod.yml": "deploy"}
EXPECTED_TIMEOUT = 20
# Ported from seazn.club's placement-stg.yml and placement-prod.yml: the log-attribution
# reminder verbatim, and the machine-count reminder with ONE change. The original named
# "hard_limit=1" and fly.toml says 2 (P3 review M-1); a number written into a workflow
# cannot be checked against the file, so the reminder no longer states one.
VERIFY_BY_LOG_ATTRIBUTION = (
    "echo \"::notice::VERIFY BY LOG ATTRIBUTION, not by reading the env var: run one Auto-schedule on "
    "stg.seazn.club, then 'fly logs --app placement-stg' must show a 'solve request=' line and "
    "'fly logs --app placement' must show NOTHING for it. A misspelled PLACEMENT_SERVICE_HOST on "
    "seazn-club-stg silently routes staging to production.\""
)
MACHINE_COUNT_REMINDER = (
    "echo \"::notice::DEPLOY.md step 5 sets 'fly scale count 10'. If the count above is not 10, the "
    "fan-out that spreads solves across machines (fly.toml's hard_limit) is not in place.\""
)

# What a deploy must not do to the network, assembled from pieces so this file does
# not match its own sweep.
ALLOCATE_NEEDLES = ("allocate" "-v4", "allocate" "-v6", "allocate" "-egress", "--fly" "cast", "ips " "allocate")


def _code(text):
    """The text without its whole-line comments."""
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*#", line)) + "\n"


def _read(name):
    return (WORKFLOWS_DIR / name).read_text()


def _workflows():
    return {path.name: path.read_text() for path in sorted(WORKFLOWS_DIR.glob("*.yml"))}


def _deploy(name):
    """(comment-stripped text, job id, job block) of a deploy workflow, which has exactly one job."""
    code = _code(_read(name))
    _, _, after_jobs = code.partition("\njobs:\n")
    headers = list(JOB_HEADER.finditer(after_jobs))
    assert len(headers) == 1, f"{name}: expected exactly one job, found {[h.group(1) for h in headers]}"
    return code, headers[0].group(1), after_jobs[headers[0].start():]


def _top_block(code, key):
    match = re.search(rf"^{key}:\n((?:[ \t]+.*\n)+)", code, re.MULTILINE)
    assert match, f"no top-level '{key}:' block"
    return match.group(1)


def _steps(block):
    _, _, after_steps = block.partition("    steps:\n")
    assert after_steps, "job has no steps"
    parts = re.split(r"(?m)^      - ", after_steps)
    return ["      - " + part for part in parts[1:]]


def _step_with(block, needle):
    found = [s for s in _steps(block) if needle in s]
    assert len(found) == 1, f"expected exactly one step containing {needle!r}, found {len(found)}"
    return found[0]


def _index_of(block, needle):
    found = [i for i, s in enumerate(_steps(block)) if needle in s]
    assert len(found) == 1, f"expected exactly one step containing {needle!r}, found {len(found)}"
    return found[0]


def _run_script(step):
    """The shell text of a step's ``run:`` (block scalar or one line), dedented."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run: (.*)$", line)
        if not match:
            continue
        inline = match.group(2)
        if inline not in ("|", "|-", ">", ">-"):
            return inline
        body = []
        for follower in lines[index + 1:]:
            if follower.strip() and len(follower) - len(follower.lstrip()) <= len(match.group(1)):
                break
            body.append(follower)
        indent = min(len(b) - len(b.lstrip()) for b in body if b.strip())
        return "\n".join(b[indent:] for b in body).rstrip() + "\n"
    raise AssertionError(f"no run: in step:\n{step}")


def _script_of(step):
    """A step's shell text, or "" for a ``uses:`` step."""
    return _run_script(step) if re.search(r"^\s*(?:- )?run:", step, re.MULTILINE) else ""


def _command(step):
    """The step's script as one line: continuations joined, whitespace collapsed."""
    return re.sub(r"\s+", " ", _run_script(step).replace("\\\n", " ")).strip()


def _job_if(block):
    """The job-level ``if:`` as one string (inline, or a folded ``>-`` block)."""
    lines = block.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^    if: (.*)$", line)
        if not match:
            continue
        value = match.group(1)
        if value in (">", ">-"):
            folded = []
            for follower in lines[index + 1:]:
                if not follower.startswith("      "):
                    break
                folded.append(follower.strip())
            return " ".join(folded)
        return value
    return None


def _tip_key():
    """The key require-main-tip.sh writes to $GITHUB_OUTPUT (the producer's side of the contract)."""
    script = (SCRIPTS_DIR / "require-main-tip.sh").read_text()
    return re.search(r'echo "(\w+)=\$\w+" >> "\$GITHUB_OUTPUT"', script).group(1)


def _fields(step):
    """{key: value} of the keys a step itself carries (the first sits after the '- ')."""
    return {m.group(1): m.group(2) or ""
            for m in re.finditer(r"(?m)^      (?:- |  )([\w-]+):(?: (.*))?$", step)}


def _step_key(step):
    """What a step DOES, not what it is called: the action it uses, the repo script it runs,
    the flyctl subcommand it runs, or a fixed tag for one of the two inline shell checks.
    A step it cannot place gets an "unrecognised step" key, so a new step reds until it is added on purpose."""
    fields = _fields(step)
    if "uses" in fields:
        return fields["uses"].split("@")[0]
    script = _script_of(step)
    found = re.search(r"scripts/([\w.-]+)", script)
    if found:
        return f"scripts/{found.group(1)}"
    found = re.search(r"(?<![\w./'-])flyctl\s+(\w+)", script)
    if found:
        return f"flyctl {found.group(1)}"
    if "sed -n 's/^[[:space:]]*app" in script:
        return "read the app name from fly.toml"
    if 'case "$CURRENT" in' in script:
        return "assert the tip check answered"
    # Not placed: the tables below will not list it, so the step-list test reds with this tag in the diff.
    return "unrecognised step: " + (script.strip().splitlines() or [step.strip().splitlines()[0]])[0]


def _sub_keys(step, key):
    """The keys of a step's ``with:`` / ``env:`` mapping; [] when the step has none."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^        {key}:\s*$", line):
            keys = []
            for follower in lines[index + 1:]:
                if follower.strip() and not follower.startswith("          "):
                    break
                found = re.match(r"^          ([\w-]+):", follower)
                if found:
                    keys.append(found.group(1))
            return keys
    return []


def _has_key(text, key):
    """True when ``key:`` appears as a YAML key (not as prose) anywhere in the text."""
    return bool(re.search(rf"(?m)^\s*(?:- )?{re.escape(key)}\s*:", text))


def _env_block(text, indent):
    pad = " " * indent
    match = re.search(rf"^{pad}env:\n((?:{pad}  .*\n)+)", text, re.MULTILINE)
    return dict(re.findall(rf"^{pad}  (\w+): (.*)$", match.group(1), re.MULTILINE)) if match else {}


# --- coverage of the sweep itself ----------------------------------------------------------


def test_the_workflows_are_ci_stg_and_prod():
    # A fourth workflow is unreviewed until it is added here, on purpose.
    assert sorted(_workflows()) == ["ci.yml", "prod.yml", "stg.yml"]
    for name in DEPLOY_WORKFLOWS:
        _, job, _ = _deploy(name)
        assert job == EXPECTED_JOB[name], (name, job)


# --- when each one runs --------------------------------------------------------------------


def test_stg_runs_only_after_a_green_ci_push_to_main_of_this_repository():
    code, _, job = _deploy("stg.yml")
    on = _top_block(code, "on")
    # workflow_run is the only trigger: no push, no pull_request, no manual dispatch
    # that could deploy a sha nobody tested.
    assert re.findall(r"^  (\w+):", on, re.MULTILINE) == ["workflow_run"], on
    ci_name = re.search(r"^name: (\S+)$", _code(_read("ci.yml")), re.MULTILINE).group(1)
    assert re.search(rf"^    workflows: \[{re.escape(ci_name)}\]$", on, re.MULTILINE), (ci_name, on)
    assert re.search(r"^    types: \[completed\]$", on, re.MULTILINE), on
    assert re.search(r"^    branches: \[main\]$", on, re.MULTILINE), on

    condition = _job_if(job)
    assert condition, "the deploy job has no job-level 'if:'"
    assert "||" not in condition, f"an OR would let a run that is not green and a push through: {condition}"
    # The gate is exactly these five, no more and no fewer. `event == push` is what
    # keeps a fork's pull request (whose head_branch may be called main) out, and
    # the repository check keeps any other repository's run out. The last one is the
    # first gate on the tip of main: on workflow_run github.sha is the default
    # branch's head, so this runs only a commit that WAS main's tip when ci finished,
    # and GitHub evaluates it before any step has run a script from the checkout.
    assert set(condition.split(" && ")) == {
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "github.event.workflow_run.head_sha == github.sha",
    }, condition


def test_stg_checks_out_and_deploys_the_commit_ci_ran_on():
    code, _, job = _deploy("stg.yml")
    head_sha = "${{ github.event.workflow_run.head_sha }}"
    # On workflow_run, github.sha is the default branch's head, not the tested commit,
    # so it never picks what is checked out, built or deployed. Its one use is the job
    # `if:`, which compares it with the tested commit (see the gate test above).
    assert code.count("github.sha") == 1 and "head_sha == github.sha" in _job_if(job), code.count("github.sha")
    checkout = _step_with(job, "actions/checkout@")
    assert re.search(rf"^          ref: {re.escape(head_sha)}$", checkout, re.MULTILINE), checkout
    env = _env_block(job, 4)
    assert env.get("HEAD_SHA") == head_sha, env
    assert env.get("IMAGE") == f"{IMAGE_REPO}:{head_sha}", env
    assert code.count(head_sha) == 3, "the checkout ref, HEAD_SHA and IMAGE are the three users of the tested commit"


def test_prod_runs_only_on_a_release_tag():
    code, _, job = _deploy("prod.yml")
    on = _top_block(code, "on")
    assert re.findall(r"^  (\w+):", on, re.MULTILINE) == ["push"], on
    assert re.search(r'^    tags: \["v\*\.\*\.\*"\]$', on, re.MULTILINE), on
    assert "branches" not in on, "a branch push must not deploy production"
    assert _job_if(job) is None, "prod has no job-level condition: the tag is the gate"
    assert "workflow_run" not in code and "github.event" not in code
    # On a tag push github.sha is the tagged commit, which is the image to deploy.
    assert _env_block(job, 4).get("IMAGE") == f"{IMAGE_REPO}:${{{{ github.sha }}}}"
    assert "head_sha" not in code


def test_neither_deploy_declares_needs():
    # Ports seazn.club's toolchain rule: `needs:` only resolves inside one workflow
    # run, so across files it is a silent no-op. The cross-file gate is workflow_run
    # (stg) and the published image (prod), never `needs:`.
    for name in DEPLOY_WORKFLOWS:
        assert not re.search(r"^\s*needs:", _code(_read(name)), re.MULTILINE), name


# --- the app each deploys to -----------------------------------------------------------------


def _verify_step(name):
    _, _, job = _deploy(name)
    return _step_with(job, "sed -n 's/^[[:space:]]*app")


def _run_verify(name, tmp_path, toml):
    (tmp_path / "fly.toml").write_text(toml)
    out = tmp_path / "github-output"
    out.write_text("")
    env = dict(os.environ, GITHUB_OUTPUT=str(out))
    result = subprocess.run(["bash", "-c", _run_script(_verify_step(name))], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    return result, out.read_text().splitlines()


def test_both_deploys_read_fly_toml_at_the_repository_root():
    for name in DEPLOY_WORKFLOWS:
        script = _run_script(_verify_step(name))
        assert re.search(r"\| head -1\)\"", script) and re.search(r' fly\.toml \|', script), (name, script)
    # And they deploy that same file.
    _, _, stg = _deploy("stg.yml")
    _, _, prod = _deploy("prod.yml")
    assert "--config fly.toml" in _command(_step_with(stg, "flyctl deploy"))
    assert "--config fly.toml" in _command(_step_with(prod, "flyctl deploy"))


def test_the_real_fly_toml_passes_both_app_name_checks(tmp_path):
    real = (REPO_ROOT / "fly.toml").read_text()
    for name in DEPLOY_WORKFLOWS:
        result, written = _run_verify(name, tmp_path, real)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        assert "::error::" not in result.stdout, result.stdout
        # prod hands the app to its later steps through the step output.
        assert written == (["app=placement"] if name == "prod.yml" else []), (name, written)


APP_LINES = {
    "renamed": ('app = "placement-stg"\n', False),
    "forked-prod": ('app = "placement-prod"\n', False),
    "prefix": ('app = "placement2"\n', False),
    "empty": ('app = ""\n', False),
    "absent": ('primary_region = "lhr"\n', False),
    "commented-out": ('# app = "placement"\nprimary_region = "lhr"\n', False),
    "indented-and-spaced": ('  app   =   "placement"   # the production app\n', True),
    "plain": ('app = "placement"\n', True),
}
# prod.yml tells "no app name at all" apart from "the wrong app name"; stg.yml has the one check.
NO_APP_NAME = {"empty", "absent", "commented-out"}


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
@pytest.mark.parametrize("case", sorted(APP_LINES))
def test_the_app_name_check_accepts_only_placement(name, case, tmp_path):
    toml, ok = APP_LINES[case]
    result, written = _run_verify(name, tmp_path, toml + "[build]\n")
    if ok:
        assert result.returncode == 0, (name, case, result.stdout, result.stderr)
        if name == "prod.yml":
            assert written == ["app=placement"], written
    else:
        assert result.returncode == 1, (name, case, result.stdout, result.stderr)
        assert "::error::" in result.stdout, result.stdout
        reason = "no app name in fly.toml" if name == "prod.yml" and case in NO_APP_NAME else "expected 'placement'"
        assert reason in result.stdout, (name, case, reason, result.stdout)
        assert written == [], f"a failed check must not hand an app name on: {written}"


def test_the_app_name_table_has_every_kind_of_case():
    # No number: the table must hold an accepted case, the dedicated "no app name at
    # all" cases prod.yml words differently, and at least one wrong-name case.
    rejected = {case for case, (_, ok) in APP_LINES.items() if not ok}
    accepted = set(APP_LINES) - rejected
    assert accepted, "no accepted case"
    assert NO_APP_NAME <= rejected, "every 'no app name' case must be a rejected one in the table"
    assert rejected - NO_APP_NAME, "no wrong-name case"


def test_the_deploys_name_their_apps():
    _, _, stg = _deploy("stg.yml")
    _, _, prod = _deploy("prod.yml")
    # stg overrides the app (fly.toml says `placement`); prod uses what it verified.
    assert _command(_step_with(stg, "flyctl deploy")) == (
        'flyctl deploy --app placement-stg --config fly.toml --image "$IMAGE" --no-public-ips'
    )
    assert _command(_step_with(prod, "flyctl deploy")) == (
        'flyctl deploy --app "$APP" --config fly.toml --image "$IMAGE" --no-public-ips'
    )
    # prod's $APP is the verified step output, written by the verify step under the same key.
    verify = _step_with(prod, "id: flycfg")
    key = re.search(r'echo "(\w+)=\$app" >> "\$GITHUB_OUTPUT"', verify).group(1)
    assert key == "app"
    for needle in ("flyctl deploy", "require-private-ips.py", "flyctl status"):
        step = _step_with(prod, needle)
        assert f"          APP: ${{{{ steps.flycfg.outputs.{key} }}}}\n" in step, (needle, step)


# --- the image: published by ci, deployed here, never rebuilt ------------------------------------


def test_the_deploys_ship_the_published_image_and_never_build():
    checked = 0
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        for needle in ("docker/build-push-action", "docker/setup-buildx-action", "docker/login-action",
                       "registry.fly.io", "auth docker", "--local-only", "--remote-only", "--build-only",
                       "--dockerfile", "push: true"):
            assert needle not in code, f"{name}: {needle} means a build or a push"
        # every flyctl deploy takes --image, so none of them can fall back to building
        deploys = [s for s in _steps(job) if "flyctl deploy" in _script_of(s)]
        assert len(deploys) == 1, (name, len(deploys))
        assert '--image "$IMAGE"' in _command(deploys[0]), _command(deploys[0])
        # every mention of the image carries the immutable sha tag
        assert code.count("seazn-placement") == 1
        assert re.findall(r"seazn-placement(?!:\$\{\{ github\.(?:event\.workflow_run\.head_)?sha \}\})", code) == []
        assert not re.search(r"(?<![-\w])latest\b", code), "no 'latest' anywhere (ubuntu-latest excepted)"
        checked += 1
    assert checked == len(DEPLOY_WORKFLOWS)


def test_each_deploy_requires_the_image_first_and_without_a_login():
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        step = _step_with(job, "scripts/require-image.sh")
        assert _command(step) == 'bash scripts/require-image.sh "$IMAGE"', _command(step)
        assert "FLY_API_TOKEN" not in step and "secrets." not in step, "the check is anonymous, like Fly's pull"
        # it gates the deploy and runs after the app name is known to be right
        assert _index_of(job, "sed -n 's/^[[:space:]]*app") < _index_of(job, "scripts/require-image.sh")
        assert _index_of(job, "scripts/require-image.sh") < _index_of(job, "flyctl deploy"), name


def test_stg_skips_a_commit_main_has_moved_past():
    _, _, job = _deploy("stg.yml")
    tip = _step_with(job, "scripts/require-main-tip.sh")
    assert _command(tip) == 'bash scripts/require-main-tip.sh "$HEAD_SHA"', _command(tip)
    assert re.search(r"^        id: tip$", tip, re.MULTILINE), tip
    # The consumer reads the key the producer writes.
    gate = f"steps.tip.outputs.{_tip_key()} == 'true'"
    assert _index_of(job, "scripts/require-image.sh") < _index_of(job, "scripts/require-main-tip.sh")
    assert _index_of(job, "scripts/require-main-tip.sh") < _index_of(job, "flyctl deploy")
    # Everything that acts on the deployment waits for the answer; the guard itself
    # and the cheap checks before it do not.
    gated_steps = (
        ("flyctl deploy", f"if: {gate}"),
        ("require-private-ips.py", f"if: {gate}"),
        ("flyctl status", f"if: always() && {gate}"),
    )
    gated = [s for s in _steps(job) if gate in s]
    assert len(gated) == len(gated_steps) > 0, [s.splitlines()[0] for s in gated]
    for needle, condition in gated_steps:
        step = _step_with(job, needle)
        assert f"        {condition}\n" in step, (needle, step)
    # prod has no such guard: a tag is a deliberate act.
    _, _, prod = _deploy("prod.yml")
    assert "require-main-tip" not in prod and "steps.tip" not in prod


# --- credentials, permissions, limits --------------------------------------------------------------


def test_deploys_use_only_FLY_TOKEN_and_only_where_flyctl_runs():
    checked = 0
    expected = 0
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        expected += len(re.findall(r"(?m)^      - ", code))  # every step bullet in the file, counted apart from _steps
        assert re.findall(r"secrets\.\w+", code) and set(re.findall(r"secrets\.\w+", code)) == {"secrets.FLY_TOKEN"}, name
        assert "github.token" not in code and "GITHUB_TOKEN" not in code, f"{name}: an alias of the job token"
        for step in _steps(job):
            body = _script_of(step)
            runs_flyctl = "flyctl" in body or "require-private-ips.py" in body
            has_token = "FLY_API_TOKEN: ${{ secrets.FLY_TOKEN }}" in step
            assert runs_flyctl == has_token, f"{name}: token scope is wrong for step:\n{step}"
            checked += 1
    assert checked == expected > 0, (checked, expected)


def test_permissions_are_read_only_with_no_job_overrides():
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        assert _top_block(code, "permissions").split() == ["contents:", "read"], name
        assert "permissions:" not in job, f"{name}: a job-level permissions block widens the token"
        assert not re.search(r":\s*write\b", code), name


def test_every_checkout_drops_its_credentials():
    checked = 0
    for name in DEPLOY_WORKFLOWS:
        _, _, job = _deploy(name)
        for step in _steps(job):
            if "actions/checkout@" in step:
                checked += 1
                assert "persist-credentials: false" in step, f"{name}: {step}"
    assert checked == len(DEPLOY_WORKFLOWS)


def test_deploy_concurrency_is_never_cancelled():
    groups = {}
    for name in DEPLOY_WORKFLOWS:
        _, _, job = _deploy(name)
        match = re.search(r"^    concurrency:\n      group: (\S+)\n      cancel-in-progress: false\n", job, re.MULTILINE)
        assert match, f"{name}: no job concurrency with cancel-in-progress: false"
        assert "${{" not in match.group(1), "the group is a literal: an expression could split it"
        groups[name] = match.group(1)
    # Staging has its own group: it never queues behind, or cancels, a production rollout.
    assert groups == {"stg.yml": "placement-stg-deploy", "prod.yml": "placement-deploy"}, groups
    assert len(set(groups.values())) == len(groups)


def test_every_deploy_job_has_a_timeout():
    for name in DEPLOY_WORKFLOWS:
        _, _, job = _deploy(name)
        assert re.findall(r"^    timeout-minutes: (\d+)$", job, re.MULTILINE) == [str(EXPECTED_TIMEOUT)], name


def test_flyctl_is_pinned_by_commit_and_by_version():
    versions = set()
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        step = _step_with(job, f"uses: {SETUP_FLYCTL}@")
        assert f"uses: {SETUP_FLYCTL}@{SETUP_FLYCTL_SHA} # v1\n" in step, step
        assert re.search(r"^          version: \${{ env\.FLYCTL_VERSION }}$", step, re.MULTILINE), step
        env = _env_block(code, 0)
        assert list(env) == ["FLYCTL_VERSION"], env
        assert re.fullmatch(r'"\d+\.\d+\.\d+"', env["FLYCTL_VERSION"]), env
        versions.add(env["FLYCTL_VERSION"])
    assert len(versions) == 1, f"stg.yml and prod.yml pin different flyctl versions: {versions}"


def test_the_ported_reminder_steps_are_kept():
    _, _, stg = _deploy("stg.yml")
    by_hand = _step_with(stg, "flyctl status")
    script = _run_script(by_hand)
    assert VERIFY_BY_LOG_ATTRIBUTION in script, script
    assert "flyctl status --app placement-stg || true" in script
    _, _, prod = _deploy("prod.yml")
    report = _step_with(prod, "flyctl status")
    assert "        if: always()\n" in report, report
    assert MACHINE_COUNT_REMINDER in _run_script(report)
    assert 'flyctl status --app "$APP" || true' in _run_script(report)


# --- a guard cannot be switched off from the side ----------------------------------------------------
#
# Each pin below is the ONLY thing that notices one kind of edit, and each is tried against its own
# mutants (a step that ignores its own failure, an `if: false`, a widened token, a stray `paths:`).
# The tables are what the deploys are MEANT to be, written from the brief, not read back from the files.

GATE = f"steps.tip.outputs.{_tip_key()} == 'true'"
READ_APP = "read the app name from fly.toml"
ASSERT_TIP = "assert the tip check answered"
CHECKOUT = "actions/checkout"
REQUIRE_IMAGE = "scripts/require-image.sh"
REQUIRE_TIP = "scripts/require-main-tip.sh"
REQUIRE_PRIVATE = "scripts/require-private-ips.py"
# The steps in order, each named by what it does (see _step_key), not by its display name.
STEP_KEYS = {
    "stg.yml": [CHECKOUT, SETUP_FLYCTL, READ_APP, REQUIRE_IMAGE, REQUIRE_TIP, ASSERT_TIP,
                "flyctl deploy", REQUIRE_PRIVATE, "flyctl status"],
    "prod.yml": [CHECKOUT, SETUP_FLYCTL, READ_APP, REQUIRE_IMAGE, "flyctl deploy", REQUIRE_PRIVATE, "flyctl status"],
}
# A step not listed here has no `if:`. Only the steps that act on the deployment wait for the tip check.
STEP_IFS = {
    "stg.yml": {"flyctl deploy": GATE, REQUIRE_PRIVATE: GATE, "flyctl status": f"always() && {GATE}"},
    "prod.yml": {"flyctl status": "always()"},
}
# A step not listed has no `with:` (or no `env:`). Checkout takes no `ref:`/`repository:`/`token:` in prod,
# and in stg only the tested commit; setup-flyctl takes only the pinned version, never a token.
STEP_WITH = {
    "stg.yml": {CHECKOUT: {"ref", "persist-credentials"}, SETUP_FLYCTL: {"version"}},
    "prod.yml": {CHECKOUT: {"persist-credentials"}, SETUP_FLYCTL: {"version"}},
}
STEP_ENV = {
    "stg.yml": {
        ASSERT_TIP: {"CURRENT"},
        "flyctl deploy": {"FLY_API_TOKEN"},
        REQUIRE_PRIVATE: {"FLY_API_TOKEN"},
        "flyctl status": {"FLY_API_TOKEN"},
    },
    "prod.yml": {
        "flyctl deploy": {"APP", "FLY_API_TOKEN"},
        REQUIRE_PRIVATE: {"APP", "FLY_API_TOKEN"},
        "flyctl status": {"APP", "FLY_API_TOKEN"},
    },
}
JOB_ENV = {"stg.yml": {"HEAD_SHA", "IMAGE"}, "prod.yml": {"IMAGE"}}
JOB_KEYS = {
    "stg.yml": ["name", "if", "runs-on", "timeout-minutes", "concurrency", "env", "steps"],
    "prod.yml": ["name", "runs-on", "timeout-minutes", "concurrency", "env", "steps"],
}
WORKFLOW_KEYS = ["name", "on", "permissions", "env", "jobs"]
# The trigger and nothing else under it: a `paths:`/`branches-ignore:` filter would make "ship what is on
# main" silently conditional. (stg.yml: no paths filter, the whole repository is the service.)
ON_KEYS = {"stg.yml": ("workflow_run", ["workflows", "types", "branches"]), "prod.yml": ("push", ["tags"])}
FLYCTL_SUBCOMMANDS = {"stg.yml": ["deploy", "status"], "prod.yml": ["deploy", "status"]}


def _step_facts(name):
    """[(step text, what it does, its own keys)] for a deploy workflow's steps, in order."""
    _, _, job = _deploy(name)
    return [(step, _step_key(step), _fields(step)) for step in _steps(job)]


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
def test_the_steps_are_exactly_these_in_this_order(name):
    keys = [key for _, key, _ in _step_facts(name)]
    assert keys == STEP_KEYS[name]
    assert len(set(keys)) == len(keys), "a key must be unique for the tables below to mean anything"


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
def test_the_step_conditions_are_exactly_these(name):
    got = {key: fields.get("if") for _, key, fields in _step_facts(name)}
    assert got == {key: STEP_IFS[name].get(key) for key in STEP_KEYS[name]}, got
    assert any(got.values()), "no step has an `if:` at all: the table is not looking at the files"


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
def test_the_with_and_env_keys_are_exactly_these(name):
    facts = _step_facts(name)
    assert facts, name
    got_with = {key: set(_sub_keys(step, "with")) for step, key, _ in facts if _sub_keys(step, "with")}
    got_env = {key: set(_sub_keys(step, "env")) for step, key, _ in facts if _sub_keys(step, "env")}
    assert got_with == STEP_WITH[name], got_with
    assert got_env == STEP_ENV[name], got_env
    # each mapping lists a key once
    for step, step_key, _ in facts:
        for mapping in ("with", "env"):
            keys = _sub_keys(step, mapping)
            assert len(keys) == len(set(keys)), (name, step_key, mapping, keys)
    _, _, job = _deploy(name)
    assert set(_env_block(job, 4)) == JOB_ENV[name], _env_block(job, 4)


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
def test_the_job_workflow_and_trigger_keys_are_exactly_these(name):
    code, _, job = _deploy(name)
    assert re.findall(r"(?m)^([\w-]+):", code) == WORKFLOW_KEYS
    assert sorted(re.findall(r"(?m)^    ([\w-]+):", job)) == sorted(JOB_KEYS[name])
    trigger, sub_keys = ON_KEYS[name]
    on = _top_block(code, "on")
    assert re.findall(r"(?m)^  ([\w-]+):", on) == [trigger]
    assert re.findall(r"(?m)^    ([\w-]+):", on) == sub_keys


@pytest.mark.parametrize("name", DEPLOY_WORKFLOWS)
def test_the_flyctl_subcommands_are_exactly_these(name):
    found = []
    for step, _, _ in _step_facts(name):
        # echoed reminders quote `fly logs` / `fly scale count`: only commands that are run count
        script = "\n".join(line for line in _script_of(step).splitlines() if not line.strip().startswith("echo"))
        found += re.findall(r"(?<![\w./'-])fly(?:ctl)?\s+(\w+)", script)
    assert sorted(found) == FLYCTL_SUBCOMMANDS[name], found


def test_no_job_or_step_ignores_its_own_failure():
    # A positive control: the predicate must see the key it bans, in both places it can sit.
    assert _has_key("    continue-on-error: true\n", "continue-on-error")
    assert _has_key("      - continue-on-error: true\n", "continue-on-error")
    assert not _has_key("# continue-on-error: true\n", "continue-on-error")
    for name in DEPLOY_WORKFLOWS:
        assert not _has_key(_code(_read(name)), "continue-on-error"), name


def test_no_step_picks_its_own_shell():
    # bash -e is what makes a failed command fail the step; `shell: bash {0}` drops it.
    assert _has_key("        shell: bash {0}\n", "shell")
    assert _has_key("    defaults:\n      run:\n        shell: sh\n", "defaults")
    for name in DEPLOY_WORKFLOWS:
        code = _code(_read(name))
        assert not _has_key(code, "shell") and not _has_key(code, "defaults"), name


def test_the_token_is_written_once_per_flyctl_step_and_nowhere_else():
    checked = 0
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        runs_flyctl = [s for s in _steps(job) if "flyctl" in _script_of(s) or "require-private-ips.py" in _script_of(s)]
        assert runs_flyctl, name
        # one `FLY_API_TOKEN: ${{ secrets.FLY_TOKEN }}` per such step, as a step-level env entry
        assert code.count("secrets.FLY_TOKEN") == len(runs_flyctl), (name, code.count("secrets.FLY_TOKEN"), len(runs_flyctl))
        entries = re.findall(r"(?m)^          (\w+): \$\{\{ secrets\.FLY_TOKEN \}\}$", code)
        assert entries == ["FLY_API_TOKEN"] * len(runs_flyctl), (name, entries)
        checked += len(runs_flyctl)
    assert checked > 0


# --- the tip check must answer, or the job is red ------------------------------------------------------


def _decision_step():
    _, _, job = _deploy("stg.yml")
    tip = _index_of(job, "id: tip")
    return job, _steps(job)[tip + 1]


def test_a_missing_tip_answer_reds_the_job():
    # If the tip step is skipped or its failure is ignored, `current` is never written, and every
    # step that waits for it is skipped: a GREEN run that deployed nothing. The step right after it
    # turns that into red. It has no `if:` of its own (success(), so no second red on a run that
    # already failed) and reads the output the tip script writes.
    job, step = _decision_step()
    assert "if" not in _fields(step), step
    assert _sub_keys(step, "env") == ["CURRENT"], step
    assert re.search(rf"(?m)^          CURRENT: \${{{{ steps\.tip\.outputs\.{_tip_key()} }}}}$", step), step
    answers = {"true": 0, "false": 0, "": 1, "True": 1, "yes": 1, "1": 1, "true ": 1, "null": 1}
    assert {v == 0 for v in answers.values()} == {True, False} and len(answers) > 2
    script = _run_script(step)
    for value, expected in answers.items():
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                env=dict(os.environ, CURRENT=value), capture_output=True, text=True)
        assert result.returncode == expected, (value, result.returncode, result.stdout, result.stderr)
        assert ("::error::" in result.stdout) == bool(expected), (value, result.stdout)
    # and it is not run before the tip step it checks, nor after the steps that depend on it
    assert _index_of(job, "id: tip") + 1 == _index_of(job, "CURRENT")
    assert _index_of(job, "CURRENT") < _index_of(job, "flyctl deploy")


# --- what a step prints must be true ---------------------------------------------------------------------


def test_the_by_hand_step_does_not_claim_a_deploy_it_cannot_know_happened():
    _, _, job = _deploy("stg.yml")
    script = _run_script(_step_with(job, "flyctl status"))
    # It runs on always(), i.e. also after a failed deploy; "Deployed" would then be false.
    assert "Deployed" not in script, script
    assert '::notice::Attempted a deploy of ${IMAGE} to placement-stg' in script, script


def test_every_hard_limit_stated_agrees_with_fly_toml():
    declared = re.findall(r"(?m)^\s*hard_limit\s*=\s*(\d+)\s*(?:#.*)?$", _code((REPO_ROOT / "fly.toml").read_text()))
    assert len(declared) == 1, f"expected exactly one hard_limit in fly.toml, found {declared}"
    # Docs and workflows (comments included) may state it; none may state another value. fly.toml's own
    # comments talk about hypothetical values ("hard_limit = 1 could pin..."), so it is the source, not a witness.
    witnesses = {**_doc_files(), **{f".github/workflows/{n}": t for n, t in _workflows().items()}}
    stated = [(rel, value) for rel, text in witnesses.items()
              for value in re.findall(r"hard_limit\s*=\s*(\d+)", _prose(text))]
    assert any(rel == "DEPLOY.md" for rel, _ in stated), "DEPLOY.md states no hard_limit: nothing was compared"
    assert [(rel, v) for rel, v in stated if v != declared[0]] == [], (declared, stated)


# --- the placement service stays internal ------------------------------------------------------------


def test_every_deploy_is_followed_by_the_private_address_check_for_the_same_app():
    checked = 0
    for name in DEPLOY_WORKFLOWS:
        _, _, job = _deploy(name)
        deploy = _step_with(job, "flyctl deploy")
        check = _step_with(job, "require-private-ips.py")
        assert _index_of(job, "flyctl deploy") < _index_of(job, "require-private-ips.py"), name
        assert "--no-public-ips" in _command(deploy), "a deploy must never allocate a public address"
        app = re.search(r'--app (\S+)', _command(deploy)).group(1)
        assert _command(check) == f"python3 scripts/require-private-ips.py {app}", (name, _command(check))
        assert "if: always()" not in check, "a failed deploy must not be followed by a 'passed' check"
        checked += 1
    assert checked == len(DEPLOY_WORKFLOWS)


def _swept_text():
    """Workflows and scripts (workflows without whole-line comments), keyed by path."""
    swept = {f".github/workflows/{name}": _code(text) for name, text in _workflows().items()}
    for path in sorted(SCRIPTS_DIR.iterdir()):
        if path.is_file():
            text = path.read_text()
            swept[f"scripts/{path.name}"] = _code(text) if path.suffix in (".sh", ".py") else text
    return swept


def test_nothing_in_ci_allocates_an_address_or_touches_one():
    swept = _swept_text()
    assert {".github/workflows/stg.yml", ".github/workflows/prod.yml", "scripts/require-private-ips.py"} <= set(swept)
    for rel, text in swept.items():
        for needle in ALLOCATE_NEEDLES:
            assert needle not in text, f"{rel}: {needle!r} allocates an address"
    # The only `ips` subcommand anything here runs is `list`.
    subcommands = []
    for rel, text in swept.items():
        subcommands += [(rel, sub) for sub in re.findall(r"(?<![\w-])ips\s+(\w+)", text)]
        subcommands += [(rel, sub) for sub in re.findall(r'"ips",\s*"(\w+)"', text)]
    assert subcommands, "no 'ips' invocation found: the private-address check does not run flyctl"
    assert {sub for _, sub in subcommands} == {"list"}, subcommands


def test_fly_toml_stays_a_raw_tcp_service_with_no_http_handler():
    code = _code((REPO_ROOT / "fly.toml").read_text())
    tables = re.findall(r"^\s*(\[\[?[\w.]+\]\]?)\s*$", code, re.MULTILINE)
    assert tables, "no table found in fly.toml; the parse is vacuous"
    assert "[http_service]" not in tables and "[[http_service]]" not in tables
    assert tables.count("[[services]]") == 1 and tables.count("[[services.ports]]") == 1, tables
    assert "handlers" not in code, "a port handler (http/tls) would publish the service"
    assert "force_https" not in code
    lines = code.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "[[services.ports]]")
    body = []
    for line in lines[start + 1:]:
        if line.strip().startswith("["):
            break
        if line.split("#")[0].strip():
            body.append(line.split("#")[0].strip())
    assert body == ["port = 50051"], body
    assert re.search(r'^\s*protocol\s*=\s*"tcp"\s*$', code, re.MULTILINE), "the service is raw TCP"


# --- the docs ---------------------------------------------------------------------------------------------


def _doc_files():
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=REPO_ROOT, capture_output=True, check=True)
    names = sorted(n for n in result.stdout.decode().split("\0") if n.endswith(".md"))
    return {rel: (REPO_ROOT / rel).read_text() for rel in names}


def test_every_workflow_named_in_the_docs_exists():
    docs = _doc_files()
    assert {"DEPLOY.md", "README.md", "AGENTS.md"} <= set(docs), sorted(docs)
    existing = set(_workflows())
    named = []
    loose = 0
    for rel, text in docs.items():
        loose += len(re.findall(r"\.yml\b", text))
        named += [(rel, name) for name in re.findall(r"(?<![\w.-])([\w-]+\.yml)\b", text)]
    # Every `.yml` mention was parsed as a file name, and there is at least one.
    assert len(named) == loose > 0, (len(named), loose)
    missing = sorted({(rel, name) for rel, name in named if name not in existing})
    assert not missing, f"docs name workflows that do not exist: {missing}"
    # And the reverse for DEPLOY.md: it describes every workflow that does exist.
    for name in existing:
        assert f".github/workflows/{name}" in docs["DEPLOY.md"], f"DEPLOY.md never names {name}"


def _prose(text):
    """Line wraps are not content: a claim wrapped over two lines is still one claim."""
    return re.sub(r"\s+", " ", text)


def test_every_job_a_doc_names_exists():
    # A reference, not a phrase: "the `x` job" in a doc must be a job some workflow has.
    jobs = set()
    for text in _workflows().values():
        _, _, after = _code(text).partition("\njobs:\n")
        jobs |= {m.group(1) for m in JOB_HEADER.finditer(after)}
    assert jobs, "no workflow has a job: the check is vacuous"
    mentioned = [(rel, job) for rel, text in _doc_files().items() for job in re.findall(r"`([\w-]+)` job\b", text)]
    assert mentioned, "no doc names a job; the check on job names is vacuous"
    assert not [(rel, job) for rel, job in mentioned if job not in jobs], (mentioned, sorted(jobs))


def _fenced_commands(text):
    """The shell commands in a doc's fenced blocks, `\\` continuations joined, one per list entry."""
    commands = []
    for block in re.findall(r"```[\w-]*\n(.*?)```", text, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if line.strip():
                commands.append(re.sub(r"\s+", " ", line).strip())
    return commands


def test_every_documented_deploy_command_allocates_no_public_address():
    # `fly deploy` on an app that holds no address allocates PUBLIC ones unless told not to
    # (flyctl v0.4.115, deploy_first.go). The runbook's manual deploys must say so too.
    checked = 0
    for rel, text in _doc_files().items():
        commands = _fenced_commands(text)
        deploys = [c for c in commands if re.match(r"(?:fly|flyctl) deploy\b", c)]
        # none hides behind `&&`, `$(...)` or a pipe, where the match above would not see it
        assert deploys == [c for c in commands if re.search(r"(?<![\w-])(?:fly|flyctl) deploy\b", c)], (rel, commands)
        for command in deploys:
            assert "--no-public-ips" in command.split(), f"{rel}: {command}"
            checked += 1
    assert checked > 0, "no fenced `fly deploy` command in any doc: the check is vacuous"
