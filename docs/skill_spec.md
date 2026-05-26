# DataElf Skill Package Specification

DataElf's user-facing capability unit is an AgentSkills-compatible skill package.
Users contribute skills, not Python backend classes.

## Directory Layout

```text
my_skill/
  SKILL.md
  references/
  scripts/
  assets/
```

Only `SKILL.md` is required for discovery.

- `references/`: optional long-form domain docs, benchmark notes, checker lists, examples, and operational guidance.
- `scripts/`: optional runnable scripts for external skills.
- `assets/`: optional static resources used by the skill.

## `SKILL.md`

Use YAML frontmatter followed by concise instructions.

```markdown
---
name: my_skill
description: Short planner-facing capability description.
allowed-tools:
  - python
  - llm
---

## Usage Instructions

When DataElf should choose this skill.

## Input Expectations

Structured inputs the execution plan should pass.

## Output Expectations

Result, artifact, metric, and trace expectations.

## Clarification Hints

Domain-specific missing information DataElf should ask about.

## Examples

Small execution-plan examples or input/output examples.
```

## Planner View

The planner sees a lightweight view by default:

- skill name
- description
- short usage summary
- allowed tools
- clarification hint summary

DataElf does not put full `SKILL.md` files or all references into the prompt by default.

## Progressive Disclosure

When `agent.include_skill_docs` is enabled, DataElf may load selected documentation:

- `skills/<name>/SKILL.md`
- `skills/<name>/references/*.md`

This is for skill-specific clarification and planning detail. It is not required for basic skill discovery.

## Runtime Contract

Skill execution returns a normalized envelope:

```json
{
  "result": {},
  "metadata": {},
  "artifacts": {},
  "metrics": {},
  "trace": {}
}
```

Built-in skills may use internal Python backends. External skills do not need DataElf-specific runtime metadata in frontmatter.

## Configuration

```yaml
skills:
  - security_audit
  - my_skill

skill_paths:
  - ./local_skills
```

## CLI

```bash
elf skills list
elf skills inspect security_audit
elf skills validate ./local_skills/my_skill
```
