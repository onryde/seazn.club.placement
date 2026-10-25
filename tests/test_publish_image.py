"""``scripts/publish-image.sh``: push the image the ``docker`` job TESTED, once.

The publish job downloads the tar the docker job built and smoke-tested, loads
it, tags it ``ghcr.io/onryde/seazn-placement:<sha>`` and pushes it. It never
rebuilds, so the pushed bytes are the tested bytes. A re-run of a push to main
must not move an existing sha tag, so the script asks the registry first.

There is no container runtime on the developer machine, so these cases run the
real script against a FAKE ``docker`` on PATH that records every call and plays
the registry. What only CI can prove is that real ``docker`` and GHCR answer the
way the fake does (the ``Digest:`` line of ``buildx imagetools inspect``).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "publish-image.sh"

IMAGE = "ghcr.io/onryde/seazn-placement:" + "a" * 40
TESTED = "placement-service:ci"
DIGEST = "sha256:" + "d" * 64
OTHER_DIGEST = "sha256:" + "e" * 64

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records every call. Plays a registry: the sha tag exists if FAKE_EXISTING is
# set, or once `docker push` has run.
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  buildx)
    # FAKE_INSPECT_ERR makes the first FAKE_INSPECT_ERR_TIMES inspects fail with
    # that stderr text (every inspect when no count is given).
    inspects=$(( $(cat "$FAKE_STATE/inspects" 2>/dev/null || echo 0) + 1 ))
    echo "$inspects" > "$FAKE_STATE/inspects"
    if [ -n "${FAKE_INSPECT_ERR:-}" ] && [ "$inspects" -le "${FAKE_INSPECT_ERR_TIMES:-999}" ]; then
      echo "$FAKE_INSPECT_ERR" >&2
      exit 1
    fi
    if [ -n "${FAKE_EXISTING:-}" ] || [ -e "$FAKE_STATE/pushed" ]; then
      echo "Name:      ${@: -1}"
      echo "MediaType: application/vnd.oci.image.index.v1+json"
      if [ -z "${FAKE_NO_DIGEST:-}" ]; then
        if [ -e "$FAKE_STATE/pushed" ]; then echo "Digest:    $FAKE_DIGEST"; else echo "Digest:    $FAKE_EXISTING_DIGEST"; fi
      fi
      exit 0
    fi
    echo "ERROR: ${@: -1}: not found" >&2
    exit 1
    ;;
  load) [ -z "${FAKE_LOAD_FAILS:-}" ] || exit 1 ;;
  tag) [ -z "${FAKE_TAG_FAILS:-}" ] || exit 1 ;;
  push) [ -z "${FAKE_PUSH_FAILS:-}" ] || exit 1; touch "$FAKE_STATE/pushed" ;;
esac
exit 0
"""

FAKE_SLEEP = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_SLEEP_LOG"
"""


def _run(tmp_path, *args, **fake):
    """Run the script with the fake docker. Returns (result, recorded calls)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    # A retry must not sleep for real in a test, but its delay must be visible.
    sleep = bin_dir / "sleep"
    sleep.write_text(FAKE_SLEEP)
    sleep.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_SLEEP_LOG": str(tmp_path / "sleep.log"),
        "FAKE_STATE": str(state),
        "FAKE_DIGEST": DIGEST,
        "FAKE_EXISTING_DIGEST": OTHER_DIGEST,
    }
    env.update({f"FAKE_{key.upper()}": value for key, value in fake.items()})
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=tmp_path
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def _output(result):
    return result.stdout + result.stderr


def _standard(tmp_path, **fake):
    return _run(tmp_path, str(tmp_path / "image.tar"), TESTED, IMAGE, **fake)


def test_the_script_exists_and_is_executable():
    assert SCRIPT.is_file(), SCRIPT
    assert os.access(SCRIPT, os.X_OK)
    assert shutil.which("bash")


def test_pushes_the_tested_image_when_the_tag_is_absent(tmp_path):
    result, calls = _standard(tmp_path)
    tar = str(tmp_path / "image.tar")
    assert result.returncode == 0, _output(result)
    # Exactly this sequence: ask, load the tested tar, tag, push, read back.
    assert calls == [
        f"docker buildx imagetools inspect {IMAGE}",
        f"docker load --input {tar}",
        f"docker tag {TESTED} {IMAGE}",
        f"docker push {IMAGE}",
        f"docker buildx imagetools inspect {IMAGE}",
    ], calls
    assert DIGEST in result.stdout, result.stdout
    assert "already exists" not in _output(result)


def test_skips_the_push_when_the_tag_already_exists(tmp_path):
    result, calls = _standard(tmp_path, existing="1")
    assert calls == [f"docker buildx imagetools inspect {IMAGE}"], calls
    assert result.returncode == 0, _output(result)
    assert "already exists" in _output(result)
    # It reports the digest that is ALREADY published, not a new one.
    assert OTHER_DIGEST in result.stdout and DIGEST not in result.stdout, result.stdout


def test_an_existing_tag_with_no_readable_digest_is_an_error(tmp_path):
    result, calls = _standard(tmp_path, existing="1", no_digest="1")
    out = _output(result)
    assert calls == [f"docker buildx imagetools inspect {IMAGE}"], calls
    assert result.returncode == 1, out
    assert "::error::" in out, out


def test_a_missing_digest_after_the_push_is_an_error(tmp_path):
    result, calls = _standard(tmp_path, no_digest="1")
    out = _output(result)
    assert any(call.startswith("docker push ") for call in calls), calls
    assert result.returncode == 1, out
    assert "::error::" in out, out


def test_a_failed_push_stops_the_script_at_the_push(tmp_path):
    # The failure must be docker push's own, not a later "no digest" error that
    # happens to fail the script too: the push is the LAST call, so nothing ran
    # after it, and the script's own ::error:: guard did not fire.
    result, calls = _standard(tmp_path, push_fails="1")
    out = _output(result)
    assert calls and calls[-1] == f"docker push {IMAGE}", calls
    assert result.returncode == 1, out
    assert "::error::" not in out, out
    assert DIGEST not in result.stdout


def test_a_failed_load_never_reaches_tag_or_push(tmp_path):
    result, calls = _standard(tmp_path, load_fails="1")
    assert any(call.startswith("docker load ") for call in calls), calls
    assert not any(call.startswith(("docker tag ", "docker push ")) for call in calls), calls
    assert result.returncode != 0, _output(result)


def test_pushes_only_the_tag_it_was_given(tmp_path):
    result, calls = _standard(tmp_path)
    pushes = [call for call in calls if call.startswith("docker push")]
    assert result.returncode == 0, _output(result)
    assert pushes == [f"docker push {IMAGE}"], pushes
    assert not any("latest" in call for call in calls), calls


def test_refuses_a_call_without_all_three_arguments(tmp_path):
    tar = str(tmp_path / "image.tar")
    # Four arguments too: an off-by-one "at least three" would push on a call the
    # workflow never makes, with the extra word silently ignored.
    for args in ((), (tar,), (tar, TESTED), (tar, TESTED, IMAGE, "extra")):
        result, calls = _run(tmp_path, *args)
        assert result.returncode == 2, (args, _output(result))
        assert "usage" in _output(result), args
        assert calls == [], f"docker was called with args {args}: {calls}"


# --- an inspect that fails is not an inspect that says "absent" -----------------

# What `docker buildx imagetools inspect` prints when the registry says the tag
# is not there. GHCR answers 403 DENIED, not 404, for a package that does not
# exist yet, so the very first publish depends on `denied` reading as absent. A
# real credential failure still fails, loudly, at `docker push`.
ABSENT_ERRORS = [
    "ERROR: ghcr.io/onryde/seazn-placement:abc: not found",
    "ERROR: failed to fetch manifest: MANIFEST_UNKNOWN: manifest unknown",
    "ERROR: NAME_UNKNOWN: repository name not known to registry",
    "ERROR: unexpected status from HEAD request: 404 Not Found",
    "ERROR: unexpected status from HEAD request: 403 Forbidden",
    "ERROR: denied: requested access to the resource is denied",
    "ERROR: unexpected status from HEAD request: 401 Unauthorized",
    # A bare status code with none of the words above: the numbers count alone.
    "ERROR: server returned status code 404",
    "ERROR: server returned status code 403",
    "ERROR: server returned status code 401",
]
# Anything else says nothing about whether the tag exists.
UNKNOWN_ERRORS = [
    "ERROR: unexpected status from HEAD request: 503 Service Unavailable",
    "ERROR: unexpected status from HEAD request: 429 Too Many Requests",
    "ERROR: unexpected status from HEAD request: 500 Internal Server Error",
    "ERROR: Head https://ghcr.io/v2/x: net/http: TLS handshake timeout",
    "ERROR: Head https://ghcr.io/v2/x: dial tcp: i/o timeout",
    "ERROR: Head https://ghcr.io/v2/x: EOF",
    "ERROR: Head https://ghcr.io/v2/x: dial tcp: lookup ghcr.io: Temporary failure in name resolution",
]


def _inspects(calls):
    return [call for call in calls if call.startswith("docker buildx imagetools inspect")]


def _wrote_nothing(calls):
    return not any(call.startswith(("docker load ", "docker tag ", "docker push ")) for call in calls)


@pytest.mark.parametrize("error", UNKNOWN_ERRORS)
def test_an_inspect_error_that_is_not_absent_never_pushes(tmp_path, error):
    # The tag EXISTS here; the registry just cannot say so. Reading the failure as
    # "absent" would load, tag and push over a published sha tag and exit 0.
    result, calls = _standard(tmp_path, existing="1", inspect_err=error)
    out = _output(result)
    assert _wrote_nothing(calls), calls
    assert result.returncode == 1, out
    assert "::error::" in out, out
    # The captured stderr is shown, not discarded.
    assert error.removeprefix("ERROR: ") in out, out
    # Bounded: it tried again, and it gave up.
    assert 2 <= len(_inspects(calls)) <= 5, calls


@pytest.mark.parametrize("error", ABSENT_ERRORS)
def test_an_inspect_error_that_means_absent_publishes(tmp_path, error):
    # Fails the first inspect only; after the push the read-back sees the tag.
    result, calls = _standard(tmp_path, inspect_err=error, inspect_err_times="1")
    tar = str(tmp_path / "image.tar")
    assert result.returncode == 0, _output(result)
    assert calls == [
        f"docker buildx imagetools inspect {IMAGE}",
        f"docker load --input {tar}",
        f"docker tag {TESTED} {IMAGE}",
        f"docker push {IMAGE}",
        f"docker buildx imagetools inspect {IMAGE}",
    ], calls
    assert DIGEST in result.stdout, result.stdout


def test_a_transient_error_then_a_clean_answer_reads_the_tag_that_exists(tmp_path):
    result, calls = _standard(
        tmp_path, existing="1", inspect_err="ERROR: 503 Service Unavailable", inspect_err_times="1"
    )
    assert result.returncode == 0, _output(result)
    assert calls == [f"docker buildx imagetools inspect {IMAGE}"] * 2, calls
    assert "already exists" in _output(result)
    assert OTHER_DIGEST in result.stdout, result.stdout


def test_a_transient_error_then_not_found_publishes(tmp_path):
    result, calls = _standard(
        tmp_path, inspect_err="ERROR: 503 Service Unavailable", inspect_err_times="1"
    )
    # inspect (503), inspect (not found), load, tag, push, read-back.
    # (After the push the fake registry holds the tag.)
    assert result.returncode == 0, _output(result)
    assert [call.split()[1] for call in calls] == ["buildx", "buildx", "load", "tag", "push", "buildx"], calls


def test_the_classifier_sees_the_error_not_the_image_name(tmp_path):
    # A sha containing "404" must not turn a 503 into "not found".
    image = "ghcr.io/onryde/seazn-placement:" + "0404" * 10
    result, calls = _run(
        tmp_path,
        str(tmp_path / "image.tar"),
        TESTED,
        image,
        existing="1",
        inspect_err=f"ERROR: {image}: 503 Service Unavailable",
    )
    assert _wrote_nothing(calls), calls
    assert result.returncode == 1, _output(result)


def test_retries_back_off_and_the_wait_grows(tmp_path):
    # Seconds slept between the attempts of a registry that keeps answering 503
    # (a fake `sleep` records them and returns at once).
    result, calls = _standard(tmp_path, existing="1", inspect_err="ERROR: 503 Service Unavailable")
    assert result.returncode == 1, _output(result)
    waits = [int(line) for line in (tmp_path / "sleep.log").read_text().split()]
    # One wait between each pair of attempts, none after the last.
    assert len(waits) == len(_inspects(calls)) - 1 >= 1, (waits, calls)
    assert waits[0] > 0 and waits == sorted(set(waits)), f"a retry must wait, and wait longer each time: {waits}"
    assert not (tmp_path / "state" / "pushed").exists()
