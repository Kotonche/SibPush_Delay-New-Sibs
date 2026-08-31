"""Versioned startup-migration helpers for the SibPush add-on."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast

from anki.collection import Collection
from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.notes import NoteId

from .logging_support import logThis
from . import state
from .processing.chunked_runner import run_chunked
from .processing.notes import process_all_notes
from .processing.suspension import card_is_suspended_by_addon, mark_card_suspended_by_addon

LEGACY_SUSPENDED_TAG = "SibPush-suspended"
VERSION_2_0_0 = (2, 0, 0)
VERSION_2_1_0 = (2, 1, 0)
VERSION_3_0_0 = (3, 0, 0)

StartupMigration = Callable[[Collection, Callable[[], None] | None], None]


MIGRATION_BATCH_SIZE = 1000
MIGRATION_BATCH_PAUSE_MS = 100


def migrate_legacy_ignore_markers(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Convert the legacy ``{"sibpush": "ignored"}`` data to the ignore marker.

    The search is intentionally broad and the JSON/value check remains authoritative. Invalid
    or non-object payloads are left byte-for-byte unchanged so startup cannot destroy another
    add-on's custom data.
    """
    migrated_count = 0
    skipped_count = 0
    candidate_ids = col.find_cards(f"has-cd:{state.LEGACY_ADDON_CUSTOM_DATA_KEY}")

    def _migrate_chunk(card_ids: Sequence[int]) -> None:
        nonlocal migrated_count, skipped_count
        for card_id in card_ids:
            card = col.get_card(card_id)
            raw_custom_data = getattr(card, "custom_data", "")
            try:
                parsed: Any = json.loads(raw_custom_data) if raw_custom_data else {}
            except (TypeError, json.JSONDecodeError):
                skipped_count += 1
                continue

            if not isinstance(parsed, dict):
                skipped_count += 1
                continue

            parsed = cast(dict[str, Any], parsed)
            if parsed.get(state.LEGACY_ADDON_CUSTOM_DATA_KEY) == state.LEGACY_ADDON_CUSTOM_DATA_IGNORED_VALUE:
                parsed[state.SIBPUSH_IGNORED_KEY] = state.SIBPUSH_MARKER_VALUE
                parsed.pop(state.LEGACY_ADDON_CUSTOM_DATA_KEY, None)
                card.custom_data = json.dumps(parsed, ensure_ascii=False) if parsed else ""
                col.update_card(card)
                migrated_count += 1

    def _finish_migration() -> None:
        if migrated_count or skipped_count:
            logThis(
                lambda: (
                    "SibPush migrated "
                    f"{migrated_count:,} legacy ignored card marker(s)"
                    + (f"; skipped {skipped_count:,} invalid payload(s)" if skipped_count else "")
                )
            )
        if on_success is not None:
            on_success()

    run_chunked(
        list(candidate_ids),
        _migrate_chunk,
        batch_size=MIGRATION_BATCH_SIZE,
        pause_ms=MIGRATION_BATCH_PAUSE_MS,
        on_complete=on_complete,
        on_success=_finish_migration,
    )


def migrate_legacy_suspension_tag(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Convert legacy suspension tags into card-level suspension provenance.

    Args:
        col (anki.collection.Collection): The collection containing the tagged notes.
        on_complete (Callable[[], None] | None): Optional callback after tag cleanup.

    Returns:
        None: The migration is performed for its side effects.
    """
    tagged_note_ids: set[NoteId] = set()
    marked_card_count = 0
    completion_called = False
    collection_stage_continued = False

    def _complete() -> None:
        nonlocal completion_called
        if completion_called:
            return
        completion_called = True
        if on_complete is not None:
            on_complete()

    def _collect_tagged_notes(card_ids: Sequence[int]) -> None:
        for card_id in card_ids:
            card = col.get_card(card_id)
            tagged_note_ids.add(card.note().id)

    def _mark_tagged_note_chunk(note_ids: Sequence[NoteId]) -> None:
        nonlocal marked_card_count
        for note_id in note_ids:
            for card_id in col.card_ids_of_note(note_id):
                card = col.get_card(card_id)
                if card.queue != QUEUE_TYPE_SUSPENDED or card_is_suspended_by_addon(card):
                    continue

                mark_card_suspended_by_addon(col, card)
                if card_is_suspended_by_addon(col.get_card(card.id)):
                    marked_card_count += 1

    def _finish_migration() -> None:
        col.tags.remove(LEGACY_SUSPENDED_TAG)

        logThis(
            lambda: (
                "SibPush migrated legacy suspension tags on "
                f"{len(tagged_note_ids):,} note(s); marked "
                f"{marked_card_count:,} suspended card(s)"
            )
        )

        if on_success is not None:
            on_success()

    def _start_card_migration() -> None:
        nonlocal collection_stage_continued
        collection_stage_continued = True
        if not tagged_note_ids:
            if on_success is not None:
                on_success()
            else:
                _complete()
            return

        run_chunked(
            list(tagged_note_ids),
            _mark_tagged_note_chunk,
            batch_size=MIGRATION_BATCH_SIZE,
            pause_ms=MIGRATION_BATCH_PAUSE_MS,
            on_complete=_complete,
            on_success=_finish_migration,
        )

    def _finish_collection_stage() -> None:
        if not collection_stage_continued:
            _complete()

    run_chunked(
        list(col.find_cards(f"tag:{LEGACY_SUSPENDED_TAG}")),
        _collect_tagged_notes,
        batch_size=MIGRATION_BATCH_SIZE,
        pause_ms=MIGRATION_BATCH_PAUSE_MS,
        on_complete=_finish_collection_stage,
        on_success=_start_card_migration,
    )


def migrate_sibpush_2_card_markers(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Transfer SibPush 2.x provenance into Progressive Siblings' marker namespace."""

    candidate_ids = set(col.find_cards(f"has-cd:{state.LEGACY_SIBPUSH_SUSPENDED_KEY}"))
    candidate_ids.update(col.find_cards(f"has-cd:{state.LEGACY_SIBPUSH_IGNORED_KEY}"))
    migrated_suspensions = 0
    migrated_ignored = 0
    skipped_count = 0

    def _migrate_chunk(card_ids: Sequence[int]) -> None:
        nonlocal migrated_suspensions, migrated_ignored, skipped_count
        for card_id in card_ids:
            card = col.get_card(card_id)
            raw_custom_data = getattr(card, "custom_data", "")
            try:
                parsed: Any = json.loads(raw_custom_data) if raw_custom_data else {}
            except (TypeError, json.JSONDecodeError):
                skipped_count += 1
                continue
            if not isinstance(parsed, dict):
                skipped_count += 1
                continue

            parsed = cast(dict[str, Any], parsed)
            changed = False
            if parsed.get(state.LEGACY_SIBPUSH_SUSPENDED_KEY) is True:
                if card.queue == QUEUE_TYPE_SUSPENDED:
                    parsed[state.SIBPUSH_SUSPENDED_KEY] = state.SIBPUSH_MARKER_VALUE
                    migrated_suspensions += 1
                parsed.pop(state.LEGACY_SIBPUSH_SUSPENDED_KEY, None)
                changed = True

            if parsed.get(state.LEGACY_SIBPUSH_IGNORED_KEY) is True:
                parsed[state.SIBPUSH_IGNORED_KEY] = state.SIBPUSH_MARKER_VALUE
                parsed.pop(state.LEGACY_SIBPUSH_IGNORED_KEY, None)
                migrated_ignored += 1
                changed = True

            if changed:
                card.custom_data = json.dumps(parsed, ensure_ascii=False) if parsed else ""
                col.update_card(card)

    def _finish_migration() -> None:
        if migrated_suspensions or migrated_ignored or skipped_count:
            logThis(
                lambda: (
                    "Progressive Siblings migrated SibPush 2.x markers: "
                    f"{migrated_suspensions:,} suspension(s), "
                    f"{migrated_ignored:,} ignored card(s)"
                    + (f"; skipped {skipped_count:,} invalid payload(s)" if skipped_count else "")
                )
            )
        if on_success is not None:
            on_success()

    run_chunked(
        list(candidate_ids),
        _migrate_chunk,
        batch_size=MIGRATION_BATCH_SIZE,
        pause_ms=MIGRATION_BATCH_PAUSE_MS,
        on_complete=on_complete,
        on_success=_finish_migration,
    )


def migrate_to_version_3(
    col: Collection, on_complete: Callable[[], None] | None = None
) -> None:
    """Transfer inherited SibPush state, then reconcile every managed note by Stability."""

    completed = False
    suspension_stage_continued = False
    ignore_stage_continued = False
    marker_stage_continued = False

    def _complete() -> None:
        nonlocal completed
        if completed:
            return
        completed = True
        if on_complete is not None:
            on_complete()

    def _finish_reconciliation() -> None:
        state.reset_persistent_state(col)
        state.installed_version = state.ADDON_VERSION
        state.save_persistent_state(col)
        logThis("Progressive Siblings completed the SibPush migration and FSRS reconciliation")

    def _after_marker_migration() -> None:
        nonlocal marker_stage_continued
        marker_stage_continued = True
        process_all_notes(
            col,
            on_complete=_complete,
            on_success=_finish_reconciliation,
        )

    def _finish_marker_stage() -> None:
        if not marker_stage_continued:
            _complete()

    def _after_ignore_migration() -> None:
        nonlocal ignore_stage_continued
        ignore_stage_continued = True
        migrate_sibpush_2_card_markers(
            col,
            on_complete=_finish_marker_stage,
            on_success=_after_marker_migration,
        )

    def _finish_ignore_stage() -> None:
        if not ignore_stage_continued:
            _complete()

    def _after_suspension_migration() -> None:
        nonlocal suspension_stage_continued
        suspension_stage_continued = True
        migrate_legacy_ignore_markers(
            col,
            on_complete=_finish_ignore_stage,
            on_success=_after_ignore_migration,
        )

    def _finish_suspension_stage() -> None:
        if not suspension_stage_continued:
            _complete()

    migrate_legacy_suspension_tag(
        col,
        on_complete=_finish_suspension_stage,
        on_success=_after_suspension_migration,
    )


def migrate_to_version_2(
    col: Collection, on_complete: Callable[[], None] | None = None
) -> None:
    """Apply the version-2 startup recovery pack.

    The pack remains the single place where the v2 upgrade behavior lives so future
    breaking versions can add new packs without changing the hook entry point.
    """

    def _finish_version_2_recovery() -> None:
        state.reset_persistent_state(col)
        state.save_persistent_state(col)
        state.installed_version = state.ADDON_VERSION
        logThis("SibPush performed version-2 recovery on collection load")

    completion_called = False
    suspension_stage_continued = False
    ignore_stage_continued = False

    def _complete() -> None:
        nonlocal completion_called
        if completion_called:
            return
        completion_called = True
        if on_complete is not None:
            on_complete()

    def _after_legacy_cleanup() -> None:
        nonlocal ignore_stage_continued
        ignore_stage_continued = True
        migrate_legacy_ignore_markers(
            col,
            on_complete=_finish_ignore_stage,
            on_success=lambda: process_all_notes(
                col, on_complete=_complete, on_success=_finish_version_2_recovery
            ),
        )

    def _finish_ignore_stage() -> None:
        if not ignore_stage_continued:
            _complete()

    def _start_ignore_stage() -> None:
        nonlocal suspension_stage_continued
        suspension_stage_continued = True
        try:
            _after_legacy_cleanup()
        except Exception:
            _complete()
            raise

    def _finish_suspension_stage() -> None:
        if not suspension_stage_continued:
            _complete()

    migrate_legacy_suspension_tag(
        col, on_complete=_finish_suspension_stage, on_success=_start_ignore_stage
    )


def migrate_to_version_2_1(
    col: Collection,
    on_complete: Callable[[], None] | None = None,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Migrate the independent card markers for direct upgrades from version 2.0."""

    def _finish_migration() -> None:
        state.installed_version = state.ADDON_VERSION
        state.save_persistent_state(col)
        logThis("SibPush migrated legacy card markers to independent provenance markers")
        if on_success is not None:
            on_success()

    migrate_legacy_ignore_markers(
        col, on_complete=on_complete, on_success=_finish_migration
    )


_STARTUP_MIGRATIONS: tuple[tuple[tuple[int, int, int], StartupMigration], ...] = (
    (VERSION_3_0_0, migrate_to_version_3),
)


def _parse_version(value: str | None) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None

    try:
        parts = [int(part) for part in normalized.split(".")]
    except ValueError:
        return None

    if len(parts) < 3:
        parts.extend([0] * (3 - len(parts)))

    return parts[0], parts[1], parts[2]


def run_startup_migrations(
    col: Collection, on_complete: Callable[[], None] | None = None
) -> None:
    """Run any versioned startup-migration packs needed for this installation."""

    current_version = _parse_version(state.installed_version)

    for target_version, migration in _STARTUP_MIGRATIONS:
        if current_version is None or current_version < target_version:
            migration(col, on_complete)
            return

    if on_complete is not None:
        on_complete()
