# Incident Response: Repository Data Exposure

## Confirmed Exposure

The repository contained tracked runtime and generated files including:

- `mercari.db`
- `mercari.db.backup`
- `dump.html`
- `mercari_page_dump.html`
- `mercari_page_dump_live.html`
- `scrape_test_results.json`
- `scraping_test_report.json`
- `shopify_products.csv`
- `shopify_products (9).csv`
- `out.txt`

Local inspection confirmed `mercari.db` contained user records and password hashes, and generated CSV/JSON/HTML files contained real scraped product URLs, descriptions, and external image URLs.

## Immediate Containment Checklist

1. Stop treating any exposed database content as private.
2. Rotate `SECRET_KEY` for every deployed environment.
3. Invalidate all existing Flask sessions after rotating `SECRET_KEY`.
4. Reset passwords for any accounts that existed in exposed DB files.
5. Review public catalog tokens and regenerate any that should not remain valid.
6. Review deploy logs and object storage for copied DB/dump artifacts.
7. Confirm `.gitignore` blocks future DB/dump/report artifacts.
8. Run a fresh secret scan before pushing.

## Git History Cleanup

History rewriting is destructive and requires coordination with all clones and deployments. Prepare locally, review, then force-push only after approval.

Recommended `git-filter-repo` flow:

```bash
python -m pip install git-filter-repo
git clone --mirror https://github.com/halc8312/ESP.git ESP-clean.git
cd ESP-clean.git
git filter-repo --invert-paths \
  --path mercari.db \
  --path mercari.db.backup \
  --path dump.html \
  --path mercari_page_dump.html \
  --path mercari_page_dump_live.html \
  --path scrape_test_results.json \
  --path scraping_test_report.json \
  --path "shopify_products.csv" \
  --path "shopify_products (9).csv" \
  --path out.txt
git log --all --name-only --pretty=format: -- "*.db" "*.backup" "*.html" "*.json" "*.csv" "out.txt" | sort -u
git push --force --mirror
```

After force-push:

```bash
git for-each-ref --format="delete %(refname)" refs/original | git update-ref --stdin
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

Ask GitHub Support to purge cached views if sensitive files remain visible through cached diffs or forks.

## Secret and Session Rotation

Generate a new secret:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

Set `SECRET_KEY` in Render or the deployment environment, redeploy, and verify that old session cookies no longer authenticate. Because Flask session cookies are signed by `SECRET_KEY`, rotating it invalidates existing sessions.

## Redis/Valkey Setup

Production now fails closed without `REDIS_URL` or `VALKEY_URL`. Provision a shared Redis/Valkey instance and set one of:

```bash
REDIS_URL=redis://USER:PASSWORD@HOST:PORT/0
VALKEY_URL=redis://USER:PASSWORD@HOST:PORT/0
```

## Post-Cleanup Verification

```bash
git log --all --name-only --pretty=format: -- "*.db" "*.sqlite" "*.sqlite3" "*.backup" "*.bak" "*.html" "*.json" "*.csv" | sort -u
git grep -n -I -e "dev-secret-key-change-this" -e "SECRET_KEY" -- .
python -m pip_audit -r requirements.txt
python -m pytest -q
```
