# Security Policy

## Supported versions

Hermes Jack-In is beta software. Security fixes are provided for the latest
released version only. A release is supported for mutating operations only on
platforms represented by a passing native release matrix; unsupported native
publication primitives fail closed.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private
vulnerability reporting for `sushiHex/hermes-jack-in`. Include the affected
version, operating system, deployment mode, minimal sanitized reproduction, and
whether unmanaged destination data or an authoritative source tree was at risk.

If private vulnerability reporting is unavailable, open a minimal public issue
asking the maintainer to enable a private channel. Do not include exploit
details, credentials, private skill text, or personal paths.

The maintainer will acknowledge a complete report, coordinate disclosure, and
publish a fix or explicit disposition as promptly as practical. No fixed
response-time or bounty commitment is offered during beta.

## Adapter security boundary

Hermes Jack-In treats the scanned Hermes skill library as trusted and
authoritative. It refuses to claim, replace, or remove destination artifacts
unless manifest ownership, physical mode, hashes, identities, and canonical
targets prove that action. Destination roots and ancestors may not be symlinks
or Windows reparse aliases. Windows mutating transactions establish each lexical
destination component relative to its already retained parent handle and reject
reparse points and cross-volume traversal. Established components retain
native-identity-checked physical handles without delete sharing until the
transaction ends. A newly created component is briefly delete-shared while its
exact creation handle carries rollback authority, then stabilizes through
identity-matched handles to the established no-delete-share state. Identity
uses `FileIdInfo` volume plus 128-bit file IDs and rejects unavailable or
all-zero carriers. Empty ancestors created by a failed establishment attempt
are removed either through the still-owned exact creation handle when identity
is unavailable or through an identity-matched reopen relative to the retained
parent. Nonempty, replaced, or otherwise unproved state is preserved.
Recoverable transaction failures trigger
identity-checked rollback; ambiguous scratch or quarantine state is preserved
rather than guessed away.

The per-destination lock file is persistent so every cooperating process locks
the same file object. If the first transaction for a missing destination fails
after acquiring that lock, its newly created parent chain can therefore remain
as intentional protocol state rather than being recursively removed. A retry
reuses the same lock and authority chain; the destination directory itself is
rolled back when it is still the exact empty directory created by the failed
attempt.

Ancestor authority is established before the destination-specific advisory
lock is acquired. Sibling destinations can therefore retain shared ancestors
concurrently; missing delete sharing prevents authority replacement, and an
ambiguous cleanup fails closed rather than retrying an indeterminate close.

The transaction model is designed for accidental failure and cooperating
writers. It is not a sandbox or a defense against a hostile process with the
same user identity. On POSIX, path checks do not retain directory authority;
a same-user process can race an ancestor name or the destination between checks.
Use POSIX mutating commands only beneath parents that are not writable by
untrusted accounts, and do not run them concurrently with hostile same-user
writers. Artifact and manifest publication are separate atomic renames; hard
termination, power loss, an occupied restore name, or concurrent same-user
mutation can leave recoverable evidence requiring manual review.

The project does not validate whether skill instructions themselves are safe.
A compromised or malicious source library can produce harmful agent behavior.
Review source skills and exact semantic overrides before sharing them.

## Optional guard boundary

`hermes-jack-in-guard` is a Claude Code `PreToolUse` defense-in-depth helper. It
requires explicit, existing, absolute physical protected roots and denies Bash
when its deployment configuration is invalid. Its parser is intentionally
bounded and lexical. It does not evaluate arbitrary subprocess semantics,
runtime-computed paths, or encoded data, and it cannot protect against another
same-user process, modified Claude settings, or a compromised hook executable.

Claude Code's Bash sandbox is unsupported on native Windows. Use WSL2 if that
OS-enforced Bash sandbox is required. Claude hooks and permission rules are
controls within Claude Code, not operating-system access control.

Back up important data independently and never use a personal destination for
destructive testing.
