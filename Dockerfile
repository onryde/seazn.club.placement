# uv, used only to turn uv.lock into a hash-pinned requirements file (below). It is
# a stage of its own, mounted into the one RUN that needs it, so the uv binary
# never lands in the image that ships. Pinned by tag and by digest: the digest
# makes the reference immutable, the tag is what a reader can compare with
# ci.yml's UV_VERSION (tests/test_locked_install.py holds the two equal).
FROM ghcr.io/astral-sh/uv:0.12.24@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a AS uv

# CPython 3.14.6, exact patch pin — the service is developed and tested on
# this interpreter (see pyproject.toml's dependency comments) and every
# dependency wheel installed against it is native cp314. Pinning the patch,
# not just the minor version, means a future 3.14.x release can't silently
# become the production interpreter without a diff to this file to review.
#
# `-bookworm`, not the bare alias and not alpine: ortools needs glibc >= 2.28.
# Debian bookworm ships glibc 2.36, comfortably above that floor. musl (alpine)
# has no manylinux wheel for ortools at all, so it would build from source or
# fail outright. Pinning the Debian release explicitly also stops Docker Hub's
# floating `3.14-slim` alias (which can move to a newer Debian release, e.g.
# trixie) from changing the base OS out from under this file.
#
# Verified to exist via a Docker Hub tag lookup — this machine has no
# container runtime (no `docker`, no usable `podman`/`colima`), so the image
# has never actually been pulled or built here. CI (Task 09) builds it for
# real; see the Task 08 report for what could and couldn't be proven locally.
FROM python:3.14.6-slim-bookworm

# Which repository this image belongs to. GHCR reads this label to link a new
# package to its repository (GitHub recommends it). It grants nothing on a package
# that already exists: that package's Actions access is set in its own settings
# (DEPLOY.md, §4). ci.yml passes the same label at build time; it is stated here
# too so a build that drops --label cannot lose it, and scripts/publish-image.sh
# refuses to push an image that does not carry it.
LABEL org.opencontainers.image.source="https://github.com/onryde/seazn.club.placement"

WORKDIR /app

# The dependencies come from uv.lock, not from whatever PyPI resolves to on the day
# of the build: a new upstream release (grpcio ships often) reaches this image only
# through a reviewed change to uv.lock. `uv export` writes the lock as a requirements
# file that pins every runtime dependency, transitive ones included, to an exact
# version and the hashes of its files, and `pip install --require-hashes` refuses
# anything else: an unpinned requirement, an unhashed one, a file whose hash is not
# listed. Nothing is resolved here.
#
#   --frozen            read uv.lock as it is, without re-resolving. ci.yml's
#                       `uv lock --check` is what fails a pyproject.toml edit made
#                       without a re-lock.
#   --no-dev            leave out the dev dependency group, should one ever exist
#   --no-emit-project   this package is installed on its own, below
#   --quiet             uv echoes the whole file to stdout as well as writing it;
#                       the build log does not need 300 lines of hashes
#
# No --extra, deliberately: grpcio-tools (the codegen toolchain that produces
# src/placement/generated/*.py) lives in [project.optional-dependencies] dev
# precisely so it never ships in this image — the generated stubs are committed,
# and a production image has no business carrying a compiler toolchain. Confirmed
# in a throwaway venv (no dev extra, non-editable install) that both stubs and
# placement.main still import without it; ci.yml's docker job proves it again in
# the built image.
#
# This layer depends only on pyproject.toml and uv.lock, so a source-only change
# does not rebuild it. The export goes to a file and the two commands are chained
# with `&&`, never piped: a pipe would hide the exporter's failure behind pip's.
COPY pyproject.toml uv.lock ./
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    uv export --frozen --no-dev --no-emit-project --quiet --format requirements-txt \
        --output-file /tmp/requirements.lock.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt

# Then this package alone. `--no-deps` is what keeps pip from resolving any
# dependency: every one is already installed from the lock. (pip still fetches the
# build backend named in [build-system], setuptools, into a throwaway build
# environment; it is not part of the image.) `pip check` then fails the build if
# what the lock installed does not satisfy this package's own requirements (a
# pyproject.toml edit made without a re-lock). The build needs src/ present —
# setuptools discovers `placement` as a namespace package there.
COPY src/ src/
RUN pip install --no-cache-dir --no-deps . \
    && pip check

# Logs are the only observability this process has (see main.py's docstring);
# without this, CPython buffers stdout when it isn't a TTY and `fly logs`
# lags or truncates on crash.
ENV PYTHONUNBUFFERED=1

# Non-root: create a dedicated, unprivileged user and switch to it before
# serving any traffic. `pip install` above already ran as root, and the
# installed package under site-packages is world-readable by default, so no
# extra chown is needed for this user to import and run it.
RUN useradd --create-home placement
USER placement

ENV PLACEMENT_PORT=50051
EXPOSE 50051

# PLACEMENT_SERVICE_SECRET is NOT set here — placement.config.Settings.from_env()
# requires it and refuses to start without one (fail closed, never an empty-
# string compare that would authenticate everyone). It is provided at deploy
# time via `fly secrets set`, never baked into an image layer. See fly.toml.
CMD ["python3", "-m", "placement.main"]
