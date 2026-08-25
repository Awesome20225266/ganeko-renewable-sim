"""Publication invariants for generation data: what may still move, and what may not.

Three different objects, three different rules:

  * **Actual** — a published measurement of generation that has already happened.
    Once published it is a statement of record. It must never be recalculated, even
    though the weather provider keeps revising its own view of the past. A
    HISTORICAL actual is final *unconditionally* — a completed day is history, and
    history does not get a second opinion.

  * **Schedule** — a forward-looking commitment. Better forecast information is
    *supposed* to change it, but only for blocks whose operational time has not
    arrived yet. Rewriting the block you are currently inside, or one already past,
    would make any deviation measured against it meaningless. A schedule for a date
    already past is historical and therefore final, unconditionally.

  * **Diagnostic (`REPROCESSED`)** — the output of an explicit administrative
    re-run. It is deliberately NOT an Actual: it is written into its own version
    space with `is_current=False` so it is stored and inspectable but never served
    to a normal API consumer. Admin reprocess answers "what would we compute
    today?", it does not restate what was published.

Everything here is derived at read/write time from columns that already exist, so
the invariants need no schema change and no backfill of historical rows.

The cutover controls WHEN enforcement begins, not WHICH blocks are eligible. Once
the configured instant has passed, enforcement is simply on, permanently. Nothing
already stored is ever rewritten by that transition — the system just stops
rewriting from that moment onward, which is what makes the change forward-only.

This module is the ONLY place that parses the cutover configuration, decides which
block numbers are protected, and names the diagnostic version space. Block-boundary
maths lives here once (`current_block_no`) so callers cannot drift apart on the
interval convention.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config.settings import Settings, get_settings
from app.logging_conf import get_logger

logger = get_logger(__name__)

# Per-block `data_label` values that represent an ACTUAL — a published statement
# about generation that has already happened.
#
# FORECAST_SIMULATED is deliberately absent: today's not-yet-reached blocks and any
# /forecast output are predictions and must stay free to move.
#
# REPROCESSED is also absent, and that is the point: an administrative re-run is a
# diagnostic, not a republished Actual. See DIAGNOSTIC_BLOCK_LABEL.
ACTUAL_BLOCK_LABELS = frozenset({
    "LIVE_ESTIMATED",        # today, block already reached
    "HISTORICAL_SIMULATED",  # completed day
})

# Actuals that are final regardless of the cutover. A completed day is history; its
# recorded value is the record, whenever it happened to be written.
PERMANENT_ACTUAL_LABELS = frozenset({"HISTORICAL_SIMULATED"})

# The label an explicit admin re-run writes. Never served to normal consumers.
DIAGNOSTIC_BLOCK_LABEL = "REPROCESSED"

# Diagnostic rows live in their own version space so they cannot collide with — or
# displace — the published rows under uq_generation_block / uq_daily_summary, both of
# which include (simulation_version, model_assumption_version).
REPROCESS_VERSION_SUFFIX = "+reprocess"


def diagnostic_version(simulation_version: str) -> str:
    """Version string an administrative re-run writes under."""
    return f"{simulation_version}{REPROCESS_VERSION_SUFFIX}"


# The 15-minute grid the whole application is built on. `current_block_no` computes
# hour*4 + minute//15 + 1, `normalize_to_blocks` emits exactly 96 blocks and
# `quality.check_day` rejects anything else, so the grid is fixed at 15 minutes.
#
# Note this is NOT `PlantConfig.block_minutes` (also 15 by default): that value is used
# by the engines for MW->MWh and does not change the block grid. Deriving the schedule
# lock from the grid constants below keeps one source of truth for block arithmetic.
BLOCK_MINUTES = 15
BLOCKS_PER_DAY = 96


def current_block_no(tz: str, sim_date: date) -> int:
    """1-based index of the 15-minute block that *contains* now, in plant-local time.

    Interval convention, verified against `normalize_to_blocks`: block N covers
    [start, end) where start = 00:00 + 15*(N-1) minutes local. So 13:50 falls in
    block 56 (13:45-14:00) and 14:00 falls in block 57 (14:00-14:15).

    Returns 96 for a date already past (every block has happened) and 0 for a
    future date (no block has happened). Those two edges are what make the same
    helper usable for "which blocks are protected" on any date.
    """
    now = datetime.now(ZoneInfo(tz))
    if now.date() != sim_date:
        return 96 if now.date() > sim_date else 0
    return now.hour * 4 + now.minute // 15 + 1


@lru_cache(maxsize=8)
def _parse_cutover(raw: str) -> datetime | None:
    """Parse the configured cutover into an aware instant.

    Returns None to mean "no gate — enforcement is already on".

    Fail-safe direction is deliberate. A blank or malformed value protects data
    rather than exposing it: silently disabling an immutability invariant because
    an environment variable was typed wrong is the worse of the two failures, and
    a fresh environment with no legacy rows wants enforcement everywhere anyway.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning(
            "GENERATION_IMMUTABILITY_CUTOVER=%r is not an ISO-8601 timestamp; "
            "enforcing immutability immediately (fail-safe)", text,
        )
        return None
    if parsed.tzinfo is None:
        logger.warning(
            "GENERATION_IMMUTABILITY_CUTOVER=%r has no UTC offset; a naive value is "
            "ambiguous, so immutability is enforced immediately (fail-safe)", text,
        )
        return None
    return parsed


def cutover_instant(settings: Settings | None = None) -> datetime | None:
    """The configured cutover as an aware instant, or None if there is no gate."""
    settings = settings or get_settings()
    return _parse_cutover(settings.GENERATION_IMMUTABILITY_CUTOVER)


def is_enforced(settings: Settings | None = None, now: datetime | None = None) -> bool:
    """True once the cutover has passed, i.e. the new rules are live.

    Gating on "has enforcement started" rather than on each block's own timestamp is
    what keeps this forward-only *and* unconditional afterwards: no stored row is
    ever rewritten by the transition, and once the instant passes, every published
    Actual is protected — including the ones written earlier the same day.
    """
    cutover = cutover_instant(settings)
    if cutover is None:
        return True
    return (now or datetime.now(UTC)) >= cutover


def is_final_actual(data_label: str | None, settings: Settings | None = None) -> bool:
    """True if this row is a published Actual that can never change again.

    This is what the APIs expose as `is_final`. A HISTORICAL actual is final
    unconditionally; a LIVE actual is final once enforcement has begun.
    """
    if data_label in PERMANENT_ACTUAL_LABELS:
        return True
    if data_label in ACTUAL_BLOCK_LABELS:
        return is_enforced(settings)
    return False


def frozen_actual_block_nos(
    existing: dict[int, str], settings: Settings | None = None
) -> set[int]:
    """Block numbers whose already-published Actual value must be carried forward.

    `existing` maps block_no -> the stored row's data_label.
    """
    return {no for no, label in existing.items() if is_final_actual(label, settings)}


def schedule_lock_blocks(settings: Settings | None = None) -> int:
    """How many blocks AFTER the current one are operationally committed.

    Rounded UP, so a lock window configured as a non-multiple of the block grid can
    never protect less time than was asked for. 120 minutes -> 8 blocks; 0 disables
    the lock and restores the plain "current block only" boundary.
    """
    minutes = max(0, (settings or get_settings()).SCHEDULE_REVISION_LOCK_MINUTES)
    return -(-minutes // BLOCK_MINUTES)  # ceil


def _schedule_protected_upto(
    sim_date: date, tz: str, settings: Settings | None = None
) -> int | None:
    """Highest protected schedule block number, or None if the date is fully revisable.

    THE single expression defining the schedule revision boundary. Everything else
    (the protected set, the first revisable block, the scheduler's skip decision)
    reads this, so the lock arithmetic exists in exactly one place.

    Three cases, in this order — the order matters:

      1. **date already past** -> BLOCKS_PER_DAY. That schedule is historical and is
         final regardless of the cutover, so this returns before the enforcement
         check below.
      2. **future date** -> None. `current_block_no` returns 0 for a future date, so
         a naive `x + lock` would protect blocks 1..8 of tomorrow and silently freeze
         the first two hours of every day-ahead schedule. Each date carries its own
         independent schedule; the lock belongs to the day being operated, so it
         deliberately does NOT spill across midnight.
      3. **today** -> X + lock, clamped to the end of the day so no index beyond
         BLOCKS_PER_DAY is ever produced. Near midnight the clamp simply means the
         whole remainder of the day is committed and nothing is revisable.
    """
    x = current_block_no(tz, sim_date)
    if x >= BLOCKS_PER_DAY:
        return BLOCKS_PER_DAY
    if x <= 0:
        return None
    if not is_enforced(settings):
        return None
    return min(BLOCKS_PER_DAY, x + schedule_lock_blocks(settings))


def protected_schedule_block_nos(
    sim_date: date,
    block_nos,
    tz: str,
    settings: Settings | None = None,
) -> set[int]:
    """Schedule block numbers a revision must not move: blocks 1..X+lock.

    While we are operating inside block X, the schedule for the next
    SCHEDULE_REVISION_LOCK_MINUTES is already committed dispatch. At the default 120
    minutes that protects X and the following 8 blocks, so a revision first takes
    effect at X+9 — e.g. operating in 15:45-16:00, everything through 17:45-18:00
    holds and 18:00-18:15 is the first block that may move.
    """
    upto = _schedule_protected_upto(sim_date, tz, settings)
    if upto is None:
        return set()
    return {no for no in block_nos if no <= upto}


def first_revisable_schedule_block_no(
    sim_date: date, tz: str, settings: Settings | None = None
) -> int | None:
    """Lowest block number a revision may move, or None if none remain for this date.

    Lets a caller decide whether a revision is worth attempting at all without
    re-deriving the lock. Returns 1 when the whole date is revisable (a future date,
    or before the cutover).
    """
    upto = _schedule_protected_upto(sim_date, tz, settings)
    if upto is None:
        return 1
    return upto + 1 if upto < BLOCKS_PER_DAY else None
