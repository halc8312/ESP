# ESP

ESP is a Flask-based product sourcing, catalog, and export tool.

## Local Setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Set local values in `.env` or the shell before starting the app. For development, `APP_ENV=development` keeps public signup enabled by default and allows the development secret fallback. Production does not.

```powershell
$env:APP_ENV = "development"
$env:DATABASE_URL = "sqlite:///mercari.db"
$env:SECRET_KEY = "local-development-secret-key-32chars"
.\.venv\Scripts\python.exe app.py
```

## Production Requirements

Production is detected when `APP_ENV=production`, `ENVIRONMENT=production`, `RENDER=true`, or `RUNTIME_ROLE` is `web` or `worker`.

Production startup fails unless:

- `SECRET_KEY` is set, is not `dev-secret-key-change-this`, and is at least 32 characters.
- `REDIS_URL` or `VALKEY_URL` is set for shared login/register rate limiting.

Production security defaults:

- `SESSION_COOKIE_SECURE=True`
- `SESSION_COOKIE_HTTPONLY=True`
- `SESSION_COOKIE_SAMESITE=Lax`
- HTTPS redirects enabled
- HSTS enabled
- public signup disabled unless `ALLOW_PUBLIC_SIGNUP=true` is explicitly set

## Accounts

For public deployments, keep `ALLOW_PUBLIC_SIGNUP=false` and create accounts through:

```powershell
$env:FLASK_APP = "app.py"
.\.venv\Scripts\flask.exe create-user
```

Passwords must be at least 12 characters, include letters and numbers, avoid common weak values, and not contain the username.

## Tests and Audit

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
```

GitHub Actions runs tests, dependency consistency checks, `pip-audit`, and a production security configuration smoke check.

## Data Hygiene

Runtime databases, WAL files, HTML dumps, generated scrape reports, logs, and export CSVs are ignored. Do not commit live DBs, scraped HTML, real customer/product dumps, session data, or generated verification artifacts. If any were pushed, follow `INCIDENT_RESPONSE.md` because deleting the current files is not enough to remove them from Git history.

## Deployment Notes

The Dockerfile runs Gunicorn with one worker because the scrape queue remains process-local. Do not increase worker count until the scrape queue is moved to a shared store. Authentication rate limiting already requires Redis/Valkey in production.
