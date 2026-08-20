# Review-only feedback proposals

`hermes-jack-in feedback-propose` turns one explicit, operator-supplied Claude
feedback document into a canonical proposal for human review. It is a manual
handoff, not a feedback daemon or an apply path.

## Authority

- Hermes source skills and reviewed overrides remain authoritative.
- A clean ownership manifest plus `check` proves only that the feedback names a
  current, unchanged projection.
- Feedback text and any claimed origin are **untrusted data**.
- The proposal is review input with `"review_status":"required"`.
- The command never applies a proposal and never modifies the source,
  destination, overrides, or ownership manifest.

## Input contract

The input must be UTF-8 JSON with exactly these string fields:

```json
{
  "version": "feedback-v1",
  "skill": "example-skill",
  "claimed_provenance": "Operator-supplied Claude Code session note.",
  "summary": "Short summary.",
  "observation": "What the operator observed.",
  "recommendation": "What a maintainer should consider."
}
```

Unknown or duplicate keys, unsupported versions, invalid skill names, empty or
control-bearing strings, files larger than 32 KiB, provenance or summary fields
over 1,024 characters, and observation or recommendation fields over 8,192
characters are rejected.

`claimed_provenance` is an operator assertion, not authenticated proof that
Claude produced the input. Record external evidence separately until Claude
exposes a supported authenticated boundary.

## Manual workflow

First project the selected skill and establish a clean check:

```bash
hermes-jack-in sync --source "$SOURCE" --destination "$DESTINATION"
hermes-jack-in check --source "$SOURCE" --destination "$DESTINATION"
```

Save one bounded feedback document, then choose a new proposal path outside the
source and destination:

```bash
hermes-jack-in feedback-propose \
  --source "$SOURCE" \
  --destination "$DESTINATION" \
  --overrides overrides.yaml \
  --input claude-feedback-v1.json \
  --output review/example-skill-proposal.json \
  --json
```

Omit `--overrides` when the projection did not use one. The output parent must
already exist. The command rejects existing output, symlink or reparse output
roots, and any output path that overlaps the source or destination.

Before the sole intentional write, the command runs the complete projection
check twice and requires stable derived identity. Every check issue fails the
operation without a proposal. The proposal records the derived skill-relative
source, source SHA-256, classification, destination mode, and desired-output
identity alongside the untrusted feedback. Equivalent JSON key ordering yields
byte-identical canonical output.

## Review handoff

A Hermes maintainer inspects the proposal and independently decides whether to
change the authoritative skill or a reviewed override through the normal test
and review workflow. Proposal acceptance itself has no side effect.

There is no `feedback-apply` command. This milestone does not add polling,
session scraping, a feedback database, learning, ranking, patch generation,
automatic source edits, reverse sync, PR creation, or a Hermes plugin/API.
