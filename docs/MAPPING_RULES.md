# Mapping rules

## Discovery and naming

- Require an existing directory and a complete, error-free traversal before the inventory can drive planning or reconciliation. A deliberately existing empty directory is valid for scan and plan reporting; mutating sync cannot empty a nonempty managed destination without explicit `--allow-empty` intent. A missing, file, unreadable, or partially traversed root is a blocking scan issue and never authorizes deletion.
- Recursively discover non-hidden directories containing `SKILL.md`.
- Ignore hidden trees such as `.hub` and `.curator_backups`.
- Reject a root-level `SKILL.md`; the Hermes source contract is a categorized library.
- Require parseable YAML mappings; the adapter's conservative lowercase/hyphen destination `name` subset (1–64 characters, no reserved Windows names, trailing hyphen, or consecutive hyphens); and non-empty `description` of at most 1024 characters. This validates the emitted destination name, not full conformance of the source directory to the Agent Skills specification; Hermes source directory basenames may differ from frontmatter names.
- Flatten categories to `<destination>/<name>/SKILL.md`, making the emitted destination directory match frontmatter `name`, because personal/project Claude commands derive from that destination directory name.
- Report duplicate flattened names as collisions; never choose one.
- Report a leaf content directory with no `SKILL.md` as malformed input.

## Classifications

### 1. `directly-portable`

The frontmatter uses Hermes Jack-In's conservative cross-agent subset (`name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools`), `metadata` is a flat string-to-string map, and neither the body nor interpreted operational frontmatter contains detected Hermes tool/runtime semantics. Metadata is treated as opaque provenance rather than executable instruction text; `description`, `compatibility`, and tool declarations remain subject to semantic scanning. These are Agent Skills portability fields, not an exhaustive list of Claude Code's native-only extensions; Claude may retain but ignore fields such as `compatibility`. Non-executable Claude-only fields require conversion. Execution-bearing fields are excluded rather than approved for live exposure. The preferred artifact is a directory symlink to the authoritative Hermes skill. If Windows returns `WinError 1314`, Hermes Jack-In creates an owned NTFS directory junction instead. Copying requires explicit `--copy`.

### 2. `metadata-path-conversion`

The behavior appears portable, but deterministic conversion is required. The converter:

- retains non-executable Claude fields: `name`, `description`, `license`, `compatibility`, `metadata`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, and `paths`;
- removes Hermes-only/unknown frontmatter rather than presenting it to Claude as meaningful;
- normalizes backslashes only inside inline Markdown link destinations (`](...)`) and the Claude `paths` field to `/` (reference definitions, HTML attributes, regexes, shell continuations, and prose remain byte-stable);
- copies non-hidden supporting files and relative directory structure;
- inserts source path and SHA-256 provenance in an HTML ownership marker.

### 3. `semantic-adaptation`

A skill containing Hermes semantics is eligible only when `overrides.example.yaml` explicitly marks it and supplies exact text replacements. The file must contain exactly one top-level `skills` mapping, must not repeat a mapping key at any depth, and every named skill must be present in the scanned library. Every `from` string must be non-empty and present in the source body. After replacement, the normal incompatibility detector runs again; any remaining construct aborts the scan. Overrides replace body text only and cannot approve execution-bearing frontmatter. This deliberately avoids global tool-name substitution.

The shipped override example is intentionally empty. Public releases do not encode assumptions about any private skill catalog.

### 4. `hermes-only`

Excluded when unresolved body text or Claude-interpreted frontmatter refers to Hermes tools or runtime concepts, including backticked tool names, call expressions, verb-gated prose, `allowed-tools`, any `.hermes` path, `HERMES_HOME`, `HERMES_SKILL_DIR`, `HERMES_SESSION_ID`, Hermes Agent/gateway services, curator behavior, or skill-management operations. Frontmatter that can initiate or redirect execution (`hooks`, `shell`, `agent`, `background`, or forked `context`) is also excluded. Exclusions are reported, not copied as inert warnings; Claude never receives misleading executable instructions.

## Tool semantics

Detected Hermes tool names include `execute_code`, `terminal`, `read_file`, `write_file`, `patch`, `search_files`, `delegate_task`, `clarify`, `cronjob`, `skill_view`, `skill_manage`, `memory`, `session_search`, web tools, and browser tools.

There is intentionally no global replacement table. Some conceptual equivalents exist (`terminal` → `Bash`, `read_file` → `Read`, `patch` → `Edit`, `delegate_task` → subagents), but equivalence depends on prose and side-effect boundaries. Those conversions require reviewed per-skill overrides.

## Ownership and drift

The destination manifest records source path/hash, classification, activation mode, a versioned desired-output identity, a typed length-framed tree hash, and the canonical target for live links. Source hashes likewise use versioned, typed, length-framed path and file-content records so record boundaries cannot collide. Schema v3 names that modern contract and distinguishes `symlink`, `junction`, `materialized`, and `copy-fallback`. Genuine v1/v2 manifests have no desired-output identity and use the legacy unframed hashes; v1 cannot claim junctions. Hermes Jack-In separately recognizes the short-lived modern layout mislabeled as v2 only when all entries have the modern identity. Mixed v2 layouts fail closed. The complete manifest schema, hashes, relative source paths, canonical targets, and physical link types are validated before ownership operations.

Legacy hashes are compatibility clues, never destructive authorization. A legacy materialization or copy migrates only when the current selected source has a matching legacy/current checkpoint and reproduces the artifact's exact directory and byte layout. A legacy live link must have the recorded physical type and point to the selected canonical source. Missing/stale/mutated legacy artifacts that cannot satisfy that proof require explicit operator reconciliation. After all entries pass, sync atomically writes one complete v3 manifest before any planned artifact mutation; a subsequent partial failure therefore cannot create a mixed-schema checkpoint.

- Repeated sync with unchanged inputs produces no actions.
- Source, override-only, and renderer-identity changes update materialized owned artifacts. Live source changes checkpoint their manifest identity without recreating a valid link. A live skill whose relative source directory changes is ownership-validated and relinked to the new target instead.
- Removed/excluded source skills remove only unchanged owned artifacts.
- Missing owned output is recreated from canonical source; modified owned output is drift and blocks mutation.
- Existing unmanaged destination names and modified owned artifacts block writes/removal.
- A non-empty managed destination cannot be repointed to another source library without first using `remove`.
- Ownership preflight completes before writes. Any schema migration is checkpointed as one complete v3 manifest first, then the manifest is checkpointed after each successful artifact operation so ordinary I/O failures are retryable.
- Unresolved destination symlinks, Windows reparse points, and reparse ancestors are rejected before resolution for sync, check, and remove.
- A modern materialization or fallback copy may contain only ordinary directories and files. Any nested symlink or Windows reparse point is rejected before hashing, checking, replacement, or removal; live-link roots use their separate physical-mode/target proof.
- A destructive operation atomically moves the current candidate to a unique same-directory quarantine, verifies the moved filesystem identity and manifest proof again, and removes only that proved quarantine after the replacement and manifest commit. A late unmanaged target is never deliberately overwritten. A wrong candidate is restored only into a still-empty destination name and otherwise remains preserved.
- Scratch paths are unique and atomically allocated beside the destination. Cleanup requires their captured filesystem identity and physical type; if the first identity read fails, cleanup is limited to descriptor closure or removal of a still-empty scratch directory.
- Symlinks and junctions are removed with unlink-only/reparse-point-only operations, never recursive tree deletion. Dangling and wrong-target links are still treated as existing artifacts and block unsafe replacement. A same-name selected relocation may replace a dangling old live link only after its physical mode and raw target exactly match the manifest.
- `sync --dry-run` executes the same planning and preflight path without creating the destination or manifest.
- A valid empty inventory may report all stale removals during plan/dry-run. Mutating sync requires `--allow-empty` before removing every entry from a nonempty managed destination and otherwise performs no writes.

Cooperating mutating invocations serialize on one OS advisory destination transaction lock held from manifest load through completion. On Windows, sync and removal walk from the physical drive/share anchor to the destination one lexical component at a time. Each component is created or opened by `NtCreateFile` relative to the retained parent handle with reparse traversal disabled and is checked through `FileIdInfo` volume plus 128-bit file identity. Once established, each component remains open without delete sharing through the transaction. An adapter-created component is briefly delete-shared while it holds the `DELETE` right needed to roll itself back without blocking concurrent peers, then stabilizes through identity-matched handles to a no-delete-share retained handle. Missing or all-zero native identities are rejected. Ancestor authority is established before the destination-specific lock; sibling destinations may retain shared ancestors concurrently, while no-delete-sharing prevents replacement of established ancestors. Replacement attempted during the brief creation window is detected by no-follow identity revalidation and fails closed rather than being traversed. Failed establishment removes only exact adapter-created empty ancestors after identity revalidation; an absent removal target creates no directories. The transaction lock file remains persistent, so a first failed mutation may leave newly created parent directories that contain that lock; later calls reuse the same protocol state instead of unlinking a lock another process may already have open. Mutating support is Windows; Linux `x86_64`, `i686`, `aarch64`, `ppc64le`, and `s390x` on kernels/filesystems supporting `renameat2(RENAME_NOREPLACE)`; and macOS filesystems supporting `renamex_np(RENAME_EXCL)`. A missing Linux libc wrapper uses a direct syscall only for those mapped architectures. Other POSIX systems fail closed for mutation; source scanning remains portable. Manifest publication quarantines and compares the exact loaded identity and bytes before a staged no-replace commit.

This protocol remains fail-closed hardening, not complete hostile same-token concurrent-writer security. Windows retains authority over the destination ancestors and root, but recursive deletion and ordinary descendant access are not handle-relative, so a hostile process can still swap or modify a descendant after its final `lstat`/hash proof; file identifiers can also be reused. Artifact and manifest commits remain separate atomic renames, so a hard kill, power loss, occupied restore name, or concurrently modified quarantine can leave an untracked target or `.adapter-quarantine-*`/`.adapter-tmp-*` artifact. Ordinary exceptions are rolled back when identities still prove safe; otherwise Hermes Jack-In preserves ambiguous state for manual reconciliation. Keep the destination otherwise quiescent when that stronger threat model matters.

## Supporting files and paths

Supporting references, templates, scripts, and assets remain beneath the skill root. Hidden files are skipped, and any source symlink is rejected rather than followed or materialized. Hermes Jack-In does not copy `.env`, credentials, sessions, logs, profile state, caches, or any material outside a selected skill directory. Relative links continue to resolve because directory structure is preserved; Windows separators in inline Markdown links become `/`. Reference-style links, CommonMark URI autolinks, and raw HTML URL-bearing attributes are containment-validated but otherwise preserved byte-for-byte.

URL structure is classified from the raw destination before decoding. Literal `http`, `https`, `mailto`, and `data` schemes retain their external behavior; other schemes and scheme-relative authorities are rejected. Only a local path is repeatedly HTML-entity/percent decoded to a bounded fixed point, and an encoded delimiter that would reclassify it as a scheme, authority, query, or fragment is rejected. Decoded local paths are also rejected when empty, absolute, drive-qualified, or traversing through `..`.

The scan covers local inline/reference Markdown links, CommonMark URI autolinks, and quoted or unquoted raw HTML attributes whose value is a single URL, including `action`, `cite`, `data`, `formaction`, `href`, `poster`, `src`, and `xlink:href`. Multi-URL or embedded-document attributes such as `archive`, `ping`, `srcset`, and `srcdoc` are rejected conservatively rather than partially parsed. Ambiguous parenthesized local inline destinations are rejected; balanced parentheses remain allowed in external URLs. The same checks run again after semantic overrides. Skills containing hidden source entries are materialized through the filtered copier rather than exposed through a whole-directory symlink.

Claude's `${CLAUDE_SKILL_DIR}` already resolves at invocation time and is retained. Hermes Jack-In does not synthesize permission grants or execute source scripts during conversion.

## Why not a plugin by default

Claude's current documented personal/project skill contract already supports symlink targets, supporting files, automatic selection, direct `/skill-name` invocation, and live `SKILL.md` updates. A plugin would add namespacing and distribution benefits, but also packaging and activation machinery unnecessary for a local cross-project library. Plugin output can be added later as another backend without changing scanning, classification, overrides, or rendering.
