"""
Daily exchange rate refresh for the customer-facing catalog.

The admin screens deal only in yen. Rates exist so the published catalog can
offer customers a currency switch, and so students keep a rough sense of where
the market is. Because nothing in the admin depends on them being current, a
failed refresh keeps the previously stored values rather than surfacing an
error: a day-old rate is far better than no price at all.
"""
from __future__ import annotations

import logging

import requests

from database import SessionLocal
from models import ExchangeRate
from time_utils import utc_now

logger = logging.getLogger(__name__)

#: No API key needed, and the response is JPY-based, matching how prices are
#: stored. See https://open.er-api.com/.
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/JPY"
EXCHANGE_RATE_TIMEOUT_SECONDS = 15

#: The currencies the catalog lets customers switch to.
SUPPORTED_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "GBP", "CNY", "KRW")

CURRENCY_LABELS: dict[str, str] = {
    "USD": "米ドル",
    "EUR": "ユーロ",
    "GBP": "英ポンド",
    "CNY": "中国元",
    "KRW": "韓国ウォン",
}

#: Used when no rate has ever been stored, matching the catalog's own fallback.
DEFAULT_JPY_PER_USD = 155.0


def _fetch_rates_from_api() -> dict[str, float]:
    """Return {currency: units per 1 JPY} for the supported currencies."""
    response = requests.get(
        EXCHANGE_RATE_API_URL,
        timeout=EXCHANGE_RATE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("result") != "success":
        raise ValueError(f"exchange rate API returned {payload.get('result')!r}")

    rates = payload.get("rates") or {}
    resolved: dict[str, float] = {}
    for currency in SUPPORTED_CURRENCIES:
        raw = rates.get(currency)
        try:
            per_jpy = float(raw)
        except (TypeError, ValueError):
            continue
        if per_jpy > 0:
            resolved[currency] = per_jpy

    if not resolved:
        raise ValueError("exchange rate API returned no usable rates")
    return resolved


def refresh_exchange_rates(session_db=None) -> dict[str, object]:
    """
    Fetch and store the latest rates.

    Returns a summary rather than raising, so a scheduled run never takes the
    process down. On failure the stored rates are left untouched.
    """
    session = session_db or SessionLocal()
    owns_session = session_db is None

    try:
        try:
            per_jpy_rates = _fetch_rates_from_api()
        except Exception as exc:
            logger.warning("Exchange rate refresh failed, keeping stored rates: %s", exc)
            return {"status": "failed", "error": str(exc), "updated": []}

        fetched_at = utc_now()
        existing = {
            row.quote_currency: row
            for row in session.query(ExchangeRate).all()
        }

        updated: list[str] = []
        for currency, per_jpy in per_jpy_rates.items():
            jpy_per_unit = 1 / per_jpy
            row = existing.get(currency)
            if row is None:
                session.add(
                    ExchangeRate(
                        quote_currency=currency,
                        jpy_per_unit=jpy_per_unit,
                        fetched_at=fetched_at,
                    )
                )
            else:
                row.jpy_per_unit = jpy_per_unit
                row.fetched_at = fetched_at
            updated.append(currency)

        session.commit()
        return {"status": "ok", "error": None, "updated": updated}
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()


def get_exchange_rates(session_db=None) -> list[dict[str, object]]:
    """Return the stored rates in the catalog's currency order."""
    session = session_db or SessionLocal()
    owns_session = session_db is None

    try:
        stored = {
            row.quote_currency: row
            for row in session.query(ExchangeRate).all()
        }
        return [
            {
                "code": currency,
                "label": CURRENCY_LABELS.get(currency, currency),
                "jpy_per_unit": stored[currency].jpy_per_unit,
                "fetched_at": stored[currency].fetched_at,
            }
            for currency in SUPPORTED_CURRENCIES
            if currency in stored
        ]
    finally:
        if owns_session:
            session.close()


def get_jpy_per_usd(session_db=None) -> float:
    """Return the stored JPY/USD rate, or the shared default if none exists."""
    session = session_db or SessionLocal()
    owns_session = session_db is None

    try:
        row = (
            session.query(ExchangeRate)
            .filter(ExchangeRate.quote_currency == "USD")
            .first()
        )
        if row is None or not row.jpy_per_unit or row.jpy_per_unit <= 0:
            return DEFAULT_JPY_PER_USD
        return float(row.jpy_per_unit)
    finally:
        if owns_session:
            session.close()


def apply_safety_margin(jpy_per_unit: float, margin_yen: int | None) -> float:
    """
    Shade a rate by the operator's safety margin.

    The margin makes foreign prices slightly higher, never lower, so a rate
    that moves between the daily refreshes cannot quietly undercut the yen
    price. It never touches the yen price itself.
    """
    margin = int(margin_yen or 0)
    if margin <= 0:
        return jpy_per_unit
    return max(jpy_per_unit - margin, jpy_per_unit * 0.5)
