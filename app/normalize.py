"""Turn whatever Carzone sends (API JSON, JSON-LD, embedded state, or raw card
text) into one uniform ad dict.

Carzone's internal format isn't documented and can change, so instead of
hard-coding paths this walks any JSON it finds, spots objects that look like a
car listing, and picks fields by key name. If Carzone renames things, add the
new key names to FIELD_KEYS below.
"""
import re
from urllib.parse import urljoin

BASE = "https://www.carzone.ie"

# Candidate key names per field (lower-case, dots removed from nested paths,
# e.g. {"make": {"name": "Ford"}} is seen as "makename").
FIELD_KEYS = {
    "id": ["id", "advertid", "adid", "listingid", "stockid", "vehicleid", "identifier", "advertreference"],
    "url": ["url", "adurl", "link", "href", "permalink", "detailurl", "canonicalurl", "detailsurl", "@id"],
    "make": ["make", "makename", "manufacturer", "brand", "brandname", "makedisplayname"],
    "model": ["model", "modelname", "modeldisplayname"],
    "variant": ["variant", "trim", "derivative", "version", "subtitle", "modelvariant", "trimname"],
    "year": ["year", "registrationyear", "regyear", "modelyear", "manufactureyear", "vehiclemodeldate",
             "productiondate", "firstregistrationyear", "yearofmanufacture"],
    "price": ["price", "priceamount", "pricevalue", "askingprice", "offersprice", "currentprice",
              "priceprice", "pricesprice", "retailprice", "cashprice"],
    "mileage": ["mileage", "odometer", "mileagevalue", "mileagekm", "kilometers", "kilometres",
                "mileagefromodometervalue", "odometervalue", "mileageamount"],
    "mileage_unit": ["mileageunit", "odometerunit", "mileagefromodometerunitcode", "mileageunitofmeasure"],
    "fuel": ["fueltype", "fuel", "fueltypename", "fueltypedisplayname"],
    "body": ["bodytype", "bodystyle", "body", "bodytypename", "bodytypedisplayname"],
    "engine": ["enginesize", "enginecapacity", "enginesizelitres", "enginesizecc", "engine",
               "vehicleengineenginedisplacementvalue", "enginedisplacement", "displacement"],
    "transmission": ["transmission", "gearbox", "transmissiontype", "vehicletransmission"],
    "colour": ["colour", "color", "exteriorcolour", "exteriorcolor"],
    "county": ["county", "location", "region", "dealercounty", "sellercounty", "locationcounty", "city",
               "addresslocality", "addressregion"],
    "seller": ["sellertype", "dealername", "sellername", "advertisertype", "sellertypename", "dealer"],
    "title": ["title", "name", "headline", "adtitle"],
    "image": ["image", "images", "photos", "photo", "media", "thumbnail", "mainimage", "primaryimage",
              "imageurl", "photourl", "thumbnailurl", "gallery"],
}

NOT_TITLE_PARENTS = {"make", "model", "brand", "location", "seller", "dealer", "fueltype", "bodytype",
                     "colour", "color", "transmission", "county", "manufacturer", "address"}

FUELS = ["plug-in hybrid", "hybrid", "electric", "diesel", "petrol", "lpg"]
BODIES = ["estate", "saloon", "hatchback", "suv", "mpv", "coupe", "convertible", "cabriolet",
          "crossover", "pickup", "van", "people carrier"]


def _norm(k):
    return re.sub(r"[^a-z@]", "", str(k).lower())


def _flatten(obj, prefix="", depth=0, out=None):
    """{path: value} for scalars and lists, up to a few levels deep."""
    if out is None:
        out = {}
    if depth > 4:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.setdefault(p, v)
                _flatten(v, p, depth + 1, out)
            else:
                out.setdefault(p, v)
    return out


def _lookup(flat, field):
    """Best value for field: prefer exact last-key matches at shallow depth."""
    wanted = FIELD_KEYS[field]
    best = None
    for path, val in flat.items():
        if val in (None, "", [], {}):
            continue
        parts = path.split(".")
        if field == "title" and len(parts) > 1 and _norm(parts[-2]) in NOT_TITLE_PARENTS:
            continue
        last = _norm(parts[-1])
        joined = _norm("".join(parts))
        tail2 = _norm("".join(parts[-2:])) if len(parts) > 1 else last
        for rank, key in enumerate(wanted):
            score = None
            if last == key:
                score = rank * 10 + len(parts)
            elif tail2 == key or joined == key:
                score = rank * 10 + len(parts) + 1
            if score is not None and (best is None or score < best[0]):
                best = (score, val)
    return best[1] if best else None


def _scalar(v):
    if isinstance(v, dict):
        for k in ("value", "name", "displayName", "label", "amount", "text", "@value"):
            if k in v and not isinstance(v[k], (dict, list)):
                return v[k]
        return None
    if isinstance(v, list):
        return _scalar(v[0]) if v else None
    return v


def to_int(v):
    v = _scalar(v)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d.]", "", str(v)).split(".")[0]
    return int(digits) if digits else None


def to_year(v):
    v = _scalar(v)
    if v is None:
        return None
    m = re.search(r"(19[89]\d|20[0-4]\d)", str(v))
    return int(m.group(1)) if m else None


def to_engine_l(v):
    v = _scalar(v)
    if v is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(v).replace(",", ""))
    if not m:
        return None
    x = float(m.group(1))
    if x > 30:  # cc
        x = x / 1000
    if x <= 0 or x > 10:
        return None
    return round(x, 1)


def to_text(v):
    v = _scalar(v)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _find_image(v, depth=0):
    if depth > 4 or v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("//"):
            s = "https:" + s
        if re.match(r"https?://", s) and (re.search(r"\.(jpe?g|png|webp|avif)", s, re.I) or "image" in s.lower() or "img" in s.lower() or "photo" in s.lower()):
            return s
        return None
    if isinstance(v, list):
        for x in v:
            r = _find_image(x, depth + 1)
            if r:
                return r
    if isinstance(v, dict):
        for k in ("large", "full", "original", "url", "src", "href", "contentUrl", "medium", "small", "uri", "path"):
            if k in v:
                r = _find_image(v[k], depth + 1)
                if r:
                    return r
        for x in v.values():
            r = _find_image(x, depth + 1)
            if r:
                return r
    return None


def _abs_url(u):
    if not u or not isinstance(u, str):
        return None
    u = u.strip()
    if u.startswith("http"):
        return u if "carzone" in u else None
    if u.startswith("/"):
        return urljoin(BASE, u)
    return None


def id_from_url(url):
    if not url:
        return None
    m = re.findall(r"(\d{6,})", url)
    return m[-1] if m else None


def make_model_from_url(url):
    """/used-cars/volkswagen/passat/fpa/123 -> ('Volkswagen', 'Passat')."""
    if not url:
        return None, None
    m = re.search(r"/used-cars/([^/]+)/([^/]+)/", url)
    if not m:
        return None, None
    nice = lambda s: " ".join(w.capitalize() if not w.isupper() else w for w in s.replace("-", " ").split())
    return nice(m.group(1)), nice(m.group(2))


def _mileage_km(value, unit, raw_text=""):
    km = to_int(value)
    if km is None:
        return None
    u = f"{unit or ''} {raw_text or ''} {value if isinstance(value, str) else ''}".lower()
    if re.search(r"\b(mi|mile|miles|smi)\b", u):
        km = int(km * 1.60934)
    return km


def _match_word(text, words):
    t = (text or "").lower()
    for w in words:
        if w in t:
            return w.title().replace("Suv", "SUV").replace("Mpv", "MPV").replace("Lpg", "LPG")
    return None


def normalize_obj(obj):
    """Return an ad dict if obj looks like a car listing, else None."""
    if not isinstance(obj, dict):
        return None
    flat = _flatten(obj)
    get = lambda f: _lookup(flat, f)

    url = None
    for cand in (get("url"),):
        url = _abs_url(_scalar(cand))
    if not url:
        for path, val in flat.items():
            if isinstance(val, str) and ("/fpa/" in val or re.search(r"/used-cars/.+/\d{6,}", val)):
                url = _abs_url(val)
                if url:
                    break

    make = to_text(get("make"))
    model = to_text(get("model"))
    title = to_text(get("title"))
    year = to_year(get("year"))
    price = to_int(get("price"))
    mileage = get("mileage")

    # A listing needs to identify a car and carry at least one number we care about.
    if not (make or title) or not (price or year or mileage):
        return None
    if isinstance(make, str) and len(make) > 40:
        make = None

    if title and make and title.strip().lower() in (make.lower(), (model or "").lower()):
        title = None
    u_make, u_model = make_model_from_url(url)
    make = make or u_make
    model = model or u_model

    ad_id = to_text(get("id"))
    if not ad_id or not re.search(r"\d", str(ad_id)):
        ad_id = id_from_url(url)
    if not ad_id:
        return None
    if not url:
        url = f"{BASE}/used-cars/{(make or 'car').lower()}/{(model or 'car').lower().replace(' ', '-')}/fpa/{ad_id}"

    fuel = to_text(get("fuel"))
    body = to_text(get("body"))
    ad = {
        "id": str(ad_id),
        "url": url,
        "title": title or " ".join(x for x in [str(year or ""), make, model] if x).strip(),
        "make": make,
        "model": model,
        "variant": to_text(get("variant")),
        "year": year,
        "price": price if price and 300 < price < 2_000_000 else None,
        "mileage_km": _mileage_km(mileage, _scalar(get("mileage_unit"))),
        "fuel": fuel,
        "body": body,
        "engine_l": to_engine_l(get("engine")),
        "transmission": to_text(get("transmission")),
        "colour": to_text(get("colour")),
        "county": to_text(get("county")),
        "seller": to_text(get("seller")),
        "image": _find_image(get("image")) or _find_image(obj),
    }
    if ad["title"] and len(ad["title"]) > 140:
        ad["title"] = ad["title"][:140]
    return ad


def walk_json(data, found=None, depth=0):
    """Collect every listing-shaped object anywhere in a JSON blob."""
    if found is None:
        found = {}
    if depth > 12:
        return found
    if isinstance(data, dict):
        ad = normalize_obj(data)
        if ad:
            prev = found.get(ad["id"])
            found[ad["id"]] = merge(prev, ad) if prev else ad
        for v in data.values():
            if isinstance(v, (dict, list)):
                walk_json(v, found, depth + 1)
    elif isinstance(data, list):
        for v in data:
            walk_json(v, found, depth + 1)
    return found


def merge(a, b):
    """Fill gaps in a with values from b."""
    if not a:
        return b
    out = dict(a)
    for k, v in b.items():
        if out.get(k) in (None, "") and v not in (None, ""):
            out[k] = v
    return out


def parse_card(card):
    """Fallback: a DOM card {href, text, img} -> ad dict."""
    url = _abs_url(card.get("href"))
    ad_id = id_from_url(url)
    if not ad_id:
        return None
    text = card.get("text") or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    make, model = make_model_from_url(url)
    price = None
    m = re.search(r"€\s?([\d,]{3,})", text)
    if m:
        price = to_int(m.group(1))
    km = None
    m = re.search(r"([\d,]{2,})\s?(km|kms|mi|miles)\b", text, re.I)
    if m:
        km = _mileage_km(m.group(1), m.group(2))
    year = None
    m = re.search(r"\b(20[0-4]\d|19[89]\d)\b", text)
    if m:
        year = int(m.group(1))
    eng = None
    m = re.search(r"\b(\d\.\d)\s?(l|litre|ltr)?\b", text, re.I)
    if m:
        eng = float(m.group(1))
    trans = "Automatic" if re.search(r"\bauto(matic)?\b", text, re.I) else ("Manual" if re.search(r"\bmanual\b", text, re.I) else None)
    title = next((l for l in lines if not l.startswith("€") and len(l) > 3), None)
    return {
        "id": ad_id, "url": url, "title": title, "make": make, "model": model, "variant": None,
        "year": year, "price": price, "mileage_km": km,
        "fuel": _match_word(text, FUELS), "body": _match_word(text, BODIES),
        "engine_l": eng, "transmission": trans, "colour": None, "county": None, "seller": None,
        "image": card.get("img") if (card.get("img") or "").startswith("http") else None,
    }
