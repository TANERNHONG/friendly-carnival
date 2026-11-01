"""Telegram bot: pushes signals and answers commands (long polling, no webhook needed)."""
from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

from . import db
from .config import settings

log = logging.getLogger("carnival.telegram")

HELP = """<b>CARNIVAL // auction intel</b>
/deals: live listings under your price list
/ending: tracked auctions ending in the next 24h
/list: everything being tracked
/track &lt;ebay link&gt;: follow an auction every 5 min
/untrack &lt;id&gt;: stop following
/scan: run the keyword scan now
/pricelist: show the price list
/add &lt;keyword&gt; = &lt;SGD&gt;: add a price list row
/remove &lt;id&gt;: delete a price list row"""


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


class TelegramBot:
    def __init__(self) -> None:
        self.token = settings.telegram_token
        self.username: str | None = None
        self.last_error: str | None = None
        self.connected = False
        self._offset = db.kv_get("telegram_offset", 0)

    @property
    def chat_id(self) -> str | None:
        return settings.telegram_chat_id or db.kv_get("telegram_chat_id")

    def status(self) -> dict:
        return {
            "enabled": bool(self.token),
            "connected": self.connected,
            "username": self.username,
            "chat_bound": bool(self.chat_id),
            "last_error": self.last_error,
        }

    async def call(self, method: str, timeout: float = 15, **payload) -> dict:
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            body = resp.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description") or f"HTTP {resp.status_code}")
            return body["result"]

    async def send(self, text: str, buttons: list[list[dict]] | None = None, chat_id: str | None = None) -> None:
        payload = {"chat_id": chat_id or self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **payload)

    # -- polling loop --------------------------------------------------------
    async def run(self) -> None:
        if not self.token:
            return
        backoff = 5
        while True:
            try:
                if not self.username:
                    me = await self.call("getMe")
                    self.username = me.get("username")
                    await self.call("setMyCommands", commands=[
                        {"command": c, "description": d} for c, d in (
                            ("deals", "Live listings under your price list"),
                            ("ending", "Tracked auctions ending in 24h"),
                            ("list", "Everything being tracked"),
                            ("track", "Follow an auction: /track <link>"),
                            ("scan", "Run the keyword scan now"),
                            ("pricelist", "Show the price list"),
                            ("help", "All commands"),
                        )])
                updates = await self.call("getUpdates", timeout=25, offset=self._offset,
                                          allowed_updates=["message", "callback_query"])
                self.connected, self.last_error, backoff = True, None, 5
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    db.kv_set("telegram_offset", self._offset)
                    try:
                        await self.handle(upd)
                    except Exception as exc:  # noqa: BLE001 - keep polling
                        log.exception("Telegram update failed")
                        self.last_error = str(exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - network hiccups, bad token, ...
                self.connected, self.last_error = False, str(exc)
                log.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def _authorised(self, chat_id: str) -> bool:
        if self.chat_id:
            return str(self.chat_id) == chat_id
        db.kv_set("telegram_chat_id", chat_id)  # first chat to /start claims the bot
        return True

    async def handle(self, upd: dict) -> None:
        from . import scanner, tracker  # local import: they import this module too

        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = str(cq["message"]["chat"]["id"])
            if not self._authorised(chat_id):
                return
            action, _, arg = (cq.get("data") or "").partition(":")
            note = ""
            if action == "trk":
                try:
                    row = await tracker.add(arg, source="telegram")
                    note = f"Tracking #{row['id']}: refresh every {settings.track_interval_minutes:g} min"
                except ValueError as exc:
                    note = str(exc)
            elif action == "ign":
                db.execute("UPDATE listings SET ignored=1 WHERE legacy_id=?", [arg])
                note = "Ignored"
            elif action == "unt":
                tracker.remove(int(arg))
                note = f"Stopped tracking #{arg}"
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
            return

        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text or not chat_id:
            return
        if not self._authorised(chat_id):
            await self.send("This bot is bound to another chat.", chat_id=chat_id)
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()

        if cmd in ("/start", "/help"):
            await self.send(HELP + ("\n\n<i>Running on DEMO data.</i>" if settings.demo_mode else ""))
        elif cmd == "/deals":
            await self.send(deals_text())
        elif cmd == "/ending":
            await self.send(tracked_text(within_hours=24))
        elif cmd == "/list":
            await self.send(tracked_text())
        elif cmd == "/track":
            if not arg:
                await self.send("Send <code>/track https://www.ebay.com/itm/…</code>")
                return
            try:
                row = await tracker.add(arg, source="telegram")
                await self.send(f"Tracking <b>#{row['id']}</b> {esc(row['title'])}\nEnds {fmt_left(row['end_time'])}. "
                                f"Refreshing every {settings.track_interval_minutes:g} min.")
            except ValueError as exc:
                await self.send(f"Couldn't track that: {esc(exc)}")
        elif cmd == "/untrack" and arg.isdigit():
            tracker.remove(int(arg))
            await self.send(f"Stopped tracking #{arg}.")
        elif cmd == "/scan":
            n = db.one("SELECT COUNT(*) AS n FROM watchlist WHERE active=1")["n"]
            await self.send(f"Scanning {n} keyword(s)…")
            asyncio.create_task(scanner.run_scan("telegram"))
        elif cmd == "/pricelist":
            rows = db.query("SELECT * FROM watchlist ORDER BY id")
            lines = [f"<code>{r['id']:>3}</code> {esc(r['keyword'])}: <b>S${r['target_sgd']:,.0f}</b>"
                     + ("" if r["active"] else " (paused)") for r in rows]
            await self.send("<b>Price list (landed SGD)</b>\n" + ("\n".join(lines) or "Empty. Use /add"))
        elif cmd == "/add" and "=" in arg:
            kw, _, price = arg.rpartition("=")
            try:
                value = float(price.replace("S$", "").replace("$", "").replace(",", "").strip())
            except ValueError:
                await self.send("Format: <code>/add seiko skx007 = 365</code>")
                return
            db.execute("INSERT INTO watchlist (keyword, target_sgd, auto_track, active, created_at) VALUES (?,?,1,1,?)",
                       [kw.strip(), value, time.time()])
            await self.send(f"Added <b>{esc(kw.strip())}</b> at S${value:,.0f}. It joins the next scan.")
        elif cmd == "/remove" and arg.isdigit():
            db.execute("DELETE FROM watchlist WHERE id=?", [int(arg)])
            await self.send(f"Removed price list row {arg}.")
        else:
            await self.send("Unknown command. /help lists them.")


bot = TelegramBot()


async def notify(kind: str, title: str, body: str, ref: str | None = None,
                 buttons: list[list[dict]] | None = None) -> None:
    """Record a signal in the feed and push it to Telegram when configured."""
    delivered, error = 0, None
    if bot.token and bot.chat_id:
        try:
            await bot.send(body, buttons)
            delivered = 1
        except Exception as exc:  # noqa: BLE001 - keep the signal in the feed regardless
            error = str(exc)
            log.warning("Telegram send failed: %s", exc)
    elif bot.token:
        error = "No chat bound yet: send /start to the bot"
    db.execute("INSERT INTO signals (ts, kind, ref, title, body, delivered, error) VALUES (?,?,?,?,?,?,?)",
               [time.time(), kind, ref, title, body, delivered, error])


# -- message builders ----------------------------------------------------------
def fmt_left(end_time: float | None) -> str:
    if not end_time:
        return "?"
    s = int(end_time - time.time())
    if s <= 0:
        return "ended"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"in {d}d {h}h" if d else (f"in {h}h {m}m" if h else f"in {m}m")


def money(v: float | None, cur: str | None) -> str:
    if v is None:
        return "–"
    sym = {"USD": "US$", "GBP": "£", "EUR": "€", "AUD": "A$", "SGD": "S$", "CAD": "C$", "JPY": "¥"}.get(cur or "", (cur or "") + " ")
    return f"{sym}{v:,.2f}"


def demo_tag() -> str:
    return " · <i>DEMO</i>" if settings.demo_mode else ""


def deals_text(limit: int = 10) -> str:
    rows = db.query(
        """SELECT * FROM listings WHERE end_time > ? AND verdict IN ('deal','watch') AND ignored=0
           ORDER BY CASE verdict WHEN 'deal' THEN 0 ELSE 1 END, end_time LIMIT ?""", [time.time(), limit])
    if not rows:
        return "No live listings under your price list right now."
    lines = ["<b>Under your price list</b>" + demo_tag()]
    for r in rows:
        tag = "🟢 DEAL" if r["verdict"] == "deal" else "🟡 WATCH"
        lines.append(f"{tag} <a href=\"{esc(r['url'])}\">{esc(r['title'][:70])}</a>\n"
                     f"   {money(r['price'], r['currency'])} · landed S${r['landed_sgd']:,.0f} · {r['bid_count']} bids · "
                     f"ends {fmt_left(r['end_time'])}")
    return "\n".join(lines)


def tracked_text(within_hours: float | None = None) -> str:
    sql = "SELECT * FROM tracked WHERE status='active'"
    args: list = []
    if within_hours:
        sql += " AND end_time <= ?"
        args.append(time.time() + within_hours * 3600)
    rows = db.query(sql + " ORDER BY end_time LIMIT 25", args)
    if not rows:
        return "Nothing tracked" + (f" ending within {within_hours:g}h." if within_hours else ". Use /track <link>.")
    lines = [("<b>Ending within 24h</b>" if within_hours else "<b>Tracked auctions</b>") + demo_tag()]
    for r in rows:
        lines.append(f"<code>#{r['id']}</code> {esc(r['title'][:60])}\n"
                     f"   {money(r['current_price'], r['currency'])} · {r['bid_count'] or 0} bids · ends {fmt_left(r['end_time'])}")
    return "\n".join(lines)
