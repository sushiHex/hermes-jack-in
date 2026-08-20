# Changelog

All notable changes to Hermes Jack-In are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Add a manual `feedback-propose` command that binds bounded, untrusted Claude
  feedback to one current owned projection and emits a canonical
  `review_status: "required"` proposal without changing Hermes or Claude state.

## [0.1.0] - 2026-08-19

### Added

- Fail-closed inventory and compatibility classification for Hermes Agent skills.
- Deterministic frontmatter conversion and explicit semantic overrides.
- Plan, sync, check, and identity-based removal workflows for Claude Code skills.
- Symlink, Windows junction, and materialized-copy deployment modes.
- Ownership manifests, collision refusal, transaction locking, quarantine,
  rollback, and drift detection.
- Hermetic build and installed wheel/sdist qualification.
- Fail-closed Claude Code `PreToolUse` guard with explicit UTF-8 event decoding
  and protected-root validation.
- Establish every Windows destination ancestor one component at a time with
  handle-relative, no-follow native opens, and retain the verified physical
  handles through each mutating transaction so a late NTFS junction or directory
  replacement cannot redirect managed writes. Failed establishment removes only
  the exact empty ancestors created by that attempt, and removal of an absent
  destination no longer creates its parent chain. Rollback identity uses native
  volume plus 128-bit file IDs and rejects indeterminate carriers; a persistent
  transaction lock can intentionally retain a retryable parent chain after a
  post-lock failure.
- Require `--allow-empty` whenever reconciliation would leave a managed
  destination with no entries, including when source skills remain present but
  are all excluded without their reviewed semantic overrides.

### Changed

- Keep GitHub Actions on reviewed Node 24 releases while retaining immutable
  full-SHA pins, and use one `uv` Dependabot stream for Python dependencies.

[Unreleased]: https://github.com/sushiHex/hermes-jack-in/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sushiHex/hermes-jack-in/releases/tag/v0.1.0
