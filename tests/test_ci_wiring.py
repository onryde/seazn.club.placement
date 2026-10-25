"""Pins on ``.github/workflows/*.yml``: who may write packages, what publishes,
which image tag exists, and what the buf gate is given.

These are pure text reads. There is no YAML library in this project's
dependencies and none is wanted: the pins below are about literal lines
(a permission, a tag, a ``needs:`` list) whose silent drift is the failure.
Comment lines are stripped first, so a comment that mentions ``packages: write``
or the old repo layout counts for nothing. Jobs are split on
``^  ([a-z0-9-]+):$`` under ``jobs:``.

Every sweep reports what it scanned and a count of zero is a FAILURE
(docs/superpowers/TEST-STRATEGY.md, anti-vacuity).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
JOB_HEADER = re.compile(r"^  ([a-z0-9-]+):[ \t]*$", re.MULTILINE)

IMAGE_REPO = "ghcr.io/onryde/seazn-placement"
SOURCE_LABEL = "org.opencontainers.image.source=https://github.com/onryde/seazn.club.placement"
# Assembled from pieces so tests/test_repo_layout.py's sweep does not match this file.
OLD_SERVICE_DIR = "services/" "placement"
DESELECT_RE = re.compile(r"--deselect (tests/[\w/.]+\.py)::(\w+)")


def _code(text):
    """The workflow without its whole-line comments."""
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*#", line)) + "\n"


def _workflows():
    return {path.name: path.read_text() for path in sorted(WORKFLOWS_DIR.glob("*.yml"))}


def _jobs(code):
    """Job id -> its block, in file order."""
    _, _, after_jobs = code.partition("\njobs:\n")
    assert after_jobs, "no 'jobs:' key found"
    headers = list(JOB_HEADER.finditer(after_jobs))
    jobs = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(after_jobs)
        jobs[header.group(1)] = after_jobs[header.start():end]
    return jobs


def _top_block(code, key):
    match = re.search(rf"^{key}:\n((?:[ \t]+.*\n)+)", code, re.MULTILINE)
    assert match, f"no top-level '{key}:' block"
    return match.group(1)


def _steps(block):
    _, _, after_steps = block.partition("    steps:\n")
    assert after_steps, "job has no steps"
    parts = re.split(r"(?m)^      - ", after_steps)
    return ["      - " + part for part in parts[1:]]


def _run_bodies(block):
    """Every ``run:`` value in the job: the key line and its deeper-indented lines."""
    bodies = []
    lines = block.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:", line)
        if not match:
            continue
        indent = len(match.group(1))
        body = [line]
        for follower in lines[index + 1:]:
            if follower.strip() and len(follower) - len(follower.lstrip()) <= indent:
                break
            body.append(follower)
        bodies.append("\n".join(body))
    return bodies


def _ci():
    workflows = _workflows()
    assert "ci.yml" in workflows, f"no ci.yml among {sorted(workflows)}"
    code = _code(workflows["ci.yml"])
    return code, _jobs(code)


def _count(needle, text):
    return text.count(needle)


# --- coverage of the sweep itself ------------------------------------------


def test_scans_the_workflows():
    workflows = _workflows()
    assert len(workflows) >= 1, "no workflow files were read; every pin below would be vacuous"
    _, jobs = _ci()
    assert list(jobs) == ["test", "docker", "buf-breaking", "publish"], list(jobs)


# --- who may write packages --------------------------------------------------


def test_only_publish_can_write_packages():
    workflows = _workflows()
    writes = []
    for name, text in workflows.items():
        for line in _code(text).splitlines():
            if re.search(r":\s*write\b", line):
                writes.append((name, line.strip()))
    assert writes == [("ci.yml", "packages: write")], writes

    code, jobs = _ci()
    assert "packages: write" in jobs["publish"], "packages: write must sit inside the publish job"
    assert _top_block(code, "permissions").split() == ["contents:", "read"]
    # The publish job narrows nothing it does not need: contents stays read.
    assert re.search(r"^    permissions:\n      contents: read\n      packages: write\n", jobs["publish"], re.MULTILINE)


def test_publish_runs_only_on_main_after_every_gate():
    _, jobs = _ci()
    publish = jobs["publish"]
    condition = re.search(r"^    if: (.+)$", publish, re.MULTILINE)
    assert condition, "publish has no job-level 'if:'"
    condition = condition.group(1)
    assert "github.event_name == 'push'" in condition, condition
    assert "github.ref == 'refs/heads/main'" in condition, condition
    assert "||" not in condition, f"an OR would let a pull request publish: {condition}"
    needs = re.search(r"^    needs: \[(.*)\]$", publish, re.MULTILINE)
    assert needs, "publish has no inline 'needs: [...]'"
    assert {"test", "docker", "buf-breaking"} <= {n.strip() for n in needs.group(1).split(",")}


def test_only_publish_logs_in_and_pushes():
    code, jobs = _ci()
    assert _count("docker/login-action", code) == 1 and "docker/login-action" in jobs["publish"]
    assert _count("secrets.GITHUB_TOKEN", code) == 1 and "secrets.GITHUB_TOKEN" in jobs["publish"]
    assert _count("push: true", code) == 1 and "push: true" in jobs["publish"]
    assert "push: false" in jobs["docker"] and "push: true" not in jobs["docker"]
    assert "registry: ghcr.io" in jobs["publish"]


# --- the one image ------------------------------------------------------------


def test_image_is_the_sha_tag_only():
    code, jobs = _ci()
    sha_tag = f"{IMAGE_REPO}:${{{{ github.sha }}}}"
    assert _count(sha_tag, code) == 1 and sha_tag in jobs["publish"], "the sha tag must appear once, in publish"
    # Every reference to the image name, whatever follows it, is the sha tag.
    tags = re.findall(r"seazn-placement:(\$\{\{ github\.sha \}\}|\S*)", code)
    assert tags == ["${{ github.sha }}"], tags
    assert not re.search(r"(?<![-\w])latest\b", code), "no 'latest' anywhere (ubuntu-latest excepted)"
    assert "docker/metadata-action" not in code, "metadata-action derives extra tags (latest, branch)"
    assert not re.search(r"seazn-placement:(main|master|latest)", code)


def test_image_links_to_this_repo():
    code, jobs = _ci()
    assert _count(SOURCE_LABEL, code) == 1, "the source label must appear exactly once"
    assert re.search(rf"^          labels: {re.escape(SOURCE_LABEL)}$", jobs["publish"], re.MULTILINE), (
        "the label belongs under 'labels:' of the publish build step"
    )


def test_publish_reports_the_pushed_digest():
    _, jobs = _ci()
    publish = jobs["publish"]
    assert re.search(r"^        id: build$", publish, re.MULTILINE)
    assert "DIGEST: ${{ steps.build.outputs.digest }}" in publish
    # An empty digest must fail the job, not print an empty line.
    assert re.search(r'test -n "\$DIGEST"', publish) and "::error::" in publish


# --- what the buf gate is given -------------------------------------------------


def test_buf_job_has_full_history():
    _, jobs = _ci()
    block = jobs["buf-breaking"]
    assert re.search(r"^          fetch-depth: 0$", block, re.MULTILINE), (
        "the buf-breaking checkout needs full history, or the base ref is not in the clone"
    )
    steps = _steps(block)
    on_pull_request = [s for s in steps if "origin/${{ github.base_ref }}" in s]
    on_push = [s for s in steps if "${{ github.event.before }}" in s]
    assert len(on_pull_request) == 1, on_pull_request
    assert len(on_push) == 1, on_push
    assert "if: github.event_name == 'pull_request'" in on_pull_request[0]
    assert "if: github.event_name == 'push'" in on_push[0]
    for step in (on_pull_request[0], on_push[0]):
        assert 'run: bash scripts/buf-breaking.sh "$BASE"' in step, step
        assert re.search(r"^          BASE: ", step, re.MULTILINE), step


def test_no_expression_is_interpolated_into_a_run_block():
    # The base ref reaches the script through env, never through the shell text.
    checked = 0
    for name, text in _workflows().items():
        for job, block in _jobs(_code(text)).items():
            for body in _run_bodies(block):
                checked += 1
                assert "${{" not in body, f"{name}:{job}: expression inside a run block:\n{body}"
    assert checked >= 5, f"only {checked} run blocks were scanned"


def test_buf_is_pinned_once_and_both_users_take_it_from_env():
    code, jobs = _ci()
    env = _top_block(code, "env")
    assert re.findall(r'^  (\w+):', env, re.MULTILINE) == ["BUF_VERSION"], env
    assert re.search(r'^  BUF_VERSION: "\d+\.\d+\.\d+"$', env, re.MULTILINE), env
    users = {job: block for job, block in jobs.items() if "bufbuild/buf-action" in block}
    assert sorted(users) == ["buf-breaking", "test"], sorted(users)
    for job, block in users.items():
        assert _count("bufbuild/buf-action@v1", block) == 1, job
        assert "setup_only: true" in block, f"{job}: buf-action must only install buf"
        assert "version: ${{ env.BUF_VERSION }}" in block, job


def test_the_test_job_installs_buf_before_pytest():
    _, jobs = _ci()
    steps = _steps(jobs["test"])
    install = [i for i, s in enumerate(steps) if "bufbuild/buf-action" in s]
    pytest_steps = [i for i, s in enumerate(steps) if "python -m pytest" in s]
    assert len(install) == 1 and len(pytest_steps) == 2, (install, pytest_steps)
    assert install[0] < min(pytest_steps), "buf must be on PATH before the first pytest step"


# --- the pytest job --------------------------------------------------------------


def test_blocking_pytest_deselects_exactly_the_load_sensitive_test():
    _, jobs = _ci()
    steps = _steps(jobs["test"])
    blocking = [s for s in steps if "python -m pytest tests/ -q" in s]
    advisory = [s for s in steps if "continue-on-error: true" in s]
    assert len(blocking) == 1 and len(advisory) == 1, (len(blocking), len(advisory))
    assert "continue-on-error" not in blocking[0]
    deselected = DESELECT_RE.findall(blocking[0])
    assert len(deselected) == 1, deselected
    path, name = deselected[0]
    # The deselect must name a test that exists, or it silently deselects nothing.
    assert re.search(rf"^def {name}\(", (REPO_ROOT / path).read_text(), re.MULTILINE), (path, name)
    # The advisory step re-runs that one test and nothing else.
    assert f"python -m pytest {path}::{name} -v" in advisory[0]


# --- the docker job -----------------------------------------------------------------


def test_docker_job_builds_the_root_dockerfile_without_pushing():
    _, jobs = _ci()
    block = jobs["docker"]
    for line in ("context: .", "file: Dockerfile", "push: false", "load: true", "tags: placement-service:ci"):
        assert re.search(rf"^          {re.escape(line)}$", block, re.MULTILINE), line
    assert "docker run --rm placement-service:ci" in block
    assert "Container starts and the port accepts a TCP connection" in block


# --- triggers and layout ---------------------------------------------------------------


def test_triggers_and_concurrency():
    code, _ = _ci()
    on = _top_block(code, "on")
    assert re.search(r"^  pull_request:\n", on, re.MULTILINE), on
    assert re.search(r"^  push:\n    branches: \[main\]\n", on, re.MULTILINE), on
    assert "pull_request_target" not in code, "pull_request_target hands fork PRs a write token"
    concurrency = _top_block(code, "concurrency")
    assert "group: placement-ci-${{ github.ref }}" in concurrency
    # Cancelling a push-to-main run would abandon a publish half way.
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in concurrency


def test_no_old_layout_paths():
    scanned = 0
    for name, text in _workflows().items():
        for number, line in enumerate(_code(text).splitlines(), 1):
            scanned += 1
            assert OLD_SERVICE_DIR not in line, f"{name}:{number}: {line.strip()}"
            assert "working-directory" not in line, f"{name}:{number}: the repo root is the service"
    assert scanned > 50, f"only {scanned} workflow lines were scanned"
