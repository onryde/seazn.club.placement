"""Pins on where the dependencies of the image and of the CI test job come from.

``uv.lock`` is committed, and until this file's change nothing installed from it:
the Dockerfile ran ``pip install .`` and the test job ran ``pip install -e
".[dev]"``, so every build resolved whatever PyPI had that day. A bad upstream
release would have reached the image, and the stg deploy after it, untested.

What is pinned, because each of these can drift with every other test green:

* the Dockerfile exports the lock (``uv export --frozen``, hashes kept, no extra) to
  a file and installs that file with ``pip install --require-hashes``, then installs
  this package with ``--no-deps``; the export's output path is the install's input
  path, the two are chained with ``&&`` (never piped, never ``||``), and the uv they
  run is the one from the digest-pinned stage and no other image;
* the test job does the same with the ``dev`` extra, after ``uv lock --check``, in
  three steps that carry no ``if:``, ``continue-on-error``, ``shell:`` or ``env:``,
  in a job that carries none either;
* nothing anywhere runs a ``pip install`` or a ``uv`` command that can resolve;
* the uv that ci.yml installs is the release the Dockerfile's stage names;
* ``uv.lock`` records the requirements ``pyproject.toml`` declares (the offline half
  of ``uv lock --check``, so a stale lock is red in a plain pytest run too).

These are text reads (there is no YAML library in this project's dependencies and
none is wanted). The helpers are duplicated from ``tests/test_ci_wiring.py`` on
purpose, so this file stands alone. Every sweep reports what it scanned and a count
of zero is a FAILURE (seazn.club's TEST-STRATEGY.md, see AGENTS.md, "Paths in
comments": anti-vacuity). Expected values come from pyproject.toml and from what the
tools document, never from the files under test.
"""

import fnmatch
import json
import re
import shlex
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CI_YML = WORKFLOWS_DIR / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"

JOB_HEADER = re.compile(r"^  ([a-z0-9-]+):[ \t]*$", re.MULTILINE)
UV_IMAGE = re.compile(r"^ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+)@sha256:([0-9a-f]{64})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# uv export, as the Dockerfile runs it: no other flag. An allowlist rather than a list
# of banned flags, so --no-hashes, --extra, --all-extras, --all-groups, --no-header,
# --prune ... each need a reviewed change to this table.
DOCKER_EXPORT_FLAGS = {"--frozen", "--no-dev", "--no-emit-project", "--quiet", "--format", "--output-file"}
# ... and as the test job runs it: the same, plus the extras pyproject.toml declares.
CI_EXPORT_FLAGS = {"--frozen", "--no-emit-project", "--quiet", "--format", "--output-file", "--extra"}
# pip flags allowed on each of the two installs; anything else (--index-url, --pre,
# --no-build-isolation ...) changes what gets installed and needs a reviewed change.
LOCKED_PIP_FLAGS = {"--no-cache-dir", "--require-hashes", "-r", "--requirement", "-q", "--quiet"}
PROJECT_PIP_FLAGS = {"--no-cache-dir", "--no-deps", "-e", "--editable", "-q", "--quiet"}
PIP_VALUE_FLAGS = {"-r", "--requirement", "-e", "--editable"}
OPERATORS = {"&&", "||", ";", ";;", "|", "&"}
PIP_INSTALL = re.compile(r"\bpip3?\s+install\b")
UV_COMMAND = re.compile(r"(?<![\w./=-])uv\s+[a-z]")


# --- reading the files ------------------------------------------------------------------------


def _code(text):
    """The text without its whole-line comments."""
    return "\n".join(line for line in text.splitlines() if not re.match(r"\s*#", line)) + "\n"


def _instructions(dockerfile):
    """Dockerfile text -> [(KEYWORD, rest)]: comment lines skipped, a trailing backslash
    joins the next line, as docker reads them."""
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
    return instructions


def _stages(instructions):
    """[(image, alias or None, [instructions after its FROM])], in file order."""
    stages = []
    for keyword, rest in instructions:
        if keyword == "FROM":
            words = [w for w in shlex.split(rest) if not w.startswith("--")]
            alias = words[2] if len(words) == 3 and words[1].upper() == "AS" else None
            stages.append((words[0], alias, []))
        elif stages:
            stages[-1][2].append((keyword, rest))
    return stages


def _run_shell(rest):
    """(mount flags, shell text) of a RUN instruction. Exec form is joined into a shell line."""
    if rest.startswith("["):
        return [], " ".join(json.loads(rest))
    flags = []
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        flags.append(flag)
        rest = rest.lstrip()
    return flags, rest


def _tokens(line):
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # an open quote that closes on a later line: fall back to words
        return line.split()


def _shell(text):
    """(commands, operators): the commands of a shell text as token lists, and the
    ``&&`` ``||`` ``;`` ``|`` between them in order. A newline separates two commands
    without being an operator."""
    commands, operators = [], []
    for line in re.sub(r"\\\n\s*", " ", text).splitlines():
        current = []
        for token in _tokens(line):
            if token in OPERATORS:
                if current:
                    commands.append(current)
                    current = []
                operators.append(token)
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands, operators


def _flags(args):
    """{flag: value or None} of a command's arguments; --flag=value is split, a flag that
    takes a value (see the callers' tables) is the caller's business. Positional words are ignored."""
    found = {}
    for arg in args:
        if arg.startswith("-"):
            flag, equals, value = arg.partition("=")
            found[flag] = value if equals else None
    return found


def _flag_values(args, flag):
    """Every word that follows ``flag`` (``--flag value`` form) in the arguments."""
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag]


def _pip_installs(commands):
    """The arguments after ``pip install`` for every command that runs one, wherever the
    word ``pip`` sits (``pip``, ``pip3``, ``python -m pip``, ``uv pip``)."""
    found = []
    for command in commands:
        for index, word in enumerate(command[:-1]):
            if Path(word).name in ("pip", "pip3") and command[index + 1] == "install":
                found.append(command[index + 2:])
                break
    return found


def _uv_commands(commands):
    return [c for c in commands if Path(c[0]).name == "uv"]


def _pip_kind(args):
    """'locked' for ``pip install [--no-cache-dir] --require-hashes -r FILE``, 'project'
    for ``pip install [--no-cache-dir] --no-deps (-e) .``, None for anything else, i.e.
    anything that can resolve."""
    flags, files, editables, targets = set(), [], [], []
    index = 0
    while index < len(args):
        arg = args[index]
        value = args[index + 1] if index + 1 < len(args) else None
        if arg in ("-r", "--requirement"):
            flags.add(arg)
            files.append(value)
            index += 2
        elif arg in ("-e", "--editable"):
            flags.add(arg)
            editables.append(value)
            index += 2
        elif arg.startswith("-"):
            flags.add(arg)
            index += 1
        else:
            targets.append(arg)
            index += 1
    if "--require-hashes" in flags and flags <= LOCKED_PIP_FLAGS and len(files) == 1 and files[0] \
            and not editables and not targets:
        return "locked"
    if "--no-deps" in flags and "--require-hashes" not in flags and flags <= PROJECT_PIP_FLAGS \
            and not files and [*editables, *targets] == ["."]:
        return "project"
    return None


def _uv_problem(command):
    """Why a ``uv ...`` command may resolve dependencies, or None when it only reads the lock."""
    sub, args = (command[1] if len(command) > 1 else ""), command[2:]
    if sub == "lock":
        return None if args == ["--check"] else "uv lock is only allowed as exactly `uv lock --check`"
    if sub == "export":
        return None if ("--frozen" in args or "--locked" in args) else "uv export without --frozen/--locked resolves"
    return f"uv {sub or '(nothing)'} may resolve"


def _problems(text):
    """Every way a shell text can install or resolve something outside the lock."""
    commands, _ = _shell(text)
    problems = [f"pip install {' '.join(a)}" for a in _pip_installs(commands) if _pip_kind(a) is None]
    problems += [f"{' '.join(c)}: {p}" for c in _uv_commands(commands) if (p := _uv_problem(c))]
    return problems


def _run_bodies(workflow):
    """The shell text of every ``run:`` in a workflow (whole-line comments dropped)."""
    bodies = []
    lines = _code(workflow).splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        first = "" if match.group(2) in ("|", "|-", "|+", ">", ">-", ">+") else match.group(2)
        body = [first]
        for follower in lines[index + 1:]:
            if follower.strip() and len(follower) - len(follower.lstrip()) <= indent:
                break
            body.append(follower)
        bodies.append("\n".join(body))
    return bodies


def _shell_sources():
    """(label, shell text) for everywhere this repo runs a shell: each RUN of the
    Dockerfile, each ``run:`` of each workflow, each script under scripts/."""
    sources = []
    for number, (keyword, rest) in enumerate(_instructions(DOCKERFILE.read_text())):
        if keyword == "RUN":
            sources.append((f"Dockerfile RUN #{number}", _run_shell(rest)[1]))
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for number, body in enumerate(_run_bodies(path.read_text())):
            sources.append((f"{path.name} run #{number}", body))
    for path in sorted(SCRIPTS_DIR.glob("*.sh")):
        sources.append((f"scripts/{path.name}", _code(path.read_text())))
    return sources


def _jobs(code):
    _, _, after_jobs = code.partition("\njobs:\n")
    assert after_jobs, "no 'jobs:' key found"
    headers = list(JOB_HEADER.finditer(after_jobs))
    return {
        header.group(1): after_jobs[header.start():(headers[i + 1].start() if i + 1 < len(headers) else len(after_jobs))]
        for i, header in enumerate(headers)
    }


def _steps(block):
    _, _, after_steps = block.partition("    steps:\n")
    assert after_steps, "job has no steps"
    return ["      - " + part for part in re.split(r"(?m)^      - ", after_steps)[1:]]


def _step_keys(step):
    """The keys a step itself carries (the first sits after the '- ')."""
    return re.findall(r"(?m)^      (?:- |  )([\w-]+):", step)


def _step_with(step):
    """{key: value} of a step's ``with:`` mapping."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^        with:\s*$", line):
            found = {}
            for follower in lines[index + 1:]:
                if follower.strip() and not follower.startswith("          "):
                    break
                match = re.match(r"^          ([\w-]+): (.*)$", follower)
                if match:
                    found[match.group(1)] = match.group(2)
            return found
    return {}


def _step_script(step):
    bodies = _run_bodies(step)
    assert len(bodies) <= 1, step
    return bodies[0] if bodies else ""


def _ci():
    code = _code(CI_YML.read_text())
    return code, _jobs(code)


def _ci_env():
    code, _ = _ci()
    match = re.search(r"^env:\n((?:[ \t]+.*\n)+)", code, re.MULTILINE)
    assert match, "no top-level 'env:' block"
    return dict(re.findall(r"^  (\w+): \"?([^\"\n]*)\"?$", match.group(1), re.MULTILINE))


def _extras():
    """The optional-dependency groups pyproject.toml declares, i.e. what `--extra` may name."""
    return set(tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"])


def _dockerignore_excludes(name, patterns):
    """True when a .dockerignore pattern list excludes the top-level file ``name``."""
    verdict = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        pattern = pattern.lstrip("!").removeprefix("**/").rstrip("/")
        if pattern and fnmatch.fnmatch(name, pattern):
            verdict = not negated
    return verdict


# --- the resolver sweep: nothing installs or resolves outside the lock ----------------------------

OK_SHELL = {
    "the image's locked install": "pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt",
    "the job's locked install": 'pip install --require-hashes -r "$RUNNER_TEMP/requirements.lock.txt"',
    "the image's project install": "pip install --no-cache-dir --no-deps .",
    "the job's project install": "pip install --no-deps -e .",
    "the lock check": "uv lock --check",
    "an export from the lock": "uv export --frozen --no-emit-project --format requirements-txt --output-file r.txt",
    "an export that must not change the lock": "uv export --locked --output-file r.txt",
    "pip check is not an install": "pip check",
    "a chain of the two installs": "uv export --frozen -o r.txt && pip install --require-hashes -r r.txt && pip install --no-deps .",
    "an install named only in a string": "echo 'pip install .'",
}
BAD_SHELL = {
    "bare pip install .": "pip install .",
    "pip install . without the lock": "pip install --no-cache-dir .",
    "editable with the dev extra": 'pip install -e ".[dev]"',
    "editable without --no-deps": "pip install -e .",
    "python -m pip": "python -m pip install .",
    "pip3": "pip3 install --no-deps . pytest",
    "a package by name": "pip install grpcio",
    "an extra target next to the locked file": "pip install --require-hashes -r r.txt .",
    "a requirements file without hashes": "pip install -r r.txt",
    "--require-hashes on a target": "pip install --require-hashes .",
    "an extra index": "pip install --no-deps --extra-index-url https://example.com/simple .",
    "an extra index next to the locked file": "pip install --require-hashes -r r.txt --extra-index-url https://example.com/simple",
    "a flag nobody reviewed on the locked file": "pip install --require-hashes -r r.txt --no-build-isolation",
    "two requirement files": "pip install --require-hashes -r a.txt -r b.txt",
    "-r without a file": "pip install --require-hashes -r",
    "a project install of something else": "pip install --no-deps ./other",
    "uv pip": "uv pip install --require-hashes -r r.txt",
    "uv sync": "uv sync",
    "uv sync --frozen (not used here, so not allowed)": "uv sync --frozen",
    "uv lock (re-locks)": "uv lock",
    "uv lock --upgrade": "uv lock --upgrade",
    "uv export without --frozen": "uv export --no-emit-project --output-file r.txt",
    "uv run": "uv run pytest",
    "uv add": "uv add grpcio",
    "bare uv": "uv",
    "after &&": "echo hi && pip install .",
    "after ||": "true || pip install --no-cache-dir .",
    "on a later line": "echo hi\npip install .",
    "in a continuation": "pip install \\\n  --no-cache-dir .",
}


def test_the_resolver_sweep_classifies_each_shape():
    # The cases that make the sweep below mean something: it must accept exactly the
    # installs that read the lock and refuse every shape that resolves.
    for name, text in OK_SHELL.items():
        assert _problems(text) == [], name
    for name, text in BAD_SHELL.items():
        assert _problems(text) != [], name
    assert len(OK_SHELL) >= 8 and len(BAD_SHELL) >= 20


def test_nothing_installs_or_resolves_outside_the_lock():
    sources = _shell_sources()
    pip_found = uv_found = pip_raw = uv_raw = 0
    labels = set()
    for label, text in sources:
        labels.add(label.split(" ")[0])
        commands, _ = _shell(text)
        pip_found += len(_pip_installs(commands))
        uv_found += len(_uv_commands(commands))
        pip_raw += len(PIP_INSTALL.findall(text))
        uv_raw += len(UV_COMMAND.findall(text))
        assert _problems(text) == [], f"{label}:\n{text}"
    # Anti-vacuity: it read the Dockerfile, every workflow and every script (the expected set is
    # the directory listing, not a typed list), and parsed every `pip install` and `uv <command>`
    # that a plain regex finds in the same text.
    expected = {"Dockerfile"} | {p.name for p in WORKFLOWS_DIR.glob("*.yml")} | {f"scripts/{p.name}" for p in SCRIPTS_DIR.glob("*.sh")}
    assert labels == expected and len(expected) > 3, (sorted(labels), sorted(expected))
    assert pip_found == pip_raw > 0, (pip_found, pip_raw)
    assert uv_found == uv_raw > 0, (uv_found, uv_raw)


# --- the Dockerfile -------------------------------------------------------------------------------


def _docker():
    """(uv stage, final stage, final-stage instructions) with the shape asserted."""
    stages = _stages(_instructions(DOCKERFILE.read_text()))
    assert len(stages) >= 2, [s[0] for s in stages]
    uv_stage, final = stages[0], stages[-1]
    assert uv_stage[1] == "uv" and not uv_stage[2], "the first stage is the bare uv image, aliased `uv`"
    assert UV_IMAGE.match(uv_stage[0]), f"uv must be pinned by tag and digest: {uv_stage[0]}"
    assert not final[0].startswith("ghcr.io/astral-sh/"), "the image that ships must not be the uv image"
    return uv_stage, final, final[2]


def test_the_dockerfile_installs_the_lock_with_hashes_then_the_project_without_deps():
    uv_stage, _, final = _docker()
    runs = [(index, rest) for index, (keyword, rest) in enumerate(final) if keyword == "RUN"]
    locked = [(i, r) for i, r in runs if any(_pip_kind(a) == "locked" for a in _pip_installs(_shell(_run_shell(r)[1])[0]))]
    project = [(i, r) for i, r in runs if any(_pip_kind(a) == "project" for a in _pip_installs(_shell(_run_shell(r)[1])[0]))]
    assert len(locked) == 1 and len(project) == 1, (len(locked), len(project))
    (locked_at, locked_run), (project_at, project_run) = locked[0], project[0]
    assert locked_at < project_at, "the lock goes in before this package"

    # The locked RUN: uv export, then pip install of the file it wrote. Two commands, one
    # `&&`: no pipe (pipefail is off in /bin/sh, a failed export would vanish), no `||`, no `;`.
    mounts, script = _run_shell(locked_run)
    commands, operators = _shell(script)
    assert operators == ["&&"] and len(commands) == 2, (operators, commands)
    export, install = commands
    assert export[:2] == ["uv", "export"], export
    assert set(_flags(export)) == DOCKER_EXPORT_FLAGS, set(_flags(export)) ^ DOCKER_EXPORT_FLAGS
    assert _flag_values(export, "--format") == ["requirements-txt"], export
    # Producer and consumer of the file are the same path.
    written = _flag_values(export, "--output-file")
    assert len(written) == 1 and written == _flag_values(install, "-r"), (written, install)
    # The runtime image has no extra: pyproject.toml has some (anti-vacuity), none is exported.
    assert _extras(), "pyproject.toml declares no extras, so 'the image takes none' proves nothing"
    assert "--extra" not in export and not any(a.startswith("--extra=") for a in export), export

    # uv comes from the pinned stage, mounted for this one RUN, and from nowhere else.
    assert len(mounts) == 1 and mounts[0].startswith("--mount="), mounts
    mount = dict(part.partition("=")[::2] for part in mounts[0].removeprefix("--mount=").split(","))
    assert mount.get("type") == "bind" and mount.get("from") == uv_stage[1], mount
    assert mount.get("source") == "/uv" and Path(mount.get("target", "")).name == "uv", mount

    # The project RUN: install without deps, then pip check, one `&&`.
    commands, operators = _shell(_run_shell(project_run)[1])
    assert operators == ["&&"] and len(commands) == 2, (operators, commands)
    assert _pip_kind(_pip_installs(commands[:1])[0]) == "project", commands
    # Not editable: the image runs the package from site-packages (see the comment before `USER`),
    # not from a /app/src checkout that an editable install would point at.
    assert not {"-e", "--editable"} & set(commands[0]), commands
    assert commands[1][-2:] == ["pip", "check"] and commands[1][0] in ("pip", "pip3", "python", "python3"), commands

    # Layering, which the comment in the Dockerfile promises: the manifest and the lock are
    # in before the lock install, the source after it and before the project install.
    def copied(rest):
        return [w for w in shlex.split(rest)[:-1] if not w.startswith("--")]

    def copy_at(name):
        return [i for i, (k, r) in enumerate(final) if k == "COPY" and any(c.rstrip("/") == name for c in copied(r))]

    for name in ("pyproject.toml", "uv.lock"):
        assert len(copy_at(name)) == 1 and copy_at(name)[0] < locked_at, (name, copy_at(name), locked_at)
    assert len(copy_at("src")) == 1 and locked_at < copy_at("src")[0] < project_at, (copy_at("src"), locked_at, project_at)


def test_the_dockerfile_uses_the_uv_stage_and_no_other_image_for_anything():
    uv_stage, _, final = _docker()
    uses = [(k, r) for k, r in final if "from=" in r]
    assert len(uses) == 1, uses
    # `from=` names a stage, or else an image: a misspelt alias would pull docker.io/library/<alias>.
    for keyword, rest in uses:
        assert re.search(rf"(?<![\w-])from={re.escape(uv_stage[1])}(?=[,\s])", rest), (keyword, rest)
    assert not [r for k, r in final if k in ("FROM", "ADD") or (k == "COPY" and "--from" in r)]


def test_the_dockerfile_reader_sees_real_instructions_and_stages():
    text = (
        "FROM ghcr.io/astral-sh/uv:1.2.3@sha256:" + "a" * 64 + " AS uv\n"
        "# FROM commented-out AS nothing\n"
        "FROM python:3.14\n"
        "COPY pyproject.toml uv.lock ./\n"
        "RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \\\n"
        "    uv export --frozen \\\n"
        "      --output-file /tmp/r.txt \\\n"
        "    && pip install --require-hashes -r /tmp/r.txt\n"
    )
    stages = _stages(_instructions(text))
    assert [(image.split("@")[0], alias) for image, alias, _ in stages] == [
        ("ghcr.io/astral-sh/uv:1.2.3", "uv"), ("python:3.14", None)], stages
    assert UV_IMAGE.match(stages[0][0]) and not stages[0][2]
    (_, copy), (_, run) = stages[1][2]
    assert copy == "pyproject.toml uv.lock ./"
    mounts, script = _run_shell(run)
    assert mounts == ["--mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv"]
    commands, operators = _shell(script)
    assert operators == ["&&"] and [c[:2] for c in commands] == [["uv", "export"], ["pip", "install"]], commands
    assert _flag_values(commands[0], "--output-file") == _flag_values(commands[1], "-r") == ["/tmp/r.txt"]
    assert UV_IMAGE.match("ghcr.io/astral-sh/uv:1.2.3") is None, "a tag with no digest is not a pin"
    assert _run_shell('["pip", "install", "."]')[1] == "pip install ."
    # A pipe, an `||` and a `;` are operators the install RUN must not contain; the reader must see them.
    assert _shell("uv export | pip install -r /dev/stdin")[1] == ["|"]
    assert _shell("a || b ; c && d")[1] == ["||", ";", "&&"]
    assert _shell("echo 'a || b ; c'")[1] == [], "an operator inside quotes is not an operator"


def test_dockerignore_leaves_the_manifest_and_the_lock_in_the_build_context():
    patterns = [l.strip() for l in DOCKERIGNORE.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert patterns, "no patterns were read from .dockerignore"
    for name in ("pyproject.toml", "uv.lock"):
        assert not _dockerignore_excludes(name, patterns), f".dockerignore drops {name}, the build cannot read it"
    # The reader itself: it must see each way a file gets excluded, and a re-include.
    for pattern in ("uv.lock", "*.lock", "**/uv.lock", "uv.lock/", "uv.*"):
        assert _dockerignore_excludes("uv.lock", [pattern]), pattern
    assert not _dockerignore_excludes("uv.lock", ["*.lock", "!uv.lock"])
    assert not _dockerignore_excludes("uv.lock", ["src/", "*.pyc"])


# --- the uv pin ------------------------------------------------------------------------------------


def test_ci_installs_the_uv_the_dockerfile_names_and_verifies_it():
    env = _ci_env()
    assert re.fullmatch(r"\d+\.\d+\.\d+", env.get("UV_VERSION", "")), env
    assert SHA256.match(env.get("UV_SHA256_LINUX_X86_64", "")), env
    uv_stage, _, _ = _docker()
    tag = UV_IMAGE.match(uv_stage[0]).group(1)
    assert tag == env["UV_VERSION"], f"Dockerfile uv {tag} != ci.yml UV_VERSION {env['UV_VERSION']}"
    _, jobs = _ci()
    setups = [s for job in jobs.values() for s in _steps(job) if "astral-sh/setup-uv@" in s]
    assert len(setups) == 1, len(setups)
    assert _step_with(setups[0]) == {
        "version": "${{ env.UV_VERSION }}",
        "checksum": "${{ env.UV_SHA256_LINUX_X86_64 }}",
        # The default is 'auto', which saves and restores a cache on GitHub-hosted runners.
        "enable-cache": "false",
    }, setups[0]


# --- the test job ----------------------------------------------------------------------------------


def test_the_test_job_installs_the_lock_in_three_steps_nothing_can_switch_off():
    code, jobs = _ci()
    # Nothing at workflow or job level can skip the job or its steps.
    assert re.findall(r"(?m)^([\w-]+):", code) == ["name", "on", "concurrency", "permissions", "env", "jobs"]
    block = jobs["test"]
    assert re.findall(r"(?m)^    ([\w-]+):", block) == ["name", "runs-on", "timeout-minutes", "steps"], block
    steps = _steps(block)

    def where(predicate):
        return [i for i, s in enumerate(steps) if predicate(s)]

    def commands_of(step):
        return _shell(_step_script(step))[0]

    setup = where(lambda s: "astral-sh/setup-uv@" in s)
    check = where(lambda s: any(c[:2] == ["uv", "lock"] for c in commands_of(s)))
    install = where(lambda s: any(c[:2] == ["uv", "export"] for c in commands_of(s)))
    pip_check = where(lambda s: _step_script(s).strip() == "pip check")
    pytests = where(lambda s: "python -m pytest" in s)
    for name, found in {"setup-uv": setup, "uv lock --check": check, "install": install, "pip check": pip_check}.items():
        assert len(found) == 1, (name, found)
    assert len(pytests) == 2, pytests
    assert setup[0] < check[0] < install[0] < pip_check[0] < min(pytests), (setup, check, install, pip_check, pytests)

    # Each carries only the keys that make it run: no `if`, `continue-on-error`, `shell`, `env`.
    assert _step_keys(steps[setup[0]]) == ["name", "uses", "with"], steps[setup[0]]
    for found in (check, install, pip_check):
        assert _step_keys(steps[found[0]]) == ["name", "run"], steps[found[0]]
    assert "continue-on-error" not in "".join(steps[i] for i in (setup + check + install + pip_check))

    assert _step_script(steps[check[0]]).strip() == "uv lock --check"

    # The install step: export the lock with the dev extra, install the file with hashes, then this
    # package without deps. Newline-separated under GitHub's default `bash -e`; no operator at all.
    commands, operators = _shell(_step_script(steps[install[0]]))
    assert operators == [] and len(commands) == 3, (operators, commands)
    export, locked, project = commands
    assert export[:2] == ["uv", "export"], export
    assert set(_flags(export)) == CI_EXPORT_FLAGS, set(_flags(export)) ^ CI_EXPORT_FLAGS
    assert _flag_values(export, "--format") == ["requirements-txt"], export
    extras = _flag_values(export, "--extra")
    assert extras and set(extras) == _extras(), (extras, _extras())
    written = _flag_values(export, "--output-file")
    assert len(written) == 1 and written == _flag_values(locked, "-r"), (written, locked)
    assert [_pip_kind(_pip_installs([locked])[0]), _pip_kind(_pip_installs([project])[0])] == ["locked", "project"]
    assert project[-2:] == ["-e", "."], project


# --- the lock matches pyproject.toml -----------------------------------------------------------------


def _requirement(text, extra=None):
    """'name>=1,<2' -> (name, extra marker or None, frozenset of specifier clauses)."""
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*(.*)", text.strip())
    assert match, text
    clauses = frozenset(c.strip().replace(" ", "") for c in match.group(2).split(",") if c.strip())
    return (match.group(1).lower().replace("_", "-"), f"extra == '{extra}'" if extra else None, clauses)


def test_uv_lock_records_the_requirements_pyproject_declares():
    # The offline half of `uv lock --check` (ci.yml runs the real one): a pyproject.toml edit
    # that was not followed by `uv lock` is red here as well.
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    declared = {_requirement(r) for r in project["dependencies"]}
    for extra, requirements in project["optional-dependencies"].items():
        declared |= {_requirement(r, extra) for r in requirements}
    locked_project = [p for p in tomllib.loads(UV_LOCK.read_text())["package"] if p["name"] == project["name"]]
    assert len(locked_project) == 1, [p["name"] for p in locked_project]
    metadata = locked_project[0]["metadata"]
    recorded = {
        (r["name"], r.get("marker"), frozenset(r.get("specifier", "").replace(" ", "").split(",")) - {""})
        for r in metadata["requires-dist"]
    }
    assert len(declared) > 5, "pyproject.toml declared almost nothing; the comparison would be vacuous"
    assert recorded == declared, (sorted(recorded - declared, key=str), sorted(declared - recorded, key=str))
    assert set(metadata["provides-extras"]) == _extras()
    assert tomllib.loads(UV_LOCK.read_text())["requires-python"] == project["requires-python"]


def test_every_locked_package_carries_hashes():
    # `pip install --require-hashes` refuses a requirement with no hash, so a package the lock
    # holds without one would fail the image build rather than install unchecked.
    packages = [p for p in tomllib.loads(UV_LOCK.read_text())["package"] if "registry" in p.get("source", {})]
    assert len(packages) > 5, "no registry packages were read from uv.lock"
    unhashed = [
        p["name"] for p in packages
        if not ([w for w in p.get("wheels", []) if w.get("hash")] or p.get("sdist", {}).get("hash"))
    ]
    assert unhashed == [], unhashed
