"""App entry point: FastAPI + the background schedules + the Telegram poller."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, fx, ml, scanner, tracker
from . import provider as providers
from .api import router
from .config import ROOT, settings
from .telegram_bot import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("carnival")


def _first_scan_at() -> datetime:
    last = db.one("SELECT MAX(started_at) AS t FROM scan_runs")["t"]
    due = (last or 0) + settings.scan_interval_hours * 3600
    return datetime.fromtimestamp(max(due, time.time() + 15), tz=timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    await fx.refresh()
    if settings.demo_mode:
        log.warning("DEMO MODE: no eBay keys configured, serving simulated auctions")
        providers.get().seed()
    await asyncio.to_thread(ml.ensure_trained)

    sched = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600})
    now = datetime.now(timezone.utc)
    sched.add_job(scanner.run_scan, "interval", hours=settings.scan_interval_hours, id="scan",
                  next_run_time=_first_scan_at())
    sched.add_job(tracker.poll, "interval", minutes=settings.track_interval_minutes, id="track",
                  next_run_time=now + timedelta(seconds=8))
    sched.add_job(tracker.end_watch, "interval", seconds=20, id="end_watch", next_run_time=now + timedelta(seconds=4))
    sched.add_job(fx.refresh, "interval", hours=1, id="fx")
    sched.start()
    app.state.scheduler = sched
    poller = asyncio.create_task(bot.run())
    log.info("CARNIVAL up on http://%s:%s (mode=%s, telegram=%s)", settings.host, settings.port,
             "demo" if settings.demo_mode else "live", "on" if bot.token else "off")
    try:
        yield
    finally:
        poller.cancel()
        sched.shutdown(wait=False)


app = FastAPI(title="CARNIVAL auction intel", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
