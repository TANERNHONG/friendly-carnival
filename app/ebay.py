"""eBay Browse API client (official API — no scraping).

Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
Needs a Production keyset (client id + secret); uses the client-credentials OAuth grant.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx

from . import db
from .config import settings

log = logging.getLogger("carnival.ebay")

HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
SCOPE = "https://api.ebay.com/oauth/api_scope"
ITEM_ID_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,15})")


class EbayError(RuntimeError):
    pass


def parse_item_ref(ref: str) -> str | None:
    """Return the legacy (numeric) item id from a listing URL or a bare id."""
    ref = (ref or "").strip()
    if ref.isdigit() and 9 <= len(ref) <= 15:
        return ref
    m = ITEM_ID_RE.search(ref)
    if m:
        return m.group(1)
    q = parse_qs(urlparse(ref).query)
    for key in ("item", "itemId", "iid"):
        if key in q and q[key][0].isdigit():
            return q[key][0]
    m = re.search(r"\bv1\|(\d{9,15})\|", ref)
    return m.group(1) if m else None


async def resolve_item_ref(ref: str) -> str | None:
    """Like parse_item_ref, but follows short links such as ebay.us/m/xxxx."""
    legacy = parse_item_ref(ref)
    if legacy or not ref.startswith("http"):
        return legacy
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(ref, headers={"User-Agent": "Mozilla/5.0"})
            return parse_item_ref(str(resp.url))
    except httpx.HTTPError:
        return None


def _iso(ts: str | None) -> float | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _money(obj: dict | None) -> tuple[float | None, str | None]:
    if not obj or obj.get("value") is None:
        return None, None
    return float(obj["value"]), obj.get("currency")


def normalize(raw: dict) -> dict:
    """Map a Browse API item / item_summary onto the app's flat listing shape."""
    price, currency = _money(raw.get("currentBidPrice") or raw.get("price"))
    shipping = ship_cur = None
    opts = raw.get("shippingOptions") or []
    if opts:
        shipping, ship_cur = _money(opts[0].get("shippingCost"))
    seller = raw.get("seller") or {}
    image = (raw.get("image") or {}).get("imageUrl")
    item_id = raw.get("itemId") or ""
    legacy = raw.get("legacyItemId") or parse_item_ref(item_id) or ""
    end_time = _iso(raw.get("itemEndDate"))
    return {
        "item_id": item_id,
        "legacy_id": str(legacy),
        "title": raw.get("title") or "",
        "url": raw.get("itemWebUrl") or f"https://www.ebay.com/itm/{legacy}",
        "image": image,
        "seller": seller.get("username") or "unknown",
        "seller_feedback": seller.get("feedbackScore"),
        "price": price,
        "currency": currency,
        "shipping": shipping if (ship_cur in (None, currency)) else None,
        "bid_count": int(raw.get("bidCount") or 0),
        "end_time": end_time,
        "condition": raw.get("condition"),
        "location": (raw.get("itemLocation") or {}).get("country"),
        "buying_options": raw.get("buyingOptions") or [],
        "ended": bool(end_time and end_time <= time.time()),
    }


class EbayClient:
    name = "ebay"

    def __init__(self) -> None:
        self.base = HOSTS.get(settings.ebay_env, HOSTS["production"])
        self._token: str | None = None
        self._token_exp = 0.0

    # -- plumbing ---------------------------------------------------------
    def _count_call(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        counter = db.kv_get("ebay_calls", {})
        counter = {day: counter.get(day, 0) + 1}
        db.kv_set("ebay_calls", counter)

    @staticmethod
    def calls_today() -> int:
        return db.kv_get("ebay_calls", {}).get(time.strftime("%Y-%m-%d", time.gmtime()), 0)

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        basic = base64.b64encode(f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()).decode()
        resp = await client.post(
            f"{self.base}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        if resp.status_code != 200:
            raise EbayError(f"OAuth failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 7200))
        return self._token

    async def _get(self, path: str, params: dict | None = None, marketplace: str | None = None) -> httpx.Response:
        if self.calls_today() >= settings.ebay_daily_call_budget:
            raise EbayError("Daily eBay API call budget reached")
        ctx = f"contextualLocation=country={settings.ship_to_country}"
        if settings.ship_to_zip:
            ctx += f",zip={settings.ship_to_zip}"
        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._get_token(client)
            self._count_call()
            return await client.get(
                f"{self.base}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": marketplace or settings.ebay_marketplace,
                    "X-EBAY-C-ENDUSERCTX": ctx,
                },
            )

    # -- API --------------------------------------------------------------
    async def search_auctions(self, keyword: str, *, marketplace: str | None = None,
                              category_ids: str | None = None, limit: int | None = None) -> list[dict]:
        limit = limit or settings.max_results_per_keyword
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            params = {
                "q": keyword,
                "filter": "buyingOptions:{AUCTION}",
                "sort": "endingSoonest",
                "limit": min(200, limit - len(out)),
                "offset": offset,
            }
            if category_ids:
                params["category_ids"] = category_ids
            resp = await self._get("/buy/browse/v1/item_summary/search", params, marketplace)
            if resp.status_code != 200:
                raise EbayError(f"search '{keyword}' failed ({resp.status_code}): {resp.text[:200]}")
            body = resp.json()
            batch = body.get("itemSummaries") or []
            out.extend(normalize(x) for x in batch)
            offset += len(batch)
            if not batch or offset >= int(body.get("total", 0)):
                break
        return [x for x in out if "AUCTION" in x["buying_options"]]

    async def get_by_legacy(self, legacy_id: str) -> dict | None:
        resp = await self._get("/buy/browse/v1/item/get_item_by_legacy_id", {"legacy_item_id": legacy_id})
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            # Multi-variation listings need a variation id; surface the API's message.
            raise EbayError(f"item {legacy_id} lookup failed ({resp.status_code}): {resp.text[:200]}")
        return normalize(resp.json())

    async def get_items(self, refs: list[dict]) -> dict[str, dict | None]:
        """Batch lookup (Browse getItems, 20 ids per call). refs: [{item_id, legacy_id}]."""
        result: dict[str, dict | None] = {}
        with_ids = [r for r in refs if r.get("item_id")]
        for i in range(0, len(with_ids), 20):
            chunk = with_ids[i:i + 20]
            try:
                resp = await self._get("/buy/browse/v1/item/", {"item_ids": ",".join(r["item_id"] for r in chunk)})
                if resp.status_code != 200:
                    raise EbayError(resp.text[:200])
                found = {n["legacy_id"]: n for n in (normalize(x) for x in resp.json().get("items") or [])}
                for r in chunk:
                    result[r["legacy_id"]] = found.get(r["legacy_id"])
            except Exception as exc:  # noqa: BLE001 - fall back to single lookups
                log.info("getItems batch failed (%s); falling back to single lookups", exc)
        for r in refs:
            if r["legacy_id"] not in result:
                try:
                    result[r["legacy_id"]] = await self.get_by_legacy(r["legacy_id"])
                except EbayError as exc:
                    log.warning("%s", exc)
                    result[r["legacy_id"]] = None
        return result
