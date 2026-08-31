"""
Utilities for manipulating and asserting Anki Card objects in a test collection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING
from types import SimpleNamespace

from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV

if TYPE_CHECKING:
    from anki.cards import Card, CardId
    from anki.collection import Collection


_addon_constants_state = SimpleNamespace(
    ADDON_CUSTOM_DATA_KEY="",
    ADDON_CUSTOM_DATA_IGNORED_VALUE="",
    SIBPUSH_IGNORED_KEY="",
    SIBPUSH_SUSPENDED_KEY="",
    SIBPUSH_MARKER_VALUE=True,
    PROGRESSIVE_UNLOCKED_KEY="",
    CONFIG_IGNORED_KEY="",
)


def set_addon_constants(addon: object) -> None:
    _addon_constants_state.ADDON_CUSTOM_DATA_KEY = getattr(addon, "ADDON_CUSTOM_DATA_KEY", "")
    _addon_constants_state.ADDON_CUSTOM_DATA_IGNORED_VALUE = getattr(
        addon, "ADDON_CUSTOM_DATA_IGNORED_VALUE", ""
    )
    _addon_constants_state.SIBPUSH_IGNORED_KEY = getattr(addon, "SIBPUSH_IGNORED_KEY", "")
    _addon_constants_state.SIBPUSH_SUSPENDED_KEY = getattr(addon, "SIBPUSH_SUSPENDED_KEY", "")
    _addon_constants_state.SIBPUSH_MARKER_VALUE = getattr(addon, "SIBPUSH_MARKER_VALUE", True)
    _addon_constants_state.PROGRESSIVE_UNLOCKED_KEY = getattr(
        addon, "PROGRESSIVE_UNLOCKED_KEY", ""
    )
    _addon_constants_state.CONFIG_IGNORED_KEY = getattr(addon, "CONFIG_IGNORED_KEY", "")


def _addon_constants() -> SimpleNamespace:
    return _addon_constants_state


def set_review_card_state(col: "Collection", card: "Card", *, ivl: int) -> None:
    """
    Force a card into the 'Review' state with a specific interval.

    This is used to simulate 'mature' or 'immature' siblings.
    """
    card.type = CARD_TYPE_REV
    card.queue = QUEUE_TYPE_REV
    card.ivl = ivl
    card.due = 1  # Arbitrary due date (tomorrow)
    col.update_card(card)


def _load_custom_data(card: "Card") -> dict[str, object]:
    raw_custom_data = getattr(card, "custom_data", "")
    if not raw_custom_data:
        return {}

    try:
        parsed = json.loads(raw_custom_data)
    except (TypeError, json.JSONDecodeError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def set_card_custom_data(col: "Collection", card: "Card", custom_data: dict[str, object]) -> None:
    fresh_card = col.get_card(card.id)
    fresh_card.custom_data = json.dumps(custom_data, ensure_ascii=False) if custom_data else ""
    col.update_card(fresh_card)


def card_custom_data(col: "Collection", card: "Card") -> dict[str, object]:
    return _load_custom_data(col.get_card(card.id))


def set_card_ignored(col: "Collection", card: "Card") -> None:
    addon = _addon_constants()
    data = card_custom_data(col, card)
    data[getattr(addon, "SIBPUSH_IGNORED_KEY")] = getattr(addon, "SIBPUSH_MARKER_VALUE")
    set_card_custom_data(col, card, data)


def clear_card_ignored(col: "Collection", card: "Card") -> None:
    addon = _addon_constants()
    data = card_custom_data(col, card)
    if data.pop(getattr(addon, "SIBPUSH_IGNORED_KEY"), None) is None:
        return
    set_card_custom_data(col, card, data)


def card_is_ignored(col: "Collection", card: "Card") -> bool:
    addon = _addon_constants()
    return card_custom_data(col, card).get(getattr(addon, "SIBPUSH_IGNORED_KEY")) is getattr(
        addon, "SIBPUSH_MARKER_VALUE"
    )


def set_card_suspended_by_addon(col: "Collection", card: "Card") -> None:
    addon = _addon_constants()
    data = card_custom_data(col, card)
    data[getattr(addon, "SIBPUSH_SUSPENDED_KEY")] = getattr(addon, "SIBPUSH_MARKER_VALUE")
    set_card_custom_data(col, card, data)


def card_is_suspended_by_addon(col: "Collection", card: "Card") -> bool:
    addon = _addon_constants()
    return card_custom_data(col, card).get(getattr(addon, "SIBPUSH_SUSPENDED_KEY")) is getattr(
        addon, "SIBPUSH_MARKER_VALUE"
    )


def card_is_unlocked(col: "Collection", card: "Card") -> bool:
    addon = _addon_constants()
    return card_custom_data(col, card).get(
        getattr(addon, "PROGRESSIVE_UNLOCKED_KEY")
    ) is getattr(addon, "SIBPUSH_MARKER_VALUE")


def assert_card_is_suspended_by_addon(col: "Collection", card: "Card") -> None:
    assert card_is_suspended_by_addon(col, card), (
        f"Card {card.id} should carry SibPush suspension provenance"
    )


def assert_card_is_not_suspended_by_addon(col: "Collection", card: "Card") -> None:
    assert not card_is_suspended_by_addon(col, card), (
        f"Card {card.id} should not carry SibPush suspension provenance"
    )


def assert_card_is_ignored(col: "Collection", card: "Card") -> None:
    assert card_is_ignored(col, card), f"Card {card.id} should be marked as ignored"


def assert_card_is_not_ignored(col: "Collection", card: "Card") -> None:
    assert not card_is_ignored(col, card), f"Card {card.id} should not be marked as ignored"


def card_queue(col: "Collection", card_id: "CardId") -> int:
    """
    Fetch the current queue status of a card directly from the database.

    0 = New, 2 = Review, -1 = Suspended.
    """
    return col.get_card(card_id).queue


def assert_card_queues(
    col: "Collection", cards: Sequence["Card"], expected_queues: Sequence[int]
) -> None:
    """
    Assert that a list of cards matches a sequence of expected queue statuses.
    """
    actual_queues = [card_queue(col, card.id) for card in cards]
    assert actual_queues == expected_queues, f"Expected {expected_queues}, but got {actual_queues}"
