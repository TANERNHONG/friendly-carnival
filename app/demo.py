"""DEMO MODE: a deterministic auction simulator that stands in for the eBay API.

Used automatically when no eBay keys are configured. Every listing, seller and price here
is synthetic and labelled as demo data in the UI and in Telegram messages. Auctions follow
patterns real eBay auctions show (late bidding / sniping, cheaper endings while US bidders
sleep, seller-specific premiums) so the charts and the prediction model have something to learn.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import random
import time
from dataclasses import dataclass, field

from . import db, fx

DEMO_URL = "https://demo.invalid/itm/{}"
ANCHOR = 1_700_000_000  # fixed origin for the auction "slots"
HOUR = 3600
DAY = 86400


@dataclass(frozen=True)
class Seller:
    name: str
    feedback: int
    premium: float  # multiplier on final price
    snipe: float    # share of the price gain that lands in the final minutes


@dataclass(frozen=True)
class Product:
    keyword: str
    titles: tuple[str, ...]
    base_usd: float
    sigma: float
    ship_usd: float
    target_sgd: float
    cadence_h: float  # a new auction ends roughly every N hours


SELLERS = (
    Seller("kanto_vault", 4120, 1.08, 0.42),
    Seller("mintcondition_sg", 860, 0.96, 0.22),
    Seller("retro_relics_co", 15400, 1.02, 0.35),
    Seller("pixelpawn", 230, 0.90, 0.16),
    Seller("grade10graded", 7800, 1.12, 0.52),
    Seller("attic_finds_uk", 1290, 0.93, 0.28),
    Seller("nebula_collectibles", 3300, 1.00, 0.38),
    Seller("dusty_shelf_deals", 95, 0.88, 0.14),
)

PRODUCTS = (
    Product("charizard base set psa 8", (
        "1999 Pokemon Base Set Charizard #4/102 Holo PSA 8 NM-MT",
        "Charizard Base Set Unlimited Holo Rare PSA 8 Pokemon 1999",
        "PSA 8 Charizard 4/102 Base Set Holo WOTC",
    ), 520, 0.17, 20, 680, 6.0),
    Product("umbreon vmax alt art psa 10", (
        "Umbreon VMAX 215/203 Evolving Skies Alt Art PSA 10 GEM MINT",
        "PSA 10 Umbreon VMAX Alternate Art Evolving Skies Moonbreon",
    ), 1350, 0.15, 25, 1850, 8.0),
    Product("game boy color atomic purple", (
        "Nintendo Game Boy Color Atomic Purple CGB-001 Tested Working",
        "Game Boy Color Atomic Purple Handheld Console w/ Battery Cover",
        "GBC Atomic Purple Console Clean Screen",
    ), 68, 0.24, 25, 125, 4.5),
    Product("seiko skx007", (
        "Seiko SKX007 Diver 200m Automatic Black Dial Jubilee",
        "Seiko SKX007K2 Automatic Divers Watch 7S26",
    ), 240, 0.16, 30, 340, 7.0),
    Product("lego 75192 millennium falcon", (
        "LEGO Star Wars UCS 75192 Millennium Falcon NEW SEALED",
        "LEGO 75192 Ultimate Collector Series Millennium Falcon Sealed Box",
    ), 700, 0.12, 60, 1020, 9.0),
    Product("nintendo 64 console boxed", (
        "Nintendo 64 N64 Console Boxed Complete w/ Controller",
        "N64 Console CIB Original Box Inserts Charcoal",
    ), 150, 0.22, 45, 245, 5.5),
)
PRODUCT_BY_KW = {p.keyword: p for p in PRODUCTS}


def _rng(*parts) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _demo_id(*parts) -> str:
    digest = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:14], 16)
    return f"4{digest % 10**11:011d}"


def _sgt_hour(ts: float) -> float:
    return (ts / HOUR + 8) % 24


def _end_time_multiplier(end_time: float) -> float:
    h = _sgt_hour(end_time)
    mult = 1.0
    if 14 <= h < 20:      # 1am-7am US Eastern: fewer bidders awake
        mult = 0.86
    elif 8 <= h < 12:     # US prime time
        mult = 1.06
    if time.gmtime(end_time - 5 * HOUR).tm_wday == 6:  # Sunday evening in the US
        mult *= 1.05
    return mult


@dataclass
class Auction:
    legacy_id: str
    keyword: str
    title: str
    seller: Seller
    start_price: float
    final_price: float
    shipping: float
    start_time: float
    end_time: float
    bid_times: list[float] = field(default_factory=list)
    bid_prices: list[float] = field(default_factory=list)

    def state(self, t: float) -> tuple[float, int]:
        idx = bisect.bisect_right(self.bid_times, min(t, self.end_time))
        price = self.start_price if idx == 0 else self.bid_prices[idx - 1]
        return round(price, 2), idx

    def to_item(self, t: float) -> dict:
        price, bids = self.state(t)
        return {
            "item_id": f"v1|{self.legacy_id}|0",
            "legacy_id": self.legacy_id,
            "title": self.title,
            "url": DEMO_URL.format(self.legacy_id),
            "image": None,
            "seller": self.seller.name,
            "seller_feedback": self.seller.feedback,
            "price": price,
            "currency": "USD",
            "shipping": self.shipping,
            "bid_count": bids,
            "end_time": self.end_time,
            "condition": "Used",
            "location": "US",
            "buying_options": ["AUCTION"],
            "ended": t >= self.end_time,
            "demo": True,
        }


def build_auction(legacy_id: str, keyword: str, end_time: float) -> Auction:
    rng = _rng("auction", legacy_id)
    product = PRODUCT_BY_KW.get(keyword) or rng.choice(PRODUCTS)
    seller = rng.choice(SELLERS)
    duration = rng.choice((3, 5, 7, 7, 7, 10)) * DAY
    start_time = end_time - duration
    final = product.base_usd * math.exp(rng.gauss(0, product.sigma)) * seller.premium * _end_time_multiplier(end_time)
    final = round(final, 2)
    start = min(rng.choice((0.99, 0.99, 9.99, round(final * rng.uniform(0.2, 0.6)))), final * 0.8)
    n = max(3, min(70, int(3 + math.log(final / start + 1) * rng.uniform(3, 6))))
    snipe = max(0.05, min(0.7, seller.snipe + rng.gauss(0, 0.06)))
    k = max(1, round(n * snipe * 0.5))
    m = n - k
    early_times = sorted(start_time + rng.betavariate(2.4, 1.0) * (duration - 1800) for _ in range(m))
    late_times = sorted(end_time - rng.uniform(2, 180) for _ in range(k))
    pre_snipe = max(start, final * (1 - snipe * rng.uniform(0.75, 1.15)))
    early_prices = [start + (pre_snipe - start) * ((j + 1) / m) ** 1.5 for j in range(m)]
    late_prices = [pre_snipe + (final - pre_snipe) * ((j + 1) / k) for j in range(k)]
    return Auction(
        legacy_id=legacy_id,
        keyword=product.keyword,
        title=rng.choice(product.titles),
        seller=seller,
        start_price=round(start, 2),
        final_price=final,
        shipping=round(product.ship_usd * rng.uniform(0.8, 1.3), 2),
        start_time=start_time,
        end_time=end_time,
        bid_times=early_times + late_times,
        bid_prices=[round(p, 2) for p in early_prices + late_prices],
    )


def _slot_end(product: Product, k: int) -> float:
    cad = product.cadence_h * HOUR
    return ANCHOR + k * cad + _rng("slot-jitter", product.keyword, k).uniform(0, cad * 0.8)


def _slots_between(product: Product, t0: float, t1: float):
    cad = product.cadence_h * HOUR
    for k in range(int((t0 - ANCHOR) // cad) - 1, int((t1 - ANCHOR) // cad) + 1):
        end = _slot_end(product, k)
        if t0 <= end < t1:
            yield _demo_id("slot", product.keyword, k), end


class DemoProvider:
    name = "demo"

    def __init__(self) -> None:
        self._cache: dict[str, Auction] = {}
        self._index: dict[str, tuple[str, float]] = {}

    def _auction(self, legacy_id: str, keyword: str | None = None, end_time: float | None = None) -> Auction:
        if legacy_id in self._cache:
            return self._cache[legacy_id]
        if keyword is None or end_time is None:
            keyword, end_time = self._locate(legacy_id)
        auction = build_auction(legacy_id, keyword, end_time)
        self._cache[legacy_id] = auction
        return auction

    def _locate(self, legacy_id: str) -> tuple[str, float]:
        if legacy_id in self._index:
            return self._index[legacy_id]
        now = time.time()
        for p in PRODUCTS:
            for lid, end in _slots_between(p, now - 60 * DAY, now + 12 * DAY):
                self._index[lid] = (p.keyword, end)
        if legacy_id in self._index:
            return self._index[legacy_id]
        # An id we never generated (e.g. a pasted real link): invent a stable auction for it.
        registry = db.kv_get("demo_registry", {})
        if legacy_id not in registry:
            rng = _rng("pasted", legacy_id)
            registry[legacy_id] = [rng.choice(PRODUCTS).keyword, now + rng.uniform(3 * HOUR, 3 * DAY)]
            db.kv_set("demo_registry", registry)
        kw, end = registry[legacy_id]
        self._index[legacy_id] = (kw, end)
        return kw, end

    @staticmethod
    def calls_today() -> int:
        return 0

    async def search_auctions(self, keyword: str, *, marketplace=None, category_ids=None, limit=None) -> list[dict]:
        now = time.time()
        product = PRODUCT_BY_KW.get(keyword.lower().strip())
        if product is None:
            # Unknown keyword: borrow the closest product so the demo still returns something.
            words = set(keyword.lower().split())
            product = max(PRODUCTS, key=lambda p: len(words & set(p.keyword.split())))
        out = []
        for lid, end in _slots_between(product, now, now + 7 * DAY):
            a = self._auction(lid, product.keyword, end)
            if a.start_time <= now:
                out.append(a.to_item(now))
        return out[: limit or 200]

    async def get_by_legacy(self, legacy_id: str) -> dict | None:
        return self._auction(legacy_id).to_item(time.time())

    async def get_items(self, refs: list[dict]) -> dict[str, dict | None]:
        now = time.time()
        return {r["legacy_id"]: self._auction(r["legacy_id"]).to_item(now) for r in refs}

    # -- seeding ----------------------------------------------------------
    def seed(self) -> None:
        """Populate an empty database with a price list, tracked history and live tracked auctions."""
        if db.one("SELECT 1 FROM watchlist LIMIT 1"):
            return
        now = time.time()
        watch_ids = {}
        for p in PRODUCTS:
            watch_ids[p.keyword] = db.execute(
                "INSERT INTO watchlist (keyword, target_sgd, auto_track, active, created_at) VALUES (?,?,?,?,?)",
                [p.keyword, p.target_sgd, 1, 1, now - 30 * DAY],
            )
        rng = _rng("seed-history")
        for p in PRODUCTS:
            # Completed auctions we "tracked" over the last three weeks.
            for lid, end in _slots_between(p, now - 21 * DAY, now - HOUR):
                if rng.random() < 0.3:
                    self._seed_tracked(self._auction(lid, p.keyword, end), watch_ids[p.keyword], p.target_sgd,
                                       track_hours=rng.uniform(18, 60), now=now)
            # A few live auctions already being followed.
            live = [x for x in _slots_between(p, now + 2 * HOUR, now + 2 * DAY)][:2]
            for lid, end in live:
                a = self._auction(lid, p.keyword, end)
                if a.start_time < now:
                    self._seed_tracked(a, watch_ids[p.keyword], p.target_sgd, track_hours=rng.uniform(6, 30), now=now)

    def _seed_tracked(self, a: Auction, watch_id: int, target: float, track_hours: float, now: float) -> None:
        begin = max(a.start_time, a.end_time - track_hours * HOUR)
        stop = min(now, a.end_time)
        ended = a.end_time <= now
        price0, bids0 = a.state(begin)
        tid = db.execute(
            """INSERT INTO tracked (item_id, legacy_id, url, title, image, seller, seller_feedback, keyword,
               watchlist_id, target_sgd, currency, start_price, end_time, status, source, added_at, demo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            [f"v1|{a.legacy_id}|0", a.legacy_id, DEMO_URL.format(a.legacy_id), a.title, None, a.seller.name,
             a.seller.feedback, a.keyword, watch_id, target, "USD", price0, a.end_time, "active", "demo-seed", begin],
        )
        rows = []
        t = begin
        while t <= stop:
            price, bids = a.state(t)
            rows.append((tid, t, price, fx.to_sgd(price, "USD"), bids, max(0.0, a.end_time - t)))
            t += 300
        if ended:  # the post-end capture the live tracker also performs
            price, bids = a.state(a.end_time)
            rows.append((tid, a.end_time + 60, price, fx.to_sgd(price, "USD"), bids, 0.0))
        db.executemany(
            "INSERT INTO snapshots (tracked_id, ts, price, price_sgd, bid_count, seconds_left) VALUES (?,?,?,?,?,?)", rows
        )
        last_price, last_bids = a.state(stop)
        update = {
            "current_price": last_price,
            "current_sgd": fx.to_sgd(last_price, "USD"),
            "shipping_sgd": fx.to_sgd(a.shipping, "USD"),
            "bid_count": last_bids,
            "last_polled_at": stop,
        }
        if ended:
            update.update(status="ended", final_price=a.final_price, final_sgd=fx.to_sgd(a.final_price, "USD"),
                          ended_at=a.end_time + 60, alerts_sent="[60, 15, 5]")
        db.update("tracked", tid, update)
