import re
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

HIDE_POA = os.environ.get("HIDE_POA", "1") == "1"
DB_PATH = os.environ.get("DB_PATH", "/data/carwatch.db")
_lock = threading.RLock()
_conn = None

AD_FIELDS = ["source", "url", "title", "make", "model", "variant", "year", "price", "mileage_km", "fuel", "body",
             "engine_l", "transmission", "colour", "county", "seller", "image"]
EXCLUDABLE = ["make", "model", "variant", "fuel", "body", "engine_l", "transmission", "colour", "county", "seller"]
SEARCH_FIELDS = ["name", "url", "enabled", "min_year", "max_year", "max_km", "min_price", "max_price",
                 "min_engine", "max_engine", "fuel", "body"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  min_year INTEGER, max_year INTEGER, max_km INTEGER, min_price INTEGER, max_price INTEGER,
  min_engine REAL, max_engine REAL, fuel TEXT, body TEXT,
  last_run TEXT, last_count INTEGER, last_error TEXT
);
CREATE TABLE IF NOT EXISTS ads(
  id TEXT PRIMARY KEY,
  url TEXT, title TEXT, make TEXT, model TEXT, variant TEXT, year INTEGER, price INTEGER,
  mileage_km INTEGER, fuel TEXT, body TEXT, engine_l REAL, transmission TEXT, colour TEXT,
  county TEXT, seller TEXT, image TEXT,
  first_seen TEXT, last_seen TEXT, price_history TEXT DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'new', status_at TEXT
);
CREATE TABLE IF NOT EXISTS ad_search(ad_id TEXT, search_id INTEGER, PRIMARY KEY(ad_id, search_id));
CREATE TABLE IF NOT EXISTS exclusions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, field TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(field, value)
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, found INTEGER, new_ads INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS ads_status ON ads(status);
"""

# Columns added after the first release; created on start-up if missing.
MIGRATIONS = {
    "ads": {"source": "TEXT DEFAULT 'carzone'", "checked_at": "TEXT", "gone_at": "TEXT"},
    "searches": {"next_from": "INTEGER", "total": "INTEGER", "refreshed_at": "TEXT"},
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        _conn.execute("UPDATE ads SET source='carzone' WHERE source IS NULL")
        _conn.commit()
    return _conn


def q(sql, args=(), one=False):
    with _lock:
        cur = conn().execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# ---------- searches ----------

def list_searches():
    return q("SELECT * FROM searches ORDER BY id")


def save_search(data, sid=None):
    vals = {k: data.get(k) for k in SEARCH_FIELDS if k in data}
    for k in ("min_year", "max_year", "max_km", "min_price", "max_price"):
        if k in vals:
            vals[k] = int(vals[k]) if vals[k] not in (None, "") else None
    for k in ("min_engine", "max_engine"):
        if k in vals:
            vals[k] = float(vals[k]) if vals[k] not in (None, "") else None
    for k in ("fuel", "body"):
        if k in vals:
            vals[k] = (vals[k] or "").strip() or None
    if "enabled" in vals:
        vals["enabled"] = 1 if vals["enabled"] else 0
    if sid:
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            x(f"UPDATE searches SET {sets} WHERE id=?", (*vals.values(), sid))
        return sid
    vals.setdefault("enabled", 1)
    cols = ", ".join(vals)
    return x(f"INSERT INTO searches({cols}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))


def get_search(sid):
    return q("SELECT * FROM searches WHERE id=?", (sid,), one=True)


def update_paging(sid, next_from=None, total=None, refreshed=False, count=None, last_error=None):
    """DoneDeal searches: remember how far we've paged and when page 1 was last fetched."""
    if last_error is not None:
        x("UPDATE searches SET last_error=? WHERE id=?", (last_error, sid))
        return
    t = now()
    x("UPDATE searches SET next_from=?, total=?, last_error=NULL, last_run=?, "
      "last_count=COALESCE(?, last_count), refreshed_at=CASE WHEN ? THEN ? ELSE refreshed_at END WHERE id=?",
      (next_from, total, t, count, 1 if refreshed else 0, t, sid))


def delete_search(sid):
    x("DELETE FROM searches WHERE id=?", (sid,))
    x("DELETE FROM ad_search WHERE search_id=?", (sid,))


# ---------- ads ----------

def upsert_ad(ad, search_id):
    """Insert or refresh an ad. Returns True if it's new."""
    t = now()
    ad = dict(ad)
    ad.setdefault("source", "carzone")
    with _lock:
        c = conn()
        row = c.execute("SELECT price, price_history FROM ads WHERE id=?", (ad["id"],)).fetchone()
        if row is None:
            hist = [[t, ad["price"]]] if ad.get("price") else []
            cols = ["id", *AD_FIELDS, "first_seen", "last_seen", "price_history"]
            vals = [ad["id"], *[ad.get(f) for f in AD_FIELDS], t, t, json.dumps(hist)]
            c.execute(f"INSERT INTO ads({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            is_new = True
        else:
            hist = json.loads(row["price_history"] or "[]")
            if ad.get("price") and ad["price"] != row["price"]:
                hist.append([t, ad["price"]])
            sets = ", ".join(f"{f}=COALESCE(?, {f})" for f in AD_FIELDS)
            c.execute(f"UPDATE ads SET {sets}, last_seen=?, price_history=?, gone_at=NULL WHERE id=?",
                      (*[ad.get(f) for f in AD_FIELDS], t, json.dumps(hist), ad["id"]))
            is_new = False
        c.execute("INSERT OR IGNORE INTO ad_search(ad_id, search_id) VALUES (?,?)", (ad["id"], search_id))
        c.commit()
    return is_new


def record_check(ad_id, gone=False, price=None, error=False):
    """Result of re-checking a saved ad on its own page."""
    t = now()
    with _lock:
        c = conn()
        row = c.execute("SELECT price, price_history FROM ads WHERE id=?", (ad_id,)).fetchone()
        if row is None:
            return
        if error:
            c.execute("UPDATE ads SET checked_at=? WHERE id=?", (t, ad_id))
        elif gone:
            c.execute("UPDATE ads SET checked_at=?, gone_at=? WHERE id=?", (t, t, ad_id))
        else:
            hist = json.loads(row["price_history"] or "[]")
            new_price = row["price"]
            if price and price != row["price"]:
                hist.append([t, price])
                new_price = price
            c.execute("UPDATE ads SET checked_at=?, last_seen=?, price=?, price_history=?, gone_at=NULL WHERE id=?",
                      (t, t, new_price, json.dumps(hist), ad_id))
        c.commit()


def set_status(ad_id, status):
    x("UPDATE ads SET status=?, status_at=? WHERE id=?", (status, now(), ad_id))


# ---------- filtering ----------

def _alts(s):
    return [a.strip().lower() for a in (s or "").split(",") if a.strip()]


def _slug(v):
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def _url_param(url, key):
    from urllib.parse import urlparse, parse_qs
    vals = parse_qs(urlparse(url or "").query).get(key)
    return vals[0] if vals and "," not in vals[0] else None


def matches_search(ad, s):
    """Local refinements on top of the Carzone URL. Unknown values pass."""
    def lo(v, m):
        return m is None or v is None or v >= m

    def hi(v, m):
        return m is None or v is None or v <= m

    # Make/model in the Carzone search URL must match (catches ads and carousels on the page).
    for key, field in (("make", "make"), ("model", "model")):
        want = _url_param(s["url"], key)
        if want and ad[field] and _slug(want) != _slug(ad[field]):
            return False
    if not (lo(ad["year"], s["min_year"]) and hi(ad["year"], s["max_year"])):
        return False
    if not hi(ad["mileage_km"], s["max_km"]):
        return False
    if not (lo(ad["price"], s["min_price"]) and hi(ad["price"], s["max_price"])):
        return False
    if not (lo(ad["engine_l"], s["min_engine"]) and hi(ad["engine_l"], s["max_engine"])):
        return False
    for field in ("fuel", "body"):
        alts = _alts(s[field])
        if alts and ad[field] and not any(a in ad[field].lower() for a in alts):
            return False
    return True


def _engine(v):
    """'3.0 L', '3', 3.0 -> '3.0' so engine sizes compare reliably."""
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return f"{float(m.group()):.1f}" if m else None


def excluded(ad, exclusions):
    for e in exclusions:
        v = ad.get(e["field"])
        if v in (None, ""):
            continue
        if e["field"] == "engine_l":
            if _engine(v) == _engine(e["value"]):
                return True
        elif str(v).strip().lower() == e["value"].strip().lower():
            return True
    return False


def ads_for_list(which, search_id=None, stale_hours=72):
    searches = {s["id"]: s for s in list_searches()}
    links = {}
    for r in q("SELECT ad_id, search_id FROM ad_search"):
        links.setdefault(r["ad_id"], []).append(r["search_id"])
    exclusions = q("SELECT * FROM exclusions")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if which == "market":
        # Carzone is re-scraped on a schedule, so an ad missing for a while has gone. DoneDeal pages
        # are only fetched as you swipe, so its ads stay until swiped or found to be removed.
        rows = q("SELECT * FROM ads WHERE status='new' AND gone_at IS NULL "
                 "AND (source='donedeal' OR last_seen >= ?) ORDER BY first_seen DESC, id", (cutoff,))
    elif which == "liked":
        rows = q("SELECT * FROM ads WHERE status='liked' ORDER BY status_at DESC")
    else:
        rows = q("SELECT * FROM ads WHERE status='disliked' ORDER BY status_at DESC")

    out = []
    for ad in rows:
        sids = [sid for sid in links.get(ad["id"], []) if sid in searches]
        if which == "market":
            if ad["price"] is None and HIDE_POA:  # "price on application" ads
                continue
            if excluded(ad, exclusions):
                continue
            sids = [sid for sid in sids if searches[sid]["enabled"] and matches_search(ad, searches[sid])]
            if not sids or (search_id and search_id not in sids):
                continue
        ad["searches"] = [searches[sid]["name"] for sid in sids]
        ad["search_ids"] = sids
        ad["active"] = ad["gone_at"] is None
        ad["price_history"] = json.loads(ad["price_history"] or "[]")
        out.append(ad)
    _add_comparables(out)
    return out


def _add_comparables(ads):
    """Median asking price of the same make+model within ±1 year, from everything seen."""
    pool = q("SELECT id, make, model, year, price FROM ads WHERE price IS NOT NULL AND year IS NOT NULL")
    groups = {}
    for p in pool:
        groups.setdefault(((p["make"] or "").lower(), (p["model"] or "").lower()), []).append(p)
    for ad in ads:
        ad["comp_median"] = None
        ad["comp_count"] = 0
        if not (ad["make"] and ad["model"] and ad["year"]):
            continue
        prices = sorted(p["price"] for p in groups.get((ad["make"].lower(), ad["model"].lower()), [])
                        if p["id"] != ad["id"] and abs(p["year"] - ad["year"]) <= 1)
        if len(prices) >= 3:
            n = len(prices)
            ad["comp_median"] = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) // 2
            ad["comp_count"] = n


def counts(stale_hours=72):
    return {k: len(ads_for_list(k, stale_hours=stale_hours)) for k in ("market", "liked", "disliked")}


# ---------- exclusions ----------

def list_exclusions():
    return q("SELECT * FROM exclusions ORDER BY field, value")


def add_exclusion(field, value):
    if field not in EXCLUDABLE or not value:
        raise ValueError("Can't exclude on that field")
    if field == "engine_l":
        value = _engine(value)
        if not value:
            raise ValueError("Not an engine size")
    x("INSERT OR IGNORE INTO exclusions(field, value) VALUES (?,?)", (field, value.strip()))


def delete_exclusion(eid):
    x("DELETE FROM exclusions WHERE id=?", (eid,))
