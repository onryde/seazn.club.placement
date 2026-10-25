import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = REPO_ROOT / "proto"
PROTO_PATH = PROTO_DIR / "scheduler.proto"
TRACKED_DIR = REPO_ROOT / "src" / "placement" / "generated"
STUB_NAMES = ("scheduler_pb2.py", "scheduler_pb2_grpc.py")

# protoc emits a flat top-level `import scheduler_pb2 as scheduler__pb2` into
# the generated *_pb2_grpc.py. That resolves only if the generated directory is
# itself on sys.path, so `from placement.generated import scheduler_pb2_grpc`
# raises ModuleNotFoundError. Rewriting it to a package-relative import is the
# standard workaround, and it has to happen as part of generation because
# protoc overwrites the file on every run.
FLAT_IMPORT_RE = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)

REGEN_HINT = (
    "Generated stubs are out of date with proto/scheduler.proto. "
    "Regenerate with: venv/bin/python3 tests/test_proto_compiles.py"
)


def _generate(out_dir):
    """Compile the proto into out_dir and apply the import rewrite."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable, "-m", "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(PROTO_PATH),
        ],
        capture_output=True, text=True,
    )
    grpc_stub = out_dir / "scheduler_pb2_grpc.py"
    if grpc_stub.exists():
        grpc_stub.write_text(
            FLAT_IMPORT_RE.sub(r"from . import \1 as \2", grpc_stub.read_text())
        )
    return result


def test_generated_stubs_match_proto(tmp_path):
    """Drift gate: the tracked stubs must equal a fresh compile of the proto.

    Generation happens in tmp_path, never in the source tree, so a stale
    checked-in stub fails loudly instead of being silently healed.
    """
    result = _generate(tmp_path)
    assert result.returncode == 0, result.stderr

    for name in STUB_NAMES:
        fresh = tmp_path / name
        tracked = TRACKED_DIR / name
        assert fresh.exists(), f"protoc did not emit {name}"
        assert tracked.exists(), f"{name} is not committed. {REGEN_HINT}"
        assert tracked.read_bytes() == fresh.read_bytes(), f"{name} is stale. {REGEN_HINT}"


def test_grpc_stub_imports_as_a_package_module():
    """The rewrite exists so this import works — assert the behavior, not a string."""
    module = importlib.import_module("placement.generated.scheduler_pb2_grpc")
    assert module.SchedulerServiceStub is not None
    assert module.SchedulerServiceServicer is not None

    # Cheap diagnostic kept alongside the real check: if the import above ever
    # fails, this pinpoints the flat-import regression as the cause.
    stub_src = (TRACKED_DIR / "scheduler_pb2_grpc.py").read_text()
    assert "from . import scheduler_pb2 as scheduler__pb2" in stub_src
    assert not FLAT_IMPORT_RE.search(stub_src), (
        "generated grpc stub still contains a flat cross-module import"
    )


# Field-by-field contract. A rename, a renumber, a type change or a
# repeated/singular flip in proto/scheduler.proto breaks these — this task owns
# catching that, because a mismatch here becomes a bug in every downstream task.
#
# Tuples are (field number, wire type, is_repeated, has_presence), and BOTH of
# the last two carry a specific defect this suite has already shipped once:
#
#   is_repeated   the request's shape.
#   has_presence  proto3's `optional` keyword — the ONLY way a scalar can
#                 distinguish "not sent" from "sent as 0". Which fields carry
#                 it is a design decision, not an accident, so it is asserted
#                 rather than left to whoever edits the .proto next: adding or
#                 deleting the keyword compiles, round-trips and changes no
#                 behaviour that any other test can see, while deciding whether
#                 a whole constraint family can silently evaporate.
#
# The wire type is equally load-bearing and is the reason this file grew to
# cover the nested messages at all: `int64 -> int32` on `Tier.value` or on
# either `start_at_ms` survived every behavioural test in the suite. It cannot
# be caught by data — durations do not scale with the epoch, and the corpus's
# largest makespan (~1.5e9) fits inside int32 — so the only place the narrowing
# is observable is the descriptor.
EXPECTED_REQUEST_FIELDS = {
    "request_id":     (1,  FieldDescriptor.TYPE_STRING,  False, False),
    "court_names":    (2,  FieldDescriptor.TYPE_STRING,  True,  False),
    "entrant_count":  (3,  FieldDescriptor.TYPE_UINT32,  False, False),
    "division_count": (4,  FieldDescriptor.TYPE_UINT32,  False, False),
    "fixtures":       (5,  FieldDescriptor.TYPE_MESSAGE, True,  False),
    "slots":          (6,  FieldDescriptor.TYPE_MESSAGE, True,  False),
    "step_minutes":   (7,  FieldDescriptor.TYPE_INT32,   False, False),
    "existing":       (8,  FieldDescriptor.TYPE_MESSAGE, True,  False),
    "dependencies":   (9,  FieldDescriptor.TYPE_MESSAGE, True,  False),
    # Field 10 (`division_rules`) is RESERVED, not a live field -- see
    # `test_division_rules_field_and_name_are_reserved` below.
    "constraints":    (11, FieldDescriptor.TYPE_MESSAGE, False, True),
    "wall_seconds":   (12, FieldDescriptor.TYPE_DOUBLE,  False, False),
    "rule_groups":    (13, FieldDescriptor.TYPE_MESSAGE, True,  False),
    # C10 (2026-08-16, wire person indices design).
    "person_count":   (14, FieldDescriptor.TYPE_UINT32,  False, False),
}

EXPECTED_RESPONSE_FIELDS = {
    "assignments":      (1, FieldDescriptor.TYPE_MESSAGE, True,  False),
    "status":           (2, FieldDescriptor.TYPE_ENUM,    False, False),
    "tiers_completed":  (3, FieldDescriptor.TYPE_INT32,   False, False),
    "objective_values": (4, FieldDescriptor.TYPE_MESSAGE, True,  False),
    "elapsed_ms":       (5, FieldDescriptor.TYPE_INT64,   False, False),
    "wall_exhausted":   (6, FieldDescriptor.TYPE_BOOL,    False, False),
    "error":            (7, FieldDescriptor.TYPE_MESSAGE, False, True),
}

#: Every other message in the contract. `SolveBuildRequest`/`SolveBuildResponse`
#: were covered from the start; these were not, which is how three proto mutants
#: (`Assignment.start_at_ms` and `Tier.value` narrowed to int32, among them)
#: survived all 57 proto-aware tests.
EXPECTED_MESSAGE_FIELDS = {
    "Slot": {
        "court_index": (1, FieldDescriptor.TYPE_UINT32, False, True),
        "start_at_ms": (2, FieldDescriptor.TYPE_INT64,  False, False),
        "day_index":   (3, FieldDescriptor.TYPE_INT32,  False, True),
    },
    "Fixture": {
        "entrant_indices": (1, FieldDescriptor.TYPE_UINT32, True,  False),
        "division_index":  (2, FieldDescriptor.TYPE_UINT32, False, True),
        # C1 (2026-08-12 round-ordering design).
        "round":           (3, FieldDescriptor.TYPE_UINT32, False, True),
        # C10 (2026-08-16, wire person indices design).
        "person_indices":  (4, FieldDescriptor.TYPE_UINT32, True,  False),
        # #622 (round-scoped required court tags).
        "allowed_court_indices": (5, FieldDescriptor.TYPE_UINT32, True, False),
    },
    "PinnedRow": {
        "court_index":        (1, FieldDescriptor.TYPE_UINT32, False, True),
        "start_at_ms":        (2, FieldDescriptor.TYPE_INT64,  False, False),
        "rule_group_indices": (3, FieldDescriptor.TYPE_UINT32, True,  False),
        "entrant_indices":    (4, FieldDescriptor.TYPE_UINT32, True,  False),
        # C1 (2026-08-12 round-ordering design).
        "round":              (5, FieldDescriptor.TYPE_UINT32, False, True),
    },
    "RuleGroup": {
        "fixture_indices":      (1, FieldDescriptor.TYPE_UINT32, True,  False),
        "min_rest_minutes":     (2, FieldDescriptor.TYPE_INT32,  False, True),
        "max_fixtures_per_day": (3, FieldDescriptor.TYPE_INT32,  False, True),
    },
    "Assignment": {
        "fixture_index": (1, FieldDescriptor.TYPE_UINT32, False, False),
        "court_index":   (2, FieldDescriptor.TYPE_UINT32, False, False),
        "start_at_ms":   (3, FieldDescriptor.TYPE_INT64,  False, False),
    },
    "OrderPair": {
        "before_index": (1, FieldDescriptor.TYPE_UINT32, False, True),
        "after_index":  (2, FieldDescriptor.TYPE_UINT32, False, True),
    },
    "BuildConstraints": {
        "match_minutes": (1, FieldDescriptor.TYPE_INT32, False, False),
        "gap_minutes":   (2, FieldDescriptor.TYPE_INT32, False, True),
    },
    "Tier": {
        "name":     (1, FieldDescriptor.TYPE_STRING, False, False),
        "value":    (2, FieldDescriptor.TYPE_INT64,  False, False),
    },
    "SolveError": {
        "code":    (1, FieldDescriptor.TYPE_STRING, False, False),
        "message": (2, FieldDescriptor.TYPE_STRING, False, False),
    },
}

EXPECTED_SOLVE_STATUS = {
    "SOLVE_STATUS_UNSPECIFIED": 0,
    "SOLVE_STATUS_OPTIMAL": 1,
    "SOLVE_STATUS_FEASIBLE": 2,
    "SOLVE_STATUS_INFEASIBLE": 3,
    "SOLVE_STATUS_UNKNOWN": 4,
    "SOLVE_STATUS_ERROR": 5,
}


def _assert_fields(descriptor, expected):
    actual = descriptor.fields_by_name
    assert set(actual) == set(expected), (
        f"{descriptor.full_name} field set changed: "
        f"unexpected={sorted(set(actual) - set(expected))}, "
        f"missing={sorted(set(expected) - set(actual))}"
    )
    for name, (number, type_, repeated, presence) in expected.items():
        field = actual[name]
        got = (field.number, field.type, field.is_repeated, field.has_presence)
        assert got == (number, type_, repeated, presence), (
            f"{descriptor.full_name}.{name} changed: "
            f"got (number={field.number}, type={field.type}, repeated={field.is_repeated}, "
            f"has_presence={field.has_presence}), "
            f"expected (number={number}, type={type_}, repeated={repeated}, has_presence={presence})"
        )


def test_solve_build_request_contract():
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    _assert_fields(pb2.SolveBuildRequest.DESCRIPTOR, EXPECTED_REQUEST_FIELDS)


def test_division_rules_field_and_name_are_reserved():
    """`division_rules` (proto field 10, the `DivisionRule`-typed predecessor
    of `rule_groups`) was retired 2026-08-12 -- see the retirement design doc
    and `placement.schema`'s module docstring. RESERVED, not just deleted:
    reusing field 10 for something else would be a wire collision against any
    peer still holding bytes encoded under the old meaning during a deploy
    window, and reusing the NAME would silently un-reserve the number for a
    generator that keys off name rather than number. Both are asserted, and
    both must fail on a proto that only deletes the field without reserving
    it -- deletion alone leaves the number and name free for the next field
    added, which is exactly the renumbering `_RULES.md` calls "never safe
    across the stg/prod deploy split".
    """
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    proto = descriptor_pb2.DescriptorProto()
    pb2.SolveBuildRequest.DESCRIPTOR.CopyToProto(proto)

    assert (10, 11) in [(rr.start, rr.end) for rr in proto.reserved_range], (
        f"field 10 is not reserved on SolveBuildRequest: {list(proto.reserved_range)}"
    )
    assert "division_rules" in proto.reserved_name, (
        f"the name 'division_rules' is not reserved on SolveBuildRequest: {list(proto.reserved_name)}"
    )
    # And the field is genuinely gone as a live field, not merely reserved
    # ALONGSIDE a lingering declaration (which protoc would refuse to compile
    # anyway, but the refusal happens before this test ever runs).
    assert "division_rules" not in pb2.SolveBuildRequest.DESCRIPTOR.fields_by_name
    assert not hasattr(pb2, "DivisionRule")


def test_solve_build_response_contract():
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    _assert_fields(pb2.SolveBuildResponse.DESCRIPTOR, EXPECTED_RESPONSE_FIELDS)


@pytest.mark.parametrize("message_name", sorted(EXPECTED_MESSAGE_FIELDS))
def test_nested_message_contract(message_name):
    """Every message the two top-level ones are built out of.

    Parametrized so a narrowing names the message it happened in, rather than
    reporting one failure for the whole contract.
    """
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    _assert_fields(getattr(pb2, message_name).DESCRIPTOR, EXPECTED_MESSAGE_FIELDS[message_name])


def test_every_message_in_the_proto_is_covered():
    """The contract above must not silently stop covering a new message.

    Without this, adding a message to `scheduler.proto` and forgetting to
    describe it here is invisible — the per-message tests only check what they
    already know about, which is exactly how the nested messages went
    uncovered for four review rounds.
    """
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    declared = set(pb2.DESCRIPTOR.message_types_by_name)
    covered = set(EXPECTED_MESSAGE_FIELDS) | {"SolveBuildRequest", "SolveBuildResponse"}
    assert declared == covered, (
        f"messages in scheduler.proto not covered by a field contract: {sorted(declared - covered)}; "
        f"contracts for messages that no longer exist: {sorted(covered - declared)}"
    )


def test_solve_status_enum_contract():
    pb2 = importlib.import_module("placement.generated.scheduler_pb2")
    enum = pb2.SolveStatus.DESCRIPTOR
    actual = {value.name: value.number for value in enum.values}
    assert actual == EXPECTED_SOLVE_STATUS


if __name__ == "__main__":
    # Regeneration entry point, referenced by REGEN_HINT. This is the only
    # path that writes into the source tree; the tests never do.
    outcome = _generate(TRACKED_DIR)
    if outcome.returncode != 0:
        sys.exit(outcome.stderr)
    print(f"regenerated {', '.join(STUB_NAMES)} in {TRACKED_DIR}")
