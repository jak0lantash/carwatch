"""DoneDeal: search pages and single ads, read with a plain HTTP client (no browser).

DoneDeal is a Next.js site: every page embeds its data as JSON in <script id="__NEXT_DATA__">.
A search page holds 30 ads plus paging info; `start=30` gives page 2, `sort=publishdatedesc`
puts the newest ads first (so new listings land on page 1).
"""
import json
import logging
import os
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from .throttle import RateLimited

log = logging.getLogger("donedeal")
HOST = "donedeal.ie"
PAGE_SIZE = 30
DEBUG_DIR = os.environ.get("DEBUG_DIR", "/data/debug")

MULTIWORD_MAKES = ["Land Rover", "Alfa Romeo", "Aston Martin", "Rolls Royce", "Rolls-Royce", "Mercedes Benz",
                   "Mercedes-Benz", "DS Automobiles", "John Deere", "Lynk & Co", "Great Wall"]
BODIES = ["Estate", "Saloon", "Hatchback", "SUV", "MPV", "Coupe", "Convertible", "Cabriolet", "Crossover",
          "Pickup", "Pick Up", "Van", "Commercial", "People Carrier", "Camper", "Other"]
COUNTY_FIX = {"Londonderry": "Derry"}


def is_donedeal(url):
    return HOST in (urlparse(url or "").netloc or "")


def ad_id(raw_id):
    return f"dd-{raw_id}"


# ---------- fetching ----------

_session = None


def fetch(url):
    """Returns (status, final_url, html). Presents Chrome's TLS fingerprint, like DaftWatch."""
    global _session
    from curl_cffi import requests as creq
    if _session is None:
        _session = creq.Session(impersonate="chrome")
    r = _session.get(url, timeout=40, headers={"Accept-Language": "en-IE,en;q=0.9"})
    if r.status_code == 429:
        raise RateLimited("DoneDeal asked us to slow down (HTTP 429)")
    if r.status_code == 403:
        _session = None
        raise RateLimited("DoneDeal showed its security check (HTTP 403)")
    return r.status_code, str(r.url), r.text


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        raise RateLimited("No listing data in the page (title: %s). DoneDeal may be blocking us."
                          % (title.group(1).strip()[:80] if title else "none"))
    return json.loads(m.group(1))


def page_url(url, start):
    u = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "start"]
    if not any(k == "sort" for k, _ in params):
        params.append(("sort", "publishdatedesc"))
    if start:
        params.append(("start", str(start)))
    return urlunparse(u._replace(query=urlencode(params), fragment=""))


def make_model_from_url(url):
    """/cars/Audi/A6 -> ('Audi', 'A6'); /cars/Land-Rover -> ('Land Rover', None)."""
    parts = [unquote(p) for p in urlparse(url or "").path.split("/") if p]
    if len(parts) < 2 or parts[0] != "cars":
        return None, None
    nice = lambda s: s.replace("-", " ").replace("+", " ").strip() or None
    return nice(parts[1]), (nice(parts[2]) if len(parts) > 2 else None)


# ---------- parsing ----------

def _int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group().replace(",", "")) if m else None


def _fuel(s):
    s = " ".join(w if w.isupper() else w.capitalize() for w in (s or "").split())
    return s.replace("Plug-in", "Plug-In") or None


def _split_alt(alt, url_make):
    """'Audi A6 Estate, Diesel, 2018, Blue' -> make, model, body, fuel, colour."""
    parts = [p.strip() for p in (alt or "").split(",")]
    head = parts[0] if parts else ""
    make = model = body = None
    candidates = ([url_make] if url_make else []) + MULTIWORD_MAKES
    for m in candidates:
        if m and head.lower().startswith(m.lower() + " "):
            make, rest = head[:len(m)], head[len(m):].strip()
            break
    else:
        make, _, rest = head.partition(" ")
    for b in sorted(BODIES, key=len, reverse=True):
        if rest.lower().endswith(" " + b.lower()) or rest.lower() == b.lower():
            body, rest = b, rest[: len(rest) - len(b)].strip()
            break
    model = rest or None
    fuel = _fuel(parts[1]) if len(parts) > 1 else None
    colour = parts[3] if len(parts) > 3 else None
    return make or None, model, body, fuel, colour


def parse_ad(a, search_url=None):
    url_make, url_model = make_model_from_url(search_url)
    make, model, body, fuel, colour = _split_alt(a.get("imageAlt"), url_make)
    if url_make:
        make = url_make if (make or "").lower() == url_make.lower() or not make else make
    if url_model and make and url_make and make.lower() == url_make.lower():
        model = url_model
    meta = a.get("metaInfo") or []
    year = engine = None
    for m in meta:
        m = str(m).replace("\xa0", " ")
        if year is None and re.fullmatch(r"(19[89]\d|20[0-4]\d)", m.strip()):
            year = int(m)
        em = re.match(r"(\d\.\d)\s*(?:l\b|litre)?\s*(.*)$", m.strip(), re.I)
        if em:
            engine = float(em.group(1))
            fuel = _fuel(em.group(2)) or fuel
        elif not fuel and re.fullmatch(r"[A-Za-z -]+", m.strip()) and any(
                w in m.lower() for w in ("petrol", "diesel", "electric", "hybrid", "lpg")):
            fuel = _fuel(m)
    pi = a.get("priceInfo") or {}
    price = None if pi.get("priceOnRequest") else (_int(pi.get("priceInEuro")) or None)
    if price is not None and not 500 <= price <= 2_000_000:
        price = None
    cover = ((a.get("gallery") or {}).get("coverImage") or {})
    title = a.get("title") or ""
    trans = "Automatic" if re.search(r"\bauto(matic)?\b", title, re.I) else (
        "Manual" if re.search(r"\bmanual\b", title, re.I) else None)
    county = a.get("county")
    return {
        "id": ad_id(a["id"]),
        "source": "donedeal",
        "url": a.get("friendlyUrl") or f"https://www.donedeal.ie{a.get('relativeUrl', '')}",
        "title": title[:140],
        "make": make, "model": model, "variant": None,
        "year": year, "price": price, "mileage_km": _int(a.get("mileageInKm")),
        "fuel": fuel, "body": body, "engine_l": engine, "transmission": trans,
        "colour": colour, "county": COUNTY_FIX.get(county, county),
        "seller": "Dealer" if a.get("dealer") else "Private",
        "image": cover.get("large") or cover.get("medium") or cover.get("small2"),
    }


def fetch_page(search, start):
    """Returns (raw_count, total, ads) for one search page."""
    url = page_url(search["url"], start)
    status, _, html = fetch(url)
    if status != 200:
        raise RuntimeError(f"DoneDeal answered HTTP {status}")
    pp = next_data(html).get("props", {}).get("pageProps", {})
    raw = pp.get("ads") or []
    paging = pp.get("paging") or {}
    total = _int(paging.get("totalResults"))
    ads = []
    for a in raw:
        try:
            ads.append(parse_ad(a, search["url"]))
        except Exception:
            log.exception("could not read DoneDeal ad %s", a.get("id"))
    if start == 0:
        _save_debug(search, url, raw, ads, total)
    return len(raw), total, ads


def _save_debug(search, url, raw, ads, total):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"dd-search{search['id']}.json"), "w") as f:
            json.dump({"url": url, "title": f"DoneDeal search: {total} results", "screenshot": False,
                       "json_responses": [{"url": url, "sample": json.dumps(raw[:2])[:4000]}],
                       "cards": [], "parsed_ads": ads[:10], "parsed_count": len(ads)}, f, indent=2)
    except Exception as e:
        log.warning("debug save failed: %s", e)


GONE_STATES = {"expired", "sold", "deleted", "withdrawn", "closed", "unpublished", "removed", "inactive"}


def check_ad(ad):
    """Returns {'gone': bool, 'price': int|None, 'state': str}."""
    url = ad["url"].split("#")[0]
    status, final, html = fetch(url)
    if status in (404, 410):
        return {"gone": True, "price": None, "state": f"HTTP {status}"}
    if status != 200:
        raise RuntimeError(f"DoneDeal answered HTTP {status}")
    pp = next_data(html).get("props", {}).get("pageProps", {})
    state = str(pp.get("state") or "")
    if "id" not in pp or state.lower() in GONE_STATES:
        return {"gone": True, "price": None, "state": state or "no ad data"}
    price = _int(pp.get("price"))
    if price is not None and not 500 <= price <= 2_000_000:
        price = None
    return {"gone": False, "price": price, "state": state}
