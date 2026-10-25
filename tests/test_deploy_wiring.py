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
  value is interpolated into a ``run:`` block.

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
# Verbatim from the workflows these were ported from (seazn.club's placement-stg.yml
# and placement-prod.yml): the log-attribution reminder and the machine-count reminder.
VERIFY_BY_LOG_ATTRIBUTION = (
    "echo \"::notice::VERIFY BY LOG ATTRIBUTION, not by reading the env var: run one Auto-schedule on "
    "stg.seazn.club, then 'fly logs --app placement-stg' must show a 'solve request=' line and "
    "'fly logs --app placement' must show NOTHING for it. A misspelled PLACEMENT_SERVICE_HOST on "
    "seazn-club-stg silently routes staging to production.\""
)
MACHINE_COUNT_REMINDER = (
    "echo \"::notice::DEPLOY.md step 5 sets 'fly scale count 10'. If the count above is not 10, the "
    "fan-out that makes hard_limit=1 useful is not in place.\""
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
    # The gate is exactly these four, no more and no fewer. `event == push` is what
    # keeps a fork's pull request (whose head_branch may be called main) out, and
    # the repository check keeps any other repository's run out.
    assert set(condition.split(" && ")) == {
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    }, condition


def test_stg_checks_out_and_deploys_the_commit_ci_ran_on():
    code, _, job = _deploy("stg.yml")
    head_sha = "${{ github.event.workflow_run.head_sha }}"
    # On workflow_run, github.sha is the default branch's head, not the tested commit.
    assert "github.sha" not in code, "github.sha is the wrong commit on a workflow_run"
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


def test_the_app_name_table_has_both_outcomes():
    assert {ok for _, ok in APP_LINES.values()} == {True, False}
    assert len(APP_LINES) >= 6


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
    script = (SCRIPTS_DIR / "require-main-tip.sh").read_text()
    key = re.search(r'echo "(\w+)=\$\w+" >> "\$GITHUB_OUTPUT"', script).group(1)
    gate = f"steps.tip.outputs.{key} == 'true'"
    assert _index_of(job, "scripts/require-image.sh") < _index_of(job, "scripts/require-main-tip.sh")
    assert _index_of(job, "scripts/require-main-tip.sh") < _index_of(job, "flyctl deploy")
    # Everything that acts on the deployment waits for the answer; the guard itself
    # and the cheap checks before it do not.
    gated = [s for s in _steps(job) if gate in s]
    assert len(gated) == 3, [s.splitlines()[0] for s in gated]
    for needle, condition in (
        ("flyctl deploy", f"if: {gate}"),
        ("require-private-ips.py", f"if: {gate}"),
        ("What to verify by hand", f"if: always() && {gate}"),
    ):
        step = _step_with(job, needle)
        assert f"        {condition}\n" in step, (needle, step)
    # prod has no such guard: a tag is a deliberate act.
    _, _, prod = _deploy("prod.yml")
    assert "require-main-tip" not in prod and "steps.tip" not in prod


# --- credentials, permissions, limits --------------------------------------------------------------


def test_deploys_use_only_FLY_TOKEN_and_only_where_flyctl_runs():
    checked = 0
    for name in DEPLOY_WORKFLOWS:
        code, _, job = _deploy(name)
        assert re.findall(r"secrets\.\w+", code) and set(re.findall(r"secrets\.\w+", code)) == {"secrets.FLY_TOKEN"}, name
        assert "github.token" not in code and "GITHUB_TOKEN" not in code, f"{name}: an alias of the job token"
        for step in _steps(job):
            body = _script_of(step)
            runs_flyctl = "flyctl" in body or "require-private-ips.py" in body
            has_token = "FLY_API_TOKEN: ${{ secrets.FLY_TOKEN }}" in step
            assert runs_flyctl == has_token, f"{name}: token scope is wrong for step:\n{step}"
            checked += 1
    assert checked > 10


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
    by_hand = _step_with(stg, "What to verify by hand")
    script = _run_script(by_hand)
    assert VERIFY_BY_LOG_ATTRIBUTION in script, script
    assert "flyctl status --app placement-stg || true" in script
    _, _, prod = _deploy("prod.yml")
    report = _step_with(prod, "Report machine count")
    assert "        if: always()\n" in report, report
    assert MACHINE_COUNT_REMINDER in _run_script(report)
    assert 'flyctl status --app "$APP" || true' in _run_script(report)


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
