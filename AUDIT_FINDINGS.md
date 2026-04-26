# Audit Findings

Audit date: 2026-04-25

## Confirmed Findings

| ID | Priority | Finding | Evidence | Status |
| --- | --- | --- | --- | --- |
| F-01 | P0 | Runtime DBs, DB backup, scraped HTML dumps, generated JSON reports, and export CSVs were tracked. | `git ls-files` showed `mercari.db`, `mercari.db.backup`, `dump.html`, `mercari_page_dump*.html`, `scrape_test_results.json`, `scraping_test_report.json`, and Shopify CSVs. `mercari.db` contained users/password hashes and product source URLs. | Fixed in working tree; history cleanup still manual. |
| F-02 | P0 | Git history still contains DB/dump/generated artifacts. | `git log --all --name-only` still lists the removed DB, HTML, JSON, and CSV paths. | Documented in `INCIDENT_RESPONSE.md`; force-push not performed. |
| F-03 | P0 | Production could use `dev-secret-key-change-this` fallback. | `app.py` previously read `SECRET_KEY` with that fallback. | Fixed with production fail-closed checks in `security_config.py`. |
| F-04 | P0 | Production cookie, HTTPS, and HSTS controls were not enforced by the app. | Existing app had `ProxyFix` but no `SESSION_COOKIE_SECURE`, HSTS, or app-level HTTPS redirect. Public Render response lacked `Secure` and HSTS. | Fixed locally with secure production defaults, HTTPS redirect, HSTS, and tests. |
| F-05 | P0 | `/register` was publicly open and accepted weak passwords. | Existing route always allowed signup and passed password directly to `set_password`. Public Render `/register` returned 200 on 2026-04-25. | Fixed locally: production signup default disabled, explicit `ALLOW_PUBLIC_SIGNUP` required, shared password policy added. |
| F-06 | P0 | Login/register rate limiting was missing and not backed by a shared store. | No login/register rate-limit implementation existed. | Fixed locally: Redis/Valkey-backed limiter with dev memory fallback; production requires `REDIS_URL` or `VALKEY_URL`. |
| F-07 | P0 | Dependencies were unpinned and no dependency audit CI existed. | `requirements.txt` had only bare package names and `.github/workflows` did not exist. | Fixed with pinned requirements, `requirements-dev.txt`, and CI running tests plus `pip-audit`. |
| F-08 | P0 | External image download lacked Content-Type, byte, and pixel validation. | `services/image_service.py` and `/export_images` downloaded unbounded content beyond request timeout. | Fixed with streaming byte cap, Content-Type validation, Pillow verification, pixel limit, and tests. |
| F-09 | P0 | Public catalog leaked internal procurement fields. | `_build_catalog_item()` returned `source_url` and `site`; template exposed source link and `data-site`. | Fixed and regression-tested. |
| F-10 | P1 | License, privacy policy, terms, and security policy were missing. | No `LICENSE`, privacy, terms, or `SECURITY.md` present. | Added `LICENSE_PENDING.md`, policy drafts, and `SECURITY.md`. |

## False Positives / Not Reproduced

| Item | Result |
| --- | --- |
| Account password-change policy mismatch | No account password-change screen was found in the current codebase. Policy is now shared by registration and CLI user creation. |
| Binary image upload endpoint | No direct binary image upload route was found. CSV upload exists; `MAX_CONTENT_LENGTH` now caps request bodies and external image fetches are validated. |
| Tracked selector JSON as data dump | `config/scraping_selectors.json` and `config/element_fingerprints.json` appear to be application configuration, not user/session data. |

## New Findings

| ID | Priority | Finding | Status |
| --- | --- | --- |
| N-01 | P0 | `mercari.db` contained password hashes and usernames, so deletion alone is insufficient. | Rotation/reset checklist added to `INCIDENT_RESPONSE.md`. |
| N-02 | P1 | `database.py` printed full database URL, which can leak credentials with hosted DB URLs. | Fixed by masking passwords in log output. |
| N-03 | P1 | Existing `SECURITY_ANALYSIS.md` is a historical report and describes pre-fix state. | Superseded by this file; keep only as historical context or remove later. |
| N-04 | P1 | Public Render deployment still appears to be running old code. | Manual redeploy and header verification required. |
| N-05 | P1 | Root-level diagnostic scripts generated ignored dumps during pytest import. | Fixed by moving side-effecting code under `if __name__ == "__main__"` and writing manual outputs to the OS temp directory. |

## Verification Snapshot

- Initial `pytest -q`: 155 passed.
- Post-fix `pytest -q`: 173 passed.
- Post-fix `python -m pip check`: no broken requirements.
- Post-fix `python -m pip_audit -r requirements.txt`: no known vulnerabilities found.
- Current tracked artifact scan: no tracked DB/dump/report/export artifact paths remain.
- Git history scan: removed artifact paths still exist in history and need coordinated rewrite.

## Remaining Manual Work

- Rewrite Git history and force-push after coordination.
- Rotate `SECRET_KEY`.
- Reset exposed account passwords and invalidate sessions.
- Provision and verify Redis/Valkey for production auth rate limiting.
- Redeploy Render and verify HTTPS redirect, `Secure` cookies, HSTS, `/register` disabled, and production startup fail-closed behavior.
- Have counsel approve license, privacy policy, and terms before general public release.
