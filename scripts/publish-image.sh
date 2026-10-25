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
# registry is asked first, and a failed question is classified, not shrugged off:
#
#   inspect succeeds        the tag exists: nothing is pushed, its digest is printed.
#   the registry says no    "not found", MANIFEST_UNKNOWN, NAME_UNKNOWN, 404, and
#                           denied/401/403 (GHCR answers 403 DENIED for a package
#                           that does not exist yet, so the first publish depends
#                           on it): the tag is absent and is pushed. A real
#                           credential failure still fails, at `docker push`.
#   anything else           5xx, 429, a timeout, TLS, EOF, DNS: the answer is
#                           unknown. It is retried a bounded number of times and
#                           then the script fails with the captured error. It
#                           never pushes on an answer it did not get. A failure
#                           that says both ("503 ..." with "not found" in its
#                           body) is unknown too: the status wins.
#
# Exit codes: 0 pushed, or already published; 1 the registry could not be asked or
# a digest could not be read; 2 usage; anything else is the failing docker
# command's own code.
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

# The registry's answer to `imagetools inspect <image>` when it failed, read from
# the output ($1). Prints "absent" or "unknown".
#
# A signal that the registry could not ANSWER wins over any word that looks like
# "absent": a 502 page whose body says "404 Not Found", or a 429 whose request id
# reads 4F2A:403:9C1, says nothing about the tag. Both patterns are word-bounded
# on the status codes so the digits of a commit sha in the image name cannot make
# a 503 read as 404 (or the reverse).
classify() {
  local could_not_answer='(^|[^[:alnum:]])(429|5[0-9][0-9]|eof)([^[:alnum:]]|$)|timeout|timed out|unavailable|gateway|reset|refused|no such host|name resolution'
  local says_absent='not found|manifest_unknown|name_unknown|denied|unauthorized|(^|[^[:alnum:]])(401|403|404)([^[:alnum:]]|$)'
  if grep -Eiq "$could_not_answer" <<<"$1"; then
    echo unknown
  elif grep -Eiq "$says_absent" <<<"$1"; then
    echo absent
  else
    echo unknown
  fi
}

attempts=3
delay=5  # seconds; the wait before attempt n+1 is delay * n

# Sets $existing to the inspect output and returns 0 when the tag exists, returns
# 1 when the registry says it does not, and exits 1 when it cannot tell.
tag_exists() {
  local attempt=1 out
  while :; do
    if out="$(docker buildx imagetools inspect "$image" 2>&1)"; then
      existing="$out"
      return 0
    fi
    if [ "$(classify "$out")" = absent ]; then
      return 1
    fi
    if [ "$attempt" -ge "$attempts" ]; then
      echo "::error::cannot tell whether $image is already published (attempt $attempt of $attempts): ${out//$'\n'/ }"
      exit 1
    fi
    echo "::warning::could not ask the registry about $image (attempt $attempt of $attempts), retrying: ${out//$'\n'/ }"
    sleep "$((delay * attempt))"
    attempt=$((attempt + 1))
  done
}

existing=""
if tag_exists; then
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
