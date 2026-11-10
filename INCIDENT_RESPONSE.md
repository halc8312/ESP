# Repository Data Exposure Response

## Recorded exposure and current limits

Historical revisions included runtime SQLite files (including `mercari.db` and backups), generated HTML, scrape reports, and product exports. The database artifacts included account records and password hashes. Deleting the current files does not remove their older Git objects, PR diffs, cached copies, or clones.

The 2026-11-10 review did not establish that production credentials or customer records were present, but it also did not establish that old test credentials were never reused. Keep original artifacts restricted. Record paths, dates, affected categories, and remediation status without publishing password hashes, tokens, or account details.

## Containment and verification

1. Determine which environments, accounts, tokens, and secrets were affected. Treat any exposed usable credential as compromised.
2. Reset affected or reused passwords. Revoke and reissue exposed API keys and catalog tokens where applicable. If session-signing material was exposed or reused, rotate it consistently across web and worker and verify old sessions no longer authenticate.
3. Check deployment artifacts, logs, backups, and image build contexts for copied data. Keep evidence outside the public repository with restricted access.
4. Verify that runtime data and environment files are excluded from Git and Docker build contexts. Preserve intentional synthetic test fixtures.
5. Scan the current tree and relevant historical branches/PRs. A clean current tree is not proof of clean history, and deleting a file does not revoke a credential.
6. Record what was verified, what was rotated, the time, responsible operator, and remaining uncertainty in the private incident record.

## History remediation

History rewriting affects commit IDs, branches, open PRs, forks, clones, and deployment references. It is a separate coordinated operation after credential containment and a reviewed path/ref inventory. Do not force-push a mirror or delete review history as an ordinary documentation cleanup.

When sensitive historical objects require removal, use GitHub's current sensitive-data-removal procedure, coordinate with repository collaborators, and contact GitHub Support for cached views when necessary. Verify all affected refs and copies afterwards; do not claim complete removal solely from a clean default branch.

## Recovery checks

Confirm authentication, user isolation, web/worker signing, image access, queue ownership, and monitoring after any rotation. Do not use production data or secrets in public CI. Follow [OPERATIONS_HANDOVER](docs/OPERATIONS_HANDOVER.md) for isolated restore testing and [SECURITY](SECURITY.md) for production controls.
