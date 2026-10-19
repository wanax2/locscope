"""
LocScope - more analyses: where you slept, places you stopped going / new regulars, after-hours call-ins,
flight log, weather, exploration (fog of war), driving speeds, Maidenhead grid squares, day notes, carbon footprint.

Used by locscope_extras.analyze(); not meant to be run by itself.
"""

import bisect
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict, Counter
from datetime import datetime, timedelta, date

from locscope_extras import hav, PlaceIndex, at

M_PER_MI = 1609.344


def _label(an, idx, lat, lon):
    from locscope import display_name, region_label
    p = idx.nearest(lat, lon, 300)
    if p is not None:
        return display_name(an, p), p
    r = an.regions.lookup(lat, lon) if an.regions is not None else None
    return ("Near " + region_label(r)) if r else "%.3f, %.3f" % (lat, lon), None


# ==========================================================================
# Where you slept
# ==========================================================================

def nights(an, home, wps, idx):
    found = {}
    for v in an.visits:
        ls, le = an.local(v["start"]), an.local(v["end"])
        c = ls.replace(hour=3, minute=0, second=0, microsecond=0)
        if c < ls:
            c += timedelta(days=1)
        n = 0
        while c <= le and n < 400:
            found.setdefault(c.date() - timedelta(days=1), (v["lat"], v["lon"], v.get("place")))
            c += timedelta(days=1)
            n += 1
    for p in an.points:  # nights with no stop recorded: use a GPS fix between 1 and 5 am
        h = p[4].hour
        if 1 <= h < 5:
            found.setdefault(p[4].date() - timedelta(days=1), (p[1], p[2], None))
    out = []
    from locscope import display_name
    for d in sorted(found):
        lat, lon, pk = found[d]
        if pk and pk in an.places:
            label, pl = display_name(an, an.places[pk]), an.places[pk]
        else:
            label, pl = _label(an, idx, lat, lon)
        km = hav(lat, lon, home["lat"], home["lon"]) / 1000 if home else None
        if home and km * 1000 <= home["r"]:
            kind = "home"
        elif any(hav(lat, lon, w["lat"], w["lon"]) <= w["r"] for w in wps):
            kind = "work"
        elif km is not None and km >= 50:
            kind = "away"
        else:
            kind = "local"
        out.append(dict(date=d, label="Home" if kind == "home" else label, lat=lat, lon=lon, kind=kind,
                        km=round(km, 1) if km is not None else None))
    return out


# ==========================================================================
# Places you stopped going to / new regulars
# ==========================================================================

def lifecycle(an):
    from locscope import display_name
    if not an.days:
        return None
    end = max(an.days)
    by = defaultdict(list)
    for v in an.visits:
        by[v["place"]].append(an.local(v["start"]).date())
    stopped, new, loyal = [], [], []
    for k, ds in by.items():
        p = an.places.get(k)
        if p is None:
            continue
        ds.sort()
        months = {d.strftime("%Y-%m") for d in ds}
        n, first, last = len(ds), ds[0], ds[-1]
        tag = "Home" if an.home is p else "Work" if an.work is p else ""
        row = dict(name=display_name(an, p), lat=round(p["lat"], 4), lon=round(p["lon"], 4), visits=n,
                   first=first.isoformat(), last=last.isoformat(), months=len(months), tag=tag,
                   since=(end - last).days, span=(last - first).days)
        if n >= 6 and len(months) >= 3 and (end - last).days >= 180:
            stopped.append(row)
        if (end - first).days <= 365 and n >= 4:
            new.append(row)
        if n >= 6:
            loyal.append(row)
    stopped.sort(key=lambda r: -r["visits"])
    new.sort(key=lambda r: -r["visits"])
    loyal.sort(key=lambda r: -r["span"])
    return dict(stopped=stopped[:200], new=new[:200], loyal=loyal[:50], end=end.isoformat())


# ==========================================================================
# After-hours call-ins (on-call weeks run Monday to Monday)
# ==========================================================================

def callins(an, wh):
    if not wh:
        return None
    rows = sorted(wh["rows"], key=lambda r: r["arrive"])
    out = []
    prev = None
    for r in rows:
        a = r["arrive"]
        why = None
        if a.weekday() >= 5:
            why = "Weekend"
        elif a.hour < 6 or a.hour >= 18:
            why = "Night"
        elif prev is not None and prev["leave"].date() == a.date() and (a - prev["leave"]).total_seconds() >= 3600 \
                and a.hour >= 15:
            why = "Came back in"
        if why:
            wk = a.date() - timedelta(days=a.weekday())
            out.append(dict(date=a.date(), arrive=a, leave=r["leave"], hours=r["hours"], why=why, week=wk,
                            place=r["place"]))
        prev = r
    if not out:
        return dict(rows=[], weeks=[], hours=[0] * 24, months=[])
    weeks = Counter(c["week"] for c in out)
    week_hours = defaultdict(float)
    for c in out:
        week_hours[c["week"]] += c["hours"]
    hours = [0] * 24
    for c in out:
        hours[c["arrive"].hour] += 1
    months = Counter(c["date"].strftime("%Y-%m") for c in out)
    return dict(rows=out, weeks=sorted(((w, n, week_hours[w]) for w, n in weeks.items()), key=lambda x: x[0]),
                hours=hours, months=sorted(months.items()))


# ==========================================================================
# Flight log
# ==========================================================================

class Airports:
    def __init__(self, path):
        self.grid = defaultdict(list)
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a = dict(iata=r["iata"], name=r["name"], city=r["city"], country=r["country"],
                         lat=float(r["lat"]), lon=float(r["lon"]), big=r["size"] == "L")
                self.grid[(int(math.floor(a["lat"])), int(math.floor(a["lon"])))].append(a)

    def near(self, lat, lon, maxkm=40):
        best, bs = None, None
        cy, cx = int(math.floor(lat)), int(math.floor(lon))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for a in self.grid.get((cy + dy, cx + dx), ()):
                    d = hav(lat, lon, a["lat"], a["lon"]) / 1000
                    if d > maxkm:
                        continue
                    score = d if a["big"] else d * 1.6 + 3
                    if bs is None or score < bs:
                        best, bs = a, score
        return best


def flights(an, mapdir):
    ap = None
    try:
        ap = Airports(os.path.join(mapdir, "airports.csv"))
    except (OSError, KeyError, ValueError):
        pass
    cands = []
    for x in an.activities:
        if x["mode"] == "Flying" and x["slat"] is not None and x["elat"] is not None:
            cands.append(dict(start=x["start"], end=x["end"], ls=an.local(x["start"]), le=an.local(x["end"]),
                              a=(x["slat"], x["slon"]), b=(x["elat"], x["elon"]), src="Google"))
    fly_spans = [(c["start"].timestamp(), c["end"].timestamp()) for c in cands]

    def covered(t0, t1):
        return any(s - 3600 <= t0 <= e + 3600 or s - 3600 <= t1 <= e + 3600 for s, e in fly_spans)

    def jump(t0, ls, a, t1, le, b, src):
        gap = t1 - t0
        if gap <= 0 or gap > 20 * 3600:
            return
        if abs(a[0] - b[0]) < 0.9 and abs(a[1] - b[1]) < 0.9:  # certainly < 150 km
            return
        d = hav(a[0], a[1], b[0], b[1])
        if d < 150000 or d / gap * 3.6 < 180:
            return
        if covered(t0, t1):
            return
        cands.append(dict(start=ls, end=le, ls=ls, le=le, a=a, b=b, src=src))
        fly_spans.append((t0, t1))
    pts = an.points
    for p, q in zip(pts, pts[1:]):
        jump(p[0], p[4], (p[1], p[2]), q[0], q[4], (q[1], q[2]), "GPS gap")
    vs = sorted(an.visits, key=lambda v: v["start"])
    for v, w in zip(vs, vs[1:]):
        jump(v["end"].timestamp(), an.local(v["end"]), (v["lat"], v["lon"]), w["start"].timestamp(), an.local(w["start"]),
             (w["lat"], w["lon"]), "between stops")
    out = []
    for c in sorted(cands, key=lambda c: c["start"].timestamp()):
        fa = ap.near(*c["a"]) if ap else None
        fb = ap.near(*c["b"]) if ap else None
        a = (fa["lat"], fa["lon"]) if fa else c["a"]
        b = (fb["lat"], fb["lon"]) if fb else c["b"]
        if fa and fb and fa["iata"] == fb["iata"]:
            continue
        km = hav(a[0], a[1], b[0], b[1]) / 1000
        if km < 100:
            continue

        def lab(apt, ll):
            if apt:
                return apt["iata"], apt["name"], apt["city"] or apt["country"]
            from locscope import region_label
            r = an.regions.lookup(*ll) if an.regions is not None else None
            return "", ("Near " + region_label(r)) if r else "%.2f, %.2f" % ll, ""
        ls, le = c["ls"], c["le"]
        out.append(dict(start=ls, end=le, frm=lab(fa, c["a"]), to=lab(fb, c["b"]), km=km, a=a, b=b,
                        mins=(c["end"] - c["start"]).total_seconds() / 60, src=c["src"]))
    return out


# ==========================================================================
# Weather (Open-Meteo historical archive, free, no key) - optional batch
# ==========================================================================

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Light drizzle",
       53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
       65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
       95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail"}


def day_main_location(an):
    """Where you spent the most time each day (for weather lookups)."""
    best = {}
    for v in an.visits:
        ls, le = an.local(v["start"]), an.local(v["end"])
        d = ls.date()
        while d <= le.date() and (d - ls.date()).days < 60:
            d0 = max(ls, datetime(d.year, d.month, d.day, tzinfo=ls.tzinfo))
            d1 = min(le, datetime(d.year, d.month, d.day, tzinfo=ls.tzinfo) + timedelta(days=1))
            h = (d1 - d0).total_seconds()
            if h > 0 and (d not in best or h > best[d][2]):
                best[d] = (v["lat"], v["lon"], h)
            d += timedelta(days=1)
    by = defaultdict(list)
    for p in an.points:
        if p[4].date() not in best:
            by[p[4].date()].append((p[1], p[2]))
    for d, ps in by.items():
        ps.sort()
        best[d] = (ps[len(ps) // 2][0], ps[len(ps) // 2][1], 0)
    return {d: (round(v[0], 3), round(v[1], 3)) for d, v in best.items()}


def weather_batch(an, locs, cache_path, limit_days=None):
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    cell = lambda ll: (round(ll[0] * 4) / 4, round(ll[1] * 4) / 4)
    want = defaultdict(list)
    today = date.today()
    for d, ll in locs.items():
        if (today - d).days < 6:
            continue  # the archive lags a few days
        c = cell(ll)
        if f"{c[0]},{c[1]}|{d.isoformat()}" not in cache:
            want[c].append(d)
    reqs = []
    for c, ds in want.items():
        ds.sort()
        s = ds[0]
        prev = ds[0]
        for d in ds[1:] + [None]:
            if d is None or (d - prev).days > 45 or (d - s).days > 3650:
                reqs.append((c, s, prev))
                s = d
            prev = d if d else prev
    if reqs:
        print(f"Weather: {len(reqs)} lookups on Open-Meteo (free archive; sends rounded daily locations)...")
    fails = 0
    for i, (c, s, e) in enumerate(reqs):
        if fails >= 3:
            print("  weather lookups failing - skipping the rest for now")
            break
        url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(dict(
            latitude=c[0], longitude=c[1], start_date=s.isoformat(), end_date=e.isoformat(), timezone="auto",
            daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_speed_10m_max"))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LocScope (personal location-history analyzer)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.load(r)
            dd = j.get("daily", {})
            for k, ds in enumerate(dd.get("time", [])):
                cache[f"{c[0]},{c[1]}|{ds}"] = [dd["weather_code"][k], dd["temperature_2m_max"][k], dd["temperature_2m_min"][k],
                                                dd["precipitation_sum"][k], dd["snowfall_sum"][k], dd["wind_speed_10m_max"][k]]
            fails = 0
        except Exception as ex:  # noqa: BLE001
            fails += 1
            print(f"  weather lookup failed: {ex}")
        time.sleep(0.3)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(reqs)}")
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass
    out = {}
    for d, ll in locs.items():
        c = cell(ll)
        w = cache.get(f"{c[0]},{c[1]}|{d.isoformat()}")
        if w and w[1] is not None:
            out[d.isoformat()] = w
    return out


# ==========================================================================
# Exploration (fog of war)
# ==========================================================================

CELL = 0.002  # degrees (~220 m north-south)


def explored(an, mapdir, home):
    cells = set()
    cadd = cells.add
    floor = math.floor
    add = lambda la, lo: cadd((int(floor(la / CELL)), int(floor(lo / CELL))))
    prev = None
    kx = 111320.0
    for p in an.points:
        la, lo = p[1], p[2]
        cadd((int(floor(la / CELL)), int(floor(lo / CELL))))
        if prev is not None and 0 < p[0] - prev[0] <= 600:
            dy = (la - prev[1]) * kx
            dx = (lo - prev[2]) * kx * math.cos(la * 0.017453292519943295)
            d = math.sqrt(dx * dx + dy * dy)
            if 150 < d < 3000:
                n = int(d // 120)
                for k in range(1, n):
                    f = k / n
                    add(prev[1] + (la - prev[1]) * f, prev[2] + (lo - prev[2]) * f)
        prev = p
    for v in an.visits:
        add(v["lat"], v["lon"])
    res = dict(cell=CELL, n=len(cells))
    lst = sorted(cells)
    step = 1
    if len(lst) > 250000:  # keep the report a sensible size
        step = 2
        lst = sorted({(a // 2 * 2, b // 2 * 2) for a, b in lst})
    res["cells"] = lst
    res["step"] = step
    areas = []
    if home:
        cy, cx = int(math.floor(home["lat"] / CELL)), int(math.floor(home["lon"] / CELL))
        for radius in (2, 5, 10):
            tot = hit = 0
            ry = int(radius * 1000 / 111320 / CELL) + 1
            rx = int(ry / max(0.2, math.cos(math.radians(home["lat"])))) + 1
            for iy in range(cy - ry, cy + ry + 1):
                for ix in range(cx - rx, cx + rx + 1):
                    if hav(home["lat"], home["lon"], (iy + .5) * CELL, (ix + .5) * CELL) <= radius * 1000:
                        tot += 1
                        hit += (iy, ix) in cells
            areas.append([f"Within {radius} km of home", round(100 * hit / max(1, tot), 1), hit, tot])
    vr = an.x.get("visited")
    if vr and vr.get("has_us"):
        from locscope_extras import Topo
        topo = Topo(vr["_us_raw"], "counties")
        top = sorted(vr["counties"].items(), key=lambda kv: -kv[1]["days"])[:4]
        for fid, e in top:
            f = next((f for f in topo.feats if f["id"] == fid), None)
            if not f:
                continue
            x0, y0, x1, y1 = f["bbox"]
            iy0, iy1 = int(math.floor(y0 / CELL)), int(math.floor(y1 / CELL))
            ix0, ix1 = int(math.floor(x0 / CELL)), int(math.floor(x1 / CELL))
            stride = max(1, int(math.sqrt((iy1 - iy0 + 1) * (ix1 - ix0 + 1) / 200000)) + 0)
            tot = hit = 0
            for iy in range(iy0, iy1 + 1, stride):
                for ix in range(ix0, ix1 + 1, stride):
                    if Topo.inside_f(f, (ix + .5) * CELL, (iy + .5) * CELL):
                        tot += 1
                        hit += (iy, ix) in cells
            names = vr["counties"][fid]["name"]
            st = next((s for s, v in vr["states"].items() if s == fid[:2]), None)
            sname = vr["states"][st]["name"] if st else ""
            city = fid[:2] == "51" and fid[2:].isdigit() and int(fid[2:]) >= 500  # Virginia's independent cities
            label = names + (" (city)" if city else " County" if sname and names != sname else "")
            label += f", {sname}" if sname and names != sname else ""
            if any(x[0] == label for x in areas):
                label += f" ({fid})"
            areas.append([label, round(100 * hit / max(1, tot), 2), hit, tot])
    res["areas"] = areas
    return res


# ==========================================================================
# Driving speeds
# ==========================================================================

def driving(an):
    drives = []
    for x in an.activities:
        if x["mode"] not in ("Driving", "Motorcycle"):
            continue
        secs = (x["end"] - x["start"]).total_seconds()
        if x["dist"] < 2000 or secs < 180:
            continue
        kmh = x["dist"] / secs * 3.6
        if not 5 <= kmh <= 160:
            continue
        ls = an.local(x["start"])
        drives.append((ls, kmh, x["dist"] / 1000, secs / 60))
    if not drives:
        return None
    grid = [[[0.0, 0] for _ in range(24)] for _ in range(7)]
    by_hour = [[0.0, 0] for _ in range(24)]
    for ls, kmh, km, mins in drives:
        g = grid[ls.weekday()][ls.hour]
        g[0] += kmh * km; g[1] += km
        h = by_hour[ls.hour]
        h[0] += kmh * km; h[1] += km
    avg = lambda c: round(c[0] / c[1], 1) if c[1] else None
    return dict(grid=[[avg(c) for c in row] for row in grid], hours=[avg(c) for c in by_hour],
                n=len(drives), avg=round(sum(k * d for _, k, d, _ in drives) / sum(d for _, _, d, _ in drives), 1),
                fastest=[[d[0].strftime("%Y-%m-%d %H:%M"), round(d[1]), round(d[2], 1)] for d in sorted(drives, key=lambda d: -d[1])[:5]])


def commute_departures(an):
    """Typical commute length by departure time (15-minute slots) - when to leave."""
    out = {}
    for c in an.x.get("commutes") or []:
        slot = (c["leave"].hour * 60 + c["leave"].minute) // 15 * 15
        out.setdefault((c["dir"], slot), []).append(c["mins"])
    rows = []
    for (d, slot), v in sorted(out.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        v.sort()
        rows.append([d, slot, round(v[len(v) // 2]), len(v)])
    return rows


# ==========================================================================
# Maidenhead grid squares (amateur radio)
# ==========================================================================

def maidenhead(lat, lon, n=6):
    lon += 180
    lat += 90
    s = chr(65 + int(lon // 20)) + chr(65 + int(lat // 10))
    lon %= 20
    lat %= 10
    s += str(int(lon // 2)) + str(int(lat // 1))
    if n >= 6:
        lon %= 2
        lat %= 1
        s += chr(97 + int(lon / (2 / 24))) + chr(97 + int(lat / (1 / 24)))
    return s


def grids(an, home):
    days6 = defaultdict(set)
    seen = defaultdict(set)
    for p in an.points:  # same 6-char square <=> same (lat*24, lon*12) cell
        seen[(int((p[1] + 90) * 24), int((p[2] + 180) * 12))].add(p[4].date())
    for (iy, ix), ds in seen.items():
        days6[maidenhead((iy + .5) / 24 - 90, (ix + .5) / 12 - 180)] |= ds
    for v in an.visits:
        days6[maidenhead(v["lat"], v["lon"])].add(an.local(v["start"]).date())
    for x in an.activities:
        for la, lo, t in ((x["slat"], x["slon"], x["start"]), (x["elat"], x["elon"], x["end"])):
            if la is not None:
                days6[maidenhead(la, lo)].add(an.local(t).date())
    days4 = defaultdict(set)
    for k, ds in days6.items():
        days4[k[:4]] |= ds
    fmt = lambda m: {k: [len(ds), min(ds).isoformat(), max(ds).isoformat()] for k, ds in m.items()}
    return dict(g4=fmt(days4), g6=fmt(days6), home=maidenhead(home["lat"], home["lon"]) if home else None)


# ==========================================================================
# Day notes
# ==========================================================================

def load_notes(path):
    notes = {}
    if not path or not os.path.exists(path):
        return notes
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                d, n = row.get("date", ""), row.get("note", "")
                if d and n:
                    try:
                        date.fromisoformat(d[:10])
                        notes[d[:10]] = (notes.get(d[:10], "") + " / " + n).strip(" /") if d[:10] in notes else n
                    except ValueError:
                        pass
        print(f"  loaded {len(notes)} day notes from {path}")
    except OSError:
        pass
    return notes


# ==========================================================================
# Carbon footprint (rough estimates, kg CO2e per passenger-km)
# ==========================================================================

CO2 = {"Driving": 0.17, "Motorcycle": 0.11, "Bus": 0.10, "Rail": 0.035, "Boat": 0.11, "Skiing": 0.0,
       "Walking": 0.0, "Running": 0.0, "Cycling": 0.0, "Other": 0.0, "Unknown": 0.0}


def flight_factor(km):
    return 0.25 if km < 1000 else 0.15


def carbon(an, fl):
    years = defaultdict(Counter)
    act_days = set()
    for x in an.activities:
        y = an.local(x["start"]).year
        km = x["dist"] / 1000
        act_days.add(an.local(x["start"]).date())
        if x["mode"] == "Flying":
            years[y]["Flying"] += km * flight_factor(km)
        else:
            years[y][x["mode"]] += km * CO2.get(x["mode"], 0.0)
    for f in fl:
        if f["src"] != "Google":
            years[f["start"].year]["Flying"] += f["km"] * flight_factor(f["km"])
    flown = Counter()
    for f in fl:
        if f["src"] != "Google":
            flown[f["start"].date()] += f["km"]
    for d, m in an.day_dist.items():
        if d not in act_days and m:
            km = max(0.0, m / 1000 - flown.get(d, 0))
            if km:
                years[d.year]["Unclassified (counted as car)"] += km * CO2["Driving"]
    return {y: {k: round(v) for k, v in c.items() if v >= 1} for y, c in sorted(years.items())}


# ==========================================================================
# Run + payload + CSV + privacy
# ==========================================================================

def analyze(an, mapdir, home, wps):
    idx = PlaceIndex(an)
    a = an.args
    m = {}
    m["nights"] = nights(an, home, wps, idx)
    m["life"] = lifecycle(an)
    m["callins"] = callins(an, an.x.get("work"))
    m["flights"] = flights(an, mapdir)
    m["drive"] = driving(an)
    m["depart"] = commute_departures(an)
    m["grids"] = grids(an, home)
    m["explore"] = explored(an, mapdir, home) if not getattr(a, "no_explore", False) else None
    m["notes"] = load_notes(getattr(a, "notes", None))
    m["carbon"] = carbon(an, m["flights"])
    m["dayloc"] = day_main_location(an)
    m["weather"] = {}
    if getattr(a, "weather", False):
        cdir = a.geodata or os.path.join(os.path.dirname(os.path.abspath(__file__)), "geodata")
        try:
            os.makedirs(cdir, exist_ok=True)
        except OSError:
            cdir = a.out
        m["weather"] = weather_batch(an, m["dayloc"], os.path.join(cdir, "weather_cache.json"))
    an.x["more"] = m
    return m


def payload(an):
    m = an.x.get("more") or {}
    out = {}
    ns = m.get("nights") or []
    if ns:
        out["nights"] = [[n["date"].isoformat(), n["label"], round(n["lat"], 4), round(n["lon"], 4), n["kind"], n["km"]] for n in ns]
    if m.get("life"):
        out["life"] = m["life"]
    ci = m.get("callins")
    if ci and ci["rows"]:
        out["callins"] = dict(rows=[[c["date"].isoformat(), c["arrive"].strftime("%H:%M"),
                                     c["leave"].strftime("%H:%M" if c["leave"].date() == c["arrive"].date() else "%b %d %H:%M"),
                                     round(c["hours"], 2), c["why"], c["week"].isoformat(), c["place"]] for c in ci["rows"]],
                              weeks=[[w.isoformat(), n, round(h, 1)] for w, n, h in ci["weeks"]], hours=ci["hours"],
                              months=ci["months"])
    fl = m.get("flights") or []
    if fl:
        out["flights"] = [[f["start"].strftime("%Y-%m-%d"), f["start"].strftime("%H:%M"), f["end"].strftime("%H:%M"),
                           f["frm"][0], f["frm"][1], f["frm"][2], f["to"][0], f["to"][1], f["to"][2], round(f["km"]),
                           round(f["mins"]), round(f["a"][0], 3), round(f["a"][1], 3), round(f["b"][0], 3), round(f["b"][1], 3),
                           f["src"]] for f in fl]
    if m.get("drive"):
        out["drive"] = m["drive"]
    if m.get("depart"):
        out["depart"] = m["depart"]
    if m.get("grids"):
        out["grids"] = m["grids"]
    if m.get("explore"):
        out["explore"] = m["explore"]
    out["notes"] = m.get("notes") or {}
    out["carbon"] = m.get("carbon") or {}
    out["weather"] = m.get("weather") or {}
    out["dayloc"] = {d.isoformat(): ll for d, ll in (m.get("dayloc") or {}).items()}
    return out


def write_csvs(an, out):
    m = an.x.get("more") or {}

    def w(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f)
            cw.writerow(header)
            cw.writerows(rows)
    if m.get("nights"):
        w("nights.csv", ["night_of", "slept_at", "type", "km_from_home", "lat", "lon"],
          [[n["date"].isoformat(), n["label"], n["kind"], n["km"] if n["km"] is not None else "", f"{n['lat']:.5f}", f"{n['lon']:.5f}"]
           for n in m["nights"]])
    ci = m.get("callins")
    if ci and ci["rows"]:
        w("call_ins.csv", ["date", "weekday", "arrive", "leave", "hours", "type", "oncall_week_starting", "place"],
          [[c["date"].isoformat(), c["arrive"].strftime("%a"), c["arrive"].strftime("%H:%M"), c["leave"].strftime("%Y-%m-%d %H:%M"),
            f"{c['hours']:.2f}", c["why"], c["week"].isoformat(), c["place"]] for c in ci["rows"]])
    if m.get("flights"):
        w("flights.csv", ["date", "depart", "arrive", "from", "from_airport", "to", "to_airport", "km", "miles", "minutes", "detected_from"],
          [[f["start"].strftime("%Y-%m-%d"), f["start"].strftime("%H:%M"), f["end"].strftime("%H:%M"), f["frm"][0] or f["frm"][1],
            f["frm"][1], f["to"][0] or f["to"][1], f["to"][1], f"{f['km']:.0f}", f"{f['km'] * 1000 / M_PER_MI:.0f}",
            f"{f['mins']:.0f}", f["src"]] for f in m["flights"]])
    if m.get("grids"):
        w("grid_squares.csv", ["square", "precision", "days", "first", "last"],
          [[k, "4", v[0], v[1], v[2]] for k, v in sorted(m["grids"]["g4"].items())] +
          [[k, "6", v[0], v[1], v[2]] for k, v in sorted(m["grids"]["g6"].items())])
    if m.get("life"):
        L = m["life"]
        w("places_stopped_and_new.csv", ["list", "place", "visits", "first", "last", "active_months", "days_since_last"],
          [["stopped going", r["name"], r["visits"], r["first"], r["last"], r["months"], r["since"]] for r in L["stopped"]] +
          [["new regular", r["name"], r["visits"], r["first"], r["last"], r["months"], r["since"]] for r in L["new"]])
    if m.get("carbon"):
        w("carbon_kg_co2e.csv", ["year", "mode", "kg_co2e"],
          [[y, k, v] for y, c in m["carbon"].items() for k, v in c.items()])
    if m.get("weather"):
        w("weather.csv", ["date", "conditions", "high_c", "low_c", "precip_mm", "snow_cm", "wind_kmh"],
          [[d, WMO.get(v[0], v[0]), v[1], v[2], v[3], v[4], v[5]] for d, v in sorted(m["weather"].items())])


def scrub(D, hidden):
    X = D.get("x") or {}
    M = X.get("more")
    if not M:
        return
    for k in ("nights", "callins", "depart", "notes", "dayloc"):
        M.pop(k, None)
    if M.get("life"):
        for k in ("stopped", "new", "loyal"):
            M["life"][k] = [r for r in M["life"][k] if not hidden(r["lat"], r["lon"])]
    if M.get("explore"):
        c = M["explore"]["cell"]
        M["explore"]["cells"] = [p for p in M["explore"]["cells"] if not hidden((p[0] + .5) * c, (p[1] + .5) * c)]
        M["explore"]["areas"] = [a for a in M["explore"]["areas"] if "home" not in a[0]]
    if M.get("grids"):
        M["grids"]["g6"] = {}
        M["grids"]["home"] = None
    if M.get("flights"):
        pass
