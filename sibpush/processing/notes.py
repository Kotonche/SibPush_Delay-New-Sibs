"""Main note-processing workflow for the SibPush add-on."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import date

from anki.cards import CARD_TYPE_NEW, QUEUE_TYPE_SIBLING_BURIED, QUEUE_TYPE_SUSPENDED, Card
from anki.collection import Collection
from anki.notes import NoteId
from aqt.utils import tooltip

from ..cards.formatting import capture_snapshots, format_note_change
from ..config.parser import config_settings
from ..logging_support import logThis
from ..state import save_persistent_state, sync_last_full_scan_date, sync_last_processed_mod_ts, sync_last_unmanaged_note_ids
from .query import (
    get_all_child_cards_batch,
    get_child_cards,
    get_new_note_ids,
    get_modified_note_ids_since,
    should_run_unmanaged_notes,
)
from .chunked_runner import run_chunked
from .progression import card_stability, is_mature, resolve_stages, transition_threshold
from .suspension import (
    card_is_ignored,
    card_is_suspended_by_addon,
    card_is_unlocked,
    mark_card_unlocked,
    note_is_ignored_deck,
    suspend_cards,
    unsuspend_cards,
)

MODIFIED_NOTE_BATCH_SIZE = 1000
MODIFIED_NOTE_BATCH_PAUSE_MS = 100
MODIFIED_NOTE_TOOLTIP_PERIOD_MS = 3000


def process_note(
    col: Collection,
    note_id: NoteId,
    coming_from_reviewer_hook: bool = False,
    prefetched_siblings: Sequence[Card] | None = None,
) -> None:
    """Reconcile one note's stages using card ordinal and FSRS Stability.

    The progression is monotonic. A reviewed card or a card carrying the unlocked marker is
    treated as previously reached even if its current Stability later falls. Suspended cards are
    restored only when their suspension provenance belongs to this add-on.

    Args:
        col (anki.collection.Collection): The collection that owns the note.
        note_id (int): The note identifier to process.
        coming_from_reviewer_hook (bool): Whether the call came from the reviewer hook.
            When True, indicates the user just answered a card, so we bury the next new
            card to prevent immediate review.
        prefetched_siblings (Sequence[Card] | None): Pre-loaded sibling cards from a batch fetch.
            When provided, skips the per-note database query. Used by batch processing to
            avoid N database calls. Pass None (default) to query the database for this note's cards.

    Returns:
        None: The collection is updated in place with suspended/unsuspended cards and tags.
    """

    debug_enabled = bool(config_settings["debug"])
    siblings = (
        prefetched_siblings if prefetched_siblings is not None else get_child_cards(col, note_id)
    )
    if len(siblings) <= 1:
        return

    if coming_from_reviewer_hook and note_is_ignored_deck(siblings[0]):
        return

    note = siblings[0].note()
    stages = [stage for stage in resolve_stages(note, siblings) if not card_is_ignored(stage.card)]
    if len(stages) <= 1:
        return

    before_snapshots = capture_snapshots(siblings) if debug_enabled else None
    actions: list[str] = []
    changed = False

    # The first existing stage is always logically available, but a user suspension is respected.
    first = stages[0]
    if not card_is_unlocked(first.card):
        mark_card_unlocked(col, first.card)
        actions.append(f"mark {first.name} unlocked")
        changed = True
    if first.card.queue == QUEUE_TYPE_SUSPENDED and card_is_suspended_by_addon(first.card):
        unsuspend_cards(col, [first.card])
        actions.append(f"restore first stage {first.name}")
        changed = True

    for previous, target in zip(stages, stages[1:]):
        threshold = transition_threshold(note, previous, target)
        stability = card_stability(previous.card)
        previously_reached = target.card.type != CARD_TYPE_NEW or card_is_unlocked(target.card)
        eligible_now = is_mature(previous.card, threshold)
        should_be_available = previously_reached or eligible_now
        newly_unlocked = should_be_available and not card_is_unlocked(target.card)

        if debug_enabled:
            logThis(
                lambda previous=previous, target=target, threshold=threshold, stability=stability,
                previously_reached=previously_reached, eligible_now=eligible_now: (
                    "[Progressive Siblings] "
                    f"note={note_id} from={previous.name!r} ord={previous.ord} "
                    f"stability={stability!r} threshold={threshold:g} mature={eligible_now} "
                    f"to={target.name!r} ord={target.ord} previously_reached={previously_reached}"
                )
            )

        if should_be_available:
            if newly_unlocked:
                mark_card_unlocked(col, target.card)
                actions.append(f"unlock {target.name}")
                changed = True

            if (
                target.card.queue == QUEUE_TYPE_SUSPENDED
                and card_is_suspended_by_addon(target.card)
            ):
                unsuspend_cards(col, [target.card])
                actions.append(f"restore {target.name}")
                changed = True

            if coming_from_reviewer_hook and newly_unlocked:
                fresh_target = col.get_card(target.card.id)
                if (
                    fresh_target.type == CARD_TYPE_NEW
                    and fresh_target.queue != QUEUE_TYPE_SUSPENDED
                    and fresh_target.queue != QUEUE_TYPE_SIBLING_BURIED
                ):
                    col.sched.bury_cards(ids=[fresh_target.id], manual=False)
                    actions.append(f"bury {target.name} until tomorrow")
                    changed = True
            continue

        # Only future New cards can be locked. Review history and manually suspended cards are
        # never rewritten, and an unlocked marker permanently wins over a later Stability drop.
        if target.card.type == CARD_TYPE_NEW and target.card.queue != QUEUE_TYPE_SUSPENDED:
            suspend_cards(col, [target.card], note_id)
            fresh_target = col.get_card(target.card.id)
            if fresh_target.queue == QUEUE_TYPE_SUSPENDED:
                actions.append(f"keep {target.name} locked")
                changed = True

    if debug_enabled and changed:
        updated_siblings = sorted(get_child_cards(col, note_id), key=lambda card: card.ord)
        after_snapshots = capture_snapshots(updated_siblings)
        logThis(
            lambda: format_note_change(
                note,
                col,
                note_id,
                before_snapshots or [],
                after_snapshots,
                "; ".join(actions),
            )
        )


def _process_note_batch(col: Collection, note_ids: Sequence[NoteId]) -> None:
    """Process a batch of notes efficiently with a single database query.

    This is a performance optimization that processes multiple notes at once.
    Instead of making N database queries (one per note), it:
    1. Fetches all sibling cards for all notes in a single query
    2. Processes each note with its prefetched siblings

    This dramatically reduces database overhead during full scans.

    Args:
        col (anki.collection.Collection): The collection to process.
        note_ids (Sequence[anki.notes.NoteId]): The note ids to process in this batch.

    Returns:
        None: Each note is processed in place via process_note().
    """

    if config_settings["debug"]:
        logThis(lambda: f"Processing {len(note_ids)} note(s)")

    # Batch fetch all sibling cards for all notes in one database query
    # Returns: {note_id: [card1, card2, ...], ...}
    all_siblings_by_nid = get_all_child_cards_batch(col, note_ids)

    # Process each note with its prefetched siblings
    for note_id in note_ids:
        process_note(col, note_id, prefetched_siblings=all_siblings_by_nid.get(note_id))


def _persist_processed_mod_timestamp(col: Collection, scan_started_at: int) -> None:
    """Persist the processed watermark after a browser scan completes."""

    sync_last_processed_mod_ts(scan_started_at)
    save_persistent_state(col)


def _show_modified_note_progress(processed_count: int, total_count: int) -> None:
    """Show a short tooltip describing modified-note scan progress."""

    tooltip(
            f"Progressive Siblings has processed {processed_count:,}/{total_count:,} notes",
        period=MODIFIED_NOTE_TOOLTIP_PERIOD_MS,
    )


def process_all_notes(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Process every eligible new note in the collection.

    Args:
        col (anki.collection.Collection): The collection to process.

    Returns:
        None: The collection is updated in place.
    """

    current_full_scan_date = date.today().isoformat()
    new_note_ids = get_new_note_ids(col)

    def _finish_success() -> None:
        sync_last_full_scan_date(current_full_scan_date)
        if on_success is not None:
            on_success()

    run_chunked(
        new_note_ids,
        lambda chunk: _process_note_batch(col, chunk),
        batch_size=MODIFIED_NOTE_BATCH_SIZE,
        pause_ms=MODIFIED_NOTE_BATCH_PAUSE_MS,
        on_complete=on_complete,
        on_success=_finish_success,
    )


def process_modified_notes(
    col: Collection,
    modified_since: int,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Process notes changed after a timestamp and persist the new scan watermark.

    The browser path uses this helper instead of a day-gated scan. The watermark is recorded at
    the end of the scan so we do not skip notes that were still waiting in later chunks when a
    browser scan began.

    Args:
        col (anki.collection.Collection): The collection to process.
        modified_since (int): The timestamp threshold for changed notes.
        on_complete (Callable[[], None] | None): Cleanup callback that always runs when the scan
            finishes or errors.
        on_success (Callable[[], None] | None): Optional callback that runs only after a
            successful scan.

    Returns:
        None: The collection is updated in place.
    """

    current_scan_timestamp = int(time.time())
    modified_note_ids = get_modified_note_ids_since(col, modified_since)

    if not modified_note_ids:
        _persist_processed_mod_timestamp(col, current_scan_timestamp)
        try:
            if on_success is not None:
                on_success()
        finally:
            if on_complete is not None:
                on_complete()
        return

    def _finish_success() -> None:
        _persist_processed_mod_timestamp(col, current_scan_timestamp)
        if on_success is not None:
            on_success()

    run_chunked(
        modified_note_ids,
        lambda chunk: _process_note_batch(col, chunk),
        batch_size=MODIFIED_NOTE_BATCH_SIZE,
        pause_ms=MODIFIED_NOTE_BATCH_PAUSE_MS,
        on_progress=_show_modified_note_progress,
        on_complete=on_complete,
        on_success=_finish_success,
    )


def process_new_unmanaged_notes(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Process only unmanaged new notes in the collection.

    This is the lighter recurring scan used after the initial startup/day-change full pass.
    It only revisits notes that are still new and do not already have the add-on tag.

    Args:
        col (anki.collection.Collection): The collection to process.

    Returns:
        None: The collection is updated in place.
    """

    should_run, current_unmanaged_note_ids = should_run_unmanaged_notes(col)
    if not should_run:
        if on_complete is not None:
            on_complete()
        return

    def _finish_success() -> None:
        sync_last_unmanaged_note_ids(current_unmanaged_note_ids)
        if on_success is not None:
            on_success()

    run_chunked(
        current_unmanaged_note_ids,
        lambda chunk: _process_note_batch(col, chunk),
        batch_size=MODIFIED_NOTE_BATCH_SIZE,
        pause_ms=MODIFIED_NOTE_BATCH_PAUSE_MS,
        on_complete=on_complete,
        on_success=_finish_success,
    )
