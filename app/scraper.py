"""Loads each saved Carzone search in headless Chromium and harvests listings.

Three sources, merged by ad id:
  1. JSON the page fetches from Carzone's own backend (intercepted responses)
  2. JSON embedded in the page (JSON-LD, __NEXT_DATA__, window state)
  3. The rendered result cards (links + text), as a fallback
"""
import json
import logging
import os
import random
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import db
from .normalize import merge, parse_card, walk_json

log = logging.getLogger("scraper")

PAGE_PARAM = os.environ.get("PAGE_PARAM", "page")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
DEBUG_DIR = os.environ.get("DEBUG_DIR", "/data/debug")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

state = {"running": False, "last_started": None, "last_finished": None, "last_error": None, "progress": ""}
_run_lock = threading.Lock()

EMBEDDED_JS = """
() => {
  const out = [];
  document.querySelectorAll('script[type="application/ld+json"], script[type="application/json"], script#__NEXT_DATA__')
    .forEach(s => { try { out.push(JSON.parse(s.textContent)); } catch (e) {} });
  for (const k of ['__NEXT_DATA__','__NUXT__','__INITIAL_STATE__','__PRELOADED_STATE__','__APOLLO_STATE__','__STATE__']) {
    try { if (window[k]) out.push(JSON.parse(JSON.stringify(window[k]))); } catch (e) {}
  }
  return out;
}
"""

CARDS_JS = r"""
() => {
  const seen = new Set(); const out = [];
  const re = /\/fpa\/|\/used-cars\/[^?#]+\/\d{6,}/;
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.href;
    if (!re.test(href) || seen.has(href)) return;
    seen.add(href);
    // climb to the largest ancestor that still holds only this one listing = the card
    const countAds = n => new Set([...n.querySelectorAll('a[href]')].map(x => x.href).filter(h => re.test(h))).size;
    let el = a, hops = 0;
    while (el.parentElement && el.parentElement !== document.body && hops < 10 && countAds(el.parentElement) <= 1) {
      el = el.parentElement; hops++;
    }
    const img = el.querySelector('img');
    let src = img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : '';
    if (!src.startsWith('http') && img && img.srcset) src = img.srcset.split(',').pop().trim().split(' ')[0];
    out.push({ href, text: (el.innerText || '').slice(0, 1500), img: src });
  });
  return out;
}
"""


def page_url(url, n):
    if n <= 1:
        return url
    p = urlparse(url)
    qs = parse_qs(p.query)
    qs[PAGE_PARAM] = [str(n)]
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


def _dismiss_cookies(page):
    for label in ("Accept All", "Accept all", "Accept", "I Accept", "Agree", "AGREE", "Allow all"):
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{label}", re.I)).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=1500)
                page.wait_for_timeout(600)
                return
        except Exception:
            pass


def scrape_page(context, url, debug_name=None):
    page = context.new_page()
    captured = []

    def on_response(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch") and "json" in resp.headers.get("content-type", ""):
                captured.append((resp.url, resp.json()))
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _dismiss_cookies(page)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        for _ in range(6):  # lazy-loaded images and results
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(500)
        page.wait_for_timeout(1000)

        found = {}
        for _, data in captured:
            walk_json(data, found)
        for data in page.evaluate(EMBEDDED_JS):
            walk_json(data, found)
        cards = page.evaluate(CARDS_JS)
        for card in cards:
            ad = parse_card(card)
            if not ad:
                continue
            found[ad["id"]] = merge(found.get(ad["id"]), ad) if ad["id"] in found else ad

        if debug_name:
            _save_debug(debug_name, url, captured, cards, found, page)
        return list(found.values())
    finally:
        page.close()


def _save_debug(name, url, captured, cards, found, page):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"{name}.json"), "w") as f:
            json.dump({
                "url": url,
                "title": page.title(),
                "json_responses": [{"url": u, "sample": json.dumps(d)[:4000]} for u, d in captured[:30]],
                "cards": cards[:10],
                "parsed_ads": list(found.values())[:10],
                "parsed_count": len(found),
            }, f, indent=2, default=str)
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"), full_page=False)
    except Exception as e:
        log.warning("debug save failed: %s", e)


def run_all(only_search_id=None):
    if not _run_lock.acquire(blocking=False):
        return False
    state.update(running=True, last_started=db.now(), last_error=None, progress="Starting browser")
    run_id = db.x("INSERT INTO runs(started) VALUES (?)", (db.now(),))
    total, new = 0, 0
    errors = []
    try:
        from playwright.sync_api import sync_playwright
        searches = [s for s in db.list_searches() if s["enabled"] and (not only_search_id or s["id"] == only_search_id)]
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent=UA, locale="en-IE", timezone_id="Europe/Dublin",
                                          viewport={"width": 1366, "height": 900})
            for s in searches:
                s_found, s_err = 0, None
                seen_ids = set()
                try:
                    for n in range(1, MAX_PAGES + 1):
                        state["progress"] = f"{s['name']}: page {n}"
                        ads = scrape_page(context, page_url(s["url"], n), debug_name=f"search{s['id']}_p{n}" if n == 1 else None)
                        fresh = [a for a in ads if a["id"] not in seen_ids]
                        if not fresh:
                            break
                        for ad in fresh:
                            seen_ids.add(ad["id"])
                            if db.upsert_ad(ad, s["id"]):
                                new += 1
                        s_found += len(fresh)
                        time.sleep(random.uniform(4, 9))  # be gentle
                    if s_found == 0:
                        s_err = "No ads found on the page. Open Settings > Debug to see what the scraper saw."
                except Exception as e:
                    log.exception("search %s failed", s["name"])
                    s_err = str(e)[:300]
                total += s_found
                if s_err:
                    errors.append(f"{s['name']}: {s_err}")
                db.x("UPDATE searches SET last_run=?, last_count=?, last_error=? WHERE id=?",
                     (db.now(), s_found, s_err, s["id"]))
            browser.close()
    except Exception as e:
        log.exception("scrape run failed")
        errors.append(str(e)[:300])
    finally:
        err = "; ".join(errors) or None
        db.x("UPDATE runs SET finished=?, found=?, new_ads=?, error=? WHERE id=?", (db.now(), total, new, err, run_id))
        state.update(running=False, last_finished=db.now(), last_error=err, progress=f"Found {total}, {new} new")
        _run_lock.release()
    return True


def start_background(only_search_id=None):
    if state["running"]:
        return False
    threading.Thread(target=run_all, args=(only_search_id,), daemon=True).start()
    return True


def scheduler_loop(interval_hours):
    """Runs forever: scrape, then sleep interval ± 15% jitter."""
    time.sleep(20)
    while True:
        if os.environ.get("DEMO") != "1":
            run_all()
        jitter = random.uniform(0.85, 1.15)
        time.sleep(max(900, interval_hours * 3600 * jitter))
