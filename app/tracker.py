"""Follows individual auctions: a snapshot every few minutes until the item sells, plus ending alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import db, fx, ml
from . import provider as providers
from .config import settings
from .ebay import resolve_item_ref
from .telegram_bot import demo_tag, esc, fmt_left, money, notify

log = logging.getLogger("carnival.tracker")

LAST_LOOK_S = 90      # extra poll just before the end, to catch the pre-snipe price
FINAL_CAPTURE_S = 45  # poll this long after the end to record the final price


def _match_watchlist(title: str) -> dict | None:
    title_l = title.lower()
    best = None
    for w in db.query("SELECT * FROM watchlist"):
        words = [x for x in w["keyword"].lower().split() if not x.startswith("-")]
        if words and all(x in title_l for x in words):
            if best is None or len(words) > len(best["keyword"].split()):
                best = w
    return best


def _snapshot(tracked_id: int, item: dict, now: float) -> None:
    db.execute(
        "INSERT INTO snapshots (tracked_id, ts, price, price_sgd, bid_count, seconds_left) VALUES (?,?,?,?,?,?)",
        [tracked_id, now, item["price"], fx.to_sgd(item["price"], item["currency"]), item["bid_count"],
         max(0.0, (item["end_time"] or now) - now)],
    )


async def add(ref: str, *, keyword: str | None = None, watchlist_id: int | None = None,
              target_sgd: float | None = None, source: str = "manual") -> dict:
    legacy = await resolve_item_ref(ref)
    if not legacy:
        raise ValueError("no eBay item number found in that link")
    existing = db.one("SELECT * FROM tracked WHERE legacy_id=?", [legacy])
    if existing:
        if existing["status"] == "removed":
            db.update("tracked", existing["id"], {"status": "active"})
            existing["status"] = "active"
        return existing
    active = db.one("SELECT COUNT(*) AS n FROM tracked WHERE status='active'")["n"]
    if active >= settings.max_active_tracked:
        raise ValueError(f"already tracking {active} auctions (MAX_ACTIVE_TRACKED)")
    await fx.refresh()
    item = await providers.get().get_by_legacy(legacy)
    if not item:
        raise ValueError(f"eBay returned no live listing for item {legacy}")
    if item["ended"]:
        raise ValueError("that listing has already ended")
    if target_sgd is None:
        w = _match_watchlist(item["title"])
        if w:
            keyword, watchlist_id, target_sgd = w["keyword"], w["id"], w["target_sgd"]
    now = time.time()
    tid = db.execute(
        """INSERT INTO tracked (item_id, legacy_id, url, title, image, seller, seller_feedback, keyword, watchlist_id,
           target_sgd, currency, start_price, current_price, current_sgd, shipping_sgd, bid_count, end_time, status,
           source, added_at, last_polled_at, demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",
        [item["item_id"], legacy, item["url"], item["title"], item["image"], item["seller"], item["seller_feedback"],
         keyword or "unassigned", watchlist_id, target_sgd, item["currency"], item["price"], item["price"],
         fx.to_sgd(item["price"], item["currency"]), fx.to_sgd(item["shipping"], item["currency"]),
         item["bid_count"], item["end_time"], source, now, now, 1 if item.get("demo") else 0],
    )
    _snapshot(tid, item, now)
    log.info("Tracking #%s %s", tid, item["title"])
    return db.one("SELECT * FROM tracked WHERE id=?", [tid])


def remove(tracked_id: int) -> None:
    db.execute("UPDATE tracked SET status='removed' WHERE id=? AND status='active'", [tracked_id])


async def poll(ids: list[int] | None = None) -> int:
    """Snapshot every active tracked auction (or just `ids`). Returns how many ended this round."""
    rows = db.query("SELECT * FROM tracked WHERE status='active'")
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    if not rows:
        return 0
    await fx.refresh()
    items = await providers.get().get_items([{"item_id": r["item_id"], "legacy_id": r["legacy_id"]} for r in rows])
    now = time.time()
    ended = 0
    for r in rows:
        item = items.get(r["legacy_id"])
        if item and item["price"] is not None:
            _snapshot(r["id"], item, now)
            db.update("tracked", r["id"], {
                "current_price": item["price"],
                "current_sgd": fx.to_sgd(item["price"], item["currency"]),
                "bid_count": item["bid_count"],
                "end_time": item["end_time"] or r["end_time"],
                "last_polled_at": now,
                "error": None,
            })
        elif r["end_time"] and now < r["end_time"]:
            db.update("tracked", r["id"], {"error": "lookup failed", "last_polled_at": now})
        end = (item or {}).get("end_time") or r["end_time"]
        if end and now >= end + FINAL_CAPTURE_S:
            await _finalise(r["id"], item)
            ended += 1
    if ended:
        await asyncio.to_thread(ml.ensure_trained)
    return ended


async def _finalise(tracked_id: int, item: dict | None) -> None:
    r = db.one("SELECT * FROM tracked WHERE id=?", [tracked_id])
    final = (item or {}).get("price") or r["current_price"]
    final_sgd = fx.to_sgd(final, r["currency"])
    db.update("tracked", tracked_id, {"status": "ended", "final_price": final, "final_sgd": final_sgd,
                                       "ended_at": time.time()})
    landed = ml.landed_sgd(final_sgd, r["shipping_sgd"])
    series = ml.series_for(tracked_id)
    hour_out = ""
    if series is not None:
        p1h, _ = series.at(3600)
        if p1h == p1h and p1h:
            hour_out = f"\n1h before the end it stood at {money(p1h, r['currency'])}: +{(final / p1h - 1) * 100:.0f}% after that."
    verdict = ""
    if r["target_sgd"] and landed is not None:
        verdict = (f"\nLanded S${landed:,.0f} vs target S${r['target_sgd']:,.0f}: "
                   + ("<b>under</b>" if landed <= r["target_sgd"] else "<b>over</b>"))
    await notify(
        "ended", r["title"],
        f"🏁 <b>SOLD / ENDED</b>{demo_tag()}\n{esc(r['title'])}\nSeller {esc(r['seller'])} · {r['bid_count'] or 0} bids\n"
        f"Final {money(final, r['currency'])} (S${final_sgd:,.0f}){verdict}{hour_out}\n"
        f"<i>Added to the playbook dataset.</i>",
        ref=str(tracked_id),
    )


def _prediction_for(r: dict, now: float) -> dict | None:
    return ml.predict(price=r["current_price"], currency=r["currency"], seconds_left=(r["end_time"] or now) - now,
                      seller=r["seller"], keyword=r["keyword"], bid_count=r["bid_count"], end_time=r["end_time"],
                      shipping_sgd=r["shipping_sgd"], target_sgd=r["target_sgd"], recent=ml.series_for(r["id"]))


async def end_watch() -> None:
    """Runs every ~20s: ending alerts, a last look before the end, and the final capture after it."""
    now = time.time()
    rows = db.query("SELECT * FROM tracked WHERE status='active' AND end_time IS NOT NULL AND end_time < ?",
                    [now + max(settings.alert_minutes or (60,)) * 60 + 60])
    for r in rows:
        secs = r["end_time"] - now
        sent = json.loads(r["alerts_sent"] or "[]")
        crossed = [m for m in settings.alert_minutes if secs <= m * 60 and m not in sent]
        if crossed and secs > 0:
            await _ending_alert(r, min(crossed), now)
            sent.extend(crossed)
        if secs <= LAST_LOOK_S and "last_look" not in sent and secs > 0:
            sent.append("last_look")
            db.update("tracked", r["id"], {"alerts_sent": json.dumps(sent)})
            await poll([r["id"]])
        if secs <= -FINAL_CAPTURE_S:
            await poll([r["id"]])
        db.execute("UPDATE tracked SET alerts_sent=? WHERE id=? AND status='active'", [json.dumps(sent), r["id"]])


async def _ending_alert(r: dict, minutes: int, now: float) -> None:
    pred = _prediction_for(r, now)
    landed_now = ml.landed_sgd(r["current_sgd"], r["shipping_sgd"])
    lines = [
        f"⏱ <b>ENDING {fmt_left(r['end_time']).upper()}</b>{demo_tag()}",
        f"<b>{esc(r['title'])}</b>",
        f"Seller {esc(r['seller'])} · {r['bid_count'] or 0} bids",
        f"Now {money(r['current_price'], r['currency'])} · landed S${landed_now or 0:,.0f}"
        + (f" · target S${r['target_sgd']:,.0f}" if r["target_sgd"] else ""),
    ]
    if pred:
        lines.append(f"Predicted final S${pred['p50_sgd']:,.0f} (80%: S${pred['p10_sgd']:,.0f}–{pred['p90_sgd']:,.0f}) "
                     f"· {pred['model']}, {esc(pred['scope'])}")
        if "max_bid" in pred:
            if pred["max_bid"] > (r["current_price"] or 0):
                lines.append(f"Max bid to stay on target: <b>{money(pred['max_bid'], r['currency'])}</b> "
                             f"(S${pred['max_bid_sgd']:,.0f}) · chance it closes under: {pred['prob_under_target'] * 100:.0f}%")
            else:
                lines.append("Already past your target: walk away or raise the price list.")
    buttons = [[{"text": "Stop tracking", "callback_data": f"unt:{r['id']}"}]]
    if not settings.demo_mode:
        buttons[0].insert(0, {"text": "Open listing", "url": r["url"]})
    await notify("ending", r["title"], "\n".join(lines), ref=str(r["id"]), buttons=buttons)
