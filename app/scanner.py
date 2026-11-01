"""Keyword scan: pull live auctions for every price-list row and tally them against the target (landed SGD).

Verdicts
  deal  - the model gives at least a 60% chance the final landed cost stays at or under target
          (before the model has data: the current landed cost is under target with <24h left)
  watch - currently under target, but more likely than not to finish above it
  over  - already above target
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import db, fx, ml, tracker
from . import provider as providers
from .config import settings
from .telegram_bot import demo_tag, esc, fmt_left, money, notify

log = logging.getLogger("carnival.scanner")

MAX_SIGNALS_PER_SCAN = 8
_lock = asyncio.Lock()


def tally(item: dict, w: dict, now: float) -> tuple[dict, dict | None]:
    price_sgd = fx.to_sgd(item["price"], item["currency"])
    ship_sgd = fx.to_sgd(item["shipping"], item["currency"]) if item["shipping"] is not None else None
    landed_now = ml.landed_sgd(price_sgd, ship_sgd)
    target = w["target_sgd"]
    pred = ml.predict(price=item["price"], currency=item["currency"], seconds_left=item["end_time"] - now,
                      seller=item["seller"], keyword=w["keyword"], bid_count=item["bid_count"],
                      end_time=item["end_time"], shipping_sgd=ship_sgd, target_sgd=target)
    predicted_landed = pred["landed_p50_sgd"] if pred else None
    if landed_now is None or landed_now > target:
        verdict = "over"
    elif pred and "prob_under_target" in pred:
        verdict = "deal" if pred["prob_under_target"] >= ml.DEAL_PROBABILITY else "watch"
    else:
        verdict = "deal" if item["end_time"] - now <= 24 * 3600 else "watch"
    basis = predicted_landed if predicted_landed is not None else landed_now
    row = {
        "item_id": item["item_id"],
        "legacy_id": item["legacy_id"],
        "watchlist_id": w["id"],
        "title": item["title"],
        "url": item["url"],
        "image": item["image"],
        "seller": item["seller"],
        "seller_feedback": item["seller_feedback"],
        "price": item["price"],
        "currency": item["currency"],
        "shipping": item["shipping"],
        "price_sgd": price_sgd,
        "shipping_sgd": ship_sgd,
        "landed_sgd": landed_now,
        "bid_count": item["bid_count"],
        "end_time": item["end_time"],
        "condition": item["condition"],
        "location": item["location"],
        "verdict": verdict,
        "delta_pct": round((basis / target - 1) * 100, 1) if (basis and target) else None,
        "pred_landed_sgd": predicted_landed,
        "prob_under": pred.get("prob_under_target") if pred else None,
        "last_seen": now,
    }
    return row, pred


async def run_scan(trigger: str = "schedule") -> dict:
    if _lock.locked():
        return {"skipped": "a scan is already running"}
    async with _lock:
        started = time.time()
        run_id = db.execute("INSERT INTO scan_runs (started_at, trigger) VALUES (?,?)", [started, trigger])
        await fx.refresh()
        await asyncio.to_thread(ml.ensure_trained)
        provider = providers.get()
        watch = db.query("SELECT * FROM watchlist WHERE active=1 ORDER BY id")
        counts = {"items": 0, "deal": 0, "watch": 0}
        fresh: list[tuple[dict, dict | None, dict]] = []
        bootstrap: list[tuple[dict, dict | None, dict]] = []
        bootstrap_full = False
        errors = []
        for w in watch:
            try:
                items = await provider.search_auctions(w["keyword"], marketplace=w["marketplace"],
                                                       category_ids=w["category_ids"])
            except Exception as exc:  # noqa: BLE001 - one bad keyword shouldn't sink the scan
                log.warning("Scan of '%s' failed: %s", w["keyword"], exc)
                errors.append(f"{w['keyword']}: {exc}")
                continue
            now = time.time()
            for item in items:
                if not item["end_time"] or item["end_time"] <= now or item["price"] is None:
                    continue
                row, pred = tally(item, w, now)
                prev = db.one("SELECT first_seen, signalled_at, ignored FROM listings WHERE item_id=?", [row["item_id"]])
                row["first_seen"] = prev["first_seen"] if prev else now
                db.upsert("listings", row, "item_id")
                counts["items"] += 1
                if row["verdict"] in counts:
                    counts[row["verdict"]] += 1
                if row["verdict"] == "deal" and not (prev and (prev["signalled_at"] or prev["ignored"])):
                    fresh.append((row, pred, w))
                elif (row["verdict"] == "watch" and not bootstrap_full and not ml.current().get("active")
                      and row["end_time"] - now < 36 * 3600 and not prev):
                    # Cold start: follow a few under-target auctions to completion so the model gets data.
                    bootstrap.append((row, pred, w))
                    bootstrap_full = len(bootstrap) >= 3 * len(watch)
            db.update("watchlist", w["id"], {"last_scan_at": time.time()})

        await _auto_track(fresh + bootstrap)
        await _signal(fresh, counts, len(watch), errors)
        db.update("scan_runs", run_id, {"finished_at": time.time(), "keywords": len(watch), "items": counts["items"],
                                        "deals": counts["deal"], "near": counts["watch"],
                                        "error": "; ".join(errors) or None})
        log.info("Scan done: %s", counts)
        return {"run_id": run_id, **counts, "errors": errors}


async def _auto_track(fresh) -> None:
    horizon = time.time() + settings.auto_track_hours * 3600
    for row, _pred, w in sorted(fresh, key=lambda x: x[0]["end_time"]):
        if not w["auto_track"] or row["end_time"] > horizon:
            continue
        try:
            await tracker.add(row["legacy_id"], keyword=w["keyword"], watchlist_id=w["id"],
                              target_sgd=w["target_sgd"], source="auto")
        except ValueError as exc:
            log.info("Auto-track skipped: %s", exc)
            break


async def _signal(fresh, counts: dict, keywords: int, errors: list[str]) -> None:
    now = time.time()
    if fresh or errors:
        summary = (f"📡 <b>SCAN COMPLETE</b>{demo_tag()}\n{keywords} keywords · {counts['items']} live auctions · "
                   f"<b>{counts['deal']}</b> deals · {counts['watch']} watch")
        if errors:
            summary += "\n⚠️ " + esc("; ".join(errors)[:300])
        await notify("scan", "Scan complete", summary)
    for row, pred, w in sorted(fresh, key=lambda x: x[0]["delta_pct"] or 0)[:MAX_SIGNALS_PER_SCAN]:
        lines = [
            f"🟢 <b>DEAL</b> · {esc(w['keyword'])}{demo_tag()}",
            f"<b>{esc(row['title'])}</b>",
            f"Seller {esc(row['seller'])} ({row['seller_feedback'] or 0}) · {row['bid_count']} bids · "
            f"ends {fmt_left(row['end_time'])}",
            f"Now {money(row['price'], row['currency'])} · landed S${row['landed_sgd']:,.0f} · "
            f"target S${w['target_sgd']:,.0f}",
        ]
        if pred:
            lines.append(f"Predicted final landed S${pred['landed_p50_sgd']:,.0f} "
                         f"({row['delta_pct']:+.0f}% vs target) · {pred.get('prob_under_target', 0) * 100:.0f}% "
                         "chance it closes under")
        buttons = [[{"text": "Track", "callback_data": f"trk:{row['legacy_id']}"},
                    {"text": "Ignore", "callback_data": f"ign:{row['legacy_id']}"}]]
        if not settings.demo_mode:
            buttons[0].insert(0, {"text": "Open", "url": row["url"]})
        await notify("deal", row["title"], "\n".join(lines), ref=row["legacy_id"], buttons=buttons)
        db.execute("UPDATE listings SET signalled_at=? WHERE item_id=?", [now, row["item_id"]])
