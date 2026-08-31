"""Legacy configuration migration helpers for the SibPush add-on."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

from . import parser
from ..state import CONFIG_IGNORED_KEY, get_config_file_path, get_mw


def _load_sibpush_profile_config() -> dict[str, Any] | None:
    """Read SibPush's collection-adjacent config without modifying the original file."""

    progressive_path = get_config_file_path()
    if progressive_path is None:
        return None
    sibpush_path = progressive_path.with_name("sibpush_config.json")
    try:
        with sibpush_path.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _merge_sibpush_config(
    progressive_defaults: dict[str, Any], sibpush_config: dict[str, Any]
) -> dict[str, Any]:
    """Carry deck/tag policy forward while retaining Progressive note-type definitions."""

    migrated = deepcopy(progressive_defaults)
    default_threshold = sibpush_config.get("default_interval", 7)
    migrated["default_stability_threshold"] = default_threshold
    migrated["debug"] = bool(sibpush_config.get("debug", migrated.get("debug", False)))

    migrated_deck_rules: list[dict[str, Any]] = []
    raw_deck_rules = sibpush_config.get("custom_deck_rules", [])
    if isinstance(raw_deck_rules, list):
        for raw_rule in raw_deck_rules:
            if not isinstance(raw_rule, dict):
                continue
            rule = dict(raw_rule)
            rule["stability_threshold"] = rule.pop("interval", default_threshold)
            migrated_deck_rules.append(rule)
    migrated["custom_deck_rules"] = migrated_deck_rules

    migrated_tag_rules: dict[str, dict[str, Any]] = {}
    raw_tag_rules = sibpush_config.get("tag_rules", {})
    if isinstance(raw_tag_rules, dict):
        for raw_tag, raw_rule in raw_tag_rules.items():
            if not isinstance(raw_rule, dict):
                continue
            migrated_tag_rules[str(raw_tag)] = {
                "stability_threshold": raw_rule.get("interval", default_threshold)
            }
    if migrated_tag_rules:
        migrated["tag_rules"] = migrated_tag_rules
    return migrated


def _get_deck_lookup() -> dict[str, str]:
    """Build a mapping of current deck names to deck ids.

    Returns:
        dict[str, str]: A lookup table keyed by deck name.
    """

    mw = get_mw()
    if mw is None:
        return {}

    col = getattr(mw, "col", None)
    if col is None or not hasattr(col, "decks"):
        return {}

    return {deck.name: str(deck.id) for deck in col.decks.all_names_and_ids()}


def _build_migrated_config(
    config: dict[str, Any], deck_lookup: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Convert a legacy ignored_decks config into the new schema.

    Args:
        config (dict[str, Any]): The old configuration dictionary.
        deck_lookup (dict[str, str] | None): Optional mapping of deck names to deck ids.

    Returns:
        dict[str, Any] | None: The migrated configuration, or None when migration is not possible.
    """

    legacy_ignored_decks = config.get("ignored_decks")
    if not isinstance(legacy_ignored_decks, list):
        return None

    lookup = deck_lookup or {}
    migrated_rules: list[dict[str, Any]] = []
    default_interval = parser.parse_int(config.get("default_interval", 30), 30)
    legacy_rule_interval = parser.parse_int(
        config.get("interval", default_interval), default_interval
    )

    for deck_label in cast(list[object], legacy_ignored_decks):
        raw_label = str(deck_label).strip()
        if not raw_label:
            continue

        did = lookup.get(raw_label)
        if did is None and raw_label.isdigit():
            # Legacy configs may already have stored the deck id as text; keep that value
            # when there is no current deck-name match to translate it back.
            did = raw_label

        if did is None:
            # We can only migrate a legacy deck name when we know the current deck list.
            if not lookup:
                return None
            continue

        migrated_rules.append({"did": did, "name": raw_label, CONFIG_IGNORED_KEY: True})

    return {
        "default_interval": default_interval,
        "custom_deck_rules": [
            {**rule, "interval": legacy_rule_interval} for rule in migrated_rules
        ],
        "debug": bool(config.get("debug", False)),
    }


def migrate_legacy_config() -> bool:
    """Rewrite an old-style config into the new format when needed.

    Returns:
        bool: True when a migration was written, otherwise False.
    """

    profile_config_file = get_config_file_path()
    if profile_config_file is not None and profile_config_file.exists():
        return False

    addon_manager = parser.addon_manager
    if addon_manager is None:
        return False

    current_config = addon_manager.getConfig(parser.addon_module_name())
    if not isinstance(current_config, dict):
        return False
    current_config = cast(dict[str, Any], current_config)

    sibpush_profile_config = _load_sibpush_profile_config()
    if sibpush_profile_config is not None:
        migrated_config = _merge_sibpush_config(current_config, sibpush_profile_config)
        parser.save_profile_config(migrated_config)
        parser.config_settings.clear()
        parser.config_settings.update(parser.parse_config(migrated_config))
        return True

    if "ignored_decks" in current_config:
        migrated_config = _build_migrated_config(current_config, _get_deck_lookup())
        if migrated_config is None:
            return False

        parser.save_profile_config(migrated_config)
        parser.config_settings.clear()
        parser.config_settings.update(parser.parse_config(migrated_config))
        return True

    parser.save_profile_config(current_config)
    parser.config_settings.clear()
    parser.config_settings.update(parser.parse_config(current_config))
    return True
