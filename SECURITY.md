# Security Policy

## Supported Status

This project is being prepared for a release candidate. Treat production use as conditional until the remaining manual actions in `AUDIT_FINDINGS.md` are complete.

## Reporting a Vulnerability

Do not open a public issue for secrets, real data exposure, authentication bypasses, or private deployment details. Report privately to the repository owner with:

- affected URL, route, or file path
- reproduction steps
- expected impact
- relevant logs or screenshots with secrets redacted

## Required Production Controls

- Set `APP_ENV=production` or `RUNTIME_ROLE=web`.
- Set a unique `SECRET_KEY` of at least 32 characters.
- Set `REDIS_URL` or `VALKEY_URL` for shared auth rate limiting.
- Keep `ALLOW_PUBLIC_SIGNUP=false` unless a deliberate public signup launch has been reviewed.
- Terminate TLS at the edge and forward `X-Forwarded-Proto`.
- Verify `SESSION_COOKIE_SECURE`, HSTS, and HTTPS redirects after each deployment.

## Data Handling

Do not commit runtime DBs, generated dumps, scrape reports, customer CSVs, or live product exports. If such files reach Git history, follow `INCIDENT_RESPONSE.md`.
