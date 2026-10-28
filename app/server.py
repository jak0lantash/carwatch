import base64
import glob
import json
import logging
import os
import threading

from flask import Flask, Response, jsonify, request, send_from_directory

from . import checker, db, donedeal, pager, scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "6"))
# An ad not seen for this long is treated as sold/removed and drops out of the market list.
STALE_HOURS = float(os.environ.get("STALE_HOURS", str(max(48, INTERVAL_HOURS * 3 + 24))))
PASSWORD = os.environ.get("APP_PASSWORD", "")

app = Flask(__name__, static_folder="static", static_url_path="")


@app.before_request
def auth():
    if not PASSWORD:
        return None
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            _, pw = base64.b64decode(header[6:]).decode().split(":", 1)
            if pw == PASSWORD:
                return None
        except Exception:
            pass
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="CarWatch"'})


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/ads")
def get_ads():
    which = request.args.get("list", "market")
    if which not in ("market", "liked", "disliked"):
        return jsonify(error="Unknown list"), 400
    sid = request.args.get("search_id", type=int)
    ads = db.ads_for_list(which, search_id=sid, stale_hours=STALE_HOURS)
    if which == "market" and os.environ.get("DEMO") != "1":
        pager.plan(ads if not sid else db.ads_for_list("market", stale_hours=STALE_HOURS))
    return jsonify(ads)


@app.post("/api/ads/<ad_id>/status")
def set_status(ad_id):
    status = (request.json or {}).get("status")
    if status not in ("new", "liked", "disliked"):
        return jsonify(error="Status must be new, liked or disliked"), 400
    db.set_status(ad_id, status)
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    last = db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 1", one=True)
    return jsonify(scraper=scraper.state, last_run=last, counts=db.counts(STALE_HOURS),
                   interval_hours=INTERVAL_HOURS, demo=os.environ.get("DEMO") == "1",
                   pager=pager.status(), pager_busy=pager.busy(), refresh_hours=pager.REFRESH_HOURS,
                   checker=checker.state, check_saved_hours=checker.PERIOD_HOURS)


@app.post("/api/scrape")
def scrape_now():
    if os.environ.get("DEMO") == "1":
        return jsonify(started=False, error="Demo mode: checking Carzone is switched off"), 403
    sid = (request.json or {}).get("search_id") if request.is_json else None
    targets = [s for s in db.list_searches() if s["enabled"] and (not sid or s["id"] == sid)]
    dd = [s for s in targets if donedeal.is_donedeal(s["url"])]
    for s in dd:
        pager.request(s["id"], "first")
    started = bool(dd)
    if any(not donedeal.is_donedeal(s["url"]) for s in targets):
        started = scraper.start_background(sid) or started
    return jsonify(started=started, state=scraper.state, pager=pager.status())


@app.get("/api/searches")
def searches():
    return jsonify(db.list_searches())


def _bad_url(url):
    url = url or ""
    if "carzone.ie" in url:
        return None
    if donedeal.is_donedeal(url) and "/cars" in url:
        return None
    return "Paste a carzone.ie or donedeal.ie car search address"


@app.post("/api/searches")
def add_search():
    data = request.json or {}
    err = _bad_url(data.get("url"))
    if not data.get("name") or err:
        return jsonify(error=err or "Give the search a name"), 400
    return jsonify(id=db.save_search(data))


@app.put("/api/searches/<int:sid>")
def edit_search(sid):
    data = request.json or {}
    if "url" in data and _bad_url(data.get("url")):
        return jsonify(error=_bad_url(data.get("url"))), 400
    old = db.get_search(sid)
    db.save_search(data, sid)
    if old and "url" in data and data["url"] != old["url"]:  # new address: start paging afresh
        db.x("UPDATE searches SET next_from=NULL, total=NULL, refreshed_at=NULL WHERE id=?", (sid,))
    return jsonify(ok=True)


@app.delete("/api/searches/<int:sid>")
def remove_search(sid):
    db.delete_search(sid)
    return jsonify(ok=True)


@app.get("/api/exclusions")
def exclusions():
    return jsonify(db.list_exclusions())


@app.post("/api/exclusions")
def add_exclusion():
    data = request.json or {}
    try:
        db.add_exclusion(data.get("field"), data.get("value"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.delete("/api/exclusions/<int:eid>")
def remove_exclusion(eid):
    db.delete_exclusion(eid)
    return jsonify(ok=True)


@app.get("/api/debug")
def debug():
    """What the scraper saw on the first page of each search (JSON + screenshot names)."""
    out = []
    for path in sorted(glob.glob(os.path.join(scraper.DEBUG_DIR, "*.json"))):
        with open(path) as f:
            item = json.load(f)
        item["name"] = os.path.basename(path)[:-5]
        out.append(item)
    return jsonify(out)


@app.get("/api/debug/<name>.png")
def debug_png(name):
    return send_from_directory(scraper.DEBUG_DIR, f"{name}.png")


def start():
    db.conn()
    if os.environ.get("DEMO") == "1":
        from .demo import seed
        seed()
    threading.Thread(target=scraper.scheduler_loop, args=(INTERVAL_HOURS,), daemon=True).start()
    pager.start()
    checker.start()
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    start()
