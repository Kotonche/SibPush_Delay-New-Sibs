"""Deck-browser actions for managing SibPush per-deck rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import aqt
from aqt.qt import QInputDialog, QMenu
from aqt.utils import askUser, tooltip

from ..config.parser import get_custom_deck_rule_snapshot, update_custom_deck_rule
from ..processing.chunked_runner import run_chunked
from ..processing.notes import process_note
from ..processing.suspension import unsuspend_all_addon_cards
from ..state import CONFIG_IGNORED_KEY, SIBPUSH_IGNORED_KEY, get_mw


def _get_collection() -> Any | None:
    """Return the active collection, if Anki is ready.

    Returns:
        Any | None: The Anki collection object, or None if Anki is not running or the collection is not loaded.
    """

    current_mw = get_mw()
    if current_mw is None:
        return None

    return getattr(current_mw, "col", None)


def _add_action(menu: QMenu, label: str, handler: Callable[[int], None], deck_id: int) -> None:
    """Add a deck action and connect it to the supplied handler."""

    action: Any = cast(Any, menu).addAction(label)
    if action is not None:
        action.triggered.connect(lambda _=False, did=deck_id: handler(did))  # type: ignore[union-attr]


def _get_deck_name(col: Any, deck_id: int) -> str:
    """Return the readable deck name for a deck id.

    Args:
        col (Any): The Anki collection object.
        deck_id (int): The deck identifier.

    Returns:
        str: The human-readable deck name, or the deck ID as a string if the name cannot be found.
    """

    deck = col.decks.get(deck_id)
    if isinstance(deck, Mapping):
        name = cast(Any, deck).get("name", deck_id)
        return str(name or deck_id).strip() or str(deck_id)

    return str(deck_id)


def _toggle_ignore_state(deck_id: int) -> None:
    """Flip the ignore state for one deck.

    This function toggles whether a deck is ignored by the add-on. When a deck
    is ignored, the add-on will not manage sibling card suspension for that deck.

    Args:
        deck_id (int): The deck identifier to toggle.

    Returns:
        None: The configuration is updated as a side effect.
    """

    col = _get_collection()
    if col is None:
        return

    deck_name = _get_deck_name(col, deck_id)
    snapshot = get_custom_deck_rule_snapshot(str(deck_id))
    update_custom_deck_rule(
        str(deck_id),
        deck_name,
        ignored=not snapshot[CONFIG_IGNORED_KEY],
        stability_threshold=snapshot["stability_threshold"],
    )


def _set_custom_stability_threshold(deck_id: int) -> None:
    """Prompt for and save a custom FSRS Stability threshold for one deck.

    This function displays a dialog prompting the user to enter a custom
    maturity interval (in days) for the specified deck. The interval determines
    when sibling cards are considered "mature" and can be unsuspended.

    Args:
        deck_id (int): The deck identifier to configure.

    Returns:
        None: The configuration is updated if the user accepts the dialog.
    """

    col = _get_collection()
    if col is None:
        return

    deck_name = _get_deck_name(col, deck_id)
    snapshot = get_custom_deck_rule_snapshot(str(deck_id))
    value, accepted = QInputDialog.getDouble(
        get_mw(),
        "Progressive Siblings — Stability threshold",
        f"Minimum FSRS Stability in days for '{deck_name}'",
        snapshot["stability_threshold"],
        0,
        100000,
        2,
    )
    if not accepted:
        return

    update_custom_deck_rule(
        str(deck_id),
        deck_name,
        ignored=snapshot[CONFIG_IGNORED_KEY],
        stability_threshold=value,
    )


def _reprocess_deck(deck_id: int) -> None:
    """Reconcile all managed notes that have a card in the selected deck."""

    col = _get_collection()
    if col is None:
        return

    note_ids = list(col.find_notes(f"did:{deck_id}"))

    def _process_chunk(chunk: Sequence[int]) -> None:
        for note_id in chunk:
            process_note(col, note_id)

    run_chunked(
        note_ids,
        _process_chunk,
        batch_size=500,
        pause_ms=100,
        on_success=lambda: tooltip(
            f"Progressive Siblings reprocessed {len(note_ids):,} note(s)"
        ),
    )


def _restore_all_managed_cards(_: int) -> None:
    """Expose the provenance-aware recovery command required for safe removal."""

    col = _get_collection()
    current_mw = get_mw()
    if col is None or current_mw is None:
        return
    if not askUser(
        "Restore every New card currently suspended by Progressive Siblings?\n\n"
        "Cards suspended manually will not be changed.",
        parent=current_mw,
        defaultno=True,
    ):
        return
    unsuspend_all_addon_cards(
        col,
        on_success=lambda: tooltip("Progressive Siblings restored its managed cards"),
    )


def _show_ignored_cards_in_browser(deck_id: int) -> None:
    """Open the card browser pre-filled with a search for ignored cards in one deck.

    This function is called when the user selects "Show ignored cards" from the
    SibPush deck submenu. It opens (or focuses) the Anki card browser with a search
    query that finds cards in the given deck that SibPush has marked as ignored.

    Args:
        deck_id (int): The deck identifier whose ignored cards should be shown.

    Returns:
        None: The browser window is opened as a side effect.
    """

    col = _get_collection()
    if col is None:
        return

    deck_name = _get_deck_name(col, deck_id)
    query = f'deck:"{deck_name}" prop:cds:{SIBPUSH_IGNORED_KEY}=true'

    current_mw = get_mw()
    if current_mw is None:
        return

    aqt.dialogs.open("Browser", current_mw, search=(query,))


def add_deck_actions_to_options_menu(menu: QMenu, deck_id: int) -> None:
    """Add the SibPush submenu to the deck browser options menu.

    This function is called by Anki's deck browser when the user right-clicks on a deck.
    It adds a "SibPush" submenu with three options:
    - Toggle the ignore state (ignore/unignore the deck)
    - Set a custom maturity interval for the deck
    - Open the card browser to show cards ignored by SibPush in this deck

    Args:
        menu (QMenu): The Qt menu widget to add items to.
        deck_id (int): The identifier of the deck being right-clicked.

    Returns:
        None: Menu items are added as a side effect.
    """

    if deck_id is None:  # type: ignore[unreachable]
        return

    col = _get_collection()
    if col is None:
        return

    snapshot = get_custom_deck_rule_snapshot(str(deck_id))
    submenu = menu.addMenu("Progressive Siblings")
    if submenu is None:
        return

    # Add the ignore/unignore toggle action
    ignore_label = "Unignore current deck" if snapshot[CONFIG_IGNORED_KEY] else "Ignore current deck"
    _add_action(submenu, ignore_label, _toggle_ignore_state, deck_id)

    # Add the custom interval configuration action
    _add_action(
        submenu,
        "Set Stability threshold…",
        _set_custom_stability_threshold,
        deck_id,
    )

    _add_action(submenu, "Reprocess deck", _reprocess_deck, deck_id)

    # Add an action to search the browser for cards ignored by SibPush in this deck
    _add_action(submenu, "Show ignored cards…", _show_ignored_cards_in_browser, deck_id)
    _add_action(submenu, "Restore all managed cards…", _restore_all_managed_cards, deck_id)
