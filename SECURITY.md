# Security Policy

## Reporting a Vulnerability

Report secrets, data exposure, authentication bypasses, and private deployment details privately to the repository owner. Do not post them in a public issue or pull request. Include the affected route or file, reproduction steps, impact, and redacted evidence.

## Production Controls

- Set `APP_ENV=production` or `RUNTIME_ROLE=web` and an explicit, unique `SECRET_KEY` of at least 32 characters. Share the required signing configuration between web and worker.
- Configure shared Redis/Valkey for authentication rate limiting, queues, locks, and heartbeats.
- Keep `ALLOW_PUBLIC_SIGNUP=false` unless a public signup launch has been reviewed.
- Terminate TLS at the edge, forward the protocol correctly, and verify secure session cookies, HSTS, and HTTPS redirects after deployment.
- Preserve user/shop/pricelist isolation and keep sourcing details out of the public catalog.
- Run dependency audits and relevant regression tests for the release being deployed. CI results do not establish the state of a different production release.

## Data and Credential Handling

Do not commit runtime databases, generated dumps, customer exports, session cookies, API keys, private contact details, or live connection strings. Keep environment values in the deployment's secret store and a restricted handover record. `.gitignore` and `.dockerignore` reduce accidental inclusion but do not replace review.

Historical revisions contain database artifacts with account records and password hashes. Current-tree cleanup does not remove that history or establish whether old credentials were reused. Verify affected account reuse and complete any necessary resets or rotation using [INCIDENT_RESPONSE](INCIDENT_RESPONSE.md). Do not publish the original values as evidence.

Current delivery and operational verification gaps are tracked in [DELIVERY_STATUS](docs/DELIVERY_STATUS.md) and [OPERATIONS_HANDOVER](docs/OPERATIONS_HANDOVER.md).
