# Privacy Policy Draft

This draft requires legal review before public release.

## Data Collected

ESP may store account usernames, password hashes, shop settings, product records, pricing rules, uploaded/imported CSV content during processing, public catalog page-view metadata, and operational logs.

## Catalog Analytics

Public catalog analytics store a truncated hash of the visitor IP address, a short device label, referrer domain, timestamp, and optional product ID. Raw IP addresses should not be stored by application code.

## Use of Data

Data is used to authenticate users, manage product catalogs, generate exports, monitor product availability, and provide catalog analytics.

## Third Parties

The app may fetch product and image data from configured source sites and may call exchange-rate services from public catalog pages. Deployment providers, Redis/Valkey providers, and database providers may process operational data.

## Retention and Deletion

Define retention periods for accounts, product records, catalog analytics, logs, and backups before public launch. Provide a documented deletion request process.

## Security

Production deployments must use HTTPS, secure cookies, HSTS, strong secrets, and shared-store authentication rate limiting.
