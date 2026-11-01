"""Final-price prediction and auction analytics, learned from auctions the tracker followed to the end.

Two models, evaluated on held-out auctions; whichever has the lower error is used:

* curve - empirical "final / price-at-T" ratios at the same time-left, from the most specific scope
          with enough history (same seller -> same keyword -> everything). Works from ~4 auctions.
* gbm   - quantile gradient boosting (P10/P50/P90) over time left, bids, bid and price velocity,
          seller snipe tendency, price relative to the keyword's typical final, and end hour.
          Trained once 25+ auctions have completed.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import db, fx
from .config import settings

log = logging.getLogger("carnival.ml")

GRID_H = np.array([1 / 60, 3 / 60, 5 / 60, 10 / 60, 0.25, 0.5, 1, 2, 3, 4, 6, 9, 12, 18, 24, 36, 48, 72, 96, 120, 168])
MIN_SCOPE_N = 4
MIN_GBM_N = 25
MIN_HORIZON_N = 10  # sold auctions observed at this time-left before price-path models are trusted
QUANTILES = (0.1, 0.5, 0.9)
TARGET_COVERAGE = 0.8
SNIPE_WINDOW_S = 600
DEAL_PROBABILITY = 0.6  # P(landed <= target) needed to call a listing a deal


def landed_sgd(price_sgd: float | None, shipping_sgd: float | None) -> float | None:
    if price_sgd is None:
        return None
    return round((price_sgd + (shipping_sgd or 0)) * (1 + settings.import_tax_pct / 100), 2)


def max_item_price_sgd(target_sgd: float | None, shipping_sgd: float | None) -> float | None:
    """Highest item price (SGD) whose landed cost still fits the price list target."""
    if not target_sgd:
        return None
    return round(target_sgd / (1 + settings.import_tax_pct / 100) - (shipping_sgd or 0), 2)


def sgt_hour(ts: float) -> int:
    return int((ts / 3600 + 8) % 24)


def sgt_dow(ts: float) -> int:
    return time.gmtime(ts + 8 * 3600).tm_wday


@dataclass
class Series:
    secs: np.ndarray    # seconds left, descending
    price: np.ndarray   # item currency
    bids: np.ndarray

    def at(self, secs_left: float) -> tuple[float, float]:
        """Observed (price, bids) at a moment with `secs_left` remaining; NaN if not observed."""
        n = np.searchsorted(-self.secs, -secs_left, side="right")
        if n == 0:
            return math.nan, math.nan
        i = n - 1
        if self.secs[i] - secs_left > max(900.0, 0.25 * secs_left):
            return math.nan, math.nan  # gap in the observations
        return float(self.price[i]), float(self.bids[i])


@dataclass
class Book:
    rows: list[dict]
    series: list[Series]
    ratio: np.ndarray       # [n, G] final / price_at(h)
    snipe: np.ndarray       # [n] share of final added in the last 10 minutes
    index: np.ndarray       # [n] final_sgd / median final_sgd of its keyword

    @property
    def n(self) -> int:
        return len(self.rows)


_state: dict = {"version": None}
_train_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Training data
# ---------------------------------------------------------------------------
def _load_book() -> Book:
    rows = db.query(
        """SELECT id, legacy_id, seller, keyword, final_price, final_sgd, currency, shipping_sgd, target_sgd,
                  end_time, bid_count, demo
           FROM tracked WHERE status='ended' AND final_price > 0 ORDER BY end_time"""
    )
    snaps: dict[int, list] = {r["id"]: [] for r in rows}
    if rows:
        for s in db.query(
            f"SELECT tracked_id, seconds_left, price, bid_count FROM snapshots "
            f"WHERE tracked_id IN ({','.join('?' * len(rows))}) ORDER BY tracked_id, ts",
            [r["id"] for r in rows],
        ):
            snaps[s["tracked_id"]].append((s["seconds_left"], s["price"], s["bid_count"] or 0))
    series, keep = [], []
    for r in rows:
        pts = [p for p in snaps[r["id"]] if p[0] is not None and p[1]]
        if len(pts) < 3:
            continue
        arr = np.array(pts, dtype=float)
        series.append(Series(arr[:, 0], arr[:, 1], arr[:, 2]))
        keep.append(r)
    n = len(keep)
    ratio = np.full((n, len(GRID_H)), np.nan)
    snipe = np.full(n, np.nan)
    for i, (r, s) in enumerate(zip(keep, series)):
        for j, h in enumerate(GRID_H):
            p, _ = s.at(h * 3600)
            if p and p > 0:
                ratio[i, j] = max(1.0, r["final_price"] / p)
        p10, _ = s.at(SNIPE_WINDOW_S)
        if p10 == p10:
            snipe[i] = max(0.0, 1 - p10 / r["final_price"])
    finals = np.array([r["final_sgd"] or 0 for r in keep], dtype=float)
    index = np.full(n, np.nan)
    for kw in {r["keyword"] for r in keep}:
        mask = np.array([r["keyword"] == kw for r in keep])
        index[mask] = finals[mask] / np.median(finals[mask])
    return Book(keep, series, ratio, snipe, index)


def _scope_mask(book: Book, seller: str | None, keyword: str | None, exclude: int | None = None,
                col: int | None = None) -> tuple[np.ndarray, str]:
    base = np.ones(book.n, dtype=bool)
    if exclude is not None:
        base[exclude] = False
    if col is not None:
        base &= ~np.isnan(book.ratio[:, col])
    for label, mask in (
        (f"seller {seller}", np.array([r["seller"] == seller for r in book.rows], dtype=bool) if seller else None),
        (f"keyword “{keyword}”", np.array([r["keyword"] == keyword for r in book.rows], dtype=bool) if keyword else None),
    ):
        if mask is not None and (base & mask).sum() >= MIN_SCOPE_N:
            return base & mask, label
    return base, "all tracked auctions"


def _grid_col(hours_left: float) -> int:
    return int(np.argmin(np.abs(np.log(GRID_H) - math.log(max(hours_left, 1 / 120)))))


# ---------------------------------------------------------------------------
# Features for the boosted model
# ---------------------------------------------------------------------------
def _features(price_sgd, hours_left, bids, bids_1h, chg_1h, seller_snipe, kw_median, end_time) -> list[float]:
    hour = sgt_hour(end_time) if end_time else 0
    rel = price_sgd / kw_median if (kw_median and kw_median == kw_median and price_sgd) else math.nan
    return [
        math.log(max(hours_left, 1 / 120)),
        float(bids if bids is not None else math.nan),
        float(bids_1h if bids_1h is not None else math.nan),
        float(chg_1h if chg_1h is not None else math.nan),
        float(seller_snipe if seller_snipe is not None else math.nan),
        rel,
        math.log1p(price_sgd or 0),
        math.sin(2 * math.pi * hour / 24),
        math.cos(2 * math.pi * hour / 24),
    ]


def _loo_median(values: np.ndarray, groups: list, i: int) -> float:
    mask = np.array([g == groups[i] for g in groups])
    mask[i] = False
    vals = values[mask]
    vals = vals[~np.isnan(vals)]
    return float(np.median(vals)) if len(vals) >= 2 else math.nan


def _training_rows(book: Book):
    X, y, groups, meta = [], [], [], []
    sellers = [r["seller"] for r in book.rows]
    keywords = [r["keyword"] for r in book.rows]
    finals_sgd = np.array([r["final_sgd"] for r in book.rows], dtype=float)
    for i, (r, s) in enumerate(zip(book.rows, book.series)):
        seller_snipe = _loo_median(book.snipe, sellers, i)
        kw_med = _loo_median(finals_sgd, keywords, i)
        sgd_per_unit = (r["final_sgd"] / r["final_price"]) if r["final_price"] else 1
        for j, h in enumerate(GRID_H):
            p, b = s.at(h * 3600)
            if not (p and p > 0):
                continue
            p1, b1 = s.at(h * 3600 + 3600)
            X.append(_features(p * sgd_per_unit, h, b, (b - b1) if b1 == b1 else None,
                               (p / p1 - 1) if (p1 and p1 == p1) else None, seller_snipe, kw_med, r["end_time"]))
            y.append(math.log(max(1.0, r["final_price"] / p)))
            groups.append(i)
            meta.append((i, j, p))
    return np.array(X), np.array(y), np.array(groups), meta


# ---------------------------------------------------------------------------
# Train / evaluate
# ---------------------------------------------------------------------------
def _widen(price: float, q10: float, q50: float, q90: float, k: float) -> tuple[float, float, float]:
    """Stretch the interval around the median (in log space) by k; finals can't fall below the current bid."""
    lo, mid, hi = (math.log(max(v, 1e-9)) for v in (q10, q50, q90))
    return max(price, math.exp(mid - k * (mid - lo))), q50, max(q50, math.exp(mid + k * (hi - mid)))


def _calibrate(held_out: list[tuple]) -> float:
    """Smallest widening factor that makes the held-out 80% interval actually cover ~80% of finals."""
    for k in np.arange(1.0, 4.01, 0.1):
        hits = [_widen(p, a, b, c, k)[0] <= f <= _widen(p, a, b, c, k)[2] for p, a, b, c, f, _ in held_out]
        if np.mean(hits) >= TARGET_COVERAGE:
            return round(float(k), 2)
    return 4.0


def _summary(held_out: list[tuple]) -> dict:
    """held_out rows: (price, q10, q50, q90, final, hours_left) from auctions the model never saw."""
    if not held_out:
        return {}
    k = _calibrate(held_out)
    errs = np.array([abs(b / f - 1) for _, _, b, _, f, _ in held_out])
    late = np.array([h <= 1 for *_, h in held_out])
    covered = [_widen(p, a, b, c, k)[0] <= f <= _widen(p, a, b, c, k)[2] for p, a, b, c, f, _ in held_out]
    return {
        "mae_pct": round(100 * float(errs.mean()), 1),
        "mae_last_hour_pct": round(100 * float(errs[late].mean()), 1) if late.any() else None,
        "coverage_pct": round(100 * float(np.mean(covered)), 1),
        "widen": k,
        "points": len(held_out),
    }


def _evaluate_curve(book: Book, meta) -> dict:
    held_out = []
    for i, j, p in meta:
        mask, _ = _scope_mask(book, book.rows[i]["seller"], book.rows[i]["keyword"], exclude=i, col=j)
        ratios = book.ratio[mask, j]
        if len(ratios) < MIN_SCOPE_N:
            continue
        q10, q50, q90 = np.quantile(ratios, QUANTILES)
        held_out.append((p, p * q10, p * q50, p * q90, book.rows[i]["final_price"], GRID_H[j]))
    return _summary(held_out)


def _fit_gbm(X, y):
    from sklearn.ensemble import HistGradientBoostingRegressor

    models = {}
    for q in QUANTILES:
        m = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=150, learning_rate=0.08,
                                          max_leaf_nodes=15, min_samples_leaf=12, random_state=7)
        models[q] = m.fit(X, y)
    return models


def _evaluate_gbm(X, y, groups, meta, book: Book) -> dict:
    from sklearn.model_selection import GroupKFold

    held_out = []
    for train, test in GroupKFold(n_splits=4).split(X, y, groups):
        models = _fit_gbm(X[train], y[train])
        preds = {q: models[q].predict(X[test]) for q in QUANTILES}
        for k, row in enumerate(test):
            i, j, p = meta[row]
            q10, q50, q90 = sorted(p * math.exp(max(0.0, preds[q][k])) for q in QUANTILES)
            held_out.append((p, q10, q50, q90, book.rows[i]["final_price"], GRID_H[j]))
    return _summary(held_out)


def current() -> dict:
    """The latest trained state, training synchronously only if nothing has been trained yet."""
    return _state if _state.get("version") is not None else ensure_trained()


def ensure_trained(force: bool = False) -> dict:
    """(Re)train when completed auctions changed. Takes seconds: call via asyncio.to_thread from async code."""
    global _state
    with _train_lock:
        marker = db.one("SELECT COUNT(*) AS n, MAX(ended_at) AS last FROM tracked WHERE status='ended'")
        version = (marker["n"], marker["last"])
        if not force and _state.get("version") == version:
            return _state
        started = time.time()
        book = _load_book()
        state = {"version": version, "book": book, "gbm": None, "metrics": {}, "active": None,
                 "trained_at": time.time()}
        if book.n >= MIN_SCOPE_N:
            X, y, groups, meta = _training_rows(book)
            state["metrics"]["curve"] = _evaluate_curve(book, meta)
            state["active"] = "curve"
            if book.n >= MIN_GBM_N and len(X) >= 100:
                try:
                    state["metrics"]["gbm"] = _evaluate_gbm(X, y, groups, meta, book)
                    state["gbm"] = _fit_gbm(X, y)
                    if state["metrics"]["gbm"].get("mae_pct", 1e9) < state["metrics"]["curve"].get("mae_pct", 1e9):
                        state["active"] = "gbm"
                except Exception:  # noqa: BLE001 - the curve model still works
                    log.exception("GBM training failed")
        _state = state
        log.info("Model refreshed on %d auctions in %.1fs (active=%s)", book.n, time.time() - started,
                 state["active"])
        return state


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------
def _cdf(x: float, price: float, q10: float, q50: float, q90: float) -> float:
    pts = [(price, 0.0), (q10, 0.1), (q50, 0.5), (q90, 0.9), (q90 + max(q90 - q50, 0.01 * q90), 1.0)]
    if x <= pts[0][0]:
        return 0.0
    for (x0, c0), (x1, c1) in zip(pts, pts[1:]):
        if x <= x1:
            return c0 + (c1 - c0) * ((x - x0) / (x1 - x0) if x1 > x0 else 1)
    return 1.0


def _prior(book: Book, keyword: str | None, price: float, sgd_per_unit: float):
    """P10/P50/P90 of what this item has sold for (item currency), floored at the current bid."""
    finals = np.array([r["final_sgd"] for r in book.rows if r["keyword"] == keyword and r["final_sgd"]], dtype=float)
    if len(finals) < MIN_SCOPE_N:
        return None
    return tuple(max(price, float(v) / sgd_per_unit) for v in np.quantile(finals, QUANTILES)), len(finals)


def predict(*, price: float | None, currency: str | None, seconds_left: float | None, seller: str | None,
            keyword: str | None, bid_count: int | None, end_time: float | None, shipping_sgd: float | None = None,
            target_sgd: float | None = None, recent: Series | None = None) -> dict | None:
    """Predicted final price (item currency + SGD) with an 80% interval and P(landed <= target)."""
    if not price or seconds_left is None:
        return None
    state = current()
    book: Book | None = state.get("book")
    if not state.get("active") or book is None:
        return None
    hours_left = max(seconds_left, 0) / 3600
    price_sgd = fx.to_sgd(price, currency) or price
    sgd_per_unit = price_sgd / price
    col = _grid_col(hours_left)
    mask, scope = _scope_mask(book, seller, keyword, col=col)
    ratios = book.ratio[mask, col]
    coverage = int(np.count_nonzero(~np.isnan(book.ratio[:, col])))
    model = state["active"]
    n_used = book.n
    if seconds_left <= 0:
        q10 = q50 = q90 = price
        model = "final"
    elif coverage < MIN_HORIZON_N:
        # Too far from the end for the tracked history to say how this price evolves:
        # fall back to what this item has actually sold for.
        prior = _prior(book, keyword, price, sgd_per_unit)
        if prior is None:
            return None
        (q10, q50, q90), n_used = prior
        model, scope = "prior", f"sold prices for “{keyword}”"
    elif model == "gbm" and state.get("gbm"):
        sellers = np.array([r["seller"] == seller for r in book.rows])
        snipe = book.snipe[sellers]
        snipe = snipe[~np.isnan(snipe)]
        kw_finals = [r["final_sgd"] for r in book.rows if r["keyword"] == keyword]
        bids_1h = chg_1h = None
        if recent is not None and len(recent.secs):
            p1, b1 = recent.at(seconds_left + 3600)
            if p1 == p1 and p1:
                bids_1h, chg_1h = (bid_count or 0) - b1, price / p1 - 1
        x = np.array([_features(price_sgd, hours_left, bid_count, bids_1h, chg_1h,
                                float(np.median(snipe)) if len(snipe) >= 2 else None,
                                float(np.median(kw_finals)) if len(kw_finals) >= 2 else None, end_time)])
        q10, q50, q90 = sorted(price * math.exp(max(0.0, state["gbm"][q].predict(x)[0])) for q in QUANTILES)
        scope = f"boosted model, {book.n} auctions"
    else:
        if len(ratios) < MIN_SCOPE_N:
            return None
        q10, q50, q90 = (price * float(r) for r in np.quantile(ratios, QUANTILES))
        n_used = int(mask.sum())
    if model in ("gbm", "curve"):
        q10, q50, q90 = _widen(price, q10, q50, q90, (state["metrics"].get(model) or {}).get("widen", 1.0))
        prior = _prior(book, keyword, price, sgd_per_unit)
        if prior is not None and q50 < prior[0][0]:
            # A median below the item's 10th-percentile sale means the price is outside anything
            # the path models saw at this time-left (e.g. a 99c start). Trust the sales history.
            (q10, q50, q90), n_used = prior
            model, scope = "prior", f"sold prices for “{keyword}”"
    out = {
        "model": model,
        "scope": scope,
        "n": n_used,
        "hours_left": round(hours_left, 3),
        "p10": round(q10, 2), "p50": round(q50, 2), "p90": round(q90, 2),
        "p10_sgd": round(q10 * sgd_per_unit, 2), "p50_sgd": round(q50 * sgd_per_unit, 2),
        "p90_sgd": round(q90 * sgd_per_unit, 2),
        "landed_p50_sgd": landed_sgd(q50 * sgd_per_unit, shipping_sgd),
        "currency": currency,
    }
    cap_sgd = max_item_price_sgd(target_sgd, shipping_sgd)
    if cap_sgd is not None:
        cap = cap_sgd / sgd_per_unit
        out["max_bid"] = round(cap, 2)
        out["max_bid_sgd"] = cap_sgd
        out["prob_under_target"] = round(_cdf(cap, price, q10, q50, q90), 3) if cap > price else 0.0
    return out


def series_for(tracked_id: int) -> Series | None:
    pts = db.query("SELECT seconds_left, price, bid_count FROM snapshots WHERE tracked_id=? ORDER BY ts", [tracked_id])
    pts = [(p["seconds_left"], p["price"], p["bid_count"] or 0) for p in pts if p["price"]]
    if not pts:
        return None
    arr = np.array(pts, dtype=float)
    return Series(arr[:, 0], arr[:, 1], arr[:, 2])


# ---------------------------------------------------------------------------
# Analytics for the playbook view
# ---------------------------------------------------------------------------
BLOCKS = [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _q(vals, q) -> float | None:
    vals = np.asarray(vals, dtype=float)
    vals = vals[~np.isnan(vals)]
    return round(float(np.quantile(vals, q)), 4) if len(vals) else None


def analytics(seller: str | None = None, keyword: str | None = None) -> dict:
    state = current()
    book: Book = state["book"]
    rows = book.rows
    sel = np.array([(not seller or r["seller"] == seller) and (not keyword or r["keyword"] == keyword) for r in rows],
                   dtype=bool) if rows else np.zeros(0, dtype=bool)

    def curve(mask):
        out = []
        for j, h in enumerate(GRID_H):
            share = 1 / book.ratio[mask, j]
            share = share[~np.isnan(share)]
            if len(share) >= 3:
                out.append({"h": float(h), "p25": _q(share, 0.25), "p50": _q(share, 0.5), "p75": _q(share, 0.75),
                            "n": int(len(share))})
        return out

    end_times = np.array([r["end_time"] for r in rows], dtype=float)
    blocks = []
    for lo, hi in BLOCKS:
        m = sel & np.array([lo <= sgt_hour(t) < hi for t in end_times], dtype=bool)
        blocks.append({"label": f"{lo:02d}–{hi:02d}", "n": int(m.sum()), "index": _q(book.index[m], 0.5)})
    dows = []
    for d, name in enumerate(DOW):
        m = sel & np.array([sgt_dow(t) == d for t in end_times], dtype=bool)
        dows.append({"label": name, "n": int(m.sum()), "index": _q(book.index[m], 0.5)})

    def group_table(field: str) -> list[dict]:
        out = []
        for name in sorted({r[field] for r in rows}):
            m = np.array([r[field] == name for r in rows], dtype=bool)
            finals = np.array([r["final_sgd"] for r in rows], dtype=float)[m]
            landed = np.array([landed_sgd(r["final_sgd"], r["shipping_sgd"]) or np.nan for r in rows], dtype=float)[m]
            targets = np.array([r["target_sgd"] or np.nan for r in rows], dtype=float)[m]
            hit = landed <= targets
            out.append({
                "name": name, "n": int(m.sum()),
                "median_final_sgd": _q(finals, 0.5), "p25_final_sgd": _q(finals, 0.25), "p75_final_sgd": _q(finals, 0.75),
                "index": _q(book.index[m], 0.5),
                "snipe_share": _q(book.snipe[m], 0.5),
                "median_bids": _q([r["bid_count"] or 0 for r, k in zip(rows, m) if k], 0.5),
                "under_target_pct": round(100 * float(np.mean(hit[~np.isnan(targets)])), 1)
                if (~np.isnan(targets)).any() else None,
            })
        return sorted(out, key=lambda x: -x["n"])

    result = {
        "n": int(sel.sum()),
        "n_total": book.n,
        "filter": {"seller": seller, "keyword": keyword},
        "curve": curve(sel) if sel.any() else [],
        "curve_all": curve(np.ones(book.n, dtype=bool)) if book.n else [],
        "snipe_share": _q(book.snipe[sel], 0.5) if sel.any() else None,
        "snipe_share_all": _q(book.snipe, 0.5) if book.n else None,
        "end_blocks": blocks,
        "dow": dows,
        "sellers": group_table("seller") if rows else [],
        "keywords": group_table("keyword") if rows else [],
        "model": {"active": state.get("active"), "metrics": state.get("metrics"), "n": book.n,
                  "trained_at": state.get("trained_at"), "min_gbm": MIN_GBM_N},
    }
    result["playbook"] = _playbook(result)
    return result


def _playbook(a: dict) -> list[dict]:
    tips: list[dict] = []
    if a["n"] < MIN_SCOPE_N:
        return [{"kind": "info", "text": f"Only {a['n']} completed auction(s) in this view. Track more links to "
                                         f"unlock a playbook (needs {MIN_SCOPE_N}+)."}]
    snipe = a.get("snipe_share")
    if snipe is not None:
        pct = round(snipe * 100)
        if pct >= 20:
            tips.append({"kind": "timing", "text": f"{pct}% of the final price arrives in the last 10 minutes. The "
                         "price you see an hour out is not the price; decide your max early and place it in the "
                         "final 10–15 seconds."})
        else:
            tips.append({"kind": "timing", "text": f"Only {pct}% of the final price lands in the last 10 minutes. "
                         "Prices here are set early; a late snipe buys little advantage."})
    curve = {round(c["h"], 3): c for c in a["curve"]}
    c1 = curve.get(1.0)
    if c1 and c1["p50"]:
        tips.append({"kind": "curve", "text": f"With 1 hour left the bid is typically {round(c1['p50'] * 100)}% of "
                     f"the final (middle half: {round(c1['p25'] * 100)}–{round(c1['p75'] * 100)}%). Divide the "
                     f"current bid by {c1['p50']:.2f} for a quick final-price estimate."})
    blocks = [b for b in a["end_blocks"] if b["n"] >= 3 and b["index"]]
    if len(blocks) >= 2:
        cheap = min(blocks, key=lambda b: b["index"])
        dear = max(blocks, key=lambda b: b["index"])
        if dear["index"] - cheap["index"] >= 0.05:
            tips.append({"kind": "window", "text": f"Auctions ending {cheap['label']}h SGT close at "
                         f"{round(cheap['index'] * 100)}% of the typical price (n={cheap['n']}); "
                         f"{dear['label']}h closes at {round(dear['index'] * 100)}% (n={dear['n']}). "
                         "Favour listings that end in the cheap window."})
    sellers = [s for s in a["sellers"] if s["n"] >= 3 and s["index"]]
    if len(sellers) >= 2 and not a["filter"]["seller"]:
        best = min(sellers, key=lambda s: s["index"])
        worst = max(sellers, key=lambda s: s["index"])
        tips.append({"kind": "seller", "text": f"{best['name']} sells about {round((1 - best['index']) * 100)}% "
                     f"below the typical price (n={best['n']}); {worst['name']} about "
                     f"{round((worst['index'] - 1) * 100)}% above (n={worst['n']})."})
    m = (a.get("model") or {}).get("metrics", {}).get((a.get("model") or {}).get("active") or "", {})
    if m:
        tips.append({"kind": "model", "text": f"Prediction error on held-out auctions: ±{m['mae_pct']}% overall, "
                     f"±{m.get('mae_last_hour_pct')}% inside the final hour. The calibrated 80% range caught the "
                     f"actual final {m['coverage_pct']}% of the time."})
    return tips
