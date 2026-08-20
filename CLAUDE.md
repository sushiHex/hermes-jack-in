# Claude Code project instructions

## Purpose

Hermes Jack-In is a fail-closed compiler from an authoritative Hermes Agent
skill library to disposable Claude Code skills. Hermes remains the source of
truth; destination output is managed state, not a second library.

Read `docs/CLAUDE_CODE_GUIDE.md` before operating the adapter and
`docs/MAPPING_RULES.md` before changing compatibility behavior.

## Non-negotiable boundaries

- Never edit managed destination output as a lasting change. Edit the Hermes
  source or a reviewed override, then reconcile.
- Never overwrite, remove, rename, or adopt an unmanaged destination entry.
- Never weaken collision, path, alias, ownership, identity, hash, hidden-entry,
  local-link, transaction, or rollback validation to increase coverage.
- Never invent global Hermes-to-Claude substitutions. Semantic adaptations are
  exact, per-skill, reviewed replacements and must leave no detected Hermes-only
  constructs.
- Treat exclusions as correct unless a specific Claude-native equivalent is
  proved.
- Never overlap source and destination trees.
- Never run `sync` or `remove` against a personal destination without explicit
  user intent. Prefer a project-local canary.
- Never point tests at a real `~/.claude/skills` directory.

## Operating sequence

Use the same explicit override file for scan, plan, sync, and check.

1. Run `scan --json` and establish complete source validity.
2. Run `plan --json` against the exact destination.
3. Explain installs, updates, removals, exclusions, and blockers.
4. Run `sync` only when mutation is intended.
5. Run `check`; success means zero issues.
6. Verify an interoperability canary with the actual Claude CLI.
7. Preview `remove`, then prove unrelated entries remain after rollback.

A command error is a blocker, not permission to bypass ownership checks.

## Development conventions

- Use `uv` and the frozen lockfile.
- Add a focused failing regression before changing behavior or safety policy.
- Keep source reads side-effect free and complete deterministic preflight before
  writes.
- Preserve idempotence: a second unchanged sync has no actions and check has no
  issues.
- Keep generated output deterministic and provenance-marked.
- Preserve the legacy manifest, lock, and inline-provenance identifiers unless
  a fully tested ownership migration is intentionally designed.
- Do not commit personal paths, live inventories, credentials, raw audit logs,
  or generated build output.
- Update public docs and validation evidence when a claim changes.

## Completion gate

```text
uv sync --frozen
uv run --frozen python scripts/release_gate.py
```

Behavior changes also require a read-only real scan and plan. Runtime discovery
changes require an isolated Claude Code canary; filesystem presence alone is
not interoperability evidence.
