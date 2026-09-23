import base64
import glob
import json
import logging
import os
import threading

from flask import Flask, Response, jsonify, request, send_from_directory

from . import db, scraper

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
    return jsonify(db.ads_for_list(which, search_id=sid, stale_hours=STALE_HOURS))


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
                   interval_hours=INTERVAL_HOURS, demo=os.environ.get("DEMO") == "1")


@app.post("/api/scrape")
def scrape_now():
    if os.environ.get("DEMO") == "1":
        return jsonify(started=False, error="Demo mode: checking Carzone is switched off"), 403
    sid = (request.json or {}).get("search_id") if request.is_json else None


@app.get("/api/searches")
def searches():
    return jsonify(db.list_searches())


@app.post("/api/searches")
def add_search():
    data = request.json or {}
    if not data.get("name") or "carzone.ie" not in (data.get("url") or ""):
        return jsonify(error="Give the search a name and paste a carzone.ie search URL"), 400
    return jsonify(id=db.save_search(data))


@app.put("/api/searches/<int:sid>")
def edit_search(sid):
    data = request.json or {}
    if "url" in data and "carzone.ie" not in (data.get("url") or ""):
        return jsonify(error="The URL must be a carzone.ie search"), 400
    db.save_search(data, sid)
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
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    start()
