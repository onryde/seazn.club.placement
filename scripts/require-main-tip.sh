#!/usr/bin/env bash
# Say whether a commit is still the tip of origin's main.
#
#   scripts/require-main-tip.sh <sha>
#
# stg.yml runs when ci finishes for a push to main. Two pushes in a row can finish
# out of order: the older commit's ci ends last and would deploy OVER the newer one,
# leaving staging on old code until the next merge. Right before it deploys, the
# job asks origin where main is now. If main has moved on, this commit is skipped.
#
# The newer commit's own stg run is what deploys it, and that run can be lost: the
# concurrency group keeps one running run and ONE PENDING run, and a newer pending
# run cancels the older one even with cancel-in-progress: false (which only protects
# the running run). If the run for the tip was the one cancelled, staging stays on an
# older commit until someone uses Actions to re-run the cancelled stg run, or the next
# merge lands.
#
# Both answers exit 0 and are written as `current=true` or `current=false` to
# $GITHUB_OUTPUT (when set); the workflow's later steps run only on `true`. Being
# unable to tell is NOT an answer: an unreadable origin, or no `main` there, exits 1
# and writes nothing, so a broken check can never read as "skip" or as "go".
#
# `origin` is asked directly (git ls-remote), never the local origin/main, which is
# whatever the checkout fetched. The checkout keeps no credentials, so this reads the
# public repository anonymously.
#
# Exit codes: 0 answered; 1 bad sha, or the tip could not be read; 2 usage.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/require-main-tip.sh <sha>" >&2
  exit 2
fi
sha="$1"

if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::'$sha' is not a full lowercase commit sha"
  exit 1
fi

if ! refs="$(git ls-remote origin refs/heads/main)"; then
  echo "::error::could not read origin (git ls-remote origin refs/heads/main failed)"
  exit 1
fi
tip="$(awk '$2 == "refs/heads/main" { print $1; exit }' <<<"$refs")"
if [[ ! "$tip" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::origin has no refs/heads/main (got: '$refs')"
  exit 1
fi

if [ "$tip" = "$sha" ]; then
  current=true
  echo "$sha is the tip of main"
else
  current=false
  echo "::notice::$sha is no longer the tip of main ($tip is). Not deploying it: the stg run for $tip deploys that one. If that run was cancelled while pending, re-run the cancelled stg run (nothing else will, until the next merge)."
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "current=$current" >> "$GITHUB_OUTPUT"
fi
