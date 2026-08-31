"""FSRS Stability and card-template progression helpers."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Sequence, cast

from anki.cards import Card
from anki.notes import Note

from ..config.parser import config_settings, custom_deck_rules_by_did
from ..logging_support import logThis


@dataclass(frozen=True)
class Stage:
    """One existing card in the configured template progression."""

    card: Card
    name: str
    ord: int


def _note_type(note: Note) -> dict[str, Any] | None:
    raw_note_type = note.note_type()
    return cast(dict[str, Any], raw_note_type) if isinstance(raw_note_type, dict) else None


def note_type_name(note: Note) -> str:
    """Return the concrete note-type name used for configuration matching."""

    note_type = _note_type(note)
    return str(note_type.get("name", "")) if note_type is not None else ""


def get_note_type_rule(note: Note) -> dict[str, Any] | None:
    """Resolve the first enabled exact-or-glob note-type rule."""

    raw_rules = config_settings.get("note_types", {})
    if not isinstance(raw_rules, dict):
        return None

    concrete_name = note_type_name(note)
    for raw_pattern, raw_rule in cast(dict[str, Any], raw_rules).items():
        pattern = str(raw_pattern).strip()
        if not pattern or not fnmatchcase(concrete_name, pattern):
            continue

        if raw_rule is True:
            return {"enabled": True}
        if not isinstance(raw_rule, dict) or not bool(raw_rule.get("enabled", False)):
            return None
        return cast(dict[str, Any], raw_rule)

    return None


def _template_name(note_type: dict[str, Any], card: Card) -> str | None:
    templates = note_type.get("tmpls")
    if not isinstance(templates, list):
        return None

    card_ord = int(card.ord)
    if card_ord < 0 or card_ord >= len(templates):
        return None

    template = templates[card_ord]
    if not isinstance(template, dict):
        return None
    name = str(template.get("name", "")).strip()
    return name or None


def _expected_stage_names(rule: dict[str, Any]) -> dict[int, str] | None:
    raw_stages = rule.get("stages")
    if raw_stages is None:
        return None
    if not isinstance(raw_stages, list):
        return {}

    expected: dict[int, str] = {}
    for index, raw_stage in enumerate(raw_stages):
        if isinstance(raw_stage, str):
            expected[index] = raw_stage.strip()
            continue
        if not isinstance(raw_stage, dict):
            return {}
        try:
            stage_ord = int(raw_stage.get("ord", index))
        except (TypeError, ValueError):
            return {}
        stage_name = str(raw_stage.get("name", "")).strip()
        if stage_ord < 0 or not stage_name or stage_ord in expected:
            return {}
        expected[stage_ord] = stage_name
    return expected


def resolve_stages(note: Note, siblings: Sequence[Card]) -> list[Stage]:
    """Return existing sibling stages ordered exclusively by ``card.ord``.

    Missing optional cards are naturally skipped. A duplicate ordinal or configured template
    name mismatch is treated as unsafe and leaves the note unmanaged.
    """

    rule = get_note_type_rule(note)
    note_type = _note_type(note)
    if rule is None or note_type is None:
        return []

    expected_names = _expected_stage_names(rule)
    if expected_names == {} and rule.get("stages") is not None:
        logThis(lambda: f"Progressive Siblings: invalid stage config for {note_type_name(note)!r}")
        return []

    stages: list[Stage] = []
    seen_ords: set[int] = set()
    for card in sorted(siblings, key=lambda candidate: int(candidate.ord)):
        card_ord = int(card.ord)
        if card_ord in seen_ords:
            logThis(
                lambda: (
                    f"Progressive Siblings: note {note.id} has duplicate card ordinal {card_ord}"
                )
            )
            return []
        seen_ords.add(card_ord)

        actual_name = _template_name(note_type, card)
        if actual_name is None:
            logThis(
                lambda: f"Progressive Siblings: note {note.id} has no template for ord {card_ord}"
            )
            return []

        if expected_names is not None:
            expected_name = expected_names.get(card_ord)
            if expected_name is None or actual_name != expected_name:
                logThis(
                    lambda: (
                        f"Progressive Siblings: template mismatch for note {note.id}, ord "
                        f"{card_ord}: expected {expected_name!r}, got {actual_name!r}"
                    )
                )
                return []

        stages.append(Stage(card=card, name=actual_name, ord=card_ord))

    return stages


def card_stability(card: Card) -> float | None:
    """Read FSRS Stability without modifying scheduler data."""

    memory_state = getattr(card, "memory_state", None)
    if memory_state is None:
        return None

    try:
        stability = float(memory_state.stability)
    except (AttributeError, TypeError, ValueError):
        return None
    return stability if stability >= 0 else None


def is_mature(card: Card, threshold: float) -> bool:
    """A card without an FSRS memory state is always immature."""

    stability = card_stability(card)
    return stability is not None and stability >= threshold


def _matching_tag_threshold(note: Note) -> float | None:
    raw_rules = config_settings.get("tag_rules", {})
    if not isinstance(raw_rules, dict):
        return None

    note_tags = {str(tag).strip() for tag in getattr(note, "tags", []) if str(tag).strip()}
    for raw_tag, raw_rule in cast(dict[str, Any], raw_rules).items():
        if str(raw_tag).strip() not in note_tags or not isinstance(raw_rule, dict):
            continue
        try:
            return float(
                raw_rule.get("stability_threshold", raw_rule.get("interval"))
            )
        except (TypeError, ValueError):
            return None
    return None


def _deck_threshold(card: Card) -> float | None:
    rule = custom_deck_rules_by_did.get(str(card.did))
    if rule is None:
        return None
    try:
        return float(rule.get("stability_threshold", rule.get("interval")))
    except (TypeError, ValueError):
        return None


def _transition_threshold(previous_name: str, target_name: str) -> float | None:
    raw_transitions = config_settings.get("progression", [])
    if not isinstance(raw_transitions, list):
        return None

    for raw_transition in raw_transitions:
        if not isinstance(raw_transition, dict):
            continue
        if (
            str(raw_transition.get("from", "")).strip() != previous_name
            or str(raw_transition.get("to", "")).strip() != target_name
        ):
            continue
        try:
            threshold = float(
                raw_transition.get(
                    "stability",
                    raw_transition.get("stability_threshold"),
                )
            )
        except (TypeError, ValueError):
            return None
        return threshold if threshold >= 0 else None
    return None


def transition_threshold(note: Note, previous: Stage, target: Stage) -> float:
    """Resolve tag, deck, transition, then global threshold precedence."""

    tag_threshold = _matching_tag_threshold(note)
    if tag_threshold is not None:
        return tag_threshold

    deck_threshold = _deck_threshold(previous.card)
    if deck_threshold is not None:
        return deck_threshold

    configured_transition = _transition_threshold(previous.name, target.name)
    if configured_transition is not None:
        return configured_transition

    try:
        return float(config_settings.get("default_stability_threshold", 7))
    except (TypeError, ValueError):
        return 7.0
