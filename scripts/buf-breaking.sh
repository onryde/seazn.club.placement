#!/usr/bin/env bash
# Contract gate: is proto/scheduler.proto still backward compatible with the
# proto at <base-ref>? Runs from the repo root; CI passes the base through env.
#
#   scripts/buf-breaking.sh <base-ref>
#
# Exit codes
#   0    compatible, or a genuine first import (nothing to compare against)
#   1    the gate could not run: buf missing, or the base is not in this clone
#   2    usage error (no <base-ref> argument at all)
#   100  buf found a breaking change (buf's own code, passed through)
#
# The skips below are the ONLY ways to exit 0 without comparing, and each
# means "there is no earlier proto". A base that is merely not fetched (a shallow
# clone, a ref nobody fetched) is a broken checkout and FAILS: skipping it would
# report a gate that never ran: an absent symptom that means "suppressed", not
# "safe".
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/buf-breaking.sh <base-ref>" >&2
  exit 2
fi
base="$1"
zeros="0000000000000000000000000000000000000000"

# Before any skip: an environment fault must show on every run, or a runner
# without buf would "skip" its way to green.
if ! command -v buf >/dev/null 2>&1; then
  echo "::error::buf is not on PATH (CI installs bufbuild/buf-action at BUF_VERSION; locally see README.md, 'Installing buf')"
  exit 1
fi

if [ -z "$base" ] || [ "$base" = "$zeros" ]; then
  echo "::notice::buf breaking skipped: no base commit (first push to main)"
  exit 0
fi

if ! resolved="$(git rev-parse --verify --quiet "${base}^{commit}")"; then
  echo "::error::base ref $base is not in this clone (checkout needs fetch-depth: 0)"
  exit 1
fi

# `git ls-tree` exits non-zero when the tree object itself is missing (a partial
# or truncated clone) and prints nothing when the tree is fine but has no proto/
# at all. Only the second is "no earlier proto". The test is "no proto/*.proto",
# not "no proto/scheduler.proto": a base that holds the contract under another
# name must still be compared, or renaming the file would skip the gate.
if ! listing="$(git ls-tree -r --name-only "$resolved" -- proto 2>&1)"; then
  echo "::error::cannot read the tree of $base ($resolved): $listing (checkout needs fetch-depth: 0)"
  exit 1
fi
if ! grep -q '\.proto$' <<<"$listing"; then
  echo "::notice::buf breaking skipped: $base has no proto/*.proto (first import)"
  exit 0
fi

# An explicit template: BSD/macOS `mktemp -d` ignores TMPDIR, GNU honors it, and
# the temp dir must land where TMPDIR says on both (tests/test_buf_breaking.py
# points TMPDIR at an empty dir and checks it is empty afterwards).
tmp="$(mktemp -d "${TMPDIR:-/tmp}/buf-breaking.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

# The tree read fine above, so a failure here is a missing blob (a partial
# clone): a broken checkout, said out loud rather than as a bare exit 128/255.
if ! git archive "$resolved" proto | tar -x -C "$tmp"; then
  echo "::error::cannot extract proto/ from $base ($resolved) (checkout needs fetch-depth: 0)"
  exit 1
fi
# The base's proto dir is laid out like the head's. buf.yaml comes from the HEAD
# on purpose: the base may predate buf.yaml (the push that introduces buf does),
# and without it the base module root would be this directory, its file would be
# 'proto/scheduler.proto' against the head's 'scheduler.proto', and every
# comparison would read "file deleted".
cp buf.yaml "$tmp/buf.yaml"

echo "buf breaking --against $base ($resolved), proto/ extracted to a temp dir"
buf breaking --against "$tmp"
