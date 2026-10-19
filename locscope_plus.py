"""
LocScope - round 4: fix-ups, parse cache, data quality, POTA parks & other collections, holidays,
family-history places, snowy-week planner, place types, calendar (.ics) export, time together.

Used by locscope.py / locscope_extras.py; not meant to be run by itself.
"""

import csv
import io
import json
import math
import os
import pickle
import sqlite3
import time
import urllib.parse
import urllib.request
import zlib
from collections import defaultdict, Counter
from datetime import datetime, timedelta, date, timezone

from locscope_extras import hav, PlaceIndex


# ==========================================================================
# Fix-ups (my_fixes.csv)
# ==========================================================================

FIX_HELP = "type,date_from,date_to,lat,lon,radius_m,name,note"
FIX_TYPES = ("rename", "remove_stop", "ignore_area", "ignore_dates")


def load_fixes(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            t = r.get("type", "").lower().replace(" ", "_")
            if t not in FIX_TYPES:
                continue
            fx = dict(type=t, name=r.get("name", ""), note=r.get("note", ""))
            for k in ("date_from", "date_to"):
                try:
                    fx[k] = date.fromisoformat(r.get(k, "")[:10]) if r.get(k) else None
                except ValueError:
                    fx[k] = None
            fx["lat"] = _f(r.get("lat"))
            fx["lon"] = _f(r.get("lon"))
            fx["radius"] = _f(r.get("radius_m")) or (150.0 if t != "ignore_area" else 200.0)
            fx["time"] = r.get("time", "")
            out.append(fx)
    print(f"  loaded {len(out)} fix-ups from {path}")
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fixes_to_named(fixes):
    """'rename' fix-ups behave exactly like entries in my_places.csv."""
    return [dict(name=f["name"], lat=f["lat"], lon=f["lon"], radius=f["radius"], address="", work=False, private=False)
            for f in fixes if f["type"] == "rename" and f["name"] and f["lat"] is not None and f["lon"] is not None]


def apply_fixes(an, fixes):
    if not fixes:
        return 0
    before = len(an.points) + len(an.visits) + len(an.activities)

    def in_dates(fx, d):
        return (fx["date_from"] is None or d >= fx["date_from"]) and (fx["date_to"] is None or d <= (fx["date_to"] or fx["date_from"]))

    def near(fx, lat, lon):
        return fx["lat"] is not None and lat is not None and hav(lat, lon, fx["lat"], fx["lon"]) <= fx["radius"]

    dates = [f for f in fixes if f["type"] == "ignore_dates" and f["date_from"]]
    areas = [f for f in fixes if f["type"] == "ignore_area" and f["lat"] is not None]
    stops = [f for f in fixes if f["type"] == "remove_stop" and f["lat"] is not None and f["date_from"]]

    def drop_point(d, lat, lon):
        return any(in_dates(f, d) for f in dates) or any(in_dates(f, d) and near(f, lat, lon) for f in areas)

    an.points = [p for p in an.points if not drop_point(p[4].date(), p[1], p[2])]
    keep = []
    for v in an.visits:
        d = an.local(v["start"]).date()
        if drop_point(d, v["lat"], v["lon"]):
            continue
        if any(d == f["date_from"] and near(f, v["lat"], v["lon"]) and (not f["time"] or an.local(v["start"]).strftime("%H:%M") == f["time"])
               for f in stops):
            continue
        keep.append(v)
    an.visits = keep
    an.activities = [x for x in an.activities if not any(in_dates(f, an.local(x["start"]).date()) for f in dates)]
    n = before - (len(an.points) + len(an.visits) + len(an.activities))
    if n:
        print(f"  fix-ups removed {n:,} points/stops/trips")
    return n


# ==========================================================================
# Parse cache (SQLite): unchanged input files load instantly on the next run
# ==========================================================================

CACHE_VERSION = 3


class ParseCache:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, blob BLOB, saved REAL)")
        self.hits = self.misses = 0

    @staticmethod
    def key(path, extra=""):
        st = os.stat(path)
        return f"v{CACHE_VERSION}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{extra}"

    def get(self, key):
        r = self.db.execute("SELECT blob FROM files WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return pickle.loads(zlib.decompress(r[0]))
        except Exception:  # noqa: BLE001
            return None

    def put(self, key, obj):
        try:
            blob = zlib.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), 1)
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (key, blob, time.time()))
            self.db.commit()
        except Exception as ex:  # noqa: BLE001
            print(f"  (couldn't cache {key.split('|')[1]}: {ex})")

    def close(self):
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


STORE_FIELDS = ("points", "visits", "activities", "pins", "labeled_places", "skipped")


def store_snapshot(st):
    return dict(points=st.points, visits=st.visits, activities=st.activities, pins=st.pins,
                labeled_places=st.labeled_places, skipped=st.skipped, labeled=dict(st.labeled), sources=dict(st.sources))


def store_merge(st, snap):
    for k in STORE_FIELDS:
        getattr(st, k).extend(snap.get(k, []))
    for k, v in snap.get("labeled", {}).items():
        st.labeled.setdefault(k, v)
    for k, v in snap.get("sources", {}).items():
        st.sources[k] += v


# ==========================================================================
# 10-minute presence slots (used by data quality and time together)
# ==========================================================================

SLOT = 600


def presence(points, visits, activities=()):
    """slot -> (lat, lon) for every 10 minutes we know where someone was."""
    slots = {}
    for v in visits:
        a, b = int(v["start"].timestamp() // SLOT), int(v["end"].timestamp() // SLOT)
        for s in range(a, min(b, a + 3 * 144) + 1):
            slots.setdefault(s, (v["lat"], v["lon"]))
    for p in points:
        slots.setdefault(int(p[0] // SLOT), (p[1], p[2]))
    for x in activities:
        a, b = int(x["start"].timestamp() // SLOT), int(x["end"].timestamp() // SLOT)
        for s in range(a, min(b, a + 144) + 1):
            if s not in slots and x["slat"] is not None:
                f = (s - a) / max(1, b - a)
                ea = x["elat"] if x["elat"] is not None else x["slat"]
                eo = x["elon"] if x["elon"] is not None else x["slon"]
                slots[s] = (x["slat"] + (ea - x["slat"]) * f, x["slon"] + (eo - x["slon"]) * f)
    return slots


def quality(an, slots):
    if not an.days:
        return None
    per = Counter()
    tz_of = {}
    for s in slots:
        dt = an.local(datetime.fromtimestamp(s * SLOT, timezone.utc))
        per[dt.date()] += 1
    first, last = min(an.days), max(an.days)
    days = {}
    d = first
    while d <= last:
        days[d] = min(100, round(per.get(d, 0) / 144 * 100))
        d += timedelta(days=1)
    gaps = []
    run = None
    for d in sorted(days):
        if days[d] == 0:
            run = [d, d] if run is None else [run[0], d]
        else:
            if run and (run[1] - run[0]).days >= 1:
                gaps.append(run)
            run = None
    if run and (run[1] - run[0]).days >= 1:
        gaps.append(run)
    months = defaultdict(list)
    for d, v in days.items():
        months[d.strftime("%Y-%m")].append(v)
    total = len(days)
    with_data = sum(1 for v in days.values() if v > 0)
    return dict(days={d.isoformat(): v for d, v in days.items()},
                gaps=sorted(([g[0].isoformat(), g[1].isoformat(), (g[1] - g[0]).days + 1] for g in gaps), key=lambda g: -g[2]),
                months=[[m, round(sum(v) / len(v))] for m, v in sorted(months.items())],
                span=total, with_data=with_data, avg=round(sum(days.values()) / max(1, total)))


# ==========================================================================
# Collections: POTA parks (downloaded once), national parks etc. (from POTA), state capitals
# ==========================================================================

POTA_URL = "https://pota.app/all_parks_ext.csv"

CAPITALS = [  # (state, city, lat, lon)
    ("Alabama", "Montgomery", 32.3770, -86.3006), ("Alaska", "Juneau", 58.3016, -134.4197), ("Arizona", "Phoenix", 33.4484, -112.0740),
    ("Arkansas", "Little Rock", 34.7465, -92.2896), ("California", "Sacramento", 38.5767, -121.4934), ("Colorado", "Denver", 39.7392, -104.9903),
    ("Connecticut", "Hartford", 41.7637, -72.6851), ("Delaware", "Dover", 39.1582, -75.5244), ("Florida", "Tallahassee", 30.4383, -84.2807),
    ("Georgia", "Atlanta", 33.7490, -84.3880), ("Hawaii", "Honolulu", 21.3069, -157.8583), ("Idaho", "Boise", 43.6150, -116.2023),
    ("Illinois", "Springfield", 39.7817, -89.6501), ("Indiana", "Indianapolis", 39.7684, -86.1581), ("Iowa", "Des Moines", 41.5868, -93.6250),
    ("Kansas", "Topeka", 39.0473, -95.6752), ("Kentucky", "Frankfort", 38.2009, -84.8733), ("Louisiana", "Baton Rouge", 30.4515, -91.1871),
    ("Maine", "Augusta", 44.3106, -69.7795), ("Maryland", "Annapolis", 38.9784, -76.4922), ("Massachusetts", "Boston", 42.3601, -71.0589),
    ("Michigan", "Lansing", 42.7325, -84.5555), ("Minnesota", "Saint Paul", 44.9537, -93.0900), ("Mississippi", "Jackson", 32.2988, -90.1848),
    ("Missouri", "Jefferson City", 38.5767, -92.1735), ("Montana", "Helena", 46.5891, -112.0391), ("Nebraska", "Lincoln", 40.8136, -96.7026),
    ("Nevada", "Carson City", 39.1638, -119.7674), ("New Hampshire", "Concord", 43.2081, -71.5376), ("New Jersey", "Trenton", 40.2206, -74.7597),
    ("New Mexico", "Santa Fe", 35.6870, -105.9378), ("New York", "Albany", 42.6526, -73.7562), ("North Carolina", "Raleigh", 35.7796, -78.6382),
    ("North Dakota", "Bismarck", 46.8083, -100.7837), ("Ohio", "Columbus", 39.9612, -82.9988), ("Oklahoma", "Oklahoma City", 35.4676, -97.5164),
    ("Oregon", "Salem", 44.9429, -123.0351), ("Pennsylvania", "Harrisburg", 40.2732, -76.8867), ("Rhode Island", "Providence", 41.8240, -71.4128),
    ("South Carolina", "Columbia", 34.0007, -81.0348), ("South Dakota", "Pierre", 44.3683, -100.3510), ("Tennessee", "Nashville", 36.1627, -86.7816),
    ("Texas", "Austin", 30.2672, -97.7431), ("Utah", "Salt Lake City", 40.7608, -111.8910), ("Vermont", "Montpelier", 44.2601, -72.5754),
    ("Virginia", "Richmond", 37.5407, -77.4360), ("Washington", "Olympia", 47.0379, -122.9007), ("West Virginia", "Charleston", 38.3498, -81.6326),
    ("Wisconsin", "Madison", 43.0731, -89.4012), ("Wyoming", "Cheyenne", 41.1400, -104.8202), ("District of Columbia", "Washington", 38.9072, -77.0369),
]

NPS_KINDS = [("National Park", "National parks"), ("National Monument", "National monuments"),
             ("National Historic", "National historic sites & parks"), ("National Seashore", "National seashores & lakeshores"),
             ("National Lakeshore", "National seashores & lakeshores"), ("National Recreation Area", "National recreation areas"),
             ("National Wildlife Refuge", "National wildlife refuges"), ("National Forest", "National forests"),
             ("National Battlefield", "National battlefields & military parks"), ("National Military Park", "National battlefields & military parks")]


def ensure_pota(folder, allow=True):
    path = os.path.join(folder, "pota_parks.csv")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 180 * 86400:
        return path
    if not allow:
        return path if os.path.exists(path) else None
    try:
        print("  downloading the Parks on the Air park list (one time, refreshed twice a year)...")
        req = urllib.request.Request(POTA_URL, headers={"User-Agent": "LocScope (personal location-history analyzer)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        os.makedirs(folder, exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
    except Exception as ex:  # noqa: BLE001
        print(f"  couldn't download the POTA park list ({ex}); you can save {POTA_URL} as {path}")
        return path if os.path.exists(path) else None
    return path


def load_pota(path):
    parks = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rd = csv.DictReader(f)
        cols = {c.lower(): c for c in (rd.fieldnames or [])}
        g = lambda row, *names: next((row.get(cols[n]) for n in names if n in cols), "")
        for row in rd:
            try:
                lat, lon = float(g(row, "latitude", "lat")), float(g(row, "longitude", "lon", "lng"))
            except (TypeError, ValueError):
                continue
            active = str(g(row, "active") or "1").strip()
            if active in ("0", "false", "False"):
                continue
            parks.append(dict(ref=g(row, "reference", "ref"), name=g(row, "name"), loc=g(row, "locationdesc", "location"),
                              lat=lat, lon=lon))
    return parks


def _grid_index(items, deg=1.0):
    gi = defaultdict(list)
    for it in items:
        gi[(int(math.floor(it["lat"] / deg)), int(math.floor(it["lon"] / deg)))].append(it)
    return gi


def collections(an, geodir, home, allow_download=True):
    """Which POTA parks / NPS units / state capitals you've been at (within a radius of their listed point)."""
    cells = defaultdict(set)
    for p in an.points:
        cells[(round(p[1], 3), round(p[2], 3))].add(p[4].date())
    for v in an.visits:
        cells[(round(v["lat"], 3), round(v["lon"], 3))].add(an.local(v["start"]).date())
    pts = list(cells.items())

    def visited(items, radius_m):
        gi = _grid_index(items)
        hits = {}
        for (la, lo), ds in pts:
            cy, cx = int(math.floor(la)), int(math.floor(lo))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for it in gi.get((cy + dy, cx + dx), ()):
                        r = it.get("r", radius_m)
                        if abs(it["lat"] - la) * 111000 <= r and hav(la, lo, it["lat"], it["lon"]) <= r:
                            hits.setdefault(id(it), [it, set()])[1] |= ds
        return hits
    out = {}
    caps = [dict(ref=s, name=f"{c}, {s}", lat=la, lon=lo, r=10000) for s, c, la, lo in CAPITALS]
    hc = visited(caps, 10000)
    out["capitals"] = dict(total=len(caps), items=[[c["name"], c["lat"], c["lon"], len(hc[id(c)][1]) if id(c) in hc else 0,
                                                    min(hc[id(c)][1]).isoformat() if id(c) in hc else "",
                                                    max(hc[id(c)][1]).isoformat() if id(c) in hc else ""] for c in caps])
    path = ensure_pota(geodir, allow_download) if geodir else None
    if path and os.path.exists(path):
        try:
            parks = load_pota(path)
        except Exception as ex:  # noqa: BLE001
            print(f"  couldn't read {path}: {ex}")
            parks = []
        if parks:
            for p in parks:
                n = p["name"].lower()
                p["r"] = 3000 if any(k in n for k in ("national park", "national forest", "wilderness", "national recreation", "state forest")) else 1000
            hp = visited(parks, 1000)
            vis = sorted((h[0] for h in hp.values()), key=lambda p: p["ref"])
            out["pota"] = dict(total=len(parks), visited=[[p["ref"], p["name"], p["loc"], round(p["lat"], 4), round(p["lon"], 4),
                                                          len(hp[id(p)][1]), min(hp[id(p)][1]).isoformat(), max(hp[id(p)][1]).isoformat()]
                                                         for p in vis])
            if home:
                near = sorted(((hav(home["lat"], home["lon"], p["lat"], p["lon"]) / 1000, p) for p in parks
                               if abs(p["lat"] - home["lat"]) < 1 and id(p) not in hp), key=lambda t: t[0])
                out["pota"]["nearby_unvisited"] = [[p["ref"], p["name"], round(p["lat"], 4), round(p["lon"], 4), round(km, 1)]
                                                   for km, p in near if km <= 60][:40]
            groups = defaultdict(list)
            for p in parks:
                if not p["ref"].startswith(("K-", "US-")):
                    continue
                for key, label in NPS_KINDS:
                    if key.lower() in p["name"].lower():
                        groups[label].append(p)
                        break
            nps = {}
            for label, items in groups.items():
                hit = [p for p in items if id(p) in hp]
                nps[label] = dict(total=len(items), visited=[[p["name"], p["loc"], round(p["lat"], 4), round(p["lon"], 4),
                                                              len(hp[id(p)][1]), min(hp[id(p)][1]).isoformat()] for p in hit])
            out["nps"] = nps
    return out


# ==========================================================================
# Holidays
# ==========================================================================

def _nth(y, m, weekday, n):
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last(y, m, weekday):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


HOLIDAYS = [
    ("New Year's Day", lambda y: date(y, 1, 1)), ("Martin Luther King Jr. Day", lambda y: _nth(y, 1, 0, 3)),
    ("Valentine's Day", lambda y: date(y, 2, 14)), ("Presidents' Day", lambda y: _nth(y, 2, 0, 3)), ("Easter", _easter),
    ("Mother's Day", lambda y: _nth(y, 5, 6, 2)), ("Memorial Day", lambda y: _last(y, 5, 0)), ("Father's Day", lambda y: _nth(y, 6, 6, 3)),
    ("Juneteenth", lambda y: date(y, 6, 19)), ("Independence Day", lambda y: date(y, 7, 4)), ("Labor Day", lambda y: _nth(y, 9, 0, 1)),
    ("Halloween", lambda y: date(y, 10, 31)), ("Veterans Day", lambda y: date(y, 11, 11)), ("Thanksgiving", lambda y: _nth(y, 11, 3, 4)),
    ("Christmas Eve", lambda y: date(y, 12, 24)), ("Christmas Day", lambda y: date(y, 12, 25)), ("New Year's Eve", lambda y: date(y, 12, 31)),
]


def holidays(an, home):
    from locscope import display_name, region_label
    m = an.x.get("more") or {}
    dayloc = m.get("dayloc") or {}
    idx = PlaceIndex(an)
    years = sorted({d.year for d in an.days})
    rows = []
    for name, fn in HOLIDAYS:
        cells = []
        for y in years:
            d = fn(y)
            ll = dayloc.get(d)
            if not ll:
                cells.append(None)
                continue
            p = idx.nearest(ll[0], ll[1], 400)
            if p is not None:
                lab = display_name(an, p)
            else:
                r = an.regions.lookup(*ll) if an.regions is not None else None
                lab = ("Near " + region_label(r)) if r else "%.3f, %.3f" % ll
            far = an.days.get(d, {}).get("far", 0) / 1000 if d in an.days else 0
            cells.append([d.isoformat(), lab, round(far), 1 if home and far >= 50 else 0])
        rows.append([name, cells])
    return dict(years=years, rows=rows)


# ==========================================================================
# Family-history places (my_family_places.csv: name, place, lat, lon, radius_km, note)
# ==========================================================================

def load_family(path, geo):
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not r.get("name") and not r.get("place"):
                continue
            lat, lon = _f(r.get("lat")), _f(r.get("lon"))
            if (lat is None or lon is None) and r.get("place") and geo is not None:
                g = geo.search(r["place"])
                if g:
                    lat, lon = g[0], g[1]
            if lat is None or lon is None:
                print(f"  family places: couldn't locate '{r.get('place') or r.get('name')}'")
                continue
            out.append(dict(name=r.get("name") or r.get("place"), place=r.get("place", ""), lat=lat, lon=lon,
                            r=(_f(r.get("radius_km")) or 15.0) * 1000, note=r.get("note", "")))
    print(f"  loaded {len(out)} family-history places")
    return out


def family(an, fam, home):
    if not fam:
        return None
    res = []
    for fp in fam:
        days = set()
        for p in an.points:
            if abs(p[1] - fp["lat"]) < fp["r"] / 100000 + .01 and hav(p[1], p[2], fp["lat"], fp["lon"]) <= fp["r"]:
                days.add(p[4].date())
        for v in an.visits:
            if abs(v["lat"] - fp["lat"]) < fp["r"] / 100000 + .01 and hav(v["lat"], v["lon"], fp["lat"], fp["lon"]) <= fp["r"]:
                days.add(an.local(v["start"]).date())
        res.append([fp["name"], fp["place"], round(fp["lat"], 4), round(fp["lon"], 4), round(fp["r"] / 1000),
                    fp["note"], len(days), min(days).isoformat() if days else "", max(days).isoformat() if days else "",
                    round(hav(home["lat"], home["lon"], fp["lat"], fp["lon"]) / 1000) if home else None, sorted(d.isoformat() for d in days)[-30:]])
    return res


# ==========================================================================
# Snowy-week planner (Open-Meteo history at home)
# ==========================================================================

def snowy_weeks(an, home, cache_path, years_back=30):
    if not home:
        return None
    cell = (round(home["lat"] * 4) / 4, round(home["lon"] * 4) / 4)
    today = date.today()
    end = date(today.year - 1, 12, 31)
    start = date(end.year - years_back + 1, 1, 1)
    key = f"{cell[0]},{cell[1]}|{start}|{end}"
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    if key not in cache:
        print(f"Snowy-week planner: getting {years_back} years of daily snowfall near home from Open-Meteo "
              f"(sends a ~25 km grid point, not your address)...")
        url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(dict(
            latitude=cell[0], longitude=cell[1], start_date=start.isoformat(), end_date=end.isoformat(),
            daily="snowfall_sum,temperature_2m_min", timezone="auto"))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LocScope (personal location-history analyzer)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.load(r)
            cache[key] = dict(time=j["daily"]["time"], snow=j["daily"]["snowfall_sum"], tmin=j["daily"]["temperature_2m_min"])
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            except OSError:
                pass
        except Exception as ex:  # noqa: BLE001
            print(f"  couldn't get snowfall history: {ex}")
            return None
    h = cache[key]
    snow = {date.fromisoformat(t): (s or 0.0) for t, s in zip(h["time"], h["snow"])}
    yrs = sorted({d.year for d in snow})

    def week_stats(mon_md):  # (month, day) of a Monday -> stats over all years for that 7-day window
        hit = tot = 0
        amounts = []
        for y in yrs:
            try:
                d0 = date(y, *mon_md)
            except ValueError:
                d0 = date(y, mon_md[0], 28)
            w = [snow.get(d0 + timedelta(days=k), None) for k in range(7)]
            w = [x for x in w if x is not None]
            if len(w) < 5:
                continue
            tot += 1
            s = sum(w)
            amounts.append(s)
            if max(w) >= 2.54:  # a day with an inch or more
                hit += 1
        return (round(100 * hit / tot) if tot else None, round(sum(amounts) / len(amounts), 1) if amounts else None,
                round(max(amounts), 1) if amounts else None)
    # upcoming season: next Monday through the end of April
    nxt = today + timedelta(days=(7 - today.weekday()) % 7)
    season_end = date(nxt.year + (1 if nxt.month > 4 else 0), 4, 30)
    upcoming = []
    d = nxt
    while d <= season_end:
        pct, avg, mx = week_stats((d.month, d.day))
        if pct is not None:
            upcoming.append([d.isoformat(), pct, avg, mx])
        d += timedelta(days=7)
    # whole-year climatology by week starting Jan 1
    clim = []
    for k in range(52):
        dd = date(2001, 1, 1) + timedelta(days=7 * k)
        pct, avg, mx = week_stats((dd.month, dd.day))
        clim.append([dd.strftime("%m-%d"), pct or 0, avg or 0])
    return dict(cell=cell, years=[yrs[0], yrs[-1]], upcoming=upcoming, clim=clim)


# ==========================================================================
# Place types (from OpenStreetMap categories of looked-up addresses)
# ==========================================================================

TYPE_GROUPS = [
    ("Gas", {"amenity": ["fuel", "charging_station"]}),
    ("Groceries", {"shop": ["supermarket", "grocery", "convenience", "greengrocer", "butcher", "bakery", "deli"]}),
    ("Food & drink", {"amenity": ["restaurant", "cafe", "fast_food", "bar", "pub", "ice_cream", "food_court", "biergarten"]}),
    ("Health & medical", {"amenity": ["hospital", "clinic", "doctors", "dentist", "pharmacy", "veterinary"], "healthcare": ["*"]}),
    ("Shopping", {"shop": ["*"]}),
    ("Parks & outdoors", {"leisure": ["park", "nature_reserve", "garden", "playground", "beach_resort", "marina", "golf_course"],
                          "natural": ["*"], "boundary": ["national_park", "protected_area"]}),
    ("Fitness & sports", {"leisure": ["fitness_centre", "sports_centre", "stadium", "swimming_pool", "pitch", "track", "ice_rink"]}),
    ("Lodging", {"tourism": ["hotel", "motel", "guest_house", "hostel", "camp_site", "caravan_site", "apartment"]}),
    ("Sights & culture", {"tourism": ["*"], "historic": ["*"], "amenity": ["theatre", "cinema", "arts_centre", "library"]}),
    ("Education", {"amenity": ["school", "university", "college", "kindergarten"]}),
    ("Worship", {"amenity": ["place_of_worship"]}),
    ("Travel & transit", {"aeroway": ["*"], "railway": ["*"], "public_transport": ["*"], "amenity": ["bus_station", "ferry_terminal", "parking"]}),
    ("Offices & services", {"office": ["*"], "amenity": ["bank", "post_office", "townhall", "courthouse", "police", "fire_station"], "craft": ["*"]}),
    ("Homes & neighborhoods", {"building": ["house", "residential", "apartments", "detached"], "place": ["*"], "highway": ["*"], "landuse": ["residential"]}),
]


def type_group(cat, typ):
    if not cat:
        return None
    for name, rules in TYPE_GROUPS:
        allowed = rules.get(cat)
        if allowed and ("*" in allowed or typ in allowed):
            return name
    return "Other"


def place_types(an):
    if not any(p.get("ptype") for p in an.places.values()):
        return None
    from locscope import display_name
    years = defaultdict(Counter)
    visits = defaultdict(Counter)
    tops = defaultdict(Counter)
    months_gas = Counter()
    for v in an.visits:
        p = an.places.get(v.get("place"))
        g = p.get("ptype") if p else None
        if not g:
            continue
        if p is an.home:
            g = "Home"
        elif p is an.work:
            g = "Work"
        ls = an.local(v["start"])
        h = (v["end"] - v["start"]).total_seconds() / 3600
        years[ls.year][g] += h
        visits[ls.year][g] += 1
        tops[g][display_name(an, p)] += h
        if g == "Gas":
            months_gas[ls.strftime("%Y-%m")] += 1
    return dict(years={y: {g: round(h, 1) for g, h in c.items()} for y, c in sorted(years.items())},
                visits={y: dict(c) for y, c in sorted(visits.items())},
                tops={g: [[n, round(h, 1)] for n, h in c.most_common(8)] for g, c in tops.items()},
                gas=sorted(months_gas.items()))


# ==========================================================================
# Calendar files (.ics) for Outlook / Google Calendar
# ==========================================================================

def _ics_text(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_ics(an, folder):
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def cal(name, events):
        if not events:
            return None
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//LocScope//EN", f"X-WR-CALNAME:{_ics_text(name)}"]
        for i, (uid, summary, start, end, allday, desc) in enumerate(events):
            lines += ["BEGIN:VEVENT", f"UID:{uid}@locscope", f"DTSTAMP:{stamp}", f"SUMMARY:{_ics_text(summary)}"]
            if allday:
                lines += [f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"]
            else:
                lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]
            if desc:
                lines.append(f"DESCRIPTION:{_ics_text(desc)}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        path = os.path.join(folder, name.lower().replace(" ", "_").replace("&", "and") + ".ics")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return path
    m = an.x.get("more") or {}
    trips = []
    for t in an.trips:
        towns = set()
        d = t["start"]
        while d <= t["end"]:
            towns |= set(getattr(an, "day_cities", {}).get(d, ()))
            d += timedelta(days=1)
        far_town = ""
        if an.regions is not None and t.get("far_ll"):
            from locscope import region_label
            far_town = region_label(an.regions.lookup(*t["far_ll"]))
        trips.append((f"trip-{t['start']}", f"Trip{': ' + far_town if far_town else ''}", t["start"], t["end"] + timedelta(days=1), True,
                      f"{t['days']} days, up to {t['far'] / 1000:.0f} km from home. Towns: {', '.join(sorted(towns)[:15])}"))
    wh = an.x.get("work")
    shifts = [(f"shift-{r['arrive'].isoformat()}", f"Work: {r['place']}", r["arrive"], r["leave"], False, f"{r['hours']:.1f} hours")
              for r in (wh["rows"] if wh else [])]
    ci = m.get("callins") or {}
    calls = [(f"call-{c['arrive'].isoformat()}", f"Call-in ({c['why'].lower()}): {c['place']}", c["arrive"], c["leave"], False,
              f"{c['hours']:.1f} hours; on-call week of {c['week'].isoformat()}") for c in ci.get("rows", [])]
    flights = [(f"flight-{f['start'].isoformat()}", f"Flight {f['frm'][0] or f['frm'][1]} → {f['to'][0] or f['to'][1]}", f["start"],
                max(f["end"], f["start"] + timedelta(minutes=30)), False, f"{f['km']:.0f} km") for f in (m.get("flights") or [])]
    nights = []
    run = None
    for n in (m.get("nights") or []):
        if n["kind"] == "away":
            if run and run["label"] == n["label"] and (n["date"] - run["end"]).days == 1:
                run["end"] = n["date"]
            else:
                run = dict(label=n["label"], start=n["date"], end=n["date"])
                nights.append(run)
    stays = [(f"stay-{r['start']}", f"Stayed at {r['label']}", r["start"], r["end"] + timedelta(days=1), True, "") for r in nights]
    out = [cal("LocScope trips", trips), cal("LocScope work shifts", shifts), cal("LocScope call-ins", calls),
           cal("LocScope flights", flights), cal("LocScope overnight stays", stays)]
    return [p for p in out if p]


# ==========================================================================
# Time together (another person's export)
# ==========================================================================

def together(an, other_store, name, my_slots, radius=250.0):
    if other_store is None or not (other_store.points or other_store.visits):
        return None
    pts = sorted((p[0], p[1], p[2], p[3], p[4]) for p in other_store.points)
    theirs = presence(pts, other_store.visits, other_store.activities)
    idx = PlaceIndex(an)
    from locscope import display_name, region_label
    both = sorted(s for s in my_slots if s in theirs)
    hit = [s for s in both if hav(*my_slots[s], *theirs[s]) <= radius]
    per_day = defaultdict(lambda: [0, Counter()])
    place_hours = Counter()
    lab_cache = {}
    for s in hit:
        ll = my_slots[s]
        k = (round(ll[0], 3), round(ll[1], 3))
        if k not in lab_cache:
            p = idx.nearest(ll[0], ll[1], 300)
            if p is not None:
                lab_cache[k] = display_name(an, p)
            else:
                r = an.regions.lookup(*ll) if an.regions is not None else None
                lab_cache[k] = ("Near " + region_label(r)) if r else "%.3f, %.3f" % ll
        d = an.local(datetime.fromtimestamp(s * SLOT, timezone.utc)).date()
        per_day[d][0] += 1
        per_day[d][1][lab_cache[k]] += 1
        place_hours[lab_cache[k]] += 1 / 6
    longest = best = 0
    best_end = None
    prev = None
    for s in hit:
        longest = longest + 1 if prev is not None and s == prev + 1 else 1
        if longest > best:
            best, best_end = longest, s
        prev = s
    overlap_days = {an.local(datetime.fromtimestamp(s * SLOT, timezone.utc)).date() for s in both}
    return dict(name=name or "the other person",
                days=[[d.isoformat(), round(v[0] / 6, 1), [n for n, _ in v[1].most_common(3)]] for d, v in sorted(per_day.items())],
                hours=round(len(hit) / 6), both_days=len(overlap_days),
                places=[[n, round(h, 1)] for n, h in place_hours.most_common(15)],
                longest=dict(hours=round(best / 6, 1),
                             end=an.local(datetime.fromtimestamp(best_end * SLOT, timezone.utc)).isoformat(timespec="minutes") if best_end else None))


# ==========================================================================
# Run / payload / CSV / privacy
# ==========================================================================

def analyze(an, mapdir, home, wps):
    a = an.args
    P = {}
    my_slots = presence(an.points, an.visits, an.activities)
    P["quality"] = quality(an, my_slots)
    geodir = a.geodata or os.path.join(os.path.dirname(os.path.abspath(__file__)), "geodata")
    if not getattr(a, "no_collections", False):
        try:
            P["collections"] = collections(an, geodir, home)
        except Exception as ex:  # noqa: BLE001
            print(f"  collections skipped: {ex}")
    P["holidays"] = holidays(an, home)
    P["family"] = family(an, getattr(an, "family_places", []), home)
    if getattr(a, "snow", False):
        try:
            os.makedirs(geodir, exist_ok=True)
            P["snow"] = snowy_weeks(an, home, os.path.join(geodir, "snow_history.json"))
        except OSError:
            P["snow"] = None
    P["types"] = place_types(an)
    other = getattr(an, "other_store", None)
    if other is not None:
        P["together"] = together(an, other, getattr(a, "together_name", None), my_slots)
    an.x["plus"] = P
    return P


def payload(an):
    P = dict(an.x.get("plus") or {})
    return {k: v for k, v in P.items() if v}


def write_csvs(an, out):
    P = an.x.get("plus") or {}

    def w(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f)
            cw.writerow(header)
            cw.writerows(rows)
    q = P.get("quality")
    if q:
        w("data_quality.csv", ["date", "coverage_pct"], sorted(q["days"].items()))
        w("data_gaps.csv", ["from", "to", "days"], q["gaps"])
    c = P.get("collections") or {}
    if c.get("pota"):
        w("pota_parks_visited.csv", ["reference", "name", "location", "lat", "lon", "days", "first", "last"], c["pota"]["visited"])
    if c.get("capitals"):
        w("state_capitals.csv", ["capital", "lat", "lon", "days", "first", "last"], c["capitals"]["items"])
    h = P.get("holidays")
    if h:
        w("holidays.csv", ["holiday"] + [str(y) for y in h["years"]],
          [[r[0]] + [(f"{cell[1]}" + (" (away)" if cell[3] else "")) if cell else "" for cell in r[1]] for r in h["rows"]])
    t = P.get("together")
    if t:
        w("time_together.csv", ["date", "hours_together", "places"], [[d, hrs, "; ".join(pl)] for d, hrs, pl in t["days"]])
    s = P.get("snow")
    if s:
        w("snowy_weeks.csv", ["week_starting_monday", "pct_years_with_1in_snow_day", "avg_weekly_snow_cm", "max_weekly_snow_cm"], s["upcoming"])


def scrub(D, hidden):
    X = D.get("x") or {}
    P = X.get("plus")
    if not P:
        return
    for k in ("together", "family", "snow", "holidays", "types"):
        P.pop(k, None)
    c = P.get("collections") or {}
    if c.get("pota"):
        c["pota"].pop("nearby_unvisited", None)
