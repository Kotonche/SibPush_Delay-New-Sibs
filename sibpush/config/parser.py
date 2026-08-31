"""Configuration parsing and update hooks for the SibPush add-on."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from aqt import mw

from ..logging_support import initialize_log_file, logThis
from ..state import (
    CONFIG_IGNORED_KEY,
    discard_pending_unsuspend_deck_id,
    get_mw,
    get_config_file_path,
    queue_pending_browser_work,
    save_persistent_state,
)

# Module-level state that caches parsed configuration data.
# These values are derived from the raw config, kept in sync by parse_config(),
# and treated as read-only by the rest of the package.
# `did` is the stable deck identifier; `name` is only for display.
ignored_deck_ids: list[str] = []  # Deck IDs that should be skipped by the add-on.
custom_deck_rules_by_did: dict[str, dict[str, Any]] = {}  # Per-deck rules indexed by deck ID.


def _addon_module_name() -> str:
    """Return the top-level module name used by Anki's add-on manager.

    Returns:
        str: The root package name for this add-on, regardless of internal package depth.
    """

    package_name = __package__ or __name__
    return package_name.split(".", 1)[0]


def addon_module_name() -> str:
    """Return the top-level module name for callers outside this parser module."""

    return _addon_module_name()


def _get_addon_manager() -> Any | None:
    """Return the live add-on manager when Anki is running.

    Returns:
        Any | None: The Anki add-on manager instance, or None if unavailable.
    """

    current_mw = get_mw()
    if current_mw is not None and hasattr(current_mw, "addonManager"):
        return current_mw.addonManager

    return addon_manager


def _read_profile_config_file(config_file: Path) -> dict[str, Any]:
    """Read a profile-local config file and normalize basic JSON failures.

    Args:
        config_file (pathlib.Path): The file to read from disk.

    Returns:
        dict[str, Any]: The decoded JSON object, or ``{}`` when the file is missing or invalid.
    """

    try:
        with config_file.open("r", encoding="utf-8") as handle:
            payload: Any = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}

    return cast(dict[str, Any], payload)


def _write_profile_config_file(config_file: Path, payload: dict[str, Any]) -> None:
    """Write a profile-local config file atomically.

    Args:
        config_file (pathlib.Path): The file to update on disk.
        payload (dict[str, Any]): The JSON-ready configuration dictionary.

    Returns:
        None: The file is updated in place.
    """

    config_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_file.parent,
            prefix=f"{config_file.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)

        os.replace(temp_path, config_file)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _load_profile_config(col: Any | None = None) -> dict[str, Any] | None:
    """Load the profile-local config snapshot when it exists.

    Args:
        col (Any | None): The collection used to resolve the profile directory.

    Returns:
        dict[str, Any] | None: The stored config object, or None when no profile file exists.
    """

    config_file = get_config_file_path(col)
    if config_file is None or not config_file.exists():
        return None

    return _read_profile_config_file(config_file)


def _save_profile_config(config: dict[str, Any], col: Any | None = None) -> None:
    """Persist the current config snapshot to the profile-local file.

    Args:
        config (dict[str, Any]): The config dictionary to persist.
        col (Any | None): The collection used to resolve the profile directory.

    Returns:
        None: The config file is updated for the current profile.
    """

    config_file = get_config_file_path(col)
    if config_file is None:
        return

    _write_profile_config_file(config_file, config)


def save_profile_config(config: dict[str, Any], col: Any | None = None) -> None:
    """Persist a profile config for migration and other parser integrations."""

    _save_profile_config(config, col)


def _parse_int(value: Any, default: int) -> int:
    """Convert a value to an integer, falling back to a default when needed.

    Args:
        value (Any): The value to convert.
        default (int): The value to return when conversion fails.

    Returns:
        int: The parsed integer or the provided default.
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_threshold(value: Any, default: float) -> float:
    """Parse a non-negative Stability threshold in days."""

    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return default
    return threshold if threshold >= 0 else default


def _default_stability_threshold(config: dict[str, Any] | None) -> float:
    """Read the Progressive setting with SibPush's interval key as a migration fallback."""

    if config is None:
        return 7.0
    return _parse_threshold(
        config.get("default_stability_threshold", config.get("default_interval", 7)),
        7.0,
    )


def parse_int(value: Any, default: int) -> int:
    """Parse an integer for callers outside this parser module."""

    return _parse_int(value, default)


def _normalize_custom_deck_rule(rule: Any, default_interval: float) -> dict[str, Any] | None:
    """Normalize one custom deck rule into the canonical schema.

    Args:
        rule (Any): The raw rule object from config.json.
        default_interval (int): The interval to use when the rule omits one.

    Returns:
        dict[str, Any] | None: A normalized rule dictionary, or None when the input is invalid.
    """

    if not isinstance(rule, dict):
        return None

    rule_dict = cast(dict[str, object], rule)
    did = str(rule_dict.get("did", "")).strip()
    name = str(rule_dict.get("name", did)).strip() or did

    if not did and not name:
        return None

    stability_threshold = _parse_threshold(
        rule_dict.get("stability_threshold", rule_dict.get("interval", default_interval)),
        default_interval,
    )
    return {
        "did": did,
        "name": name,
        CONFIG_IGNORED_KEY: bool(rule_dict.get(CONFIG_IGNORED_KEY, False)),
        "stability_threshold": stability_threshold,
        # Kept during the UI migration; inherited deck actions still read this alias.
        "interval": stability_threshold,
    }


def _normalize_tag_rule(rule: Any, default_interval: float) -> dict[str, Any] | None:
    """Normalize one tag rule into the canonical schema.

    Args:
        rule (Any): The raw rule object from config.json.
        default_interval (int): The interval to use when the rule omits one.

    Returns:
        dict[str, Any] | None: A normalized rule dictionary, or None when the input is invalid.
    """

    if not isinstance(rule, dict):
        return None

    rule_dict = cast(dict[str, object], rule)
    stability_threshold = _parse_threshold(
        rule_dict.get("stability_threshold", rule_dict.get("interval", default_interval)),
        default_interval,
    )
    return {
        "stability_threshold": stability_threshold,
        "interval": stability_threshold,
    }


def _parse_custom_deck_rules(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract the normalized custom deck rules list from a config object.

    Args:
        config (dict[str, Any] | None): The raw add-on configuration.

    Returns:
        list[dict[str, Any]]: The normalized custom deck rules.
    """

    if config is None:
        return []

    default_interval = _default_stability_threshold(config)
    raw_rules = config.get("custom_deck_rules")
    if isinstance(raw_rules, list):
        typed_rules = cast(list[object], raw_rules)
        return [
            normalized_rule
            for rule in typed_rules
            if (normalized_rule := _normalize_custom_deck_rule(rule, default_interval)) is not None
        ]

    return []


def _extract_ignored_deck_ids(custom_deck_rules: list[dict[str, Any]]) -> list[str]:
    """Extract the deck ids that should be ignored from the normalized rules.

    Args:
        custom_deck_rules (list[dict[str, Any]]): The normalized deck rules.

    Returns:
        list[str]: The deck ids marked as ignored.
    """

    return [
        str(rule.get("did", "")).strip()
        for rule in custom_deck_rules
        if rule.get(CONFIG_IGNORED_KEY) and str(rule.get("did", "")).strip()
    ]


def _index_custom_deck_rules(custom_deck_rules: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index normalized deck rules by deck id for fast lookup.

    Args:
        custom_deck_rules (list[dict[str, Any]]): The normalized deck rules.

    Returns:
        dict[str, dict[str, Any]]: The rules keyed by deck id.
    """

    return {
        str(rule.get("did", "")).strip(): rule
        for rule in custom_deck_rules
        if str(rule.get("did", "")).strip()
    }


def _extract_custom_deck_rule_effects(
    config_settings: dict[str, Any],
) -> dict[str, tuple[bool, float]]:
    """Return the effective ignore/Stability state for each configured deck."""

    default_threshold = _parse_threshold(
        config_settings.get("default_stability_threshold", 7), 7.0
    )
    raw_rules = config_settings.get("custom_deck_rules")
    if not isinstance(raw_rules, list):
        return {}

    typed_rules = cast(list[object], raw_rules)
    rule_effects: dict[str, tuple[bool, float]] = {}
    for rule in typed_rules:
        if not isinstance(rule, dict):
            continue

        rule_dict = cast(dict[str, object], rule)
        did = str(rule_dict.get("did", "")).strip()
        if not did:
            continue

        rule_effects[did] = (
            bool(rule_dict.get(CONFIG_IGNORED_KEY, False)),
            _parse_threshold(
                rule_dict.get(
                    "stability_threshold",
                    rule_dict.get("interval", default_threshold),
                ),
                default_threshold,
            ),
        )

    return rule_effects


def _should_invalidate_processing_state(
    previous_config_settings: dict[str, Any], current_config_settings: dict[str, Any]
) -> bool:
    """Return True when a config change should queue a fresh timestamp scan.

    The state is reset for changes that can alter SibPush's processing decisions:
    - default interval changes
    - tag rule changes
    - deck unignore changes
    - deck interval changes

    Ignoring a deck is intentionally excluded because that path only queues deck cleanup.
    """

    previous_default_threshold = _parse_threshold(
        previous_config_settings.get("default_stability_threshold", 7), 7.0
    )
    current_default_threshold = _parse_threshold(
        current_config_settings.get("default_stability_threshold", 7), 7.0
    )
    if previous_default_threshold != current_default_threshold:
        return True

    if previous_config_settings.get("note_types", {}) != current_config_settings.get(
        "note_types", {}
    ):
        return True

    if previous_config_settings.get("progression", []) != current_config_settings.get(
        "progression", []
    ):
        return True

    if previous_config_settings.get("tag_rules", {}) != current_config_settings.get(
        "tag_rules", {}
    ):
        return True

    previous_deck_effects = _extract_custom_deck_rule_effects(previous_config_settings)
    current_deck_effects = _extract_custom_deck_rule_effects(current_config_settings)
    all_deck_ids = set(previous_deck_effects) | set(current_deck_effects)

    for deck_id in all_deck_ids:
        previous_ignored, previous_interval = previous_deck_effects.get(
            deck_id, (False, previous_default_threshold)
        )
        current_ignored, current_interval = current_deck_effects.get(
            deck_id, (False, current_default_threshold)
        )

        if previous_interval != current_interval:
            return True

        # Only unignore transitions should invalidate the cached scan state.
        if previous_ignored and not current_ignored:
            return True

    return False


def _get_newly_unignored_deck_ids(
    previous_ignored_deck_ids: list[str], current_ignored_deck_ids: list[str]
) -> list[str]:
    """Return deck ids that were just switched from ignored to managed."""

    current_ids = set(current_ignored_deck_ids)
    return [
        deck_id for deck_id in previous_ignored_deck_ids if deck_id and deck_id not in current_ids
    ]


def get_custom_deck_rule(deck_id: str) -> dict[str, Any] | None:
    """Return the normalized custom rule for a deck id, if one exists.

    Args:
        deck_id (str): The deck identifier to look up.

    Returns:
        dict[str, Any] | None: The normalized rule dictionary, or None if no rule exists.
    """

    return custom_deck_rules_by_did.get(str(deck_id).strip())


def get_custom_deck_rule_snapshot(deck_id: str) -> dict[str, Any]:
    """Return the current deck rule state in a UI-friendly form.

    This function returns a complete rule snapshot with all fields populated,
    using defaults when no custom rule exists for the deck.

    Args:
        deck_id (str): The deck identifier to look up.

    Returns:
        dict[str, Any]: A dictionary containing 'did', 'name', 'ignored', and 'interval' keys.
    """

    rule = get_custom_deck_rule(deck_id) or {}
    default_threshold = _parse_threshold(
        config_settings.get("default_stability_threshold", 7), 7.0
    )
    return {
        "did": str(deck_id).strip(),
        "name": str(rule.get("name", "")).strip(),
        CONFIG_IGNORED_KEY: bool(rule.get(CONFIG_IGNORED_KEY, False)),
        "stability_threshold": _parse_threshold(
            rule.get("stability_threshold", rule.get("interval", default_threshold)),
            default_threshold,
        ),
        "interval": _parse_threshold(
            rule.get("interval", rule.get("stability_threshold", default_threshold)),
            default_threshold,
        ),
    }


def _prepare_custom_deck_rule(
    config: dict[str, Any], deck_id: str, deck_name: str
) -> dict[str, Any]:
    """Return the deck rule to mutate, creating one when needed.

    This function ensures a deck rule exists in the config for the given deck,
    creating a new rule with default values if one doesn't already exist.

    Args:
        config (dict[str, Any]): The configuration dictionary to modify.
        deck_id (str): The deck identifier for the rule.
        deck_name (str): The human-readable deck name for display.

    Returns:
        dict[str, Any]: The deck rule dictionary that can be mutated in place.

    Raises:
        ValueError: If deck_id is empty or whitespace-only.
    """

    normalized_deck_id = str(deck_id).strip()
    if not normalized_deck_id:
        raise ValueError("deck_id cannot be empty")

    normalized_deck_name = str(deck_name).strip() or normalized_deck_id
    default_interval = _default_stability_threshold(config)
    custom_deck_rules = cast(list[dict[str, Any]], config.setdefault("custom_deck_rules", []))

    rule: dict[str, Any] | None = next(
        (
            existing_rule
            for existing_rule in custom_deck_rules
            if str(existing_rule.get("did", "")).strip() == normalized_deck_id
        ),
        None,
    )

    if rule is None:
        rule = {
            "did": normalized_deck_id,
            "name": normalized_deck_name,
            CONFIG_IGNORED_KEY: False,
            "interval": default_interval,
            "stability_threshold": default_interval,
        }
        custom_deck_rules.append(rule)
    else:
        rule["did"] = normalized_deck_id
        rule["name"] = normalized_deck_name
        rule.setdefault(CONFIG_IGNORED_KEY, False)
        rule.setdefault("interval", default_interval)
        rule.setdefault(
            "stability_threshold", rule.get("interval", default_interval)
        )

    return rule


def _apply_config_state(config: dict[str, Any]) -> dict[str, Any]:
    """Parse a config object and refresh the in-memory runtime state.

    This helper only updates the cached runtime config and persists the timestamp state file.
    It does not queue any deferred browser work, which keeps collection startup from being treated
    like an explicit config edit.

    Args:
        config (dict[str, Any]): The raw configuration dictionary to parse.

    Returns:
        dict[str, Any]: The refreshed config_settings dictionary.
    """

    config_settings.clear()
    config_settings.update(parse_config(config))

    current_mw = get_mw()
    col = getattr(current_mw, "col", None) if current_mw is not None else None
    if col is not None:
        save_persistent_state(col)

    return config_settings


def refresh_config_state(config: dict[str, Any]) -> dict[str, Any]:
    """Refresh runtime config and queue any deferred work caused by a real config change."""

    previous_config_settings = deepcopy(config_settings)
    # Capture the old ignored-deck list before parse_config() mutates the shared caches.
    previous_ignored_deck_ids = list(ignored_deck_ids)
    refreshed_settings = _apply_config_state(config)

    newly_ignored_deck_ids = _get_newly_ignored_deck_ids(
        previous_ignored_deck_ids, ignored_deck_ids
    )
    if newly_ignored_deck_ids:
        queue_pending_browser_work(deck_ids=newly_ignored_deck_ids)

    newly_unignored_deck_ids = _get_newly_unignored_deck_ids(
        previous_ignored_deck_ids, ignored_deck_ids
    )
    if newly_unignored_deck_ids:
        for deck_id in newly_unignored_deck_ids:
            discard_pending_unsuspend_deck_id(deck_id)

    if _should_invalidate_processing_state(previous_config_settings, refreshed_settings):
        queue_pending_browser_work(reset_processing_state=True)

    current_mw = get_mw()
    col = getattr(current_mw, "col", None) if current_mw is not None else None
    if col is not None:
        save_persistent_state(col)

    return refreshed_settings


def load_config_state(col: Any | None = None) -> dict[str, Any]:
    """Load the active config snapshot for the current collection and refresh runtime state.

    Args:
        col (Any | None): The collection used to resolve the profile-local config file.

    Returns:
        dict[str, Any]: The refreshed config_settings dictionary.
    """

    config = _load_profile_config(col)
    return _apply_config_state(config or {})


def save_config_state(config: dict[str, Any]) -> dict[str, Any]:
    """Persist a config object and refresh the in-memory runtime state.

    This function writes the configuration to the profile-local SibPush config file
    and then refreshes the cached runtime state.

    Args:
        config (dict[str, Any]): The configuration dictionary to save.

    Returns:
        dict[str, Any]: The refreshed config_settings dictionary.
    """

    _save_profile_config(config)
    return refresh_config_state(config)


def update_custom_deck_rule(
    deck_id: str,
    deck_name: str,
    *,
    ignored: bool | None = None,
    interval: int | None = None,
    stability_threshold: float | None = None,
) -> dict[str, Any]:
    """Update one deck rule and save the resulting configuration.

    This function modifies (or creates) a custom deck rule and persists the
    configuration changes. At least one of 'ignored' or 'interval' should be provided.

    Args:
        deck_id (str): The deck identifier to update.
        deck_name (str): The human-readable deck name.
        ignored (bool | None): If provided, set the ignored state for the deck.
        interval (int | None): If provided, set the maturity interval threshold for the deck.

    Returns:
        dict[str, Any]: The updated rule dictionary.
    """

    updated_config = deepcopy(config_settings)
    rule = _prepare_custom_deck_rule(updated_config, deck_id, deck_name)

    if ignored is not None:
        rule[CONFIG_IGNORED_KEY] = ignored

    updated_threshold = stability_threshold if stability_threshold is not None else interval
    if updated_threshold is not None:
        rule["interval"] = updated_threshold
        rule["stability_threshold"] = updated_threshold

    save_config_state(updated_config)
    return rule


def _get_newly_ignored_deck_ids(
    previous_ignored_deck_ids: list[str], current_ignored_deck_ids: list[str]
) -> list[str]:
    """Return deck ids that were just switched to ignored.

    Args:
        previous_ignored_deck_ids (list[str]): The ignored deck ids before the config save.
        current_ignored_deck_ids (list[str]): The ignored deck ids after the config save.

    Returns:
        list[str]: Deck ids that are now ignored but were not ignored before.
    """

    previous_ids = set(previous_ignored_deck_ids)
    return [
        deck_id for deck_id in current_ignored_deck_ids if deck_id and deck_id not in previous_ids
    ]


def _parse_tag_rules(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Extract the normalized tag rules mapping from a config object.

    Args:
        config (dict[str, Any] | None): The raw add-on configuration.

    Returns:
        dict[str, dict[str, Any]]: The normalized tag rules keyed by tag name.
    """

    if config is None:
        return {}

    default_interval = _default_stability_threshold(config)
    raw_rules = config.get("tag_rules")
    if not isinstance(raw_rules, dict):
        return {}

    typed_rules = cast(dict[str, object], raw_rules)
    normalized_rules: dict[str, dict[str, Any]] = {}
    for raw_tag, raw_rule in typed_rules.items():
        tag = str(raw_tag).strip()
        if not tag:
            continue

        normalized_rule = _normalize_tag_rule(raw_rule, default_interval)
        if normalized_rule is None:
            continue

        normalized_rules[tag] = normalized_rule

    return normalized_rules


def parse_config(
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Parse raw configuration data into the cached runtime settings.

    Args:
        config (dict[str, Any] | None): The raw configuration dictionary.

    Returns:
        dict[str, bool | int | list[dict[str, Any]]]: The normalized runtime settings.
    """

    debug = bool(config.get("debug", False)) if config is not None else False
    default_interval = _default_stability_threshold(config)
    custom_deck_rules = _parse_custom_deck_rules(config)
    tag_rules = _parse_tag_rules(config)

    custom_deck_rules_by_did.clear()
    custom_deck_rules_by_did.update(_index_custom_deck_rules(custom_deck_rules))
    ignored_deck_ids[:] = _extract_ignored_deck_ids(custom_deck_rules)

    return {
        "debug": debug,
        "default_interval": default_interval,
        "default_stability_threshold": default_interval,
        "custom_deck_rules": custom_deck_rules,
        "tag_rules": tag_rules,
        "note_types": deepcopy(config.get("note_types", {})) if config is not None else {},
        "progression": deepcopy(config.get("progression", [])) if config is not None else [],
    }


# Initialize the add-on configuration at module load time.
# This happens when Anki loads the add-on, ensuring config is available immediately.
addon_manager: Any | None = getattr(mw, "addonManager", None) if mw else None
config: dict[str, Any] | None = None
config_settings: dict[str, Any] = parse_config(None)


def on_config_display(config_text: str) -> str:
    """Replace the editor JSON with the profile-local config when this looks like SibPush.

    The display hook is global, so we only swap the text when the JSON being shown matches
    SibPush's current add-on-manager config. That keeps other add-ons' config editors untouched.

    Args:
        config_text (str): The JSON text Anki is about to display.

    Returns:
        str: The profile-local config text when it exists, otherwise the original text.
    """

    addon_manager = _get_addon_manager()
    if addon_manager is None:
        return config_text

    try:
        displayed_config = json.loads(config_text)
    except (TypeError, json.JSONDecodeError, ValueError):
        return config_text

    current_config = addon_manager.getConfig(_addon_module_name())
    if not isinstance(current_config, dict) or displayed_config != current_config:
        return config_text

    profile_config = _load_profile_config()
    if profile_config is None:
        return config_text

    return json.dumps(profile_config, ensure_ascii=False, indent=4, sort_keys=True)


def on_config_save(config_text: str, addon: str) -> str:
    """Handle the config editor save hook and refresh cached settings.

    Args:
        config_text (str): The JSON text that Anki is about to save.
        addon (str): The add-on identifier that triggered the hook.

    Returns:
        str: The configuration text that should be written to disk.
    """

    global config_settings

    if addon != _addon_module_name():
        # If the addon name is not mine, return the text to be saved unchanged.
        return config_text

    # Parse the text argument as JSON and mirror it into the profile-local config file.
    config: dict[str, object] = json.loads(config_text)
    debug_before = config_settings["debug"]
    save_config_state(config)

    if config_settings["debug"]:
        if config_settings["debug"] != debug_before:
            # If debug is enabled and it was not enabled before, initialize the log file.
            initialize_log_file()

        logThis(
            lambda: (
                "Config updated: "
                f"debug={config_settings['debug']}, "
                f"default_stability_threshold={config_settings['default_stability_threshold']}, "
                f'custom_deck_rules={config_settings["custom_deck_rules"]}, '
                f'tag_rules={config_settings["tag_rules"]}, '
                f'note_types={config_settings["note_types"]}, '
                f'progression={config_settings["progression"]}, '
                f"ignored_deck_ids={ignored_deck_ids}"
            )
        )

    # Return the text Anki should continue to use for the config editor flow.
    return config_text
