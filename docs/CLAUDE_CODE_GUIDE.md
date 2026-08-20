# Claude Code operator guide

This guide describes how to operate Hermes Jack-In safely. Treat the tool as a
**fail-closed compiler with owned outputs**:

```text
authoritative Hermes skills (read-only)
  -> complete scan and validation
  -> classify: direct / convert / adapt / exclude
  -> exact no-write plan
  -> owned Claude skills plus ownership manifest
  -> drift check and runtime verification
```

Exclusion is preferable to a plausible but incorrect translation. Hermes
Jack-In prepares skill directories; it does not control Claude Code's skill
selection, permissions, or tool behavior.

## Choose a destination

| Goal | Destination | Guidance |
|---|---|---|
| Safe experiment | `<project>/.claude/skills` | Required first run; isolated and removable |
| One repository | `<repo>/.claude/skills` | Project scope; commit only deliberately |
| All local projects | `~/.claude/skills` | Personal scope; mutate only with explicit intent |
| Distribution | Not this backend | Consider an immutable, namespaced Claude plugin |

Never overlap source and destination. Hermes Jack-In rejects a destination
inside the source, a source inside the destination, and aliased/reparse paths.

## Prepare the session

Use one explicit override file for every command. Omitting it changes the
desired set and can turn an adapted skill into an exclusion and stale removal.
The repository provides `overrides.example.yaml`; copy it and review every rule
before applying it to another library.

When a shell inherits an unrelated Python environment, clear that environment
according to its documentation. A Hermes-launched Git Bash session may need
`env -u PYTHONPATH`; ordinary installed use does not.

## Operating procedure

### 1. Scan the source

```bash
hermes-jack-in scan --source "$HOME/.hermes/skills" --overrides overrides.example.yaml --json
```

A successful scan proves that the source is an existing directory, traversal
completed, and each discovered skill has a classification and reasons. A
missing, non-directory, unreadable, partially traversed, or structurally
malformed source blocks planning and mutation. A deliberately existing empty
directory may be scanned and planned, but ordinary `sync` cannot use it to
empty a nonempty managed destination.

Interpret classifications as follows:

- `directly-portable`: byte-compatible with the conservative shared contract;
  prefers a directory symlink.
- `metadata-path-conversion`: behavior appears portable, but safe metadata,
  paths, or hidden-entry filtering requires materialization.
- `semantic-adaptation`: reviewed exact replacements resolve every detected
  Hermes-only construct.
- `hermes-only`: unresolved runtime behavior remains; no output is created.

Malformed override YAML, duplicate mapping keys, empty anchors, stale
replacements, and override names absent from the scan all abort.

### 2. Plan the exact destination

```bash
hermes-jack-in plan \
  --source "$HOME/.hermes/skills" \
  --destination "$PWD/.claude/skills" \
  --overrides overrides.example.yaml --json
```

`plan` is no-write preflight. Review:

- `install`: new owned skill;
- `update`: changed source, rendering identity, or installation mode;
- `remove-stale`: an owned skill is no longer selected; and
- `excluded`: a separate JSON collection, never an action.

An empty action list means the destination matches the desired state. A blocker
means stop and diagnose—not delete files until the plan becomes green.

### 3. Exercise dry-run

```bash
hermes-jack-in sync \
  --source "$HOME/.hermes/skills" \
  --destination "$PWD/.claude/skills" \
  --overrides overrides.example.yaml --dry-run --json
```

Dry-run follows planning and ownership preflight without creating the
destination or manifest.

### 4. Protect live links

A directory symlink or Windows junction is writable through the destination.
Before personal activation, configure Claude Code permissions for the actual
source root and register the installed guard as an optional `PreToolUse` hook.
The guard has no inferred or environment-controlled roots.

Example hook command:

```text
hermes-jack-in-guard --protected-root /absolute/hermes/skills --protected-root /absolute/claude/skills
```

Place that command in a Claude Code `PreToolUse` command hook with matcher
`Bash`. Each `--protected-root` must name a unique, existing, absolute physical
directory whose path and ancestors contain no symlink or Windows reparse alias.
Invalid configuration denies Bash. Test harmless allowed and denied commands
before relying on the hook.

The guard performs bounded lexical and path analysis without enumerating the
filesystem. It covers native/MSYS spellings, relative and ancestor paths,
line continuations, ANSI-C quotes, braces, ordinary globs, globstar traversal,
ASCII POSIX bracket classes, and bounded extglobs. Locale-translation quote
markers and analysis-bound overflows fail closed.

The following synthetic commands are the complete literal read-only exemptions
locked by regression tests. They demonstrate accepted shapes; replace paths in
your actual configuration, then test them. Each command is one physical line.

<!-- guard-exemptions:start -->
```bash
ls -la C:/Users/example/repos/hermes-profile/.hermes/skills
pwd -P
git status --short
env -u PYTHONPATH uv run hermes-jack-in scan --source C:/Users/example/repos/hermes-profile/.hermes/skills --overrides C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json
env -u PYTHONPATH uv run hermes-jack-in plan --source C:/Users/example/repos/hermes-profile/.hermes/skills --destination C:/Users/example/repos/hermes-jack-in/.canary/project/.claude/skills --overrides C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json
env -u PYTHONPATH uv run hermes-jack-in check --source C:/Users/example/repos/hermes-profile/.hermes/skills --destination C:/Users/example/repos/hermes-jack-in/.canary/project/.claude/skills --overrides C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json
```
<!-- guard-exemptions:end -->

`sync` and `remove` are intentionally not exempt because they mutate deployment
state. The guard does not evaluate arbitrary subprocess semantics or protect
against another same-user process, a compromised hook script, or modified
Claude settings. Claude Code's Bash sandbox is unsupported on native Windows;
use WSL2 if OS-enforced Bash sandboxing is required. Hooks and permission rules
remain defense in depth, not operating-system access control.

### 5. Activate

```bash
hermes-jack-in sync \
  --source "$HOME/.hermes/skills" \
  --destination "$PWD/.claude/skills" \
  --overrides overrides.example.yaml --json
```

Direct skills try directory symlinks. If Windows denies symlink creation with
`WinError 1314`, Hermes Jack-In creates an owned NTFS junction. Use `--copy`
only when an explicit snapshot is preferred. Copy mode is not sticky: a later
default sync may migrate an unchanged owned copy back to a live link.

An intentionally empty source requires `--allow-empty` before mutating sync can
remove every managed skill. Without it, the destination and manifest remain
unchanged. `remove` is the other explicit whole-destination removal operation.

### 6. Check ownership and drift

```bash
hermes-jack-in check \
  --source "$HOME/.hermes/skills" \
  --destination "$PWD/.claude/skills" \
  --overrides overrides.example.yaml --json
```

Success is `{"issues": []}`.

| Issue | Meaning | Correct response |
|---|---|---|
| `source-changed` | Source differs from its checkpoint | Review a fresh plan, then sync |
| `output-modified` | Owned output was edited or replaced | Preserve and reconcile; never overwrite blindly |
| `missing-output` | Selected owned output is absent | Review plan, then recreate with sync |
| `stale-output` | Owned output is no longer selected | Review plan, then remove through sync |
| `unmanaged-collision` | Desired name is unowned | Rename or reconcile explicitly |

### 7. Verify through Claude Code

Filesystem presence is insufficient. In the isolated project, verify with the
actual Claude CLI that:

1. expected direct, converted, and adapted skills are listed;
2. direct invocation loads the expected instructions;
3. supporting references/templates/scripts remain readable;
4. automatic selection works for an appropriate natural prompt;
5. excluded skills are absent; and
6. a second sync is empty and `check` has no issues.

Do not claim runtime compatibility from unit tests or directory listings alone.

### 8. Roll back

Preview first:

```bash
hermes-jack-in remove --destination "$PWD/.claude/skills" --dry-run --json
```

Then remove only unchanged, proved-owned artifacts:

```bash
hermes-jack-in remove --destination "$PWD/.claude/skills" --json
```

The hidden manifest keeps the legacy name
`.hermes-claude-skills-adapter.json` so existing qualified destinations remain
recognizable after the public rename. Presence of that file is never sufficient
permission to delete: physical mode, hashes, identity, and canonical targets
must still prove ownership.

## Common requests

### Make Hermes skills available in Claude Code

Default to a project-local canary, scan, plan, summarize exclusions and exact
actions, then sync/check and verify with Claude Code.

### Refresh after a Hermes edit

Run scan, plan, sync, and check with the same override file. A live link may
show new bytes immediately, but sync must checkpoint the source identity.

### Make an excluded skill portable

Identify every Hermes-only tool, service, path, and lifecycle assumption. Add a
per-skill exact override only when Claude Code has a genuine equivalent. Add a
regression and verify in a canary. Otherwise leave it excluded.

### Resolve a modified generated skill

The refusal protects user data. Preserve or diff the edit, move lasting intent
to the Hermes source or reviewed override, restore/reconcile the owned artifact,
and plan again. Never weaken the hash check.

## Exit behavior

- `0`: completed; `scan` and `check` reported no issues.
- `1`: `scan` found source issues or `check` found drift/issues.
- `2`: invalid input, unsafe state, ownership conflict, malformed data, or I/O
  failure.

A nonzero exit is not permission to retry with weaker validation.

## References

Product and specification claims below were checked on 2026-08-08.

- [Mapping rules](MAPPING_RULES.md)
- [Validation evidence](VALIDATION.md)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Agent Skills specification](https://agentskills.io/specification)
- [Hermes Agent skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)
