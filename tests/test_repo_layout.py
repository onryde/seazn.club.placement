"""This repo is the placement service at its root; the docs and comments were
written for the service's old home inside seazn.club. These pins keep the
move honest.

Every sweep reports what it scanned and zero scanned is a FAILURE
(seazn.club's TEST-STRATEGY.md, see AGENTS.md, "Paths in comments": anti-vacuity).
The old-layout needles are assembled from pieces so this file does not match its
own sweep.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

OLD_LAYOUT_NEEDLES = ("services/" "placement", "../" "../")
SWEPT_SUFFIXES = {".md", ".py", ".toml", ".yml"}
SWEPT_NAMES = {"Dockerfile"}
# The ONE place the old path is meant to appear: AGENTS.md's history pointer,
# a git command that runs in a seazn.club checkout. Matched as an exact line.
ALLOWED_LINES = {
    ("AGENTS.md", "`git log placement-split-base -- services/" "placement proto` (tag at"),
}
# Files the sweep must have read, or it swept the wrong tree.
ANCHORS = {
    "README.md", "AGENTS.md", "DEPLOY.md", "Dockerfile", "fly.toml", "pyproject.toml",
    ".github/workflows/ci.yml", "tests/test_proto_compiles.py", "src/placement/main.py",
    "bench/README.md",
}


def _repo_files():
    """Tracked files plus untracked-but-not-ignored ones, so the sweep also sees
    work that is not committed yet. Raises (a failure) outside a git checkout."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    )
    return sorted(n for n in result.stdout.decode().split("\0") if n)


def _swept(rel):
    path = Path(rel)
    return path.suffix in SWEPT_SUFFIXES or path.name in SWEPT_NAMES


def test_no_old_layout_paths_in_tracked_text():
    scanned = []
    allowed_hits = {}
    for rel in _repo_files():
        if not _swept(rel):
            continue
        scanned.append(rel)
        for number, line in enumerate((REPO_ROOT / rel).read_text().splitlines(), 1):
            if not any(needle in line for needle in OLD_LAYOUT_NEEDLES):
                continue
            key = (rel, line.strip())
            if key in ALLOWED_LINES:
                allowed_hits[key] = allowed_hits.get(key, 0) + 1
                continue
            raise AssertionError(f"{rel}:{number}: old-layout path: {line.strip()}")
    assert len(scanned) > 0, "no file was scanned; the sweep is vacuous"
    missing = ANCHORS - set(scanned)
    assert not missing, f"the sweep did not read {sorted(missing)} (scanned {len(scanned)} files)"
    # An allowlist entry that matches nothing is stale and would hide a real hit later.
    assert allowed_hits == {key: 1 for key in ALLOWED_LINES}, allowed_hits


def test_gitignore_keeps_env_files_out_of_the_repo():
    # The repo is public. A local .env must never be committable by accident.
    names = [".env", ".env.local", ".env.production", ".env.test.local"]
    result = subprocess.run(
        ["git", "check-ignore", "--verbose", "--no-index", *names],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    ignored = {line.split("\t", 1)[1] for line in result.stdout.splitlines()}
    assert ignored == set(names), (result.stdout, result.stderr)
