"""
Utilities for dynamically loading the addon and patching its global state for testing.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from anki.collection import Collection
    from anki.notes import NoteId


# Default interval used for testing immature sibling logic
TEST_INTERVAL = 30


class AddonModule(Protocol):
    """Type protocol representing the test-facing addon facade."""

    mw: Any
    last_full_scan_date: str | None
    last_unmanaged_note_ids: Sequence["NoteId"] | None
    last_processed_mod_ts: int | None
    last_sync_mod_ts: int | None
    pending_browser_work: dict[str, object]
    config_settings: dict[str, object]
    custom_deck_rules_by_did: dict[str, dict[str, object]]
    ignored_deck_ids: list[str]

    def process_all_notes(self, col: "Collection") -> None: ...

    def process_new_unmanaged_notes(self, col: "Collection") -> None: ...

    def process_note(self, col: "Collection", note_id: int, coming_from_reviewer_hook: bool = False) -> None: ...

    def get_state_file_path(self, col: "Collection" | None = None) -> Any: ...

    def get_config_file_path(self, col: "Collection" | None = None) -> Any: ...

    def load_persistent_state(self, col: "Collection" | None = None) -> dict[str, int | None]: ...

    def save_persistent_state(self, col: "Collection" | None = None) -> dict[str, int | None]: ...

    def reset_persistent_state(self, col: "Collection" | None = None) -> dict[str, int | None]: ...

    def get_last_processed_mod_ts(self) -> int: ...

    def sync_last_processed_mod_ts(self, value: int | None) -> None: ...

    def get_last_sync_mod_ts(self) -> int | None: ...

    def sync_last_sync_mod_ts(self, value: int | None) -> None: ...

    def get_pending_browser_work(self) -> dict[str, Any]: ...

    def sync_pending_browser_work(self, value: dict[str, Any] | None) -> None: ...

    def queue_pending_browser_work(
        self,
        *,
        deck_ids: Sequence[str] | None = None,
        reset_processing_state: bool = False,
        refresh_unmanaged_notes: bool = False,
    ) -> dict[str, Any]: ...

    def discard_pending_unsuspend_deck_id(self, deck_id: str) -> dict[str, Any]: ...

    def consume_pending_browser_work(self) -> dict[str, Any]: ...

    def clear_pending_browser_work(self) -> dict[str, Any]: ...

    ADDON_CUSTOM_DATA_KEY: str
    ADDON_CUSTOM_DATA_IGNORED_VALUE: str
    SIBPUSH_IGNORED_KEY: str
    SIBPUSH_SUSPENDED_KEY: str
    SIBPUSH_MARKER_VALUE: bool
    PROGRESSIVE_UNLOCKED_KEY: str
    CONFIG_IGNORED_KEY: str


class FakeAddonManager:
    """Minimal add-on manager stub for config-save tests."""

    def __init__(self, config: dict[str, object] | None = None) -> None:
        self._config = deepcopy(config or {})
        self.writes: list[dict[str, object]] = []

    def getConfig(self, _addon_name: str) -> dict[str, object]:
        return deepcopy(self._config)

    def writeConfig(self, *args: object) -> None:
        if len(args) == 2:
            _, config = args
        elif len(args) == 1:
            (config,) = args
        else:
            raise TypeError("writeConfig expects one or two arguments")

        if not isinstance(config, dict):
            raise TypeError("config must be a dictionary")

        self._config = deepcopy(config)
        self.writes.append(deepcopy(config))

    def setConfig(self, *args: object) -> None:
        self.writeConfig(*args)

    @property
    def config(self) -> dict[str, object]:
        return deepcopy(self._config)


def load_addon_module() -> Any:
    """Load the addon package from the repository root for test execution.

    Returns:
        Any: A small facade that exposes the add-on's shared constants.
    """
    module_name = "sibpush_test_addon"

    for cached_name in [name for name in sys.modules if name == module_name or name.startswith(f"{module_name}.")]:
        sys.modules.pop(cached_name, None)

    module_path = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name,
        module_path,
        submodule_search_locations=[str(module_path.parent)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load addon module from {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    state_module = importlib.import_module(f"{module_name}.sibpush.state")
    return SimpleNamespace(
        __name__=module_name,
        __package__=module_name,
        ADDON_CUSTOM_DATA_KEY=state_module.ADDON_CUSTOM_DATA_KEY,
        ADDON_CUSTOM_DATA_IGNORED_VALUE=state_module.ADDON_CUSTOM_DATA_IGNORED_VALUE,
        SIBPUSH_IGNORED_KEY=state_module.SIBPUSH_IGNORED_KEY,
        SIBPUSH_SUSPENDED_KEY=state_module.SIBPUSH_SUSPENDED_KEY,
        SIBPUSH_MARKER_VALUE=state_module.SIBPUSH_MARKER_VALUE,
        PROGRESSIVE_UNLOCKED_KEY=state_module.PROGRESSIVE_UNLOCKED_KEY,
        CONFIG_IGNORED_KEY=state_module.CONFIG_IGNORED_KEY,
    )


def _load_test_modules() -> tuple[Any, Any, Any]:
    """Load the addon submodules that the test harness patches.

    Returns:
        tuple[Any, Any, Any]: The shared state, config parser, and note-processing modules.
    """

    module_name = "sibpush_test_addon"
    state_module = importlib.import_module(f"{module_name}.sibpush.state")
    parser_module = importlib.import_module(f"{module_name}.sibpush.config.parser")
    notes_module = importlib.import_module(f"{module_name}.sibpush.processing.notes")
    return state_module, parser_module, notes_module


@contextmanager
def patched_addon_state(
    col: "Collection", addon_manager: FakeAddonManager | None = None
) -> Generator[AddonModule, None, None]:
    """
    Patch the addon state so processing helpers can run against a test collection.

    This context manager:
    1. Swaps the shared `mw` handle for the provided test collection.
    2. Resets internal state caches (`last_full_scan_date` and `last_unmanaged_note_ids`).
    3. Configures test-specific settings (`default_interval=30`, no custom deck rules).
    4. Restores original state on exit.
    """
    addon = load_addon_module()
    state_module, parser_module, notes_module = _load_test_modules()
    from . import card_utils as test_card_utils
    addon.mw = state_module.mw
    addon.last_full_scan_date = state_module.last_full_scan_date
    addon.last_unmanaged_note_ids = state_module.last_unmanaged_note_ids
    addon.last_processed_mod_ts = state_module.last_processed_mod_ts
    addon.last_sync_mod_ts = state_module.last_sync_mod_ts
    addon.pending_browser_work = state_module.get_pending_browser_work()
    addon.config_settings = parser_module.config_settings
    addon.custom_deck_rules_by_did = parser_module.custom_deck_rules_by_did
    addon.ignored_deck_ids = parser_module.ignored_deck_ids
    addon.process_all_notes = notes_module.process_all_notes
    addon.process_new_unmanaged_notes = notes_module.process_new_unmanaged_notes
    addon.process_note = notes_module.process_note
    addon.get_state_file_path = state_module.get_state_file_path
    addon.get_config_file_path = state_module.get_config_file_path
    addon.load_persistent_state = state_module.load_persistent_state
    addon.save_persistent_state = state_module.save_persistent_state
    addon.reset_persistent_state = state_module.reset_persistent_state
    addon.get_last_processed_mod_ts = state_module.get_last_processed_mod_ts
    addon.sync_last_processed_mod_ts = state_module.sync_last_processed_mod_ts
    addon.get_last_sync_mod_ts = state_module.get_last_sync_mod_ts
    addon.sync_last_sync_mod_ts = state_module.sync_last_sync_mod_ts
    addon.get_pending_browser_work = state_module.get_pending_browser_work
    addon.sync_pending_browser_work = state_module.sync_pending_browser_work
    addon.queue_pending_browser_work = state_module.queue_pending_browser_work
    addon.discard_pending_unsuspend_deck_id = state_module.discard_pending_unsuspend_deck_id
    addon.consume_pending_browser_work = state_module.consume_pending_browser_work
    addon.clear_pending_browser_work = state_module.clear_pending_browser_work
    test_card_utils.set_addon_constants(addon)

    original_mw = state_module.mw
    original_last_full_scan_date = state_module.last_full_scan_date
    original_last_unmanaged_note_ids = state_module.last_unmanaged_note_ids
    original_last_processed_mod_ts = state_module.last_processed_mod_ts
    original_last_sync_mod_ts = state_module.last_sync_mod_ts
    original_pending_browser_work = state_module.get_pending_browser_work()
    original_config = deepcopy(parser_module.config_settings)
    original_ignored_deck_ids = list(parser_module.ignored_deck_ids)
    original_custom_deck_rules_by_did = deepcopy(parser_module.custom_deck_rules_by_did)

    # Mock the main window to provide access to our test collection.
    state_module.mw = SimpleNamespace(col=col, addonManager=addon_manager)
    state_module.last_full_scan_date = None
    state_module.last_unmanaged_note_ids = None
    state_module.last_processed_mod_ts = None
    state_module.last_sync_mod_ts = None
    state_module.clear_pending_browser_work()
    addon.mw = state_module.mw
    addon.last_full_scan_date = None
    addon.last_unmanaged_note_ids = None
    addon.last_processed_mod_ts = None
    addon.last_sync_mod_ts = None
    addon.pending_browser_work = state_module.get_pending_browser_work()

    # Configure test environment.
    parser_module.config_settings.clear()
    parser_module.config_settings.update(deepcopy(original_config))
    parser_module.config_settings["default_interval"] = TEST_INTERVAL
    parser_module.config_settings["default_stability_threshold"] = 7.0
    parser_module.config_settings["custom_deck_rules"] = []
    parser_module.config_settings["tag_rules"] = {}
    parser_module.config_settings["progression"] = []
    parser_module.config_settings["note_types"] = {
        "SibPush Test Note": {
            "enabled": True,
            "stages": [
                {"ord": index, "name": f"Card {index + 1}"}
                for index in range(4)
            ],
        }
    }
    parser_module.ignored_deck_ids[:] = []
    parser_module.custom_deck_rules_by_did.clear()

    try:
        yield addon
    finally:
        # Restore state to prevent leakage between tests.
        state_module.mw = original_mw
        state_module.last_full_scan_date = original_last_full_scan_date
        state_module.last_unmanaged_note_ids = original_last_unmanaged_note_ids
        state_module.last_processed_mod_ts = original_last_processed_mod_ts
        state_module.last_sync_mod_ts = original_last_sync_mod_ts
        state_module.sync_pending_browser_work(original_pending_browser_work)
        addon.mw = original_mw
        addon.last_full_scan_date = original_last_full_scan_date
        addon.last_unmanaged_note_ids = original_last_unmanaged_note_ids
        addon.last_processed_mod_ts = original_last_processed_mod_ts
        addon.last_sync_mod_ts = original_last_sync_mod_ts
        addon.pending_browser_work = original_pending_browser_work
        parser_module.config_settings.clear()
        parser_module.config_settings.update(deepcopy(original_config))
        parser_module.ignored_deck_ids[:] = original_ignored_deck_ids
        parser_module.custom_deck_rules_by_did.clear()
        parser_module.custom_deck_rules_by_did.update(deepcopy(original_custom_deck_rules_by_did))
