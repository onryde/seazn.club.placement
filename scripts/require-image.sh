#!/usr/bin/env bash
# Fail unless the image a deploy is about to ship is already published.
#
#   scripts/require-image.sh <image>
#
#   <image>  ghcr.io/onryde/seazn-placement:<40-hex lowercase commit sha>
#
# A deploy ships the image `ci.yml` tested and published; it never builds one. So
# before `flyctl deploy --image`, the registry is asked whether that exact tag
# exists. It is asked WITHOUT logging in, on purpose: Fly pulls the image
# anonymously, so an image that only an authenticated client can see (the package
# is still private) would pass a logged-in check and then fail on the machines.
#
# The reference is validated first, before any network call. An empty sha, `latest`
# or `main` would otherwise reach the registry (or, worse, Fly) as a perfectly
# valid-looking name. A prefix-and-suffix match, not a substring one.
#
# Exit codes: 0 published; 1 malformed reference, not published (or not public),
# or the registry gave no digest; 2 usage.
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: scripts/require-image.sh <image>" >&2
  exit 2
fi
image="$1"

if [[ ! "$image" =~ ^ghcr\.io/onryde/seazn-placement:[0-9a-f]{40}$ ]]; then
  echo "::error::'$image' is not ghcr.io/onryde/seazn-placement:<40-hex commit sha>. Deploys ship an immutable sha tag, never latest, a branch or an empty tag."
  exit 1
fi

if ! inspected="$(docker buildx imagetools inspect "$image" 2>&1)"; then
  echo "::error::$image is not published, or its package is not public. ci.yml publishes it after a push to main passes every job; a tag or a push that is still running there has no image yet, so wait for ci and re-run this workflow. Fly pulls the image anonymously, so the package must also be public."
  echo "$inspected"
  exit 1
fi

if ! digest="$(awk '/^Digest:/ { print $2; found = 1; exit } END { exit found ? 0 : 1 }' <<<"$inspected")"; then
  echo "::error::$image exists but the registry reported no digest for it"
  exit 1
fi
echo "$image is published"
echo "digest: $digest"
