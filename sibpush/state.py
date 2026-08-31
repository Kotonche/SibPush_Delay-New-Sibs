"""Shared runtime state for the SibPush add-on."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from aqt import mw as _mw
from anki.notes import NoteId

mw: Any = _mw

# These caches intentionally track two different triggers:
# - `last_full_scan_date` gates the expensive browser-driven scan.
# - `last_unmanaged_note_ids` gates the lighter follow-up pass that only revisits
#   new notes that are still unmanaged by the add-on.
last_full_scan_date: str | None = None
last_unmanaged_note_ids: Sequence[NoteId] | None = None

# Persistent, collection-scoped state used by the future timestamp-based scan flow.
last_processed_mod_ts: int | None = None
last_sync_mod_ts: int | None = None

# Deferred browser-render work that should be applied before the next batch scan runs.
# These keys live inside ``sibpush_state.json``; changing them is a schema change.
#
# PENDING_UNSUSPEND_DECK_IDS_KEY:
#   List of deck IDs (as strings) queued for marker-aware unsuspend cleanup. When a deck is
#   ignored, currently suspended cards previously suspended by the add-on are restored before
#   the next scan.
#   This queue ensures that unsuspend operations are performed in the correct browser context.
PENDING_UNSUSPEND_DECK_IDS_KEY = "pending_unsuspend_deck_ids"
# PENDING_PROCESSING_RESET_KEY:
#   Boolean flag. When True, signals that the next browser-driven scan should reset the
#   processing state (i.e., clear the scan watermarks and force a full reprocessing of all notes).
#   This is typically queued when config changes (like interval or tag rule edits) require
#   a fresh scan to apply new rules.
PENDING_PROCESSING_RESET_KEY = "pending_processing_state_reset"
# PENDING_UNMANAGED_REFRESH_KEY:
#   Boolean flag. When True, signals that after the next browser-driven scan, the add-on
#   should run a lighter pass to revisit any new notes that are still unmanaged (i.e.,
#   not yet owned or processed by SibPush). This is usually queued after a sync event
#   when new notes are added outside the browser context.
PENDING_UNMANAGED_REFRESH_KEY = "pending_unmanaged_refresh"
# PENDING_BROWSER_WORK_KEY:
#   The top-level key in the state file under which all deferred browser work is stored.
#   Its value is a dictionary containing the above keys. Changing this key or its structure
#   is a schema change and must be coordinated with migration logic if needed.
PENDING_BROWSER_WORK_KEY = "pending_browser_work"


def _default_pending_browser_work() -> dict[str, Any]:
    """Return the empty deferred browser-work payload.

    Returns:
        dict[str, Any]: A normalized browser-work snapshot with no queued decks and no flags set.
    """

    return {
        PENDING_UNSUSPEND_DECK_IDS_KEY: [],
        PENDING_PROCESSING_RESET_KEY: False,
        PENDING_UNMANAGED_REFRESH_KEY: False,
    }


_pending_browser_work = _default_pending_browser_work()

# Card custom-data keys must be alphanumeric and no longer than 8 bytes in this Anki build.
# Progressive Siblings owns a separate namespace so it can coexist with SibPush during migration.
PROGRESSIVE_SUSPENDED_KEY = "prgsusp"
PROGRESSIVE_IGNORED_KEY = "prgign"
PROGRESSIVE_UNLOCKED_KEY = "prgunlk"
PROGRESSIVE_MARKER_VALUE = True

# Compatibility aliases keep the inherited SibPush infrastructure working while user-facing
# naming and migration code move to Progressive Siblings terminology.
SIBPUSH_SUSPENDED_KEY = PROGRESSIVE_SUSPENDED_KEY
SIBPUSH_IGNORED_KEY = PROGRESSIVE_IGNORED_KEY
SIBPUSH_MARKER_VALUE = PROGRESSIVE_MARKER_VALUE

# Markers written by SibPush 2.x. They are never used for new writes.
LEGACY_SIBPUSH_SUSPENDED_KEY = "sibpsusp"
LEGACY_SIBPUSH_IGNORED_KEY = "sibpign"

# The old representation is read only by migration and defensive compatibility paths. It must
# not be used for new writes because it cannot represent independent marker state.
LEGACY_ADDON_CUSTOM_DATA_KEY = "sibpush"
LEGACY_ADDON_CUSTOM_DATA_IGNORED_VALUE = "ignored"

# Compatibility names retained for existing integrations. They now describe the new ignored
# marker storage, not the legacy overloaded field.
ADDON_CUSTOM_DATA_KEY = SIBPUSH_IGNORED_KEY
ADDON_CUSTOM_DATA_IGNORED_VALUE = SIBPUSH_MARKER_VALUE
CONFIG_IGNORED_KEY = "ignored"
ADDON_VERSION = "3.0.0"  # First Progressive Siblings release based on SibPush 2.3.0.
BREAKING_CHANGE_VERSION = "3.0.0"
VERSION_KEY = "addon_version"
STATE_FILENAME = "progressive_siblings_state.json"
CONFIG_FILENAME = "progressive_siblings_config.json"
_persistent_state_loaded = False

installed_version: str | None = None  # The last installed version of the add-on, loaded from the state file. This is used to determine if the breaking-change recovery flow needs to run.


def get_mw() -> Any:
    """Return the current Anki main window reference.

    Returns:
        Any: The current module-level `mw` handle.
    """

    return mw


def is_persistent_state_loaded() -> bool:
    """Return whether persistent state has already been loaded for this session."""

    return _persistent_state_loaded


def _resolve_collection_path(col: Any | None = None) -> Path | None:
    """Return the filesystem path for the active collection, if available."""

    current_col = col
    if current_col is None:
        current_mw = get_mw()
        current_col = getattr(current_mw, "col", None) if current_mw is not None else None

    if current_col is None:
        return None

    raw_path = getattr(current_col, "path", None)
    if not raw_path:
        return None

    return Path(str(raw_path))


def get_state_file_path(col: Any | None = None) -> Path | None:
    """Return the per-collection state file path.

    Args:
        col (Any | None): The collection to resolve the path from. When omitted,
            the current ``mw.col`` value is used.

    Returns:
        Path | None: The sibling ``sibpush_state.json`` path, or None when no collection path exists.
    """

    collection_path = _resolve_collection_path(col)
    if collection_path is None:
        return None

    return collection_path.with_name(STATE_FILENAME)


def get_config_file_path(col: Any | None = None) -> Path | None:
    """Return the per-profile config file path.

    Args:
        col (Any | None): The collection to resolve the path from. When omitted,
            the current ``mw.col`` value is used.

    Returns:
        Path | None: The sibling ``sibpush_config.json`` path, or None when no collection path
            exists.
    """

    collection_path = _resolve_collection_path(col)
    if collection_path is None:
        return None

    return collection_path.with_name(CONFIG_FILENAME)


def _normalize_timestamp(value: Any) -> int | None:
    """Convert a state timestamp to a non-negative integer, if possible."""

    if value is None:
        return None

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None

    return timestamp if timestamp >= 0 else None


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a dotted version string into a tuple of integers."""

    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if not normalized:
        return None

    try:
        return tuple(int(part) for part in normalized.split("."))
    except ValueError:
        return None


def _normalize_deck_id(value: Any) -> str | None:
    """Convert a deck id to a non-empty string, if possible."""

    normalized = str(value).strip()
    return normalized or None


def _normalize_deck_id_sequence(values: object) -> list[str]:
    """Normalize a collection of deck ids while preserving the first seen order.

    Args:
        values (object): A sequence-like value containing candidate deck ids.

    Returns:
        list[str]: The cleaned, de-duplicated deck ids in first-seen order.
    """

    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []

    normalized_ids: list[str] = []
    seen_ids: set[str] = set()
    for value in cast(Sequence[object], values):
        deck_id = _normalize_deck_id(value)
        if deck_id is None or deck_id in seen_ids:
            continue

        seen_ids.add(deck_id)
        normalized_ids.append(deck_id)

    return normalized_ids


def _normalize_pending_browser_work(value: Any) -> dict[str, Any]:
    """Normalize a deferred browser-work payload read from disk.

    Args:
        value (Any): The raw payload from ``sibpush_state.json``.

    Returns:
        dict[str, Any]: A safe browser-work snapshot with the expected keys and types.
    """

    if not isinstance(value, dict):
        return _default_pending_browser_work()

    typed_value = cast(dict[str, object], value)
    normalized_work: dict[str, Any] = {
        PENDING_UNSUSPEND_DECK_IDS_KEY: _normalize_deck_id_sequence(
            typed_value.get(PENDING_UNSUSPEND_DECK_IDS_KEY, [])
        ),
        PENDING_PROCESSING_RESET_KEY: bool(typed_value.get(PENDING_PROCESSING_RESET_KEY, False)),
        PENDING_UNMANAGED_REFRESH_KEY: bool(typed_value.get(PENDING_UNMANAGED_REFRESH_KEY, False)),
    }

    if normalized_work[PENDING_PROCESSING_RESET_KEY]:
        # A full reset already implies a full modified-note scan on the next browser render,
        # so the unmanaged follow-up would only duplicate work and make the UI stutter.
        normalized_work[PENDING_UNMANAGED_REFRESH_KEY] = False

    return normalized_work


def _read_state_payload(state_file: Path) -> dict[str, Any]:
    """Read a JSON payload from disk, falling back to an empty object on errors.

    Args:
        state_file (pathlib.Path): The collection-scoped state file to read.

    Returns:
        dict[str, Any]: The raw JSON object when it looks valid, otherwise ``{}``.
    """

    try:
        with state_file.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}

    return cast(dict[str, Any], payload)


def _write_state_payload(state_file: Path, payload: dict[str, Any]) -> None:
    """Write a JSON payload atomically to disk.

    Args:
        state_file (pathlib.Path): The collection-scoped state file to update.
        payload (dict[str, Any]): The JSON-ready payload to persist.

    Returns:
        None: The data is written in place on disk.
    """

    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_file.parent,
            prefix=f"{state_file.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)

        os.replace(temp_path, state_file)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def get_last_processed_mod_ts() -> int:
    """Return the last processed modification timestamp.

    Returns:
        int: ``0`` when no timestamp has been persisted yet.
    """

    return last_processed_mod_ts if last_processed_mod_ts is not None else 0


def sync_last_processed_mod_ts(value: int | None) -> None:
    """Update the cached timestamp for the last processed note batch."""

    global last_processed_mod_ts
    last_processed_mod_ts = _normalize_timestamp(value)


def get_last_sync_mod_ts() -> int | None:
    """Return the last sync modification timestamp, if one exists."""

    return last_sync_mod_ts


def needs_breaking_change_recovery() -> bool:
    """Return whether the collection needs the breaking-change recovery flow."""

    current_version = _parse_version(installed_version)
    breaking_change_version = _parse_version(BREAKING_CHANGE_VERSION)
    if current_version is None or breaking_change_version is None:
        return True

    # If the currently installed version is older than the breaking change version, then we need to run the recovery flow when upgrading to the new version. Otherwise, if the currently installed version is the same or newer than the breaking change version, then we can assume the recovery flow has already been run (or is not needed) and skip it.
    return (
        current_version < breaking_change_version
    )


def clear_stale_sync_mod_ts() -> bool:
    """Clear the stale sync watermark after the browser has caught up.

    The sync watermark is used to catch up on remote changes when the browser scan
    has not yet processed them locally. Once the local scan has advanced past the
    sync timestamp, keeping the old sync watermark would cause subsequent browser
    refreshes to keep scanning the same already-processed window again and again.
    This helper drops the stale sync watermark when it is older than or equal to
    the last processed timestamp, preventing repeated rescan loops.

    Returns:
        bool: True when the stale sync watermark was cleared, otherwise False.
    """

    current_sync_ts = get_last_sync_mod_ts()
    if current_sync_ts is None:
        return False

    if current_sync_ts <= get_last_processed_mod_ts():
        sync_last_sync_mod_ts(None)
        return True

    return False


def get_browser_scan_since_ts() -> int:
    """Return the timestamp threshold the browser scan should use.

    When both a last-sync watermark and a last-processed watermark exist, the browser scan should
    use the older one so we do not miss cards that changed on another device before the local
    browser scan ran.
    """

    processed_ts = get_last_processed_mod_ts()
    sync_ts = get_last_sync_mod_ts()
    if sync_ts is None:
        return processed_ts

    return min(processed_ts, sync_ts)


def sync_last_sync_mod_ts(value: int | None) -> None:
    """Update the cached timestamp for the last successful sync."""

    global last_sync_mod_ts
    last_sync_mod_ts = _normalize_timestamp(value)


def get_persistent_state() -> dict[str, int | None]:
    """Return a snapshot of the persistent state currently held in memory."""

    return {
        "last_processed_mod_ts": get_last_processed_mod_ts(),
        "last_sync_mod_ts": get_last_sync_mod_ts(),
    }


def get_pending_browser_work() -> dict[str, Any]:
    """Return a snapshot of the deferred browser work currently held in memory.

    Returns:
        dict[str, Any]: A copy of the queued browser work. Callers should mutate the copy,
            not the module-level cache.
    """

    return {
        PENDING_UNSUSPEND_DECK_IDS_KEY: list(_pending_browser_work[PENDING_UNSUSPEND_DECK_IDS_KEY]),
        PENDING_PROCESSING_RESET_KEY: bool(_pending_browser_work[PENDING_PROCESSING_RESET_KEY]),
        PENDING_UNMANAGED_REFRESH_KEY: bool(_pending_browser_work[PENDING_UNMANAGED_REFRESH_KEY]),
    }


def sync_pending_browser_work(value: dict[str, Any] | None) -> None:
    """Replace the deferred browser-work cache with a normalized snapshot.

    Args:
        value (dict[str, Any] | None): The browser-work payload to install in memory.

    Returns:
        None: The module-level queue cache is updated in place.
    """

    global _pending_browser_work
    _pending_browser_work = _normalize_pending_browser_work(value)


def queue_pending_browser_work(
    *,
    deck_ids: Sequence[str] | None = None,
    reset_processing_state: bool = False,
    refresh_unmanaged_notes: bool = False,
) -> dict[str, Any]:
    """Add deferred work that should run on the next browser render.

    Args:
        deck_ids (Sequence[str] | None): Deck ids to queue for unsuspend cleanup.
        reset_processing_state (bool): Whether the browser scan watermark should be reset.
        refresh_unmanaged_notes (bool): Whether the unmanaged-note pass should run next time.

    Returns:
        dict[str, Any]: The updated queued browser-work snapshot.
    """

    global _pending_browser_work

    pending_work = get_pending_browser_work()
    pending_deck_ids = pending_work[PENDING_UNSUSPEND_DECK_IDS_KEY]
    seen_ids = set(pending_deck_ids)

    if deck_ids is not None:
        for deck_id in deck_ids:
            normalized_deck_id = _normalize_deck_id(deck_id)
            if normalized_deck_id is None or normalized_deck_id in seen_ids:
                continue

            seen_ids.add(normalized_deck_id)
            pending_deck_ids.append(normalized_deck_id)

    if reset_processing_state:
        pending_work[PENDING_PROCESSING_RESET_KEY] = True
        # A queued full reset supersedes the lighter unmanaged follow-up.
        pending_work[PENDING_UNMANAGED_REFRESH_KEY] = False

    if refresh_unmanaged_notes:
        if not pending_work[PENDING_PROCESSING_RESET_KEY]:
            pending_work[PENDING_UNMANAGED_REFRESH_KEY] = True

    _pending_browser_work = pending_work
    return get_pending_browser_work()


def discard_pending_unsuspend_deck_id(deck_id: str) -> dict[str, Any]:
    """Remove a deck from the deferred unsuspend queue, if it is present.

    Args:
        deck_id (str): The deck id to remove from the queued cleanup list.

    Returns:
        dict[str, Any]: The updated queued browser-work snapshot.
    """

    global _pending_browser_work

    normalized_deck_id = _normalize_deck_id(deck_id)
    if normalized_deck_id is None:
        return get_pending_browser_work()

    pending_work = get_pending_browser_work()
    pending_deck_ids = pending_work[PENDING_UNSUSPEND_DECK_IDS_KEY]
    filtered_deck_ids = [
        queued_id for queued_id in pending_deck_ids if queued_id != normalized_deck_id
    ]

    if len(filtered_deck_ids) != len(pending_deck_ids):
        pending_work[PENDING_UNSUSPEND_DECK_IDS_KEY] = filtered_deck_ids
        _pending_browser_work = pending_work

    return get_pending_browser_work()


def consume_pending_browser_work() -> dict[str, Any]:
    """Return the current deferred browser work and clear the in-memory queue.

    Returns:
        dict[str, Any]: The queued browser work that was pending before the clear.
    """

    pending_work = get_pending_browser_work()
    clear_pending_browser_work()
    return pending_work


def clear_pending_browser_work() -> dict[str, Any]:
    """Reset the deferred browser-work cache to its empty state.

    Returns:
        dict[str, Any]: The empty browser-work snapshot after clearing.
    """

    global _pending_browser_work
    _pending_browser_work = _default_pending_browser_work()
    return get_pending_browser_work()


def load_persistent_state(col: Any | None = None) -> dict[str, int | None]:
    """Load persistent state from ``sibpush_state.json`` for the current collection.

    Args:
        col (Any | None): The collection to load state for, or ``None`` to use ``mw.col``.

    Returns:
        dict[str, int | None]: The loaded timestamp snapshot after normalization.
    """

    state_file = get_state_file_path(col)
    if state_file is None or not state_file.exists():
        _apply_state_payload({})
        return get_persistent_state()

    payload = _read_state_payload(state_file)
    _apply_state_payload(payload)
    return get_persistent_state()


def save_persistent_state(col: Any | None = None) -> dict[str, int | None]:
    """Persist the current timestamp state to ``sibpush_state.json``.

    Args:
        col (Any | None): The collection whose state file should be updated.

    Returns:
        dict[str, int | None]: The timestamp snapshot currently held in memory.
    """

    state_file = get_state_file_path(col)
    if state_file is None:
        return get_persistent_state()

    payload: dict[str, Any] = {VERSION_KEY: ADDON_VERSION}
    if last_processed_mod_ts is not None:
        payload["last_processed_mod_ts"] = last_processed_mod_ts
    if last_sync_mod_ts is not None:
        payload["last_sync_mod_ts"] = last_sync_mod_ts

    pending_browser_work = get_pending_browser_work()
    if (
        pending_browser_work[PENDING_UNSUSPEND_DECK_IDS_KEY]
        or pending_browser_work[PENDING_PROCESSING_RESET_KEY]
        or pending_browser_work[PENDING_UNMANAGED_REFRESH_KEY]
    ):
        payload[PENDING_BROWSER_WORK_KEY] = pending_browser_work

    _write_state_payload(state_file, payload)
    return get_persistent_state()


def reset_persistent_state(col: Any | None = None) -> dict[str, int | None]:
    """Clear the persisted timestamps and reset the in-memory scan state.

    Args:
        col (Any | None): The collection whose state file should be cleared.

    Returns:
        dict[str, int | None]: The cleared timestamp snapshot after reset.
    """

    global last_full_scan_date, last_unmanaged_note_ids, last_processed_mod_ts, last_sync_mod_ts
    global installed_version
    global _persistent_state_loaded

    last_full_scan_date = None
    last_unmanaged_note_ids = None
    last_processed_mod_ts = None
    last_sync_mod_ts = None
    installed_version = None
    _persistent_state_loaded = True
    clear_pending_browser_work()

    state_file = get_state_file_path(col)
    if state_file is not None:
        payload = _read_state_payload(state_file) if state_file.exists() else {}
        payload.pop("last_processed_mod_ts", None)
        payload.pop("last_sync_mod_ts", None)
        payload.pop(PENDING_BROWSER_WORK_KEY, None)
        _write_state_payload(state_file, payload)

    return get_persistent_state()


def _apply_state_payload(payload: dict[str, Any]) -> None:
    """Apply a JSON payload to the in-memory timestamp state.

    Older files may not contain deferred browser work, so missing queue data is normalized to an
    empty browser-work payload and safely loaded.
    """

    global last_processed_mod_ts, last_sync_mod_ts, installed_version, _persistent_state_loaded

    last_processed_mod_ts = _normalize_timestamp(payload.get("last_processed_mod_ts"))
    last_sync_mod_ts = _normalize_timestamp(payload.get("last_sync_mod_ts"))
    version_value = payload.get(VERSION_KEY)
    installed_version = (
        version_value.strip() if isinstance(version_value, str) and version_value.strip() else None
    )
    sync_pending_browser_work(
        _normalize_pending_browser_work(payload.get(PENDING_BROWSER_WORK_KEY))
    )
    _persistent_state_loaded = True


def get_last_full_scan_date() -> str | None:
    """Return the date of the most recent full browser-driven scan.

    Returns:
        str | None: The ISO date of the last completed full scan, or None when it has not run yet.
    """

    return last_full_scan_date


def sync_last_full_scan_date(value: str | None) -> None:
    """Update the cached date for the most recent full browser-driven scan.

    Args:
        value (str | None): The new ISO date for the completed full scan.

    Returns:
        None: The module-level cache is updated in place.
    """

    global last_full_scan_date
    last_full_scan_date = value


def get_last_unmanaged_note_ids() -> Sequence[NoteId] | None:
    """Return the note ids from the last unmanaged-note batch processing pass.

    Returns:
        Sequence[anki.notes.NoteId] | None: The last cached unmanaged note ids, or None when
            the unmanaged workflow has not run yet.
    """

    return last_unmanaged_note_ids


def sync_last_unmanaged_note_ids(note_ids: Sequence[NoteId] | None) -> None:
    """Update the cached unmanaged-note batch-processing note ids.

    Args:
        note_ids (Sequence[anki.notes.NoteId] | None): The new unmanaged note ids.

    Returns:
        None: The module-level cache is updated in place.
    """

    global last_unmanaged_note_ids
    last_unmanaged_note_ids = note_ids
