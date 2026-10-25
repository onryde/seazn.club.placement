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

WORKDIR /app

# pyproject.toml copied ahead of src/ for readability (manifest before
# code), NOT for Docker layer caching. `pip install .` below needs src/
# present to build this package at all — setuptools discovers `placement` as a
# namespace package there — so this split buys nothing on its own: a
# source-only change still busts the COPY src/ layer and therefore the RUN
# pip install layer after it, exactly like a dependency change would. A real
# dependency-only cache would need a two-pass install (deps first with no
# src/, then the local package once src/ lands); not done here, since it is
# unverified complexity with no container runtime on this machine to
# build-test it against.
COPY pyproject.toml .
COPY src/ src/

# Deliberately `pip install .`, no extras: grpcio-tools (the codegen
# toolchain that produces src/placement/generated/*.py) lives in
# [project.optional-dependencies] dev precisely so it never ships in this
# image — the generated stubs are committed, and a production image has no
# business carrying a compiler toolchain. Confirmed in a throwaway venv (no
# dev extra, non-editable install) that both stubs and placement.main still
# import without it; see the Task 08 report for the exact command and output.
RUN pip install --no-cache-dir .

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
