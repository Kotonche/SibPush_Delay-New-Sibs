# Progressive Siblings for Anki

Progressive Siblings is an Anki Desktop add-on that unlocks sibling card templates in explicit
`card.ord` order. A later stage becomes available only after the immediately preceding existing
stage reaches the configured **FSRS Stability** threshold.

The add-on controls availability only. It never changes Stability, Difficulty, desired retention,
review history, interval, or due date. FSRS remains fully responsible for scheduling every stage
after it has been unlocked.

## Default workflow

The bundled configuration supports the versioned `Greek Vocabulary` note type created by
`anki-cards-ai-generator`:

1. `01 · Recognition Boost · Multiple Choice`
2. `02 · Recognition · Comprehension`
3. `03 · Context Recall · Usage`
4. `04 · Active Recall · Production`

On a new note, only ordinal 0 remains active. Later New cards are suspended with add-on-owned
provenance. When a preceding card reaches Stability 7 days, the next existing ordinal is unlocked.
An unlock triggered directly by a Desktop review is buried until the next Anki day.

Progression is one-way. Once a stage has been reached, a later drop in Stability does not lock it
again. A missing optional card is skipped without error.

## Safety

Progressive Siblings stores independent short markers in Anki card custom data:

- `prgsusp`: this add-on owns the current suspension;
- `prgunlk`: the stage has legitimately been unlocked;
- `prgign`: the card is excluded from progression management.

Only a card carrying `prgsusp` can be automatically unsuspended. A card suspended manually by the
user is never restored by progression or recovery.

Use **Progressive Siblings → Restore all managed cards…** in a deck menu before uninstalling if
you want an explicit recovery pass. Add-on deletion runs the same provenance-aware restoration.

## Configuration

The most important settings are:

```json
{
  "default_stability_threshold": 7,
  "note_types": {
    "Greek Vocabulary*": {
      "enabled": true,
      "stages": [
        {"ord": 0, "name": "01 · Recognition Boost · Multiple Choice"},
        {"ord": 1, "name": "02 · Recognition · Comprehension"},
        {"ord": 2, "name": "03 · Context Recall · Usage"},
        {"ord": 3, "name": "04 · Active Recall · Production"}
      ]
    }
  },
  "progression": [],
  "custom_deck_rules": [],
  "tag_rules": {},
  "debug": false
}
```

`note_types` is an allowlist and supports shell-style name patterns. Template names are validated
against their configured ordinal; a mismatch leaves the note untouched and is written to the log
when debug mode is enabled.

Per-transition thresholds are optional:

```json
{
  "progression": [
    {"from": "Card 1", "to": "Card 2", "stability": 5},
    {"from": "Card 2", "to": "Card 3", "stability": 7}
  ]
}
```

Threshold precedence is: matching tag rule, deck-ID rule, named transition, global default. See
[config.md](config.md) for the complete schema.

## Desktop, mobile, and sync

Desktop reviews process only the affected note. After an AnkiMobile, AnkiDroid, or AnkiWeb review,
the Desktop sync hook records a watermark and the next deck-browser render reconciles changed
notes. Low-frequency reconciliation is chunked so large collections yield back to the Qt event
loop between batches.

## Migrating from SibPush

Disable SibPush before enabling Progressive Siblings so the two add-ons do not compete for card
state. On first startup, Progressive Siblings can transfer:

- `sibpsusp` and `sibpign` card markers;
- the older `SibPush-suspended` tag representation;
- deck, tag, ignore, and threshold rules from `sibpush_config.json`.

The original SibPush config file is not modified. After migration, managed notes are reconciled
from their current FSRS memory states without resetting scheduling history.

## Development

The acceptance suite uses a real temporary Anki collection and covers the specification's A–H
scenarios plus template validation, recovery, migration, and missing-memory-state behavior:

```bash
python run_tests.py
```

The current implementation is tested against Anki 26.8.1.

## Origin and license

Progressive Siblings is based on
[DerDemystifier/SibPush_Delay-New-Sibs](https://github.com/DerDemystifier/SibPush_Delay-New-Sibs)
and preserves its BSD 3-Clause license and attribution.
