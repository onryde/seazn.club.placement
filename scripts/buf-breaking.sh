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
# The two skips below are the ONLY ways to exit 0 without comparing, and each
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
proto="proto/scheduler.proto"
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
# or truncated clone) and prints nothing when the tree is fine but the path is
# absent. Only the second is "no proto at the base".
if ! listing="$(git ls-tree --name-only "$resolved" -- "$proto" 2>&1)"; then
  echo "::error::cannot read the tree of $base ($resolved): $listing (checkout needs fetch-depth: 0)"
  exit 1
fi
if [ -z "$listing" ]; then
  echo "::notice::buf breaking skipped: $base has no $proto (first import)"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The base's proto dir, laid out like the head. buf.yaml comes from the HEAD on
# purpose: the base may predate buf.yaml (the push that introduces buf does), and
# without it the base module root would be this directory, its file would be
# 'proto/scheduler.proto' against the head's 'scheduler.proto', and every
# comparison would read "file deleted".
git archive "$resolved" proto | tar -x -C "$tmp"
cp buf.yaml "$tmp/buf.yaml"

echo "buf breaking --against $base ($resolved), proto/ extracted to a temp dir"
buf breaking --against "$tmp"
