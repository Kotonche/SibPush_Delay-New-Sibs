# Progressive Siblings configuration

## `default_stability_threshold`

Global fallback threshold in Stability days. Default: `7`. A card with no FSRS memory state is
always immature, regardless of its interval.

## `note_types`

Explicit allowlist of managed note types. Keys may be exact names or shell-style patterns such as
`Greek Vocabulary*` for versioned models.

Each enabled rule may contain a `stages` array. `ord` is authoritative; `name` validates that a
template was not accidentally reordered or renamed. Only cards that actually exist are processed,
so optional missing templates are valid.

```json
{
  "note_types": {
    "Greek Vocabulary*": {
      "enabled": true,
      "stages": [
        {"ord": 0, "name": "01 · Recognition Boost · Multiple Choice"},
        {"ord": 1, "name": "02 · Recognition · Comprehension"}
      ]
    }
  }
}
```

## `progression`

Optional named per-transition thresholds. Names must match the validated template names.

```json
{
  "progression": [
    {"from": "Stage 1", "to": "Stage 2", "stability": 5},
    {"from": "Stage 2", "to": "Stage 3", "stability": 10}
  ]
}
```

## `custom_deck_rules`

Deck-specific rules use `did` as the authoritative identifier. `name` is informational.

```json
{
  "custom_deck_rules": [
    {
      "did": "123456789",
      "name": "Greek Vocabulary",
      "ignored": false,
      "stability_threshold": 7
    }
  ]
}
```

Ignoring a deck queues provenance-aware restoration and excludes it from later processing.

## `tag_rules`

The first matching configured tag wins and overrides deck, transition, and global thresholds.

```json
{
  "tag_rules": {
    "progression::fast": {"stability_threshold": 3},
    "progression::normal": {"stability_threshold": 7},
    "progression::slow": {"stability_threshold": 14}
  }
}
```

## `debug`

When `true`, progression decisions are written to `log.txt` in the add-on directory. The log
includes note ID, stage names and ordinals, Stability, threshold, maturity result, and action.

## Precedence

For each transition:

1. first matching tag rule;
2. source card's deck-ID rule;
3. matching entry in `progression`;
4. `default_stability_threshold`.

## Compatibility aliases

During migration, legacy `default_interval` and rule-level `interval` values are accepted as
Stability thresholds. New configuration should use the Stability names shown above.
