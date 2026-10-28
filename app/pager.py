"""DoneDeal search pages, fetched only when they're needed (same approach as DaftWatch).

- Opening the Market fetches each DoneDeal search's first page, if that's older than
  REFRESH_HOURS (results are newest first, so page 1 holds any new listings).
- When a search has fewer than PREFETCH_WHEN_LEFT ads left to swipe, its next page is fetched.
- One request at a time, at least MIN_GAP_SECONDS apart (see throttle.py). If DoneDeal says to
  slow down, the queue pauses and then carries on.
Nothing is fetched while you're not using the app.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from . import db, donedeal, throttle

log = logging.getLogger("pager")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
LOW_WATER = int(os.environ.get("PREFETCH_WHEN_LEFT", "5"))

_cv = threading.Condition()
_queue = []          # [(search_id, "first" | "next")]
state = {"doing": None, "error": None, "last_done": None}


def _ts(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def has_more(s):
    return s["total"] is None or (s["next_from"] or 0) < s["total"]


def first_page_stale(s):
    if not s["refreshed_at"]:
        return True
    return datetime.now(timezone.utc) - _ts(s["refreshed_at"]) > timedelta(hours=REFRESH_HOURS)


def request(search_id, kind):
    with _cv:
        if (search_id, kind) not in _queue:
            _queue.append((search_id, kind))
            _cv.notify()


def busy():
    with _cv:
        return bool(_queue) or state["doing"] is not None


def plan(market_ads):
    """Called whenever the Market is loaded: queue the DoneDeal pages about to be needed."""
    searches = [s for s in db.list_searches() if s["enabled"] and donedeal.is_donedeal(s["url"])]
    for s in searches:
        if first_page_stale(s):
            request(s["id"], "first")
    with _cv:
        if any(k == "next" for _, k in _queue):
            return  # one extra page at a time
    for s in sorted(searches, key=lambda s: s["id"]):
        if not s["refreshed_at"] or not has_more(s):
            continue
        left = sum(s["id"] in a.get("search_ids", []) for a in market_ads)
        if left < LOW_WATER:
            request(s["id"], "next")
            return


def status():
    with _cv:
        q = len(_queue)
    return {"doing": state["doing"], "queued": q, "error": state["error"],
            "blocked_until": throttle.blocked_until(donedeal.HOST), "last_done": state["last_done"]}


def _do(sid, kind):
    s = db.get_search(sid)
    if not s or not s["enabled"] or (kind == "next" and not has_more(s)):
        return
    start = 0 if kind == "first" else (s["next_from"] or 0)
    state["doing"] = f"{s['name']}: page {start // donedeal.PAGE_SIZE + 1}"
    throttle.wait_turn(donedeal.HOST)
    try:
        raw, total, ads = donedeal.fetch_page(s, start)
    except throttle.RateLimited as e:
        pause = throttle.slow_down(donedeal.HOST)
        state["error"] = str(e)
        db.update_paging(sid, last_error=str(e))
        log.warning("%s; pausing %d s", e, pause)
        raise
    except Exception as e:
        throttle.ok(donedeal.HOST)
        log.exception("fetching %s failed", state["doing"])
        state["error"] = str(e)[:300]
        db.update_paging(sid, last_error=str(e)[:300])
        return
    throttle.ok(donedeal.HOST)
    state["error"] = None
    new = sum(db.upsert_ad(ad, sid) for ad in ads)
    if kind == "first":
        next_from = max(s["next_from"] or 0, raw)
    else:
        next_from = start + raw
    if raw == 0:
        next_from = max(next_from, total or 0, start)
    db.update_paging(sid, next_from=next_from, total=total if total is not None else next_from,
                     refreshed=(kind == "first"), count=len(ads))
    state["last_done"] = db.now()
    log.info("%s: %d ads, %d new (%d/%s)", state["doing"], len(ads), new, next_from, total)


def _worker():
    while True:
        with _cv:
            while not _queue:
                _cv.wait()
            job = _queue[0]
        try:
            _do(*job)
        except throttle.RateLimited:
            continue  # keep the job; wait_turn() holds it until the pause is over
        except Exception:
            log.exception("pager job %s failed", job)
        finally:
            state["doing"] = None
        with _cv:
            if job in _queue:
                _queue.remove(job)


def start():
    threading.Thread(target=_worker, daemon=True, name="pager").start()
