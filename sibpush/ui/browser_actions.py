"""Card-browser actions for the SibPush add-on."""

from __future__ import annotations

from typing import Any, Callable

from aqt.browser.browser import Browser
from aqt.qt import QMenu

from ..processing.notes import process_note
from ..processing.suspension import card_is_ignored, clear_card_ignored, set_card_ignored
from ..state import get_mw


def _action_text(action: Any) -> str:
    """Return an action's display text, handling both real Qt actions and test doubles.

    Args:
        action (Any): A QAction (or QAction-like object) from a menu.

    Returns:
        str: The action's label with any '&' mnemonic markers stripped, or an empty
            string if the text cannot be determined.
    """

    text_attr = getattr(action, "text", "")
    text = text_attr() if callable(text_attr) else text_attr
    return str(text or "").strip().replace("&", "")


def _find_notes_action(menu: QMenu) -> Any | None:
    """Find the "Notes" submenu action within a card-browser context menu.

    Anki's card browser context menu ends with a "Notes" submenu (containing actions
    like Find & Replace, Find Duplicates, etc.). Locating it lets SibPush insert its own
    submenu immediately before it instead of always trailing at the very end.

    Args:
        menu (aqt.qt.QMenu): The context menu to search.

    Returns:
        Any | None: The "Notes" action if found, otherwise None.
    """

    actions_attr: Callable[[], list[Any]] | None = getattr(menu, "actions", None)
    if not callable(actions_attr):
        return None

    for action in actions_attr():
        if _action_text(action) == "Notes":
            return action

    return None


def add_browser_card_actions(browser: Browser, menu: QMenu) -> None:
    """Add the SibPush placeholder submenu to the card browser context menu.

    Args:
        browser (aqt.browser.Browser): The Anki card browser instance.
        menu (aqt.qt.QMenu): The context menu to extend.

    Returns:
        None: The menu is modified in place.
    """

    card_ids = browser.selectedCards()
    if not card_ids:
        return

    current_mw = get_mw()
    if current_mw is None:
        return
    col = getattr(current_mw, "col", None)
    if col is None:
        return

    cards = [col.get_card(cid) for cid in card_ids]
    all_ignored = all(card_is_ignored(card) for card in cards)
    label = "Ignore card" if len(card_ids) == 1 else "Ignore cards"

    # Anki appends a "Notes" submenu as the last entry in this context menu. Rather than
    # tacking SibPush on after it, insert our submenu (and surrounding separators) right
    # before it so SibPush doesn't end up trailing below Anki's own items.
    notes_action = _find_notes_action(menu)
    can_insert = notes_action is not None and callable(getattr(menu, "insertMenu", None))

    if can_insert:
        submenu: Any = QMenu("Progressive Siblings", menu)
        menu.insertSeparator(notes_action)
        menu.insertMenu(notes_action, submenu)
        menu.insertSeparator(notes_action)
    else:
        submenu = menu.addMenu("Progressive Siblings")

    ignore_action: Any = submenu.addAction(label)
    ignore_action.setCheckable(True)
    ignore_action.setChecked(all_ignored)

    def handle_ignore_toggle() -> None:
        if all_ignored:
            for card in cards:
                clear_card_ignored(col, card)
        else:
            for card in cards:
                set_card_ignored(col, card)

        browser.model.reset()

    ignore_action.triggered.connect(handle_ignore_toggle)

    reevaluate_action: Any = submenu.addAction("Re-evaluate selected notes")

    def handle_reevaluate() -> None:
        note_ids = {card.nid for card in cards}
        for note_id in note_ids:
            process_note(col, note_id)
        browser.model.reset()

    reevaluate_action.triggered.connect(handle_reevaluate)
