"""The corpus contract: what board the service is actually tested against.

Every acceptance test in `test_model.py`, `test_objective.py` and
`test_server.py` drives `placement_bench_boards.production_board()`. Nothing
asserted anything about that board, so its shape was free to move. Two
measured consequences of that, and this file exists for both:

  * `test_model.py`'s module docstring claims "37 fixtures, 5 courts, 26 days
    x 5 courts x 64 ticks" and no assertion enforced it. `bench/` is a
    benchmark directory — its whole purpose is experiments — so raising
    `PROD_TARGET_SLOTS` for a sweep silently changes what the SERVICE is
    tested against. `35 <= len(assignments) <= 37` passes against almost any
    board of roughly this size.
  * the day cap is DERIVED (`max(1, ceil(37 / 2 / 26)) == 1`), not chosen, and
    it is what makes the day-cap constraint bind at all. A looser lattice
    loosens the cap's grip and the day-cap mutant stops being killed — a guard
    going vacuous with no test changing and nobody noticing.

Prompt 02 Step 2b told its implementer to run `pytest bench/` to confirm the
board extraction survived. `bench/` contains no `test_*.py` and `pyproject.toml`
sets no `testpaths`, so that command exits 5 ("no tests ran") and always would
have — the verification was vacuous when it was written, and nothing in
`tests/` imported `placement_bench` at all. The import test below is the real
version of it.

Nothing here solves anything: these are assertions about DATA, and the file
must stay fast enough that there is never a reason to skip it.
"""

import pytest

from placement.model import DAY_MS, MIN_MS

# --- the shape the service is entitled to assume ----------------------------
#
# Change one of these only together with the board, deliberately, and expect
# `test_objective.py`'s proved-optimum constants to move with it — they are
# optima OF THIS BOARD.
EXPECTED_FIXTURES = 37
EXPECTED_COURTS = 5
EXPECTED_GRID_SLOTS = 8_320  # 26 days x 5 courts x 64 ticks
EXPECTED_DISTINCT_STARTS = 1_664  # 26 x 64 — courts share one tick set
EXPECTED_DAY_BUCKETS = 26
EXPECTED_STEP_MINUTES = 10
EXPECTED_EXISTING_ROWS = 3
EXPECTED_DEPENDENCY_PAIRS = 3

#: NOTE 40/10, not the 30/10 that `02-build-model.md`'s acceptance criteria and
#: `docs/superpowers/specs/2026-08-07-placement-scheduler-design.md` both state.
#: The board has been 40/10 since it was written; the plan and the spec carry a
#: stale number. Asserted here at the value that actually runs, so the
#: divergence is visible to the next reader instead of silently unfalsifiable.
EXPECTED_MATCH_MINUTES = 40
EXPECTED_GAP_MINUTES = 10

EXPECTED_REST_BY_DIVISION = {"d1": 80, "d2": 80}
EXPECTED_DAY_CAP_BY_DIVISION = {"d1": 1, "d2": 1}

#: The corpus must clear two different bars, and ONE constant is chosen to
#: clear both — so read this before adding a second data assertion that looks
#: like it is pulling its weight.
#:
#:   bar 1  separate a legitimate timestamp from proto3's unset scalar 0. With
#:          the old `EPOCH_MS = 0` corpus those were the same value, which is
#:          how an `existing` row with an unset `start_at_ms` built its
#:          blocking interval at epoch 0, overlapped nothing, and let a movable
#:          fixture take the pinned slot — OPTIMAL, `error` unset, through four
#:          review rounds.
#:   bar 2  keep an `int64 -> int32` narrowing of `Assignment.start_at_ms`
#:          OBSERVABLE. This is a real and separate bar: a corpus can sit above
#:          0 and still fit in int32 (any date before 1970-01-26), clearing
#:          bar 1 while staying blind to the narrowing.
#:
#: `MIN_REAL_EPOCH_MS` clears both, because it is itself far above `INT32_MAX`.
#: That makes bar 2 IMPLIED by bar 1 rather than independently checkable
#: against the data — so `min(starts) > INT32_MAX` would be unreachable behind
#: `min(starts) > MIN_REAL_EPOCH_MS` and is deliberately not asserted. What IS
#: asserted is the implication itself, on the constants, so that a future
#: attempt to "just lower the epoch floor a bit" trips immediately.
#:
#: Bar 2 is not an inference — it was demonstrated. Narrowing
#: `Assignment.start_at_ms` to int32 in `proto/scheduler.proto`, regenerating
#: the stubs, and running `test_server.py -k production_board`:
#:
#:     real-epoch corpus (this one)   -> RED
#:     same narrowing, EPOCH_MS = 0   -> GREEN      <- the control
#:
#: i.e. the corpus, not a missing assertion, was what hid the mutant.
MIN_REAL_EPOCH_MS = 1_000_000_000_000
INT32_MAX = 2_147_483_647


@pytest.fixture(scope="module")
def board():
    from placement_bench_boards import production_board

    return production_board()


# --- the extraction itself (Prompt 02 Step 2b's real verification) ----------


def test_placement_bench_still_imports():
    """The sweep script must survive the board extraction.

    `placement_bench.py` imports the moved names back out of
    `placement_bench_boards.py`, so a rename or deletion there breaks the sweep —
    and the sweep is not run by any test, by CI, or by `pytest bench/`. This
    import is the only thing standing between a board edit and a broken bench.
    """
    import placement_bench

    assert callable(placement_bench.build_board)
    assert callable(placement_bench.greedy_seed)


def test_bench_and_service_share_one_generator_rather_than_two_copies():
    """The point of the extraction: ONE generator, not a copy that drifts.

    Asserted by object identity, not by comparing values — two copies with the
    same constants today would pass a value comparison and still drift
    tomorrow.
    """
    import placement_bench
    import placement_bench_boards

    shared = (
        "DAY_MS", "DIVISIONS", "GAP_MIN", "MATCH_MIN", "MIN_MS",
        "Board", "Existing", "Fixture", "build_board", "dependency_pairs_for",
        "entrant_of", "existing_rows_for", "intervals_overlap",
    )
    for name in shared:
        assert getattr(placement_bench, name) is getattr(placement_bench_boards, name), (
            f"placement_bench.{name} is no longer the object placement_bench_boards defines — "
            "the sweep and the service tests have forked their board generation"
        )


def test_production_board_returns_the_documented_seven_tuple(board):
    """`build_model`'s parameter list, positionally. A reordering here mislands
    every argument in every acceptance test at once."""
    assert len(board) == 7
    fixtures, courts, grid_slots, step_minutes, constraints, existing, dependencies = board

    assert all(
        isinstance(fid, str) and isinstance(entrants, list) and isinstance(division, str)
        for fid, entrants, division in fixtures
    )
    assert all(isinstance(court, str) for court in courts)
    assert all(
        isinstance(court, str) and isinstance(start, int) and isinstance(day, int)
        for court, start, day in grid_slots
    )
    assert isinstance(step_minutes, int)
    assert set(constraints) == {"match_minutes", "gap_minutes", "rest_by_division", "day_cap_by_division"}
    assert all(
        isinstance(fid, str) and isinstance(court, str) and isinstance(start, int)
        for fid, court, start in existing
    )
    assert all(isinstance(before, str) and isinstance(after, str) for before, after in dependencies)


# --- the shape ---------------------------------------------------------------


def test_production_board_has_the_shape_the_service_tests_claim(board):
    fixtures, courts, grid_slots, step_minutes, constraints, existing, dependencies = board

    assert len(fixtures) == EXPECTED_FIXTURES
    assert len(courts) == EXPECTED_COURTS
    assert len(grid_slots) == EXPECTED_GRID_SLOTS
    assert len({start for _court, start, _day in grid_slots}) == EXPECTED_DISTINCT_STARTS
    assert step_minutes == EXPECTED_STEP_MINUTES
    assert len(existing) == EXPECTED_EXISTING_ROWS
    assert len(dependencies) == EXPECTED_DEPENDENCY_PAIRS

    assert constraints["match_minutes"] == EXPECTED_MATCH_MINUTES
    assert constraints["gap_minutes"] == EXPECTED_GAP_MINUTES
    assert constraints["rest_by_division"] == EXPECTED_REST_BY_DIVISION
    assert constraints["day_cap_by_division"] == EXPECTED_DAY_CAP_BY_DIVISION

    # Every court offers the identical tick set — `build_model` unions the
    # starts across courts, so a per-court grid would be silently flattened.
    per_court = {}
    for court, start, _day in grid_slots:
        per_court.setdefault(court, set()).add(start)
    assert {frozenset(s) for s in per_court.values()} == {frozenset(next(iter(per_court.values())))}

    assert all(court in courts for _fid, court, _start in existing)


def test_the_day_cap_still_binds_hard_enough_to_be_testable(board):
    """The day cap's TIGHTNESS is what gives the day-cap constraint its teeth.

    `day_cap` is derived — `max(1, ceil(37 / 2 / 26)) == 1` — not chosen. The
    larger division has 19 fixtures and there are 26 day buckets at a cap of 1,
    so seven days of slack. Widen the lattice and the cap stops binding;
    deleting the day-cap constraint from the model then changes nothing and its
    mutant survives, with no test having been edited.
    """
    fixtures, _courts, grid_slots, _step, constraints, _existing, _deps = board
    per_division = {}
    for _fid, _entrants, division in fixtures:
        per_division[division] = per_division.get(division, 0) + 1
    assert per_division == {"d1": 19, "d2": 18}

    buckets = len({day for _court, _start, day in grid_slots})
    assert buckets == EXPECTED_DAY_BUCKETS
    biggest = max(per_division.values())
    cap = constraints["day_cap_by_division"]["d1"]
    capacity = buckets * cap
    assert biggest <= capacity, "the cap is unsatisfiable — the board cannot place its own fixtures"
    assert capacity - biggest <= 10, (
        f"the day cap has gone slack: {biggest} fixtures against {capacity} capped slots. "
        "It no longer binds, so deleting the day-cap constraint would not change the board "
        "and test_model.py's day-cap assertion is vacuous."
    )


# --- the corpus is real epoch milliseconds ----------------------------------


def test_every_corpus_timestamp_is_a_real_epoch_millisecond(board):
    """Separates a legitimate timestamp from proto3's unset 0, and makes an
    int64->int32 narrowing of `Assignment.start_at_ms` observable.

    With a zero-based corpus those are the same value, which is how an
    `existing` row with an unset `start_at_ms` could build its blocking
    interval at epoch 0, overlap nothing, and let a movable fixture take the
    pinned slot — returned OPTIMAL, `error` unset, through four review rounds.
    """
    _fixtures, _courts, grid_slots, _step, _constraints, existing, _deps = board

    starts = [start for _court, start, _day in grid_slots]

    # The threshold must itself clear int32, or bar 1 stops implying bar 2 and
    # the corpus could satisfy every assertion below while being blind to the
    # narrowing. Asserted on the CONSTANTS because that is where the claim
    # lives; asserting it on `min(starts)` as well would be unreachable code.
    assert MIN_REAL_EPOCH_MS > INT32_MAX

    assert min(starts) > MIN_REAL_EPOCH_MS, (
        "the corpus has drifted back towards epoch 0, where proto3's unset start_at_ms and a "
        "legitimate test timestamp are the same value"
    )
    # The `existing` rows are the ones that matter most and they are NOT
    # covered by the assertion above — that one reads `grid_slots`. These are
    # the `Assignment` messages an int64->int32 narrowing would truncate, and
    # the row whose unset start silently voided a pin.
    assert min(start for _fid, _court, start in existing) > MIN_REAL_EPOCH_MS, (
        "a pinned row is not a real epoch timestamp — an unset start_at_ms builds its blocking "
        "interval at epoch 0, overlaps nothing, and the pin is silently ignored"
    )


def test_the_corpus_epoch_is_utc_midnight_aligned():
    """A session day must be exactly ONE day-cap bucket.

    `build_board` opens each session day at `EPOCH_MS + d*DAY_MS + OPEN_MS` and
    runs it for a fixed number of ticks; `placement_bench_boards.day_index_of`
    labels each tick with `start_ms // DAY_MS`, a UTC day. Those two agree only
    while `EPOCH_MS` is a whole number of UTC days.

    That quotient used to live in `placement.model`. Prompt 05c moved it out — the
    wire now carries a caller-resolved `Slot.day_index`, because a UTC bucket
    is the wrong one for any org away from UTC — so this test now guards the
    CORPUS's derivation rather than the solver's. The requirement and every
    measurement below are unchanged: the corpus is a UTC corpus and still says
    exactly which day each tick is on by dividing.

    Measured at `EPOCH_MS = 1767261600000` (2026-01-01T10:00:00Z, ten hours off
    midnight): each session day opens at 18:00Z and runs to 04:30Z the next
    day, so it straddles UTC midnight and occupies TWO cap buckets. The board
    grows from 26 buckets to 27 and the solver legally places two `d1` fixtures
    on session day 0 and two `d2` on session day 15 against a cap of one — and
    `test_model.py`'s day-cap assertion still passes, because it reads the same
    day label the model was given. The cap silently doubles and every guard
    around it keeps reporting green.
    """
    import placement_bench_boards as boards

    assert boards.EPOCH_MS % DAY_MS == 0, (
        f"EPOCH_MS={boards.EPOCH_MS} is {(boards.EPOCH_MS % DAY_MS) // 3_600_000}h off UTC midnight. "
        "Session days will straddle a UTC midnight and each will consume two day-cap buckets, "
        "silently doubling every per-division day cap."
    )

    # ...and prove the consequence rather than only the arithmetic: one session
    # day's ticks must all land in one bucket.
    ticks_per_day = boards.SLOTS_PER_COURT_DAY_CAP  # sizing cap, >= the real per-day tick span
    day_open = boards.EPOCH_MS + boards.OPEN_MS
    last_tick = day_open + (ticks_per_day * boards.TICKS_PER_COARSE_SLOT - 1) * boards.STEP_MIN * MIN_MS
    assert day_open // DAY_MS == last_tick // DAY_MS, (
        "a session day spans two UTC day buckets — the per-division day cap applies twice per "
        "session day instead of once"
    )
