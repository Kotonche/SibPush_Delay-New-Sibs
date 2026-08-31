"""Acceptance-test runner for Progressive Siblings."""

from __future__ import annotations

import traceback

from testing.scenarios import test_progressive_siblings as progressive_suite
from testing.scenarios import test_chunked_runner as chunked_suite
from testing.scenarios import test_state_persistence_roundtrip as state_suite
from testing.scenarios import test_recently_modified_note_ids as modified_suite
from testing.scenarios import test_combined_notes_mod_and_cards_mod_deltas as delta_suite


def _tests() -> list[tuple[str, object]]:
    tests: list[tuple[str, object]] = []
    for suite in (
        progressive_suite,
        chunked_suite,
        state_suite,
        modified_suite,
        delta_suite,
    ):
        tests.extend(
            (name, getattr(suite, name))
            for name in sorted(dir(suite))
            if name.startswith("test_") and callable(getattr(suite, name))
        )
    return tests


def main() -> None:
    tests = _tests()
    for test_name, test_func in tests:
        print(f"{test_name} ... ", end="", flush=True)
        try:
            test_func()  # type: ignore[operator]
        except Exception:
            print("FAILED")
            print(traceback.format_exc())
            raise
        print("ok")

    print(f"\nAll {len(tests)} Progressive Siblings tests passed.")


if __name__ == "__main__":
    main()
