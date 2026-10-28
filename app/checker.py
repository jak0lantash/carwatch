"""Re-checks saved (right-swiped) ads every CHECK_SAVED_HOURS, a few at a time.

Once an hour it checks the saved ads that are due, spread out so each saved ad is looked at
about once per period rather than all at once. For each, it opens the ad's own page (plain HTTP,
no browser), records any price change and marks ads that have been sold or removed.
"""
import logging
import math
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db, donedeal, throttle

log = logging.getLogger("checker")
PERIOD_HOURS = float(os.environ.get("CHECK_SAVED_HOURS", "48"))
TICK_MINUTES = float(os.environ.get("CHECK_TICK_MINUTES", "60"))
MAX_PER_TICK = int(os.environ.get("CHECK_MAX_PER_TICK", "6"))
CARZONE = "carzone.ie"

state = {"last_tick": None, "last_checked": None, "error": None}


def _carzone_check(ad):
    status, final, html = donedeal.fetch(ad["url"].split("#")[0])  # same Chrome-like client
    if status in (404, 410) or "#expired-ad" in final or "/fpa/" not in final:
        return {"gone": True, "price": None, "state": "expired"}
    if status != 200:
        raise RuntimeError(f"Carzone answered HTTP {status}")
    # The ad's own price: a meta tag (cXenseParse:inm-price) or the main price box's card-price.
    m = (re.search(r'cXenseParse:inm-price"\s+content="(\d+)"', html)
         or re.search(r'class="fpa-price[^"]*".{0,400}?data-cy="card-price"[^>]*>\s*€\s*([\d,]+)', html, re.S))
    price = int(m.group(1).replace(",", "")) if m else None
    if price is not None and not 500 <= price <= 2_000_000:
        price = None
    return {"gone": False, "price": price, "state": "live" if m else "live, no price found"}


def check_one(ad):
    host = donedeal.HOST if ad.get("source") == "donedeal" else CARZONE
    throttle.wait_turn(host)
    try:
        res = donedeal.check_ad(ad) if host == donedeal.HOST else _carzone_check(ad)
    except throttle.RateLimited:
        throttle.slow_down(host)
        raise
    throttle.ok(host)
    db.record_check(ad["id"], gone=res["gone"], price=res["price"])
    log.info("checked %s (%s): %s", ad["id"], ad.get("title", "")[:40], res)
    return res


def due_ads():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PERIOD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.q("SELECT * FROM ads WHERE status='liked' AND gone_at IS NULL "
                "AND MAX(COALESCE(checked_at, ''), COALESCE(last_seen, ''), first_seen) < ? "
                "ORDER BY MAX(COALESCE(checked_at, ''), COALESCE(last_seen, ''), first_seen)", (cutoff,))


def tick():
    state["last_tick"] = db.now()
    saved = db.q("SELECT COUNT(*) n FROM ads WHERE status='liked' AND gone_at IS NULL", one=True)["n"]
    due = due_ads()
    if not due:
        return 0
    ticks_per_period = max(1.0, PERIOD_HOURS * 60 / TICK_MINUTES)
    n = min(MAX_PER_TICK, len(due), max(1, math.ceil(saved / ticks_per_period)))
    done = 0
    for ad in due[:n]:
        try:
            check_one(ad)
            done += 1
            state["last_checked"] = db.now()
            state["error"] = None
        except throttle.RateLimited as e:
            state["error"] = str(e)
            break
        except Exception as e:
            log.exception("checking %s failed", ad["id"])
            state["error"] = f"{ad['id']}: {str(e)[:200]}"
            db.record_check(ad["id"], error=True)
    return done


def _loop():
    time.sleep(120)
    while True:
        if os.environ.get("DEMO") != "1":
            try:
                tick()
            except Exception:
                log.exception("saved-ad check failed")
        time.sleep(TICK_MINUTES * 60 * random.uniform(0.9, 1.1))


def start():
    threading.Thread(target=_loop, daemon=True, name="checker").start()
