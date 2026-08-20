# Release process

A release is made from a clean, reviewed public root commit—not from the private
development ancestry.

## Preconditions

1. The intended version is present in `pyproject.toml` and `uv.lock`. Changes
   remain under `Unreleased` until the release-preparation commit moves them
   into the matching dated version section in `CHANGELOG.md`.
2. The native GitHub Actions matrix is green on Windows, Linux, and macOS for
   every declared Python minor.
3. `uv run --frozen python scripts/release_gate.py` passes from a clean archive
   of the exact candidate.
4. Wheel and sdist contents match the release allowlist, build twice to identical
   hashes with a fixed `SOURCE_DATE_EPOCH`, and install successfully in isolated
   environments.
5. A sanitized real Hermes scan and non-mutating destination plan have zero
   issues. A temporary Claude Code canary records the exact Claude version.
6. Documentation, security policy, platform claims, and dependency lock are
   current. No private plans, inventories, paths, credentials, or raw audit logs
   are reachable from the public history.

For a private release audit, set `HERMES_JACK_IN_FORBIDDEN_MARKERS` to a
comma-separated list of owner-specific identifiers that must not occur in any
wheel or sdist member. Keep the values outside the repository and its logs.

## Source-only GitHub prerelease publication

Before tagging, convert the matching `Unreleased` notes into a dated version
section. Use a signed, annotated `vX.Y.Z` tag whose version exactly matches
package metadata, push it, and require the complete tag-triggered CI matrix to
pass. Create the GitHub release with `--prerelease --verify-tag --latest=false`.

For a source-only GitHub prerelease:

- Do not publish to PyPI or configure a PyPI publishing credential/environment.
- No wheel or sdist release assets are attached; package builds remain release
  qualification evidence only.
- Record sanitized commit, tag, CI, artifact-hash, and signing evidence in the
  release body or owner-controlled records. Never publish private audit logs.

Repository creation, remote configuration, pushing, tagging, GitHub environment
configuration, and package publication are separate owner-authorized actions.
The repository intentionally contains no automatic package-publishing workflow
until those controls exist and have been reviewed.
