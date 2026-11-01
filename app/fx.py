"""Foreign-exchange rates into SGD, cached for six hours."""
from __future__ import annotations

import logging
import time

import httpx

from . import db

log = logging.getLogger("carnival.fx")

# Rough fallback (units of currency per 1 SGD) used only when every live source is unreachable.
STATIC_PER_SGD = {
    "SGD": 1.0, "USD": 0.77, "EUR": 0.68, "GBP": 0.59, "AUD": 1.17, "CAD": 1.06,
    "JPY": 115.0, "HKD": 6.0, "MYR": 3.4, "CHF": 0.62, "NZD": 1.29, "CNY": 5.5,
}
SOURCES = (
    ("frankfurter", "https://api.frankfurter.dev/v1/latest?base=SGD"),
    ("open.er-api", "https://open.er-api.com/v6/latest/SGD"),
)
TTL = 6 * 3600

_cache: dict = {}


async def refresh(force: bool = False) -> dict:
    global _cache
    if not _cache:
        _cache = db.kv_get("fx", {}) or {}
    if not force and _cache and time.time() - _cache.get("fetched_at", 0) < TTL:
        return _cache
    async with httpx.AsyncClient(timeout=10) as client:
        for name, url in SOURCES:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                rates = resp.json().get("rates") or {}
                if "USD" in rates:
                    rates["SGD"] = 1.0
                    _cache = {"rates": rates, "source": name, "fetched_at": time.time()}
                    db.kv_set("fx", _cache)
                    return _cache
            except Exception as exc:  # noqa: BLE001 - try the next source
                log.warning("FX source %s failed: %s", name, exc)
    if not _cache:
        _cache = {"rates": STATIC_PER_SGD, "source": "static-fallback", "fetched_at": 0}
    return _cache


def to_sgd(amount: float | None, currency: str | None) -> float | None:
    if amount is None:
        return None
    currency = (currency or "SGD").upper()
    rates = (_cache or {}).get("rates") or STATIC_PER_SGD
    per_sgd = rates.get(currency) or STATIC_PER_SGD.get(currency)
    if not per_sgd:
        log.warning("No FX rate for %s; treating as SGD", currency)
        return round(amount, 2)
    return round(amount / per_sgd, 2)


def status() -> dict:
    c = _cache or {}
    return {"source": c.get("source"), "fetched_at": c.get("fetched_at"), "usd_per_sgd": (c.get("rates") or {}).get("USD")}
