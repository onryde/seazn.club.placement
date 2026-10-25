#!/usr/bin/env bash
# Push the image the `docker` job already built and smoke-tested, once.
#
#   scripts/publish-image.sh <tar> <tested-tag> <image>
#
#   <tar>         the `docker` job's exported image (outputs: type=docker)
#   <tested-tag>  the tag that image carries inside the tar (placement-service:ci)
#   <image>       what to publish, ghcr.io/onryde/seazn-placement:<commit sha>
#
# The publish job never rebuilds: a rebuild would resolve floating inputs again
# (dependency ranges, the base image tag) and could push bytes nobody tested.
#
# A re-run of a push to main must not move a sha tag that already exists, so the
# registry is asked first. If `imagetools inspect` succeeds, nothing is pushed and
# the existing digest is printed. If it fails (the tag is absent, or the registry
# is unreachable), the push is attempted, and an unreachable registry fails there.
#
# Exit codes: 0 pushed, or already published; 1 a digest could not be read;
# 2 usage; anything else is the failing docker command's own code.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: scripts/publish-image.sh <tar> <tested-tag> <image>" >&2
  exit 2
fi
tar_file="$1"
tested="$2"
image="$3"

# Reads `docker buildx imagetools inspect` output on stdin and prints its
# `Digest:` value; fails when there is none.
digest_in() {
  awk '/^Digest:/ { print $2; found = 1; exit } END { exit found ? 0 : 1 }'
}

if existing="$(docker buildx imagetools inspect "$image" 2>/dev/null)"; then
  if ! digest="$(digest_in <<<"$existing")"; then
    echo "::error::$image already exists but the registry reported no digest for it"
    exit 1
  fi
  echo "::notice::$image already exists; not pushing it again"
  echo "digest: $digest"
  exit 0
fi

docker load --input "$tar_file"
docker tag "$tested" "$image"
docker push "$image"

if ! digest="$(digest_in <<<"$(docker buildx imagetools inspect "$image")")"; then
  echo "::error::pushed $image but the registry reported no digest for it"
  exit 1
fi
echo "digest: $digest"
