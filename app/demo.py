"""DEMO=1 fills the database with made-up ads so you can try the interface."""
import random
from urllib.parse import quote

from . import db

CARS = [
    ("Skoda", "Octavia", "Estate", ["Diesel", "Petrol", "Plug-in Hybrid"], [1.6, 2.0, 1.4]),
    ("Volkswagen", "Passat", "Estate", ["Diesel", "Plug-in Hybrid"], [2.0, 1.4]),
    ("Volvo", "V60", "Estate", ["Diesel", "Plug-in Hybrid"], [2.0]),
    ("Volvo", "V90", "Estate", ["Diesel", "Plug-in Hybrid"], [2.0]),
    ("Audi", "A6", "Estate", ["Diesel"], [2.0, 3.0]),
    ("Audi", "Q7", "SUV", ["Diesel"], [3.0]),
    ("BMW", "5 Series", "Estate", ["Diesel", "Plug-in Hybrid"], [2.0, 3.0]),
    ("Mercedes-Benz", "E-Class", "Estate", ["Diesel", "Hybrid"], [2.0, 3.0]),
    ("Toyota", "Corolla", "Estate", ["Hybrid"], [1.8, 2.0]),
    ("Kia", "Ceed", "Estate", ["Plug-in Hybrid", "Diesel"], [1.6]),
    ("Ford", "Mondeo", "Estate", ["Hybrid", "Diesel"], [2.0]),
    ("Ford", "Focus", "Estate", ["Diesel"], [1.5]),
    ("MG", "5", "Estate", ["Electric"], [None]),
    ("Peugeot", "e-308 SW", "Estate", ["Electric"], [None]),
    ("Volkswagen", "ID.7 Tourer", "Estate", ["Electric"], [None]),
]
COUNTIES = ["Dublin", "Cork", "Galway", "Kildare", "Meath", "Limerick", "Wicklow", "Louth"]
COLOURS = [("Grey", "#7b8288"), ("Black", "#2a2d31"), ("White", "#e9ecef"), ("Blue", "#35557a"),
           ("Silver", "#b5bcc3"), ("Red", "#8e2f2f")]


def _img(colour_hex, label):
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 300'>
<rect width='400' height='300' fill='#cfd6db'/><rect y='210' width='400' height='90' fill='#9aa3aa'/>
<path d='M60 200 L90 150 Q100 132 125 130 L270 128 Q300 128 318 150 L350 178 Q362 186 362 200 L362 212 L60 212 Z' fill='{colour_hex}'/>
<path d='M112 150 L130 138 L205 136 L205 160 L104 162 Z M215 136 L268 136 Q288 138 300 160 L215 160 Z' fill='#dfe7ee' opacity='.85'/>
<circle cx='120' cy='214' r='26' fill='#1d2125'/><circle cx='120' cy='214' r='11' fill='#8a9299'/>
<circle cx='305' cy='214' r='26' fill='#1d2125'/><circle cx='305' cy='214' r='11' fill='#8a9299'/>
<text x='20' y='40' font-family='sans-serif' font-size='22' fill='#4a545c'>{label}</text></svg>"""
    return "data:image/svg+xml;utf8," + quote(svg)


def seed():
    if db.q("SELECT COUNT(*) n FROM ads", one=True)["n"]:
        return
    rnd = random.Random(7)
    s1 = db.save_search({"name": "Electric estate 2022+", "url": "https://www.carzone.ie/used-cars?demo=electric",
                         "min_year": 2022, "fuel": "Electric", "body": "Estate"})
    s2 = db.save_search({"name": "Hybrid estate", "url": "https://www.carzone.ie/used-cars?demo=hybrid",
                         "max_km": 200000, "fuel": "Hybrid", "body": "Estate"})
    s3 = db.save_search({"name": "Diesel 3L", "url": "https://www.carzone.ie/used-cars?demo=diesel",
                         "max_km": 150000, "fuel": "Diesel", "min_engine": 2.9, "max_engine": 3.1})
    for i in range(70):
        make, model, body, fuels, engines = rnd.choice(CARS)
        fuel = rnd.choice(fuels)
        year = rnd.randint(2017, 2025)
        eng = rnd.choice(engines) if fuel != "Electric" else None
        km = max(3000, int((2026 - year) * rnd.randint(9000, 26000)))
        base = {"Audi": 60000, "BMW": 58000, "Mercedes-Benz": 62000, "Volvo": 52000}.get(make, 38000)
        price = int(base * (0.86 ** (2026 - year)) * rnd.uniform(0.85, 1.12) / 50) * 50
        colour, hexc = rnd.choice(COLOURS)
        ad = {
            "id": str(4100000 + i), "url": f"https://www.carzone.ie/used-cars/{make.lower()}/{model.lower().replace(' ', '-')}/fpa/{4100000 + i}",
            "title": f"{year} {make} {model}", "make": make, "model": model,
            "variant": rnd.choice(["SE", "Sport", "R-Line", "Business Edition", "Momentum", "S line", None]),
            "year": year, "price": price, "mileage_km": km, "fuel": fuel, "body": body, "engine_l": eng,
            "transmission": rnd.choice(["Automatic", "Manual", "Automatic"]), "colour": colour,
            "county": rnd.choice(COUNTIES), "seller": rnd.choice(["Dealer", "Dealer", "Private"]),
            "image": _img(hexc, f"{make} {model}"),
        }
        sid = s1 if fuel == "Electric" else s2 if "Hybrid" in fuel else s3
        db.upsert_ad(ad, sid)
        if i % 9 == 0:  # a few price drops
            ad["price"] = price - 750
            db.upsert_ad(ad, sid)
