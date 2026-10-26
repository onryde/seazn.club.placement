"""Pins on ``.github/workflows/*.yml``: who may write packages, what publishes,
which image tag exists, which actions run and what the buf gate is given, plus
the one Dockerfile line that links the image to this repository.

These are pure text reads. There is no YAML library in this project's
dependencies and none is wanted: the pins below are about literal lines
(a permission, a tag, a ``needs:`` list) whose silent drift is the failure.
Comment lines are stripped first, so a comment that mentions ``packages: write``
or the old repo layout counts for nothing. Jobs are split on
``^  ([a-z0-9-]+):$`` under ``jobs:``.

Every sweep reports what it scanned and a count of zero is a FAILURE
(seazn.club's TEST-STRATEGY.md, see AGENTS.md, "Paths in comments": anti-vacuity).
Floors are derived from the files, never typed in.
"""

import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
JOB_HEADER = re.compile(r"^  ([a-z0-9-]+):[ \t]*$", re.MULTILINE)

IMAGE_REPO = "ghcr.io/onryde/seazn-placement"
SOURCE_LABEL = "org.opencontainers.image.source=https://github.com/onryde/seazn.club.placement"
LABEL_KEY, _, SOURCE_URL = SOURCE_LABEL.partition("=")
DOCKERFILE = REPO_ROOT / "Dockerfile"
TAR_PATH = "${{ runner.temp }}/placement-image.tar"
TESTED_TAG = "placement-service:ci"
# Assembled from pieces so tests/test_repo_layout.py's sweep does not match this file.
OLD_SERVICE_DIR = "services/" "placement"
DESELECT_RE = re.compile(r"--deselect (tests/[\w/.]+\.py)::(\w+)")

# Every action any workflow here may use. Equality, not subset: an unused entry
# is stale and an unlisted action is unreviewed. A workflow that needs another
# action adds it here, pinned, in the same change.
ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "astral-sh/setup-uv",
    "actions/upload-artifact",
    "actions/download-artifact",
    "bufbuild/buf-action",
    "docker/setup-buildx-action",
    "docker/build-push-action",
    "docker/login-action",
    "superfly/flyctl-actions/setup-flyctl",
}
USES_LINE = re.compile(r"^\s*(?:- )?uses:")
USES_PINNED = re.compile(r"^\s*(?:- )?uses: ([\w.-]+/[\w.-]+(?:/[\w.-]+)?)@([0-9a-f]{40}) # v\d+(?:\.\d+){0,2}$")
EXPECTED_TIMEOUTS = {"test": 30, "docker": 20, "buf-breaking": 10, "publish": 10}


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


def _step_using(block, action):
    found = [s for s in _steps(block) if re.search(rf"uses: {re.escape(action)}@", s)]
    assert len(found) == 1, f"expected exactly one {action} step, found {len(found)}"
    return found[0]


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
    # Exact, not "contains both": `always() && ...` and `!cancelled() && ...` keep
    # both substrings yet run the job after a failed gate (or a cancelled run).
    assert condition.group(1) == "github.event_name == 'push' && github.ref == 'refs/heads/main'", condition.group(1)
    for status_function in ("always(", "cancelled(", "failure("):
        assert status_function not in publish, f"{status_function} in publish lets it run past a failed gate"
    needs = re.search(r"^    needs: \[(.*)\]$", publish, re.MULTILINE)
    assert needs, "publish has no inline 'needs: [...]'"
    assert {"test", "docker", "buf-breaking"} <= {n.strip() for n in needs.group(1).split(",")}


def test_only_publish_logs_in_and_pushes():
    code, jobs = _ci()
    assert _count("docker/login-action", code) == 1 and "docker/login-action" in jobs["publish"]
    # The only credential in the file is the job's own token, and only the login step reads it.
    assert re.findall(r"secrets\.\w+", code) == ["secrets.GITHUB_TOKEN"], re.findall(r"secrets\.\w+", code)
    assert "secrets.GITHUB_TOKEN" in jobs["publish"]
    assert "github.token" not in code, "an alias of the job token would dodge the count above"
    assert "registry: ghcr.io" in jobs["publish"]
    # build-push-action never pushes. The one push is `docker push` in the
    # publish script, and nothing but the publish job runs that script.
    assert "push: true" not in code
    assert "push: false" in jobs["docker"]
    assert [job for job, block in jobs.items() if "scripts/publish-image.sh" in block] == ["publish"]


# --- the one image: built once, tested, then pushed ----------------------------


def test_image_is_the_sha_tag_only():
    code, jobs = _ci()
    sha_tag = f"{IMAGE_REPO}:${{{{ github.sha }}}}"
    assert _count(sha_tag, code) == 1 and sha_tag in jobs["publish"], "the sha tag must appear once, in publish"
    # EVERY mention of the image name carries the sha tag. A bare name pushes an
    # implicit :latest, so it is checked on the name, not on a following colon.
    assert _count("seazn-placement", code) == 1
    assert re.findall(r"seazn-placement(?!:\$\{\{ github\.sha \}\})", code) == []
    assert not re.search(r"(?<![-\w])latest\b", code), "no 'latest' anywhere (ubuntu-latest excepted)"
    assert "docker/metadata-action" not in code, "metadata-action derives extra tags (latest, branch)"


def test_image_links_to_this_repo():
    code, jobs = _ci()
    assert _count(SOURCE_LABEL, code) == 1, "the source label must appear exactly once"
    # The label is baked into the image the docker job builds and tests; the
    # publish job pushes that image, it does not build one.
    build = _step_using(jobs["docker"], "docker/build-push-action")
    assert re.search(rf"^          labels: {re.escape(SOURCE_LABEL)}$", build, re.MULTILINE), build


# --- the Dockerfile says it too -----------------------------------------------
#
# ci.yml passes the label to the build, but the image is only as good as the
# build that made it: a Dockerfile that carries the label itself keeps it
# whoever builds the image, and scripts/publish-image.sh refuses an image without
# it.


def _final_stage_labels(dockerfile):
    """Dockerfile text -> (instruction count, FROM count, {label key: [values...]})
    for the LABEL instructions of the LAST stage, the one that becomes the image.

    Comment lines are skipped and a trailing backslash joins the next line, as
    docker reads them. ``LABEL k=v`` and ``LABEL k="v"`` are the same label."""
    instructions, pending = [], ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if line.startswith("#") or (not line and not pending):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        keyword, _, rest = (pending + line).partition(" ")
        instructions.append((keyword.upper(), rest.strip()))
        pending = ""
    froms = [i for i, (keyword, _) in enumerate(instructions) if keyword == "FROM"]
    labels = {}
    if froms:
        for keyword, rest in instructions[froms[-1] + 1 :]:
            if keyword != "LABEL":
                continue
            for token in shlex.split(rest):
                key, equals, value = token.partition("=")
                assert equals, f"LABEL token without '=': {token!r}"
                labels.setdefault(key, []).append(value)
    return len(instructions), len(froms), labels


def test_the_dockerfile_labels_the_image_with_this_repo():
    count, froms, labels = _final_stage_labels(DOCKERFILE.read_text())
    # Anti-vacuity: it read a Dockerfile with a base image and instructions.
    assert froms >= 1 and count > froms, (count, froms)
    assert labels.get(LABEL_KEY) == [SOURCE_URL], labels


def test_the_dockerfile_label_reader_sees_only_the_final_stage_and_real_instructions():
    # The cases that make the test above mean something: each of these must NOT
    # read as "the image carries the label", and the two spellings that do must.
    url = SOURCE_URL
    cases = {
        "commented out": (f"FROM a\n# LABEL {LABEL_KEY}={url}\nRUN x\n", {}),
        "before the final FROM": (f"FROM a\nLABEL {LABEL_KEY}={url}\nFROM b\nRUN x\n", {}),
        "no label at all": ("FROM a\nRUN x\n", {}),
        "no FROM at all": (f"LABEL {LABEL_KEY}={url}\n", {}),
        "the wrong value": (f"FROM a\nLABEL {LABEL_KEY}={url}.git\n", {LABEL_KEY: [url + ".git"]}),
        "quoted": (f'FROM a\nLABEL {LABEL_KEY}="{url}"\n', {LABEL_KEY: [url]}),
        "unquoted": (f"FROM a\nLABEL {LABEL_KEY}={url}\n", {LABEL_KEY: [url]}),
        "in the final stage of two": (f"FROM a\nFROM b\nLABEL {LABEL_KEY}={url}\n", {LABEL_KEY: [url]}),
        "continued": (f"FROM a\nLABEL x=y \\\n  {LABEL_KEY}={url}\n", {"x": ["y"], LABEL_KEY: [url]}),
        # docker drops a comment line whole: its trailing backslash continues nothing,
        # so the LABEL on the next line is a real instruction, not part of the comment.
        "comment ending in a backslash": (f"FROM a\n# note \\\nLABEL {LABEL_KEY}={url}\n", {LABEL_KEY: [url]}),
    }
    for name, (text, expected) in cases.items():
        assert _final_stage_labels(text)[2] == expected, name


def test_docker_exports_the_tested_image_and_publish_pushes_that_one():
    _, jobs = _ci()
    docker, publish = jobs["docker"], jobs["publish"]

    build = _step_using(docker, "docker/build-push-action")
    assert f"          outputs: type=docker,dest={TAR_PATH}\n" in build, build
    assert f"          tags: {TESTED_TAG}\n" in build, build
    assert "          load:" not in build, "load: true conflicts with an exported tar"
    assert "          push: false\n" in build

    # The smoke steps run on the loaded tar, i.e. on the bytes that are exported.
    load = [i for i, s in enumerate(_steps(docker)) if 'docker load --input "$RUNNER_TEMP/placement-image.tar"' in s]
    runs = [i for i, s in enumerate(_steps(docker)) if "docker run" in s]
    assert len(load) == 1 and runs and load[0] < min(runs), (load, runs)

    upload = _step_using(docker, "actions/upload-artifact")
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in upload, upload
    assert f"          path: {TAR_PATH}\n" in upload and "if-no-files-found: error" in upload, upload
    # The tar is the whole image: a day is enough for the publish job of this run
    # and no longer than that is wanted (a copy of the image outlives the run).
    assert "          retention-days: 1\n" in upload, upload
    # The tar is already layer-compressed: zipping it again spends CPU for almost nothing.
    assert "          compression-level: 0\n" in upload, upload
    artifact = re.search(r"^          name: (\S+)$", upload, re.MULTILINE).group(1)

    # publish takes that artifact; it builds nothing.
    assert "docker/build-push-action" not in publish and "docker/setup-buildx-action" not in publish
    download = _step_using(publish, "actions/download-artifact")
    assert f"          name: {artifact}\n" in download and "          path: ${{ runner.temp }}\n" in download, download

    steps = _steps(publish)
    index = {
        "download": next(i for i, s in enumerate(steps) if "download-artifact" in s),
        "login": next(i for i, s in enumerate(steps) if "docker/login-action" in s),
        "push": next(i for i, s in enumerate(steps) if "scripts/publish-image.sh" in s),
    }
    assert index["download"] < index["push"] and index["login"] < index["push"], index
    push = steps[index["push"]]
    assert 'run: bash scripts/publish-image.sh "$TAR" "$TESTED" "$IMAGE"' in push, push
    assert f"          TAR: {TAR_PATH}\n" in push and f"          TESTED: {TESTED_TAG}\n" in push, push
    assert f"{IMAGE_REPO}:${{{{ github.sha }}}}" in publish


def test_every_checkout_drops_its_credentials():
    # The persisted token would sit in .git/config; only buf-breaking needs git
    # history, and nothing here pushes with it.
    checked = 0
    for name, text in _workflows().items():
        for job, block in _jobs(_code(text)).items():
            for step in _steps(block):
                if "actions/checkout@" in step:
                    checked += 1
                    assert "persist-credentials: false" in step, f"{name}:{job}: {step}"
    assert checked == sum(_count("actions/checkout@", _code(t)) for t in _workflows().values()) > 0


def test_dockerignore_keeps_git_out_of_the_build_context():
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert ".git" in lines, "the build context would carry .git (and any credential in its config)"


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
    expected = 0
    for name, text in _workflows().items():
        code = _code(text)
        expected += len(re.findall(r"^\s*(?:- )?run:", code, re.MULTILINE))
        for job, block in _jobs(code).items():
            for body in _run_bodies(block):
                checked += 1
                assert "${{" not in body, f"{name}:{job}: expression inside a run block:\n{body}"
    assert checked == expected > 0, (checked, expected)


def test_buf_is_pinned_once_with_its_checksum_and_both_users_take_it_from_env():
    code, jobs = _ci()
    env = _top_block(code, "env")
    # The two tools ci.yml installs from a pinned release, each a version and a checksum.
    # (uv's half is held by tests/test_locked_install.py.)
    assert re.findall(r"^  (\w+):", env, re.MULTILINE) == [
        "BUF_VERSION", "BUF_SHA256_LINUX_X86_64", "UV_VERSION", "UV_SHA256_LINUX_X86_64",
    ], env
    assert re.search(r'^  BUF_VERSION: "\d+\.\d+\.\d+"$', env, re.MULTILINE), env
    # sha256 of buf-Linux-x86_64 from that release's sha256.txt; buf-action
    # verifies the binary it installs against it. Bump the two together.
    assert re.search(r'^  BUF_SHA256_LINUX_X86_64: "[0-9a-f]{64}"$', env, re.MULTILINE), env
    users = {job: block for job, block in jobs.items() if "bufbuild/buf-action" in block}
    assert sorted(users) == ["buf-breaking", "test"], sorted(users)
    for job, block in users.items():
        assert _count("bufbuild/buf-action@", block) == 1, job
        assert "setup_only: true" in block, f"{job}: buf-action must only install buf"
        assert "version: ${{ env.BUF_VERSION }}" in block, job
        assert "checksum: ${{ env.BUF_SHA256_LINUX_X86_64 }}" in block, job


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
    build = _step_using(jobs["docker"], "docker/build-push-action")
    for line in ("context: .", "file: Dockerfile", "push: false"):
        assert re.search(rf"^          {re.escape(line)}$", build, re.MULTILINE), line
    assert "docker run --rm placement-service:ci" in jobs["docker"]
    assert "Container starts and the port accepts a TCP connection" in jobs["docker"]


def _logical_lines(body):
    """A run body's lines with backslash continuations joined."""
    return re.sub(r"\\\n\s*", " ", body).splitlines()


def test_nothing_builds_or_pushes_an_image_outside_the_publish_script():
    # The docker job builds the one image (build-push-action, push: false) and the
    # publish script pushes it. A `docker build` or `docker push` typed into any
    # run block would be a second image, or a push that skips the sha-tag guard.
    forbidden = re.compile(r"\bdocker\s+(?:buildx\s+build|build|(?:image\s+)?push)\b")
    checked = 0
    expected = 0
    for name, text in _workflows().items():
        code = _code(text)
        expected += len(re.findall(r"^\s*(?:- )?run:", code, re.MULTILINE))
        for job, block in _jobs(code).items():
            for body in _run_bodies(block):
                checked += 1
                for line in _logical_lines(body):
                    assert not forbidden.search(line), f"{name}:{job}: {line.strip()}"
    assert checked == expected > 0, (checked, expected)
    # Positive pair: the push the sweep above forbids elsewhere lives in the script.
    script = (REPO_ROOT / "scripts" / "publish-image.sh").read_text()
    assert re.search(r"^docker push ", script, re.MULTILINE)


# `docker run` options that take a value, so that value is not read as the image.
DOCKER_RUN_VALUE_FLAGS = {"-e", "--env", "-p", "--publish", "--name", "-v", "--volume", "--entrypoint", "-w", "--workdir", "--network", "-u", "--user"}


def test_every_docker_run_runs_the_image_that_was_built_here():
    _, jobs = _ci()
    docker = jobs["docker"]
    images = []
    for body in _run_bodies(docker):
        for line in _logical_lines(body):
            for command in line.split("docker run")[1:]:
                tokens = command.split()
                index = 0
                while index < len(tokens) and tokens[index].startswith("-"):
                    index += 2 if tokens[index] in DOCKER_RUN_VALUE_FLAGS else 1
                assert index < len(tokens), f"docker run names no image: {command.strip()}"
                images.append(tokens[index])
    # Derived: every `docker run` in the block was parsed, and there is at least one.
    assert len(images) == docker.count("docker run") > 0, (len(images), docker.count("docker run"))
    # The image argument itself, not any mention of the tag on the same line.
    assert images == [TESTED_TAG] * len(images), images
    # Nothing outside the docker job runs a container.
    assert [job for job, block in jobs.items() if "docker run" in block] == ["docker"]


# --- actions, triggers, limits and layout ------------------------------------------------


def test_every_action_is_sha_pinned_and_on_the_allowlist():
    found = []
    expected = 0
    for name, text in _workflows().items():
        for number, line in enumerate(_code(text).splitlines(), 1):
            if not USES_LINE.match(line):
                continue
            expected += 1
            match = USES_PINNED.match(line)
            assert match, f"{name}:{number}: not '@<40-hex sha> # vN[.N.N]': {line.strip()}"
            found.append(match.group(1))
    assert len(found) == expected > 0, (len(found), expected)
    assert set(found) == ALLOWED_ACTIONS, (sorted(set(found) - ALLOWED_ACTIONS), sorted(ALLOWED_ACTIONS - set(found)))


def test_triggers_and_concurrency():
    code, _ = _ci()
    on = _top_block(code, "on")
    assert set(re.findall(r"^  (\w+):", on, re.MULTILINE)) == {"pull_request", "push"}, on
    assert re.search(r"^  pull_request:\n", on, re.MULTILINE), on
    assert re.search(r"^  push:\n    branches: \[main\]\n", on, re.MULTILINE), on
    assert "pull_request_target" not in code, "pull_request_target hands fork PRs a write token"
    concurrency = _top_block(code, "concurrency")
    # A push is keyed on its own sha: a newer push must not cancel or replace a
    # pending run, or a middle commit never gets its image.
    assert "group: placement-ci-${{ github.event_name == 'push' && github.sha || github.ref }}" in concurrency
    # Cancelling a push-to-main run would abandon a publish half way.
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in concurrency


def test_every_job_has_a_timeout():
    _, jobs = _ci()
    found = {job: re.findall(r"^    timeout-minutes: (\d+)$", block, re.MULTILINE) for job, block in jobs.items()}
    assert {job: [int(v) for v in values] for job, values in found.items()} == {
        job: [minutes] for job, minutes in EXPECTED_TIMEOUTS.items()
    }, found


def test_no_old_layout_paths():
    scanned = 0
    expected = 0
    for name, text in _workflows().items():
        expected += len([line for line in text.splitlines() if not line.lstrip().startswith("#")])
        for number, line in enumerate(_code(text).splitlines(), 1):
            scanned += 1
            assert OLD_SERVICE_DIR not in line, f"{name}:{number}: {line.strip()}"
            assert "working-directory" not in line, f"{name}:{number}: the repo root is the service"
    assert scanned == expected > 0, (scanned, expected)
