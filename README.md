# Hermes Jack-In

**Safely share Hermes Agent skills with Claude Code.**

Hermes Jack-In is a fail-closed adapter for exposing an authoritative Hermes
Agent skill library to Claude Code. It links directly portable skills, renders
safe deterministic conversions, applies only explicit exact-match semantic
adaptations, and excludes unresolved Hermes runtime behavior.

The project is **Beta**. It does not run a daemon or modify Hermes Agent. A
mutating command writes only to its explicit destination, except that it may
create missing destination ancestors and keeps a persistent per-destination
lock file in the destination's parent; see
[Concurrent-writer boundary](#concurrent-writer-boundary). This beta remains
one-way projection only: it does not automatically capture Claude session feedback,
learn, or change source Hermes skills. The explicit `feedback-propose` command can
bind operator-supplied, untrusted feedback to one current projection and write a
review-only proposal outside both managed roots; it never applies that proposal.

Hermes Jack-In is an independent third-party project and is not affiliated with
or endorsed by Anthropic or Nous Research. “Claude,” “Claude Code,” and “Hermes
Agent” identify compatibility targets; their respective owners retain their
marks.

## Why this exists

Hermes Agent and Claude Code both consume Agent Skills, but identical Markdown
does not guarantee identical runtime behavior. Hermes skills may reference
Hermes-only tools, variables, metadata, or directory conventions. Blindly
copying an entire library can therefore expose instructions that Claude cannot
execute correctly.

Hermes Jack-In keeps Hermes authoritative and makes each decision visible:

1. **Directly portable** — expose the original directory as a live link.
2. **Deterministic conversion** — render Claude-compatible frontmatter and copy
   ordinary supporting files.
3. **Semantic adaptation** — render only after a reviewed exact-match override
   resolves every detected Hermes-only construct.
4. **Hermes-only** — exclude with machine-readable reasons.

## Requirements

- Python 3.10 or newer.
- Hermes skills stored in a categorized library containing one directory per
  skill and a `SKILL.md` in each selected skill directory.
- Claude Code personal or project skills destination.
- For mutation: Windows, macOS, or a supported Linux architecture/filesystem as
  described under [Platform boundary](#platform-boundary).

## Installation

Install current public `main`:

```bash
uv tool install git+https://github.com/sushiHex/hermes-jack-in.git
```

For a reproducible install, use the reviewed tag after its release page exists:

```bash
uv tool install git+https://github.com/sushiHex/hermes-jack-in.git@v0.2.0
```

`v0.2.0` is distributed as a source-only GitHub prerelease. The release
intentionally has no wheel or sdist assets.

For development:

```bash
git clone https://github.com/sushiHex/hermes-jack-in.git
cd hermes-jack-in
uv sync --frozen
uv run --frozen hermes-jack-in --help
```

The repository is public. No package has been published to PyPI; PyPI
installation instructions will be added only after a package is actually
published.

## Safe first run

Start with a project-local canary destination, not your personal Claude skills.
The examples below use the standard Hermes skill location; pass the actual
profile or external-library path when yours differs.

### Bash, zsh, or Git Bash

```bash
SOURCE="$HOME/.hermes/skills"
DESTINATION="$PWD/.claude/skills"

hermes-jack-in scan --source "$SOURCE" --json
hermes-jack-in plan --source "$SOURCE" --destination "$DESTINATION" --json
hermes-jack-in sync --source "$SOURCE" --destination "$DESTINATION" --dry-run --json
```

### PowerShell

```powershell
$Source = Join-Path $HOME ".hermes/skills"
$Destination = Join-Path $PWD ".claude/skills"

hermes-jack-in scan --source $Source --json
hermes-jack-in plan --source $Source --destination $Destination --json
hermes-jack-in sync --source $Source --destination $Destination --dry-run --json
```

Review every exclusion and proposed mutation. Then activate the canary:

```bash
hermes-jack-in sync --source "$SOURCE" --destination "$DESTINATION"
hermes-jack-in check --source "$SOURCE" --destination "$DESTINATION"
```

PowerShell users can run the same commands with `$Source` and `$Destination`.
Verify discovery and invocation with Claude Code before considering a personal
`~/.claude/skills` destination.

Commands:

- `scan` inventories and classifies without touching the destination.
- `plan` computes exact changes without touching the destination.
- `sync --dry-run` exercises the mutating interface without mutation.
- `sync` installs or reconciles owned artifacts.
- `check` reports source drift, destination drift, missing output, and stale
  output without mutation.
- `feedback-propose` writes one canonical, review-required proposal for a current
  owned projection; feedback remains untrusted and no managed state is changed.
- `remove` deletes only unchanged artifacts whose ownership is proved.

Add `--json` for machine-readable output. Add `--copy` to `sync` for an explicit
materialized snapshot. An empty source may be scanned and planned, but a
mutating all-empty reconciliation requires `--allow-empty`.

See the [operator guide](docs/CLAUDE_CODE_GUIDE.md),
[mapping rules](docs/MAPPING_RULES.md), and
[review-only feedback workflow](docs/FEEDBACK_PROPOSALS.md) before using a
personal destination or creating a proposal.

## Safety model

Hermes Jack-In is conservative by design:

- The source is read-only and must be an existing, completely readable
  directory. Missing, non-directory, unreadable, or incompletely scanned
  sources block operation.
- Source and destination may not overlap. Source aliases and unresolved
  destination symlinks, junctions, reparse points, or aliased ancestors are
  rejected.
- Portable lowercase destination names are derived from skill frontmatter and
  checked for collisions before any path is used.
- Existing unowned destination entries—ordinary directories, links, junctions,
  dangling aliases, and files—are never overwritten.
- Generated and copied trees use typed, length-framed hashes. Nested links or
  reparse points invalidate ownership proof and block replacement or removal.
- Replacement and removal quarantine the exact object, compare filesystem
  identity after rename, and repeat ownership proof before deletion.
- Publication uses non-overwriting native rename operations. Manifest changes
  occur only after installed identity is proved.
- Scratch, backup, quarantine, and manifest-temporary objects are cleaned only
  when their captured identity and physical type still match. Ambiguous objects
  are preserved for operator reconciliation.
- Override YAML must contain exactly one `skills` mapping, contain no duplicate
  mapping keys, name only present skills, and use non-empty exact replacement
  anchors. Stale or ambiguous adaptations abort.
- Execution-bearing frontmatter and unresolved Hermes runtime constructs are
  excluded rather than guessed.
- Local Markdown links are bounded and decoded before validation; ambiguous URL
  and embedded-document forms fail closed.
- Excluded skills never reach Claude Code.

The hidden ownership manifest is intentionally still named
`.hermes-claude-skills-adapter.json`. The corresponding lock prefix and inline
provenance identifier are legacy protocol names retained so qualified existing
destinations remain recognizable across the public product rename. They are not
alternate package or CLI names.

Legacy manifest schemas are readable, but legacy hashes never authorize
replacement or removal by themselves. Migration requires current source
selection plus exact artifact comparison; otherwise reconciliation stops.

## Live-link warning

A directory symlink or Windows junction is a live view, not a read-only copy.
Writing through it changes the authoritative Hermes skill. Use `--copy` when a
mutable live view is unacceptable. Materialized output is still adapter-owned
and must not be edited in place.

For optional defense in depth, install and configure the packaged
`hermes-jack-in-guard` as described in the
[operator guide](docs/CLAUDE_CODE_GUIDE.md#4-protect-live-links). Every protected
root must be supplied explicitly as an existing absolute physical directory.
The guard is a bounded Claude Code hook, not access control: runtime-computed
paths, another same-user process, or a compromised hook configuration remain
outside its guarantee.

Claude Code's Bash sandbox is unsupported on native Windows; use WSL2 when its
OS-enforced Bash sandbox is required. Claude permission rules and hooks are
Claude Code controls, not protection against external same-user processes.

## Concurrent-writer boundary

Cooperating `sync` and `remove` operations serialize each destination mutation
attempt with an advisory OS lock beginning before manifest load. Artifact and
manifest publication are separate atomic renames. Each artifact or manifest
commit has identity-checked rollback for its own recoverable failure; a later
failure may leave earlier completed changes committed. Retry the command to
converge the destination from its updated manifest.

This does not protect against a malicious process running as the same user.
POSIX path checks do not retain directory authority between checks; a same-user
process can race an ancestor name or the destination. Use POSIX mutating commands
only beneath parents that are not writable by untrusted accounts, and do not run
them concurrently with hostile same-user writers. Hard termination, power loss,
an occupied restore name, or mutation of a quarantine can leave an ambiguous
scratch path. Hermes Jack-In preserves that state and refuses to infer ownership.

## Platform boundary

Inventory, classification, planning, and checking use portable Python and
filesystem inspection. Mutating `sync` and `remove` are implemented for:

- Windows, using native no-replace and reparse-point behavior;
- macOS, when `renamex_np(..., RENAME_EXCL)` is available; and
- Linux, using `renameat2(..., RENAME_NOREPLACE)` when the kernel and filesystem
  support it. The implementation includes syscall dispatch for `x86_64`, `i686`,
  `aarch64`, `ppc64le`, and `s390x`; only the hosted runner architecture is
  native-qualified by the release CI matrix.

Unsupported POSIX platforms and unavailable native primitives fail closed with
no overwrite fallback. Windows behavior has been exercised on a native host.
Linux and macOS mutation support must pass native GitHub Actions jobs before a
release is tagged; see [validation](docs/VALIDATION.md).

## Rollback

Run `remove` against the same destination. It removes only unchanged artifacts
recorded in the ownership manifest, then removes that manifest. Modified,
retargeted, unproved, or unowned entries block removal and remain on disk.

## Development

```bash
uv sync --frozen
uv run --frozen python scripts/release_gate.py
```

The suite uses temporary source and destination trees. Tests must never point at
a real personal Claude skills directory. See [Contributing](CONTRIBUTING.md),
[Security](SECURITY.md), and the [Changelog](CHANGELOG.md).

## Authoritative references

Product and specification claims below were checked on 2026-08-08.

- [Claude Code: Extend Claude with skills](https://code.claude.com/docs/en/skills)
- [Claude Code: Plugins reference](https://code.claude.com/docs/en/plugins-reference)
- [Claude Code: Hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code: Permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code: Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Agent Skills specification](https://agentskills.io/specification)
- [Hermes Agent: Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)
- [Hermes Agent: Creating Skills](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills)

## License

MIT. See [LICENSE](LICENSE).
