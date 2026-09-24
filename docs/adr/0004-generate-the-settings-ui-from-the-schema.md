# 4. Generate the settings panel from the configuration schema

**Status:** accepted

## Context

The spec lists eighteen categories of setting, and the project is meant to grow.
The usual approach — a pydantic model on the server and hand-written form
controls on the client — means every new setting is edited in two places.

The two drift. In practice a setting gets added to the backend and never
surfaces in the UI, or a control edits a key that no longer exists. Neither
failure produces an error; the setting simply does not work.

## Decision

The pydantic schema is the single declaration. Field metadata (type, bounds,
description, whether it is a secret) is walked at runtime to produce a UI
descriptor, which the shell renders generically.

```python
core_scale: float = Field(default=1.0, ge=0.5, le=1.6)
```

becomes a slider from 0.5 to 1.6 with no client-side code. `Literal` becomes a
select, `bool` a toggle, a nested model a group, a list of models a repeatable
group.

## Consequences

**Good.** One place to add a setting. Drift is impossible by construction, not
by discipline. The panel stays consistent because every control of a given type
is the same component.

**Bad.** Bespoke controls need an escape hatch — the descriptor carries a
`control` field that can be specialised, and the Status tab is hand-written
because it displays state rather than editing settings. The generic layout is
also slightly less considered than a hand-tuned one would be; that is the trade.

## Secrets

Secret fields are marked in the schema and handled by the same machinery:

- `public_dict()` replaces stored secrets with `••••••••` before anything leaves
  the process, walking nested lists as well as objects.
- `patch()` drops any value equal to that sentinel.

So the panel can round-trip the entire document — editing a temperature slider
without touching the API key field — and cannot accidentally erase a credential
it was never shown. Tested in `test_config.py`.
