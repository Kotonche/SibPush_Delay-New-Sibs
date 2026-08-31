"""Query and caching helpers for the SibPush note workflow."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from anki.cards import Card, CardId
from anki.collection import Collection
from anki.notes import Note, NoteId

from ..config.parser import config_settings, custom_deck_rules_by_did, ignored_deck_ids
from ..state import get_last_unmanaged_note_ids
from .progression import get_note_type_rule


def _managed_note_ids(col: Collection, note_ids: Sequence[NoteId]) -> list[NoteId]:
    """Filter batch candidates before sibling Card objects are hydrated."""

    return [note_id for note_id in note_ids if get_note_type_rule(col.get_note(note_id)) is not None]


def get_tag_rule(note: Note) -> dict[str, Any] | None:
    """Look up the first matching tag rule for a note.

    Args:
        note (anki.notes.Note): The note whose tags should be inspected.

    Returns:
        dict[str, Any] | None: The matching rule, or None when no tag rule applies.
    """

    raw_tag_rules = config_settings["tag_rules"]
    if not isinstance(raw_tag_rules, dict):
        return None

    note_tags = {str(tag).strip() for tag in getattr(note, "tags", []) if str(tag).strip()}
    typed_tag_rules = cast(dict[str, Any], raw_tag_rules)
    for raw_tag, rule in typed_tag_rules.items():
        tag = str(raw_tag).strip()
        if tag and tag in note_tags and isinstance(rule, dict):
            return cast(dict[str, Any], rule)

    return None


def get_new_note_ids(col: Collection) -> Sequence[NoteId]:
    """Return the ids of new notes with more than one card that are not in ignored decks.

    Single-card notes are excluded up front because process_note skips them anyway,
    so filtering them here avoids loading their cards later.

    Args:
        col (anki.collection.Collection): The collection to search.

    Returns:
        Sequence[anki.notes.NoteId]: The ids of the matching new notes.
    """

    ignored_query = " ".join(f"-did:{deck_id}" for deck_id in ignored_deck_ids if deck_id)
    query = f"is:new {ignored_query}".strip()
    all_new_nids = col.find_notes(query)

    if not all_new_nids:
        return []

    db = col.db
    if db is None:
        return []

    # Keep only notes that have more than one card; single-card notes are skipped later
    # anyway, so filtering them here avoids an extra card lookup for notes with no siblings.
    nid_list = ",".join(str(n) for n in all_new_nids)
    candidate_note_ids = cast(
        list[NoteId],
        db.list(
            f"SELECT nid FROM cards WHERE nid IN ({nid_list}) GROUP BY nid HAVING COUNT(*) > 1"
        ),
    )
    return _managed_note_ids(col, candidate_note_ids)


def get_new_unmanaged_note_ids(col: Collection) -> Sequence[NoteId]:
    """Return the ids of new notes that still need add-on processing.

    This is the narrower recurring scan used after the initial startup/day-change full pass.
    It keeps the ignored-deck filter and excludes notes already marked with the add-on tag so
    later browser refreshes and sync completions only revisit genuinely unmanaged notes.

    Args:
        col (anki.collection.Collection): The collection to search.

    Returns:
        Sequence[anki.notes.NoteId]: The ids of the matching unmanaged new notes.
    """

    ignored_query = " ".join(f"-did:{deck_id}" for deck_id in ignored_deck_ids if deck_id)
    # Do not use a note-level custom-data exclusion here: one ignored sibling must not hide
    # eligible siblings on the same note. process_note() performs the authoritative card-level
    # ignored-marker filtering after this candidate query.
    query = f"is:new {ignored_query}".strip()
    all_new_nids = col.find_notes(query)

    if not all_new_nids:
        return []

    db = col.db
    if db is None:
        return []

    # This uses the same sibling-count pruning as the full scan, but also excludes notes
    # already marked with the add-on tag so the recurring unmanaged pass stays narrow.
    nid_list = ",".join(str(n) for n in all_new_nids)
    candidate_note_ids = cast(
        list[NoteId],
        db.list(
            f"SELECT nid FROM cards WHERE nid IN ({nid_list}) GROUP BY nid HAVING COUNT(*) > 1"
        ),
    )
    return _managed_note_ids(col, candidate_note_ids)


def _build_ignored_deck_exclusion_clause(note_id_expression: str) -> str:
    """Return a SQL clause that excludes notes with any cards in ignored decks."""

    deck_ids = [deck_id for deck_id in ignored_deck_ids if deck_id]
    if not deck_ids:
        return ""

    deck_id_list = ",".join(deck_ids)
    return (
        " AND NOT EXISTS ("
        "SELECT 1 FROM cards ignored_cards "
        f"WHERE ignored_cards.nid = {note_id_expression} "
        f"AND ignored_cards.did IN ({deck_id_list})"
        ")"
    )


def get_modified_note_ids_since(col: Collection, modified_since: int) -> Sequence[NoteId]:
    """Return note ids whose note or card rows were modified after a timestamp.

    The delta query stays note-based: it unions candidate note ids from both the notes and cards
    tables, excludes notes in ignored decks, and keeps only notes with more than one card so the
    downstream sibling workflow does not spend time on single-card notes that would be skipped
    anyway.

    "Note modified" (notes.mod) tracks note edits to fields' contents.
    "Card modified" (cards.mod) tracks changes to cards, like is-suspended, is-buried, etc..

    Here, we combine both types of modifications to conservatively capture any note that needs processing.

    Args:
        col (anki.collection.Collection): The collection to search.
        modified_since (int): The modification timestamp threshold.

    Returns:
        Sequence[anki.notes.NoteId]: Eligible note ids modified after the threshold.
    """

    db = col.db
    if db is None:
        return []

    note_exclusion_clause = _build_ignored_deck_exclusion_clause("notes.id")
    card_exclusion_clause = _build_ignored_deck_exclusion_clause("cards.nid")

    note_rows = cast(
        list[NoteId],
        db.list(
            "SELECT DISTINCT notes.id "
            f"FROM notes WHERE notes.mod > {modified_since}{note_exclusion_clause}"
        ),
    )
    card_rows = cast(
        list[NoteId],
        db.list(
            "SELECT DISTINCT cards.nid "
            f"FROM cards WHERE cards.mod > {modified_since}{card_exclusion_clause}"
        ),
    )

    candidate_note_ids = sorted(set(note_rows) | set(card_rows))
    if not candidate_note_ids:
        return []

    nid_list = ",".join(str(n) for n in candidate_note_ids)
    grouped_note_ids = cast(
        list[NoteId],
        db.list(
            f"SELECT nid FROM cards WHERE nid IN ({nid_list}) GROUP BY nid HAVING COUNT(*) > 1"
        ),
    )
    return _managed_note_ids(col, grouped_note_ids)


def get_deck_rule(card: Card) -> dict[str, Any] | None:
    """Look up the custom deck rule associated with a card's deck.

    Args:
        card (anki.cards.Card): The card whose deck rule should be resolved.

    Returns:
        dict[str, Any] | None: The matching rule, or None when no rule exists.
    """

    return custom_deck_rules_by_did.get(str(card.did))


def get_deck_rule_by_id(deck_id: str) -> dict[str, Any] | None:
    """Look up the current custom deck rule for a stable deck id."""

    return custom_deck_rules_by_did.get(str(deck_id))


def get_child_cards(col: Collection, note_id: NoteId) -> Sequence[Card]:
    """Return all sibling cards belonging to a note.

    Args:
        col (anki.collection.Collection): The collection to search.
        note_id (int): The note identifier to fetch cards for.

    Returns:
        Sequence[anki.cards.Card]: The cards belonging to the note.
    """

    card_ids = col.card_ids_of_note(note_id)
    return [col.get_card(card_id) for card_id in card_ids]


def get_all_child_cards_batch(
    col: Collection, note_ids: Sequence[NoteId]
) -> dict[NoteId, list[Card]]:
    """Return all sibling cards for a batch of notes in a single database query.

    This is the batch equivalent of calling get_child_cards once per note. It issues
    a single SQL query to fetch every card id grouped by note, then hydrates each Card
    object, which eliminates the N×2 database round-trips that the per-note path incurs.

    Args:
        col (anki.collection.Collection): The collection to search.
        note_ids (Sequence[anki.notes.NoteId]): The note ids whose cards should be loaded.

    Returns:
        dict[int, list[anki.cards.Card]]: A mapping from note id to the list of its cards.
            Notes with no cards in the database are absent from the mapping.
    """

    if not note_ids:
        return {}

    db = col.db
    if db is None:
        return {}

    nid_list = ",".join(str(n) for n in note_ids)
    card_ids_by_nid: dict[NoteId, list[CardId]] = {}
    rows = cast(
        list[tuple[NoteId, CardId]], db.all(f"SELECT nid, id FROM cards WHERE nid IN ({nid_list})")
    )
    for nid, cid in rows:
        card_ids_by_nid.setdefault(nid, []).append(cid)

    return {nid: [col.get_card(cid) for cid in cids] for nid, cids in card_ids_by_nid.items()}


def _note_ids_fingerprint(note_ids: Sequence[NoteId]) -> int:
    """Return a cheap O(1) fingerprint for a sequence of note ids.

    Using a frozenset hash means the comparison in should_run_unmanaged_notes is constant-time
    regardless of collection size, unlike comparing two full sequences element-by-element.

    Args:
        note_ids (Sequence[anki.notes.NoteId]): The note ids to fingerprint.

    Returns:
        int: A hash value that changes whenever the set of ids changes.
    """

    return hash(frozenset(note_ids))


def should_run_unmanaged_notes(col: Collection) -> tuple[bool, Sequence[NoteId]]:
    """Decide whether the unmanaged-note follow-up pass should run.

    The unmanaged pass is the lighter recurring scan that only revisits new notes that do not yet
    carry the add-on tag. Its cache stores only the last unmanaged note ids so repeated passes can
    skip work when the unmanaged candidate set has not changed.

    Args:
        col (anki.collection.Collection): The collection to inspect for unmanaged new notes.

    Returns:
        tuple[bool, Sequence[anki.notes.NoteId]]: A tuple containing a boolean flag indicating
            whether work should run and the current unmanaged note ids.
    """

    current_unmanaged_note_ids = get_new_unmanaged_note_ids(col)
    last_unmanaged_note_ids = get_last_unmanaged_note_ids()

    if last_unmanaged_note_ids is None:
        return True, current_unmanaged_note_ids

    # Compare set fingerprints instead of full sequences so we can cheaply detect whether
    # the unmanaged candidate set changed without caring about ordering noise from SQL.
    should_run = _note_ids_fingerprint(last_unmanaged_note_ids) != _note_ids_fingerprint(
        current_unmanaged_note_ids
    )

    return should_run, current_unmanaged_note_ids
