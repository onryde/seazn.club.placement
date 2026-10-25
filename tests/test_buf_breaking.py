"""The proto contract gate: ``scripts/buf-breaking.sh`` and ``buf.yaml``.

``proto/scheduler.proto`` is additive-only (AGENTS.md): the seazn.club web app
vendors a copy of it and deploys on its own clock, so a renumbered or removed
field is a wire break for a client that has not re-vendored yet.

Every case builds a throwaway git repository holding ``buf.yaml``, a copy of
``scripts/buf-breaking.sh`` and a minimal proto, then runs the script exactly as
CI does, with ``cwd`` set to that repository.

Two rules shape the file (docs/superpowers/TEST-STRATEGY.md):

* A skipped contract gate is a vacuous one, so nothing here skips. A missing
  ``buf`` is a FAILURE (``test_buf_is_installed``).
* A test that asserts only "exit is not 0" passes when the gate is broken in an
  unrelated way. Each "must fail" case therefore also pins buf's own exit code
  (100, "findings") and names the finding, and asserts that the script's own
  ``::error::`` guard did NOT fire. The failure has to be the contract's.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "buf-breaking.sh"
BUF_YAML = REPO_ROOT / "buf.yaml"
REAL_PROTO = REPO_ROOT / "proto" / "scheduler.proto"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

ZEROS = "0" * 40
# Forty lowercase hex digits that name no object in any repository built below.
UNRESOLVABLE = "deadbeef" * 5
# buf's documented exit code when it reports findings (lint or breaking).
BUF_FINDINGS_EXIT = 100


def _proto(*fields):
    """A minimal proto3 file whose one message carries the given field lines."""
    body = "".join(f"  {field}\n" for field in fields)
    return f'syntax = "proto3";\npackage demo.v1;\n\nmessage M {{\n{body}}}\n'


def _git_env():
    env = dict(os.environ)
    # The machine's own git config (signing, hooks path, identity) must not
    # leak into a repository these tests build.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, env=_git_env(),
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _commit(repo, files, message, *, remove=()):
    """Write ``files`` (path -> text), delete ``remove``, commit, return the sha."""
    for rel in remove:
        (repo / rel).unlink()
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _tooling():
    """The files CI has at the repo root and the script needs: config + script."""
    return {
        "buf.yaml": BUF_YAML.read_text(),
        "scripts/buf-breaking.sh": SCRIPT.read_text(),
    }


def _new_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _run(repo, base, *, env=None):
    return subprocess.run(
        ["bash", "scripts/buf-breaking.sh", base],
        cwd=repo, capture_output=True, text=True, env=env or dict(os.environ),
    )


def _output(result):
    return result.stdout + result.stderr


def _compare(tmp_path, base_proto, head_proto):
    """base commit -> head commit, both carrying the tooling. Returns the result."""
    repo = _new_repo(tmp_path)
    base = _commit(repo, {**_tooling(), "proto/scheduler.proto": base_proto}, "base")
    _commit(repo, {"proto/scheduler.proto": head_proto}, "head")
    return _run(repo, base)


def _assert_buf_found_a_break(result, *, field):
    out = _output(result)
    assert result.returncode == BUF_FINDINGS_EXIT, out
    assert "::error::" not in out, f"the script's own guard fired, not buf's finding:\n{out}"
    assert f'field "{field}"' in out and "was deleted" in out, out


def _assert_buf_compared_and_passed(result):
    out = _output(result)
    assert result.returncode == 0, out
    assert "skipped" not in out, f"the gate skipped instead of comparing:\n{out}"
    # The positive half of the "not skipped" pair: the comparison line was printed.
    assert "buf breaking --against" in out, out


def _pinned_buf_version():
    versions = re.findall(r'^  BUF_VERSION: "(\d+\.\d+\.\d+)"$', CI_YML.read_text(), re.MULTILINE)
    assert len(versions) == 1, f"BUF_VERSION must be declared exactly once in ci.yml, found {versions}"
    return versions[0]


# --- the environment -------------------------------------------------------


def test_buf_is_installed():
    # FAILS rather than skips: a skipped contract gate is a vacuous one.
    assert shutil.which("buf"), (
        "buf is not on PATH. Install the version pinned as BUF_VERSION in "
        ".github/workflows/ci.yml (see README.md, 'Installing buf')."
    )


def test_installed_buf_is_the_pinned_version():
    # The tests below judge the contract with whatever buf is on PATH; CI judges
    # it with BUF_VERSION. A different local buf would make a local green mean
    # nothing about CI's verdict.
    assert shutil.which("buf"), "buf is not on PATH"
    result = subprocess.run(["buf", "--version"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _pinned_buf_version()


def test_buf_yaml_is_a_v2_file_rule_set_over_the_proto_dir():
    text = BUF_YAML.read_text()
    assert re.search(r"^version: v2$", text, re.MULTILINE), text
    assert re.search(r"^modules:\n  - path: proto$", text, re.MULTILINE), text
    assert re.search(r"^breaking:\n  use:\n    - FILE$", text, re.MULTILINE), text


def test_fails_when_buf_is_not_on_path(tmp_path):
    # An environment fault must show at EVERY invocation. If the skip branches
    # ran before the tool check, a runner without buf would skip a first-import
    # push and report a gate that never could have run.
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = subprocess.run(
        [shutil.which("bash"), str(SCRIPT), ZEROS],
        cwd=tmp_path, capture_output=True, text=True, env={"PATH": str(empty)},
    )
    out = _output(result)
    assert result.returncode == 1, out
    assert "::error::" in out and "buf" in out, out


def test_usage_error_without_a_base_argument(tmp_path):
    # A MISSING argument is a bug in the caller; only an EMPTY one means
    # "no base commit". The two must not be confused.
    repo = _new_repo(tmp_path)
    _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "base")
    result = subprocess.run(
        ["bash", "scripts/buf-breaking.sh"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 2, _output(result)
    assert "usage" in _output(result)


# --- the skips: ONLY a genuine first import --------------------------------


def test_skips_on_all_zero_base(tmp_path):
    repo = _new_repo(tmp_path)
    _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "import")
    result = _run(repo, ZEROS)
    assert result.returncode == 0, _output(result)
    assert "skipped" in result.stdout
    assert "::notice::" in result.stdout


def test_skips_on_an_empty_base(tmp_path):
    repo = _new_repo(tmp_path)
    _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "import")
    result = _run(repo, "")
    assert result.returncode == 0, _output(result)
    assert "skipped" in result.stdout
    assert "no base commit" in result.stdout


def test_skips_when_base_has_no_proto(tmp_path):
    repo = _new_repo(tmp_path)
    base = _commit(repo, {"README.md": "# x\n"}, "base: no proto")
    _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "import")
    result = _run(repo, base)
    assert result.returncode == 0, _output(result)
    assert "first import" in result.stdout
    assert "skipped" in result.stdout


def test_fails_on_unresolvable_base(tmp_path):
    # Review Focus 1: a base that is not in this clone is a broken checkout,
    # never a first import.
    repo = _new_repo(tmp_path)
    _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "head")
    result = _run(repo, UNRESOLVABLE)
    out = _output(result)
    assert result.returncode == 1, out
    assert "::error::" in out, out
    assert "skipped" not in out, out


def test_fails_in_a_shallow_clone_that_lacks_the_base(tmp_path):
    # The real way CI hits Review Focus 1: actions/checkout's default
    # fetch-depth of 1 leaves the base commit out of the clone.
    origin = _new_repo(tmp_path, "origin")
    base = _commit(origin, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "base")
    _commit(origin, {"proto/scheduler.proto": _proto("string a = 1;", "string b = 2;")}, "head")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow))
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    result = _run(shallow, base)
    out = _output(result)
    assert result.returncode == 1, out
    assert "::error::" in out and "fetch-depth: 0" in out, out
    assert "skipped" not in out, out


def test_fails_when_the_base_tree_cannot_be_read(tmp_path):
    # The ref resolves but its tree object is gone (a partial or truncated
    # clone). That is also a broken checkout, not "the base has no proto".
    origin = _new_repo(tmp_path, "origin")
    base = _commit(origin, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "base")
    head = _commit(origin, {"proto/scheduler.proto": _proto("string a = 1;", "string b = 2;")}, "head")
    assert base != head
    objects = origin / ".git" / "objects"
    root_tree = _git(origin, "rev-parse", f"{base}^{{tree}}")
    (objects / root_tree[:2] / root_tree[2:]).unlink()
    result = _run(origin, base)
    out = _output(result)
    assert result.returncode == 1, out
    assert "::error::" in out, out
    assert "skipped" not in out, out


# --- the comparison ---------------------------------------------------------


def test_passes_an_additive_field(tmp_path):
    result = _compare(
        tmp_path,
        _proto("string a = 1;"),
        _proto("string a = 1;", "string b = 2;"),
    )
    _assert_buf_compared_and_passed(result)


def test_fails_on_a_removed_field(tmp_path):
    result = _compare(
        tmp_path,
        _proto("string a = 1;", "string b = 2;"),
        _proto("string b = 2;"),
    )
    _assert_buf_found_a_break(result, field="1")


def test_fails_on_a_renumbered_field(tmp_path):
    # The proto3 hazard placement-stg.yml:24-27 (seazn.club) names: the same
    # name on a new number reads as a different field to an old client.
    result = _compare(
        tmp_path,
        _proto("string a = 1;"),
        _proto("string a = 3;"),
    )
    _assert_buf_found_a_break(result, field="1")


def test_fails_on_a_renamed_field(tmp_path):
    # Same number, new name: wire-compatible, but ts-proto names the property
    # after the field, so seazn.club's generated TypeScript stubs would break.
    # This is what the FILE rule set catches and WIRE does not (buf.yaml).
    result = _compare(
        tmp_path,
        _proto("string a = 1;"),
        _proto("string c = 1;"),
    )
    out = _output(result)
    assert result.returncode == BUF_FINDINGS_EXIT, out
    assert "::error::" not in out, out
    assert 'changed name from "a" to "c"' in out, out


def test_fails_on_a_removed_message(tmp_path):
    # Wire-compatible while nothing sends it, yet a type the TS stubs export
    # disappears. FILE catches it; WIRE and WIRE_JSON do not.
    extra = "\nmessage N {\n  string x = 1;\n}\n"
    result = _compare(
        tmp_path,
        _proto("string a = 1;") + extra,
        _proto("string a = 1;"),
    )
    out = _output(result)
    assert result.returncode == BUF_FINDINGS_EXIT, out
    assert "::error::" not in out, out
    assert 'message "N" was deleted' in out, out


def test_a_base_without_buf_yaml_is_still_compared(tmp_path):
    # The push that introduces buf has a base from BEFORE buf.yaml existed. If
    # the base tree were handed to buf as it stands, its module root would be
    # the repo root, its file would be 'proto/scheduler.proto' against the
    # head's 'scheduler.proto', and every comparison would read "file deleted".
    # The head's buf.yaml must govern both sides. The additive change below
    # passes only if the two sides line up, and the removal fails for the
    # RIGHT reason (the field), not the layout.
    repo = _new_repo(tmp_path)
    base = _commit(repo, {"proto/scheduler.proto": _proto("string a = 1;")}, "base: no buf.yaml")
    assert not (repo / "buf.yaml").exists()
    _commit(
        repo,
        {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;", "string b = 2;")},
        "head: additive",
    )
    _assert_buf_compared_and_passed(_run(repo, base))

    _commit(repo, {"proto/scheduler.proto": _proto("string b = 2;")}, "head: removes a")
    _assert_buf_found_a_break(_run(repo, base), field="1")


def test_the_script_leaves_the_working_tree_clean(tmp_path):
    # It extracts the base into a temp dir. Nothing may land in the checkout:
    # CI's later steps must see exactly the tree that was tested.
    repo = _new_repo(tmp_path)
    base = _commit(repo, {**_tooling(), "proto/scheduler.proto": _proto("string a = 1;")}, "base")
    _commit(repo, {"proto/scheduler.proto": _proto("string a = 1;", "string b = 2;")}, "head")
    _assert_buf_compared_and_passed(_run(repo, base))
    assert _git(repo, "status", "--porcelain") == ""


def test_real_proto_against_itself_passes(tmp_path):
    # Anti-vacuity on the real contract: the repo's own proto, as base and head.
    real = REAL_PROTO.read_text()
    assert len(real) > 10_000, "proto/scheduler.proto is unexpectedly small"
    result = _compare(tmp_path, real, real + "\n// a trailing comment changes no contract\n")
    _assert_buf_compared_and_passed(result)


def test_real_proto_with_a_dropped_field_fails(tmp_path):
    # The differential half of the case above: if buf were not really parsing
    # and comparing the real file, "against itself passes" would hold for any
    # input. Drop the first plain field declaration and the gate must go red.
    real = REAL_PROTO.read_text()
    declaration = re.search(
        r"^  (?:repeated |optional )?[\w.]+ (\w+) = (\d+);\n", real, re.MULTILINE
    )
    assert declaration, "no plain field declaration found in proto/scheduler.proto"
    dropped = real[: declaration.start()] + real[declaration.end():]
    assert dropped != real
    result = _compare(tmp_path, real, dropped)
    out = _output(result)
    assert result.returncode == BUF_FINDINGS_EXIT, out
    assert "::error::" not in out, out
    assert f'field "{declaration.group(2)}" with name "{declaration.group(1)}"' in out, out
