# Internal Backend Notes

This file is intentionally internal-facing.

DataElf's user-facing capability unit is a skill package. See [skill_spec.md](skill_spec.md).

Some built-in skills still call internal Python backends so existing domain logic can remain focused and testable. Those backends are implementation details behind `invoke_skill`; they are not the public contribution interface.

## Runtime Envelope

Internal backends are normalized into the skill envelope:

```json
{
  "result": {},
  "metadata": {},
  "artifacts": {},
  "metrics": {},
  "trace": {}
}
```

## Trace Levels

- Level 1: DataElf plan trace
- Level 2: skill runtime trace
- Level 3: skill internal trace
