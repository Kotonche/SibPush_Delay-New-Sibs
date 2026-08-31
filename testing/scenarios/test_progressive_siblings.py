from __future__ import annotations

from types import SimpleNamespace
from importlib import import_module

from anki.consts import (
    CARD_TYPE_NEW,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
    QUEUE_TYPE_SIBLING_BURIED,
    QUEUE_TYPE_SUSPENDED,
)

from ..addon_utils import patched_addon_state
from ..card_utils import (
    assert_card_queues,
    card_is_suspended_by_addon,
    card_is_unlocked,
    card_custom_data,
    set_card_custom_data,
    set_card_ignored,
    set_review_card_state,
)
from ..collection_utils import temporary_collection
from ..note_utils import (
    TEST_NOTE_TYPE_NAME,
    add_note_with_siblings,
    build_test_notetype,
    make_test_deck_id,
)


def _prefetched_cards(col, cards, stabilities: dict[int, float | None] | None = None):
    """Fetch current cards and attach read-only FSRS states for one processing call."""

    stabilities = stabilities or {}
    prefetched = [col.get_card(card.id) for card in cards]
    for card in prefetched:
        stability = stabilities.get(int(card.ord))
        card.memory_state = (
            SimpleNamespace(stability=stability, difficulty=5.0)
            if stability is not None
            else None
        )
    return prefetched


def _new_four_card_note(col, text: str = "progressive note"):
    model = build_test_notetype(col, card_count=4)
    deck_id = make_test_deck_id(col)
    return add_note_with_siblings(
        col,
        model,
        deck_id,
        text,
        expected_card_count=4,
    )


def _review(col, card, stability: float) -> None:
    set_review_card_state(col, card, ivl=max(1, round(stability)))


def test_a_brand_new_note_exposes_only_first_ord() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_NEW, QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_SUSPENDED],
        )
        assert card_is_unlocked(col, cards[0])
        assert all(card_is_suspended_by_addon(col, card) for card in cards[1:])


def test_b_first_stage_maturity_unlocks_only_second_stage() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))
            _review(col, cards[0], 7.2)
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 7.2}),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_REV, QUEUE_TYPE_NEW, QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_SUSPENDED],
        )
        assert card_is_unlocked(col, cards[1])


def test_b_reviewer_unlock_is_buried_until_tomorrow() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))
            _review(col, cards[0], 7.2)
            addon.process_note(
                col,
                note.id,
                coming_from_reviewer_hook=True,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 7.2}),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_REV, QUEUE_TYPE_SIBLING_BURIED, QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_SUSPENDED],
        )


def test_c_immature_second_stage_keeps_third_locked() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 30)
        _review(col, cards[1], 4)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 4}),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_REV, QUEUE_TYPE_REV, QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_SUSPENDED],
        )


def test_d_mature_second_stage_unlocks_third() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 30)
        _review(col, cards[1], 8)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 8}),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_REV, QUEUE_TYPE_REV, QUEUE_TYPE_NEW, QUEUE_TYPE_SUSPENDED],
        )


def test_e_unlocked_stage_does_not_regress() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 30)
        _review(col, cards[1], 20)
        _review(col, cards[2], 8)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 20, 2: 8}),
            )
            assert col.get_card(cards[3].id).queue == QUEUE_TYPE_NEW

            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 20, 2: 2}),
            )

        assert col.get_card(cards[3].id).queue == QUEUE_TYPE_NEW
        assert card_is_unlocked(col, cards[3])


def test_f_manual_suspension_is_never_removed() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        col.sched.suspend_cards([cards[3].id])
        _review(col, cards[0], 30)
        _review(col, cards[1], 20)
        _review(col, cards[2], 8)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 20, 2: 8}),
            )

        assert col.get_card(cards[3].id).queue == QUEUE_TYPE_SUSPENDED
        assert not card_is_suspended_by_addon(col, cards[3])
        assert card_is_unlocked(col, cards[3])


def test_g_sync_reconciliation_can_unlock_multiple_contiguous_mature_stages() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 120)
        _review(col, cards[1], 80)
        _review(col, cards[2], 40)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 120, 1: 80, 2: 40}),
            )

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_REV, QUEUE_TYPE_REV, QUEUE_TYPE_REV, QUEUE_TYPE_NEW],
        )


def test_h_missing_final_card_is_valid() -> None:
    with temporary_collection() as col:
        model = build_test_notetype(col, card_count=3)
        deck_id = make_test_deck_id(col)
        note, cards = add_note_with_siblings(col, model, deck_id, "three stages")
        _review(col, cards[0], 30)
        _review(col, cards[1], 8)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 30, 1: 8}),
            )

        assert_card_queues(col, cards, [QUEUE_TYPE_REV, QUEUE_TYPE_REV, QUEUE_TYPE_NEW])


def test_new_card_without_memory_state_is_immature_even_with_large_interval() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        first = col.get_card(cards[0].id)
        first.type = CARD_TYPE_NEW
        first.queue = QUEUE_TYPE_NEW
        first.ivl = 365
        col.update_card(first)

        with patched_addon_state(col) as addon:
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))

        assert col.get_card(cards[1].id).queue == QUEUE_TYPE_SUSPENDED


def test_template_name_mismatch_disables_note_safely() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.config_settings["note_types"] = {
                TEST_NOTE_TYPE_NAME: {
                    "enabled": True,
                    "stages": [{"ord": 0, "name": "Wrong template name"}],
                }
            }
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))

        assert_card_queues(col, cards, [QUEUE_TYPE_NEW] * 4)


def test_unconfigured_note_type_is_ignored() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.config_settings["note_types"] = {}
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))

        assert_card_queues(col, cards, [QUEUE_TYPE_NEW] * 4)


def test_progression_does_not_modify_source_scheduling_fields() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 8)
        source_before = col.get_card(cards[0].id)
        before = (source_before.type, source_before.queue, source_before.ivl, source_before.due)

        with patched_addon_state(col) as addon:
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 8}),
            )

        source_after = col.get_card(cards[0].id)
        assert (source_after.type, source_after.queue, source_after.ivl, source_after.due) == before


def test_per_transition_threshold_is_not_hardcoded() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        _review(col, cards[0], 5.2)

        with patched_addon_state(col) as addon:
            addon.config_settings["progression"] = [
                {"from": "Card 1", "to": "Card 2", "stability": 5}
            ]
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 5.2}),
            )

        assert col.get_card(cards[1].id).queue == QUEUE_TYPE_NEW


def test_tag_threshold_overrides_named_transition() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        note.add_tag("progression::slow")
        col.update_note(note)
        _review(col, cards[0], 8)

        with patched_addon_state(col) as addon:
            addon.config_settings["progression"] = [
                {"from": "Card 1", "to": "Card 2", "stability": 5}
            ]
            addon.config_settings["tag_rules"] = {
                "progression::slow": {"stability_threshold": 14}
            }
            addon.process_note(
                col,
                note.id,
                prefetched_siblings=_prefetched_cards(col, cards, {0: 8}),
            )

        assert col.get_card(cards[1].id).queue == QUEUE_TYPE_SUSPENDED


def test_sibpush_2_markers_are_transferred_without_losing_third_party_data() -> None:
    with temporary_collection() as col:
        _, cards = _new_four_card_note(col)
        col.sched.suspend_cards([cards[1].id])

        with patched_addon_state(col) as addon:
            state = import_module(f"{addon.__name__}.sibpush.state")
            migration = import_module(f"{addon.__name__}.sibpush.migration")
            set_card_custom_data(
                col,
                cards[1],
                {
                    state.LEGACY_SIBPUSH_SUSPENDED_KEY: True,
                    state.LEGACY_SIBPUSH_IGNORED_KEY: True,
                    "third": {"owner": "other-addon"},
                },
            )
            migration.migrate_sibpush_2_card_markers(col)

            migrated = card_custom_data(col, cards[1])
            assert state.LEGACY_SIBPUSH_SUSPENDED_KEY not in migrated
            assert state.LEGACY_SIBPUSH_IGNORED_KEY not in migrated
            assert migrated[state.SIBPUSH_SUSPENDED_KEY] is True
            assert migrated[state.SIBPUSH_IGNORED_KEY] is True
            assert migrated["third"] == {"owner": "other-addon"}


def test_restore_managed_cards_leaves_manual_suspension_untouched() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))
            col.sched.suspend_cards([cards[0].id])
            suspension = import_module(f"{addon.__name__}.sibpush.processing.suspension")
            suspension.unsuspend_all_addon_cards(col)

        assert_card_queues(
            col,
            cards,
            [QUEUE_TYPE_SUSPENDED, QUEUE_TYPE_NEW, QUEUE_TYPE_NEW, QUEUE_TYPE_NEW],
        )
        assert not card_is_suspended_by_addon(col, cards[0])
        assert all(not card_is_suspended_by_addon(col, card) for card in cards[1:])


def test_restore_managed_cards_also_restores_ignored_owned_cards() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)

        with patched_addon_state(col) as addon:
            addon.process_note(col, note.id, prefetched_siblings=_prefetched_cards(col, cards))
            set_card_ignored(col, cards[2])
            suspension = import_module(f"{addon.__name__}.sibpush.processing.suspension")
            suspension.unsuspend_all_addon_cards(col)

        assert_card_queues(col, cards, [QUEUE_TYPE_NEW] * 4)
        assert all(not card_is_suspended_by_addon(col, card) for card in cards)


def test_sibpush_config_migration_preserves_progressive_rules() -> None:
    with temporary_collection() as col:
        with patched_addon_state(col) as addon:
            migration = import_module(f"{addon.__name__}.sibpush.config.migration")
            defaults = {
                "default_stability_threshold": 7,
                "note_types": {"Greek Vocabulary*": {"enabled": True}},
                "progression": [{"from": "One", "to": "Two", "stability": 9}],
                "custom_deck_rules": [],
                "tag_rules": {},
                "debug": False,
            }
            migrated = migration._merge_sibpush_config(
                defaults,
                {
                    "default_interval": 6.5,
                    "custom_deck_rules": [
                        {"did": "123", "name": "Greek", "ignored": False, "interval": 4.5}
                    ],
                    "tag_rules": {"fast": {"interval": 2.5}},
                    "debug": True,
                },
            )

        assert migrated["default_stability_threshold"] == 6.5
        assert migrated["custom_deck_rules"][0]["stability_threshold"] == 4.5
        assert migrated["tag_rules"]["fast"]["stability_threshold"] == 2.5
        assert migrated["note_types"] == defaults["note_types"]
        assert migrated["progression"] == defaults["progression"]


def test_fractional_stability_threshold_survives_config_parsing() -> None:
    with temporary_collection() as col:
        with patched_addon_state(col) as addon:
            parser = import_module(f"{addon.__name__}.sibpush.config.parser")
            parsed = parser.parse_config(
                {
                    "debug": False,
                    "default_stability_threshold": 7.25,
                    "custom_deck_rules": [
                        {
                            "did": "123",
                            "name": "Greek",
                            "ignored": False,
                            "stability_threshold": 4.75,
                        }
                    ],
                    "tag_rules": {},
                    "note_types": {},
                    "progression": [],
                }
            )
            parser.config_settings.clear()
            parser.config_settings.update(parsed)
            snapshot = parser.get_custom_deck_rule_snapshot("123")

        assert parsed["default_stability_threshold"] == 7.25
        assert snapshot["stability_threshold"] == 4.75


def test_version_3_migration_runs_all_stages_and_persists_completion() -> None:
    with temporary_collection() as col:
        note, cards = _new_four_card_note(col)
        col.sched.suspend_cards([cards[1].id])

        with patched_addon_state(col) as addon:
            state = import_module(f"{addon.__name__}.sibpush.state")
            migration = import_module(f"{addon.__name__}.sibpush.migration")
            set_card_custom_data(
                col,
                cards[1],
                {state.LEGACY_SIBPUSH_SUSPENDED_KEY: True},
            )
            completions: list[bool] = []
            migration.migrate_to_version_3(
                col,
                on_complete=lambda: completions.append(True),
            )

            assert completions == [True]
            assert state.installed_version == state.ADDON_VERSION
            assert state.LEGACY_SIBPUSH_SUSPENDED_KEY not in card_custom_data(col, cards[1])
            assert card_is_suspended_by_addon(col, cards[1])
