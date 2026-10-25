#!/usr/bin/env python3
"""Fail unless every address a Fly app holds is a private (Flycast) IPv6.

    scripts/require-private-ips.py <app>

The placement service is internal: it is reached only over Fly private networking
(``placement.flycast:50051``), never from the internet. DEPLOY.md, step 2 allocates
its one private v6; the design's security model (a shared secret instead of mTLS)
assumes there is nothing else. This runs after each deploy and checks it still holds:

    flyctl ips list --app <app> --json

Every entry must be BOTH ``Type == "private_v6"`` and an ``Address`` under Fly's
Flycast prefix ``fdaa:``. flyctl derives the type from the prefix, so the two halves
agree today; requiring both means neither can be wrong alone. Anything else fails:
a public v4, a shared v4, a public v6, an egress address, an entry this script cannot
read. So does a list with no address at all: with no private v6 ``.flycast`` does not
resolve and every solve silently falls back to a greedy board (DEPLOY.md, step 2).

The keys are flyctl's own (``fly.IPAddress`` has no json tags, so they are
capitalised). If flyctl ever renames them, entries stop matching and this FAILS; it
never passes on an entry it cannot read.

Nothing here allocates or releases an address; the error message points at
DEPLOY.md for that.

Exit codes: 0 all private; 1 anything else, or flyctl failed or could not be run; 2 usage.
"""

import json
import subprocess
import sys

PRIVATE_TYPE = "private_v6"
FLYCAST_PREFIX = "fdaa:"


def fail(message):
    print(f"::error::{message}")
    return 1


def is_private_v6(entry):
    if not isinstance(entry, dict):
        return False
    address, kind = entry.get("Address"), entry.get("Type")
    return kind == PRIVATE_TYPE and isinstance(address, str) and address.startswith(FLYCAST_PREFIX)


def describe(entry):
    if isinstance(entry, dict):
        return f"{entry.get('Address')!r} (type {entry.get('Type')!r})"
    return repr(entry)


def main(argv):
    if len(argv) != 2 or not argv[1]:
        print("usage: scripts/require-private-ips.py <app>", file=sys.stderr)
        return 2
    app = argv[1]

    try:
        result = subprocess.run(
            ["flyctl", "ips", "list", "--app", app, "--json"], capture_output=True, text=True
        )
    except OSError as error:
        # flyctl missing from PATH (FileNotFoundError) or not runnable (PermissionError):
        # an annotated failure, never a traceback, and never a pass.
        return fail(f"could not run flyctl ({type(error).__name__}: {error}); the private-address check did not run")
    if result.returncode != 0:
        return fail(f"flyctl ips list --app {app} failed (exit {result.returncode}): {result.stderr.strip()}")
    try:
        addresses = json.loads(result.stdout)
    except ValueError:
        return fail(f"flyctl ips list --app {app} --json did not print JSON")
    if not isinstance(addresses, list):
        return fail(f"flyctl ips list --app {app} --json printed {type(addresses).__name__}, not a list of addresses")
    if not addresses:
        return fail(
            f"{app} holds no IP address at all. It needs its one private v6 or .flycast does not "
            "resolve and every solve silently falls back to a greedy board (DEPLOY.md, step 2)."
        )

    offenders = [describe(entry) for entry in addresses if not is_private_v6(entry)]
    if offenders:
        return fail(
            f"{app} holds {len(offenders)} address(es) that are not a private v6: {', '.join(offenders)}. "
            "The service must be reachable over Fly private networking only (DEPLOY.md, step 3)."
        )

    print(f"{app}: {len(addresses)} address(es), all private v6")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
