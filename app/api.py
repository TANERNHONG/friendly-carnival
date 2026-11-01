"""JSON API consumed by the dashboard."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, fx, ml, scanner, tracker
from . import provider as providers
from .config import settings
from .telegram_bot import bot, demo_tag, notify

router = APIRouter(prefix="/api")


class WatchIn(BaseModel):
    keyword: str = Field(min_length=2, max_length=200)
    target_sgd: float = Field(gt=0)
    marketplace: str | None = None
    category_ids: str | None = None
    auto_track: bool = True
    active: bool = True
    notes: str | None = None


class WatchPatch(BaseModel):
    keyword: str | None = None
    target_sgd: float | None = Field(default=None, gt=0)
    marketplace: str | None = None
    category_ids: str | None = None
    auto_track: bool | None = None
    active: bool | None = None
    notes: str | None = None


class TrackIn(BaseModel):
    url: str = Field(min_length=6, max_length=2000)
    target_sgd: float | None = Field(default=None, gt=0)


class TrackPatch(BaseModel):
    target_sgd: float | None = Field(default=None, gt=0)


def _job_next(request: Request, job_id: str) -> float | None:
    job = request.app.state.scheduler.get_job(job_id)
    return job.next_run_time.timestamp() if job and job.next_run_time else None


@router.get("/status")
def status(request: Request) -> dict:
    now = time.time()
    state = ml.current()
    counts = db.one(
        """SELECT
             (SELECT COUNT(*) FROM watchlist WHERE active=1) AS watchlist,
             (SELECT COUNT(*) FROM listings WHERE end_time > :now) AS live_listings,
             (SELECT COUNT(*) FROM listings WHERE end_time > :now AND verdict='deal' AND ignored=0) AS deals,
             (SELECT COUNT(*) FROM listings WHERE end_time > :now AND verdict='watch' AND ignored=0) AS watch,
             (SELECT COUNT(*) FROM tracked WHERE status='active') AS tracked_active,
             (SELECT COUNT(*) FROM tracked WHERE status='active' AND end_time < :hour) AS ending_1h,
             (SELECT COUNT(*) FROM tracked WHERE status='ended') AS completed""",
        {"now": now, "hour": now + 3600},
    )
    last_scan = db.one("SELECT * FROM scan_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
    active = state.get("active")
    return {
        "now": now,
        "mode": "demo" if settings.demo_mode else "live",
        "marketplace": settings.ebay_marketplace,
        "ship_to": settings.ship_to_country,
        "import_tax_pct": settings.import_tax_pct,
        "near_pct": settings.near_pct,
        "alert_minutes": settings.alert_minutes,
        "schedule": {
            "scan_every_h": settings.scan_interval_hours,
            "track_every_min": settings.track_interval_minutes,
            "next_scan": _job_next(request, "scan"),
            "next_track": _job_next(request, "track"),
            "last_scan": last_scan,
        },
        "telegram": bot.status(),
        "counts": counts,
        "fx": fx.status(),
        "ebay_calls_today": providers.get().calls_today(),
        "ebay_call_budget": settings.ebay_daily_call_budget,
        "model": {"active": active, "n": state["book"].n if state.get("book") else 0,
                  "metrics": (state.get("metrics") or {}).get(active or "", {}), "min_gbm": ml.MIN_GBM_N},
    }


# -- price list ------------------------------------------------------------------
@router.get("/watchlist")
def watchlist() -> list[dict]:
    now = time.time()
    return db.query(
        """SELECT w.*,
             (SELECT COUNT(*) FROM listings l WHERE l.watchlist_id=w.id AND l.end_time > ?) AS live,
             (SELECT COUNT(*) FROM listings l WHERE l.watchlist_id=w.id AND l.end_time > ? AND l.verdict='deal') AS deals,
             (SELECT COUNT(*) FROM tracked t WHERE t.watchlist_id=w.id AND t.status='ended') AS completed
           FROM watchlist w ORDER BY w.id""",
        [now, now],
    )


@router.post("/watchlist")
def watch_add(body: WatchIn) -> dict:
    wid = db.execute(
        """INSERT INTO watchlist (keyword, target_sgd, marketplace, category_ids, auto_track, active, notes, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        [body.keyword.strip(), body.target_sgd, body.marketplace or None, body.category_ids or None,
         int(body.auto_track), int(body.active), body.notes, time.time()],
    )
    return db.one("SELECT * FROM watchlist WHERE id=?", [wid])


@router.patch("/watchlist/{wid}")
def watch_patch(wid: int, body: WatchPatch) -> dict:
    data = {k: (int(v) if isinstance(v, bool) else v) for k, v in body.model_dump(exclude_unset=True).items()}
    db.update("watchlist", wid, data)
    if "target_sgd" in data:
        db.execute("UPDATE tracked SET target_sgd=? WHERE watchlist_id=? AND status='active'", [data["target_sgd"], wid])
    row = db.one("SELECT * FROM watchlist WHERE id=?", [wid])
    if not row:
        raise HTTPException(404, "not found")
    return row


@router.delete("/watchlist/{wid}")
def watch_delete(wid: int) -> dict:
    db.execute("DELETE FROM watchlist WHERE id=?", [wid])
    return {"ok": True}


# -- scan results ------------------------------------------------------------------
@router.get("/listings")
def listings(verdict: str | None = None, watchlist_id: int | None = None, limit: int = 300) -> list[dict]:
    sql = """SELECT l.*, w.keyword, w.target_sgd,
                    EXISTS(SELECT 1 FROM tracked t WHERE t.legacy_id=l.legacy_id AND t.status='active') AS tracked
             FROM listings l LEFT JOIN watchlist w ON w.id=l.watchlist_id
             WHERE l.end_time > ? AND l.ignored=0"""
    args: list = [time.time()]
    if verdict:
        verdicts = [v for v in verdict.split(",") if v in ("deal", "watch", "over")]
        sql += f" AND l.verdict IN ({','.join('?' * len(verdicts))})"
        args += verdicts
    if watchlist_id:
        sql += " AND l.watchlist_id=?"
        args.append(watchlist_id)
    sql += " ORDER BY l.end_time LIMIT ?"
    args.append(min(limit, 1000))
    return db.query(sql, args)


@router.post("/listings/{legacy_id}/ignore")
def listing_ignore(legacy_id: str) -> dict:
    db.execute("UPDATE listings SET ignored=1 WHERE legacy_id=?", [legacy_id])
    return {"ok": True}


@router.post("/scan")
async def scan_now() -> dict:
    return await scanner.run_scan("manual")


# -- tracked auctions --------------------------------------------------------------------
def _decorate(r: dict, now: float, with_prediction: bool = True) -> dict:
    r["landed_sgd"] = ml.landed_sgd(r["current_sgd"], r["shipping_sgd"])
    r["final_landed_sgd"] = ml.landed_sgd(r["final_sgd"], r["shipping_sgd"]) if r["final_sgd"] else None
    r["max_item_sgd"] = ml.max_item_price_sgd(r["target_sgd"], r["shipping_sgd"])
    if with_prediction and r["status"] == "active":
        r["prediction"] = ml.predict(
            price=r["current_price"], currency=r["currency"], seconds_left=(r["end_time"] or now) - now,
            seller=r["seller"], keyword=r["keyword"], bid_count=r["bid_count"], end_time=r["end_time"],
            shipping_sgd=r["shipping_sgd"], target_sgd=r["target_sgd"], recent=ml.series_for(r["id"]))
    return r


@router.get("/tracked")
def tracked(status: str = "active", limit: int = 200) -> list[dict]:
    now = time.time()
    if status == "all":
        rows = db.query("SELECT * FROM tracked WHERE status!='removed' ORDER BY end_time DESC LIMIT ?", [limit])
    elif status == "ended":
        rows = db.query("SELECT * FROM tracked WHERE status='ended' ORDER BY end_time DESC LIMIT ?", [limit])
    else:
        rows = db.query("SELECT * FROM tracked WHERE status='active' ORDER BY end_time LIMIT ?", [limit])
    out = []
    for r in rows:
        spark = db.query(
            "SELECT price_sgd FROM snapshots WHERE tracked_id=? ORDER BY ts DESC LIMIT 288", [r["id"]])
        pts = [s["price_sgd"] for s in reversed(spark) if s["price_sgd"] is not None]
        step = max(1, len(pts) // 32)
        r["spark"] = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
        out.append(_decorate(r, now, with_prediction=(status == "active")))
    return out


@router.post("/tracked")
async def track_add(body: TrackIn) -> dict:
    try:
        row = await tracker.add(body.url, target_sgd=body.target_sgd, source="dashboard")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _decorate(row, time.time())


@router.get("/tracked/{tid}")
def tracked_detail(tid: int) -> dict:
    r = db.one("SELECT * FROM tracked WHERE id=?", [tid])
    if not r:
        raise HTTPException(404, "not found")
    r = _decorate(r, time.time())
    r["snapshots"] = db.query(
        "SELECT ts, price, price_sgd, bid_count, seconds_left FROM snapshots WHERE tracked_id=? ORDER BY ts", [tid])
    seller_hist = db.one(
        "SELECT COUNT(*) AS n, AVG(bid_count) AS avg_bids FROM tracked WHERE seller=? AND status='ended'", [r["seller"]])
    r["seller_history"] = seller_hist
    return r


@router.patch("/tracked/{tid}")
def tracked_patch(tid: int, body: TrackPatch) -> dict:
    db.update("tracked", tid, body.model_dump(exclude_unset=True))
    return tracked_detail(tid)


@router.post("/tracked/{tid}/poll")
async def tracked_poll(tid: int) -> dict:
    await tracker.poll([tid])
    return tracked_detail(tid)


@router.delete("/tracked/{tid}")
def tracked_delete(tid: int) -> dict:
    tracker.remove(tid)
    return {"ok": True}


# -- analytics & signals ------------------------------------------------------------------
@router.get("/analytics")
def analytics(seller: str | None = None, keyword: str | None = None) -> dict:
    return ml.analytics(seller=seller or None, keyword=keyword or None)


@router.post("/model/retrain")
def retrain() -> dict:
    state = ml.ensure_trained(force=True)
    return {"active": state.get("active"), "metrics": state.get("metrics")}


@router.get("/signals")
def signals(limit: int = 60) -> list[dict]:
    return db.query("SELECT * FROM signals ORDER BY ts DESC LIMIT ?", [min(limit, 500)])


@router.post("/telegram/test")
async def telegram_test() -> dict:
    await notify("test", "Test signal", f"✅ <b>CARNIVAL is connected</b>{demo_tag()}\nSignals will arrive here.")
    last = db.one("SELECT * FROM signals ORDER BY id DESC LIMIT 1")
    return {"delivered": bool(last and last["delivered"]), "error": last and last["error"], "status": bot.status()}
