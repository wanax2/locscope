"""
LocScope extras: work hours, mileage log, commutes, calendar, year in review, trip journal,
states/counties/countries map, Google Earth (KML/GPX) export, shareable privacy copy.

Used by locscope.py; not meant to be run by itself.
"""

import bisect
import csv
import json
import math
import time
import os
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

EARTH_R = 6371008.8
M_PER_MI = 1609.344


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


# ==========================================================================
# Trips between stops, for data that has stops but no Google "activity" trips
# ==========================================================================

def derive_trips(an):
    """For days with no Google trip segments (raw GPS / Records.json / GPX), make trips between consecutive stops,
    measuring distance along the GPS points in between and guessing the mode from average speed."""
    if not an.visits:
        return 0
    have = {an.local(x["start"]).date() for x in an.activities}
    pts = an.points
    times = [p[0] for p in pts]
    vs = sorted(an.visits, key=lambda v: v["start"])
    made = 0
    for a, b in zip(vs, vs[1:]):
        t0, t1 = a["end"], b["start"]
        gap = (t1 - t0).total_seconds()
        if gap < 120 or gap > 6 * 3600:
            continue
        straight = hav(a["lat"], a["lon"], b["lat"], b["lon"])
        if straight < 400:
            continue
        if an.local(t0).date() in have:
            continue
        i0, i1 = bisect.bisect_left(times, t0.timestamp()), bisect.bisect_right(times, t1.timestamp())
        seg = [(a["lat"], a["lon"])] + [(p[1], p[2]) for p in pts[i0:i1]] + [(b["lat"], b["lon"])]
        dist = sum(hav(*seg[k], *seg[k + 1]) for k in range(len(seg) - 1))
        if len(seg) <= 3 or dist > straight * 3:
            dist = straight * 1.3
        kmh = dist / gap * 3.6
        mode = "Flying" if kmh > 250 else "Driving" if kmh >= 15 else "Walking" if kmh < 6.5 else "Cycling"
        an.activities.append(dict(start=t0, end=t1, mode=mode, dist=dist, slat=a["lat"], slon=a["lon"],
                                  elat=b["lat"], elon=b["lon"], src="derived"))
        made += 1
    an.activities.sort(key=lambda x: x["start"])
    return made


# ==========================================================================
# Place helpers
# ==========================================================================

class PlaceIndex:
    def __init__(self, an):
        self.an = an
        self.grid = defaultdict(list)
        for p in an.places.values():
            self.grid[(int(p["lat"] * 100), int(p["lon"] * 100))].append(p)

    def nearest(self, lat, lon, maxd=300):
        best, bd = None, maxd
        cy, cx = int(lat * 100), int(lon * 100)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for p in self.grid.get((cy + dy, cx + dx), ()):
                    d = hav(lat, lon, p["lat"], p["lon"])
                    if d < bd:
                        best, bd = p, d
        return best


def work_places(an):
    """Your work places: the inferred/labeled Work place, places named 'Work', places marked work in my_places,
    and anything named with --work-place."""
    out = []
    wanted = {w.strip().lower() for w in (getattr(an.args, "work_place", None) or "").split(",") if w.strip()}
    for n in an.named:
        if n.get("work") or n["name"].strip().lower() in wanted or n["name"].strip().upper() == "WORK":
            out.append(dict(name=n["name"], lat=n["lat"], lon=n["lon"], r=max(150.0, n.get("radius", 150.0))))
    if an.work is not None and not any(hav(an.work["lat"], an.work["lon"], w["lat"], w["lon"]) < 300 for w in out):
        from locscope import display_name
        out.append(dict(name=display_name(an, an.work), lat=an.work["lat"], lon=an.work["lon"], r=200.0))
    return out


def home_place(an):
    if an.home is None:
        return None
    r = 200.0
    for n in an.named:
        if n["name"].strip().upper() == "HOME":
            r = max(r, n.get("radius", 150.0))
    return dict(name="Home", lat=an.home["lat"], lon=an.home["lon"], r=r)


def at(v, places):
    for p in places:
        if hav(v["lat"], v["lon"], p["lat"], p["lon"]) <= p["r"]:
            return p
    return None


# ==========================================================================
# Work hours
# ==========================================================================

def work_hours(an, wps):
    if not wps:
        return None
    vs = sorted((v for v in an.visits if at(v, wps)), key=lambda v: v["start"])
    if not vs:
        return None
    shifts = []
    for v in vs:  # merge visits less than 45 min apart into one shift
        wp = at(v, wps)
        if shifts and (v["start"] - shifts[-1]["end"]).total_seconds() <= 45 * 60 and shifts[-1]["place"] == wp["name"]:
            shifts[-1]["end"] = max(shifts[-1]["end"], v["end"])
            shifts[-1]["secs"] += (v["end"] - v["start"]).total_seconds()
        else:
            shifts.append(dict(start=v["start"], end=v["end"], secs=(v["end"] - v["start"]).total_seconds(),
                               place=wp["name"]))
    rows = []
    for s in shifts:
        ls, le = an.local(s["start"]), an.local(s["end"])
        hours = (s["end"] - s["start"]).total_seconds() / 3600
        if hours < 0.25:
            continue
        rows.append(dict(date=ls.date(), arrive=ls, leave=le, hours=hours, place=s["place"],
                         overnight=le.date() > ls.date(), week=ls.date() - timedelta(days=ls.weekday())))
    if not rows:
        return None
    weekly = defaultdict(lambda: [0.0, 0, 0])
    monthly = defaultdict(lambda: [0.0, 0, 0])
    for r in rows:
        w = weekly[r["week"]]
        w[0] += r["hours"]; w[1] += 1; w[2] += r["overnight"]
        m = monthly[r["date"].strftime("%Y-%m")]
        m[0] += r["hours"]; m[1] += 1; m[2] += r["overnight"]
    data_weeks = {d - timedelta(days=d.weekday()) for d in an.days}
    weeks = sorted(weekly)
    # fill empty weeks so the chart shows gaps (vacations) honestly
    full = []
    if weeks:
        d = weeks[0]
        while d <= weeks[-1]:
            full.append(d)
            d += timedelta(days=7)
    longest = max(rows, key=lambda r: r["hours"])
    worked = [weekly[w][0] for w in weeks]
    return dict(rows=rows, weekly=weekly, weeks=full, data_weeks=data_weeks, monthly=monthly, longest=longest,
                avg_week=sum(worked) / len(worked), total=sum(r["hours"] for r in rows),
                overnights=sum(r["overnight"] for r in rows), places=sorted({r["place"] for r in rows}))


# ==========================================================================
# Commutes
# ==========================================================================

def commutes(an, home, wps):
    if home is None or not wps:
        return []
    vs = sorted(an.visits, key=lambda v: v["start"])
    tag = []
    for v in vs:
        if at(v, [home]):
            tag.append("H")
        elif at(v, wps):
            tag.append("W")
        else:
            tag.append("")
    acts = sorted(an.activities, key=lambda x: x["start"])
    astarts = [x["start"] for x in acts]
    out = []
    for i, v in enumerate(vs):
        if tag[i] not in ("H", "W"):
            continue
        stops = 0
        for j in range(i + 1, len(vs)):
            if (vs[j]["start"] - v["end"]).total_seconds() > 4 * 3600:
                break
            if tag[j] == tag[i]:
                break
            if tag[j] in ("H", "W"):
                leave, arrive = v["end"], vs[j]["start"]
                mins = (arrive - leave).total_seconds() / 60
                if 2 <= mins <= 240:
                    k0, k1 = bisect.bisect_left(astarts, leave - timedelta(minutes=5)), bisect.bisect_left(astarts, arrive)
                    seg = acts[k0:k1]
                    km = sum(x["dist"] for x in seg) / 1000
                    modes = Counter()
                    for x in seg:
                        modes[x["mode"]] += x["dist"]
                    out.append(dict(dir="to work" if tag[j] == "W" else "to home", leave=an.local(leave),
                                    arrive=an.local(arrive), mins=mins, stops=stops, km=km,
                                    mode=modes.most_common(1)[0][0] if modes else ""))
                break
            stops += 1
    return out


# ==========================================================================
# Mileage log
# ==========================================================================

DRIVE_MODES = ("Driving", "Motorcycle")


def mileage(an, home, wps, rate=None):
    idx = PlaceIndex(an)
    from locscope import display_name
    vs = sorted(an.visits, key=lambda v: v["end"])
    vends = [v["end"] for v in vs]
    vstarts_sorted = sorted(an.visits, key=lambda v: v["start"])
    vstarts = [v["start"] for v in vstarts_sorted]
    named_work = {n["name"] for n in an.named if n.get("work")}

    def end_label(lat, lon, when, before):
        if lat is None:
            return "", None
        v = None
        if before:  # the stop you were leaving
            k = bisect.bisect_right(vends, when + timedelta(minutes=20)) - 1
            if k >= 0 and (when - vends[k]).total_seconds() < 3600 and hav(lat, lon, vs[k]["lat"], vs[k]["lon"]) < 600:
                v = vs[k]
        else:       # the stop you arrived at
            k = bisect.bisect_left(vstarts, when - timedelta(minutes=20))
            if k < len(vstarts_sorted) and (vstarts[k] - when).total_seconds() < 3600 and \
                    hav(lat, lon, vstarts_sorted[k]["lat"], vstarts_sorted[k]["lon"]) < 600:
                v = vstarts_sorted[k]
        p = an.places.get(v["place"]) if v is not None and v.get("place") else idx.nearest(lat, lon)
        if p is not None:
            return display_name(an, p), p
        r = an.regions.lookup(lat, lon) if an.regions is not None else None
        from locscope import region_label
        return ("Near " + region_label(r)) if r else "%.4f, %.4f" % (lat, lon), None

    def kind(lat, lon, p):
        if lat is None:
            return ""
        if home and hav(lat, lon, home["lat"], home["lon"]) <= home["r"]:
            return "H"
        for w in wps:
            if hav(lat, lon, w["lat"], w["lon"]) <= w["r"]:
                return "W"
        if p is not None and p.get("named") and p["named"].get("work"):
            return "W"
        return ""

    rows = []
    for x in an.activities:
        if x["mode"] not in DRIVE_MODES or x["dist"] < 300:
            continue
        fl, fp = end_label(x["slat"], x["slon"], x["start"], True)
        tl, tp = end_label(x["elat"], x["elon"], x["end"], False)
        fk, tk = kind(x["slat"], x["slon"], fp), kind(x["elat"], x["elon"], tp)
        if {fk, tk} == {"H", "W"}:
            cat = "Commute"
        elif "W" in (fk, tk) or (fl in named_work or tl in named_work):
            cat = "Work"
        else:
            cat = "Personal"
        miles = x["dist"] / M_PER_MI
        ls, le = an.local(x["start"]), an.local(x["end"])
        rows.append(dict(date=ls.date(), start=ls, end=le, frm=fl, to=tl, miles=miles, km=x["dist"] / 1000,
                         mins=(x["end"] - x["start"]).total_seconds() / 60, cat=cat,
                         amount=round(miles * rate, 2) if rate and cat == "Work" else None,
                         derived=x.get("src") == "derived"))
    return rows


# ==========================================================================
# Calendar + year in review
# ==========================================================================

def calendar(an, wh):
    work_by_day = defaultdict(float)
    if wh:
        for r in wh["rows"]:
            work_by_day[r["date"]] += r["hours"]
    stops = Counter()
    places = defaultdict(set)
    for v in an.visits:
        d = an.local(v["start"]).date()
        stops[d] += 1
        places[d].add(v.get("place"))
    acts = Counter(an.local(x["start"]).date() for x in an.activities)
    photos = Counter(an.local(p["when"]).date() for p in getattr(an, "pins", []) if p["kind"] == "photo" and p["when"])
    towns = getattr(an, "day_cities", {})
    cal = {}
    for d, di in an.days.items():
        # [km, farthest km from home, work h, stops, distinct places, trips/moves, photos, towns, gps points]
        cal[d.isoformat()] = [round(an.day_dist.get(d, 0) / 1000, 1), round(di["far"] / 1000, 1),
                              round(work_by_day.get(d, 0), 1), stops.get(d, 0), len(places.get(d, ())),
                              acts.get(d, 0), photos.get(d, 0), ", ".join(sorted(towns.get(d, ()))[:5]), di["pts"]]
    return cal


def month_tops(an):
    """Top places by hours for each month: {'2024-07': [[name, hours, lat, lon], ...]}"""
    from locscope import display_name
    by = defaultdict(Counter)
    for v in an.visits:
        by[an.local(v["start"]).strftime("%Y-%m")][v["place"]] += (v["end"] - v["start"]).total_seconds() / 3600
    out = {}
    for m, c in by.items():
        out[m] = [[display_name(an, an.places[k]), round(h, 1), round(an.places[k]["lat"], 4), round(an.places[k]["lon"], 4)]
                  for k, h in c.most_common(6) if k in an.places]
    return out


def year_review(an, wh, comm, miles_rows, visited):
    from locscope import display_name
    years = sorted({d.year for d in an.days})
    out = []
    first_seen = {}
    for p in an.places.values():
        first_seen[p["key"]] = an.local(p["first"]).year
    for y in years:
        days = [d for d in an.days if d.year == y]
        km = sum(an.day_dist.get(d, 0) for d in days) / 1000
        modes = Counter()
        act_days = set()
        for x in an.activities:
            ls = an.local(x["start"])
            if ls.year == y:
                modes[x["mode"]] += x["dist"] / 1000
                act_days.add(ls.date())
        for d in days:
            if d not in act_days and an.day_dist.get(d):
                modes["Unclassified (GPS)"] += an.day_dist[d] / 1000
        place_hours = Counter()
        for v in an.visits:
            if an.local(v["start"]).year == y:
                place_hours[v["place"]] += (v["end"] - v["start"]).total_seconds() / 3600
        top = [[display_name(an, an.places[k]), round(h), round(an.places[k]["lat"], 4), round(an.places[k]["lon"], 4)]
               for k, h in place_hours.most_common(6)]
        new = [display_name(an, an.places[k]) for k in place_hours if first_seen.get(k) == y]
        trips = [t for t in an.trips if t["start"].year == y]
        longest = max(trips, key=lambda t: (t["days"], t["far"]), default=None)
        far_day = max(days, key=lambda d: an.days[d]["far"], default=None)
        busiest = max(days, key=lambda d: an.day_dist.get(d, 0), default=None)
        towns = states = countries = 0
        if an.region_stats:
            towns = sum(1 for e in an.region_stats["city"].values() if any(d.year == y for d in e["days"]))
            states = sum(1 for e in an.region_stats["state"].values() if any(d.year == y for d in e["days"]))
            countries = sum(1 for e in an.region_stats["country"].values() if any(d.year == y for d in e["days"]))
        photos = sum(1 for p in getattr(an, "pins", []) if p["kind"] == "photo" and p["when"] and an.local(p["when"]).year == y)
        wk = None
        if wh:
            yr = [r for r in wh["rows"] if r["date"].year == y]
            if yr:
                wk = dict(hours=round(sum(r["hours"] for r in yr)), shifts=len(yr),
                          overnights=sum(r["overnight"] for r in yr))
        cm = [c for c in comm if c["leave"].year == y and c["dir"] == "to work"]
        mi = [r for r in miles_rows if r["date"].year == y]
        out.append(dict(
            year=y, days=len(days), km=round(km), modes=[[m, round(v)] for m, v in modes.most_common() if v >= 1],
            places=len(place_hours), new=len(new), new_list=new[:12], top=top,
            trips=len(trips), trip_days=sum(t["days"] for t in trips),
            longest=dict(start=longest["start"].isoformat(), end=longest["end"].isoformat(), days=longest["days"],
                         far=round(longest["far"] / 1000)) if longest else None,
            farthest=dict(date=far_day.isoformat(), km=round(an.days[far_day]["far"] / 1000)) if far_day and an.home else None,
            busiest=dict(date=busiest.isoformat(), km=round(an.day_dist.get(busiest, 0) / 1000, 1)) if busiest else None,
            towns=towns, states=states, countries=countries, photos=photos, work=wk,
            commute=round(sorted(c["mins"] for c in cm)[len(cm) // 2]) if cm else None,
            miles=round(sum(r["miles"] for r in mi)),
            us_states=len({k for k, days_ in (visited or {}).get("states", {}).items() if y in days_["years"]}) if visited else None,
        ))
    return out


# ==========================================================================
# Countries / US states / US counties (offline polygons, point-in-polygon)
# ==========================================================================

class Topo:
    """Minimal TopoJSON reader + point-in-polygon index."""

    def __init__(self, topo, objname, idkey=None):
        self.topo = topo
        tr = topo.get("transform")
        arcs = []
        for arc in topo["arcs"]:
            x = y = 0
            pts = []
            for p in arc:
                if tr:
                    x += p[0]; y += p[1]
                    pts.append((x * tr["scale"][0] + tr["translate"][0], y * tr["scale"][1] + tr["translate"][1]))
                else:
                    pts.append((p[0], p[1]))
            arcs.append(pts)
        self.feats = []
        self.grid = defaultdict(list)
        for g in topo["objects"][objname]["geometries"]:
            if g.get("type") == "Polygon":
                polys = [g["arcs"]]
            elif g.get("type") == "MultiPolygon":
                polys = g["arcs"]
            else:
                continue
            rings = []
            for poly in polys:
                for ring in poly:
                    pts = []
                    for ai in ring:
                        a = arcs[ai] if ai >= 0 else arcs[~ai][::-1]
                        pts.extend(a if not pts else a[1:])
                    rings.append(pts)
            xs = [p[0] for r in rings for p in r]
            ys = [p[1] for r in rings for p in r]
            if not xs:
                continue
            fid = str(g.get("id") or (g.get("properties") or {}).get("name") or len(self.feats))
            f = dict(id=fid, name=(g.get("properties") or {}).get("name", ""), rings=rings,
                     bbox=(min(xs), min(ys), max(xs), max(ys)))
            self.feats.append(f)
            for gx in range(int(math.floor(f["bbox"][0])), int(math.floor(f["bbox"][2])) + 1):
                for gy in range(int(math.floor(f["bbox"][1])), int(math.floor(f["bbox"][3])) + 1):
                    self.grid[(gx, gy)].append(f)

    @staticmethod
    def _inside(x, y, rings):
        c = False
        for r in rings:
            n = len(r)
            j = n - 1
            for i in range(n):
                xi, yi = r[i]
                xj, yj = r[j]
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                    c = not c
                j = i
        return c

    BAND = 0.25

    @classmethod
    def _prep(cls, f):
        """Per ring: bbox + edges bucketed into horizontal bands, so a point test only looks at nearby edges."""
        prep = []
        for r in f["rings"]:
            if len(r) < 3:
                continue
            ys = [p[1] for p in r]
            xs = [p[0] for p in r]
            bands = defaultdict(list)
            n = len(r)
            j = n - 1
            for i in range(n):
                xi, yi = r[i]
                xj, yj = r[j]
                if yi != yj:
                    e = (xi, yi, xj, yj)
                    for b in range(int(math.floor(min(yi, yj) / cls.BAND)), int(math.floor(max(yi, yj) / cls.BAND)) + 1):
                        bands[b].append(e)
                j = i
            prep.append((min(ys), max(ys), max(xs), dict(bands)))
        f["prep"] = prep
        return prep

    @classmethod
    def inside_f(cls, f, x, y):
        prep = f.get("prep") or cls._prep(f)
        c = False
        b = int(math.floor(y / cls.BAND))
        for y0, y1, x1, bands in prep:
            if y < y0 or y > y1 or x > x1:
                continue
            for xi, yi, xj, yj in bands.get(b, ()):
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                    c = not c
        return c

    def find(self, lon, lat):
        for f in self.grid.get((int(math.floor(lon)), int(math.floor(lat))), ()):
            b = f["bbox"]
            if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and self.inside_f(f, lon, lat):
                return f
        return None


def visited_regions(an, mapdir):
    """Which countries / US states / US counties you've been in, and on how many days."""
    if not mapdir or not os.path.isdir(mapdir):
        return None
    try:
        with open(os.path.join(mapdir, "world-countries-50m.json"), encoding="utf-8") as f:
            world_raw = json.load(f)
        with open(os.path.join(mapdir, "us-counties-10m.json"), encoding="utf-8") as f:
            us_raw = json.load(f)
    except (OSError, ValueError):
        return None
    world = Topo(world_raw, "countries")
    counties = Topo(us_raw, "counties")
    state_names = {str(g.get("id")): (g.get("properties") or {}).get("name", "")
                   for g in us_raw["objects"]["states"]["geometries"]}
    cells = defaultdict(set)
    for p in an.points:
        cells[(round(p[1], 2), round(p[2], 2))].add(p[4].date())
    for v in an.visits:
        ls, le = an.local(v["start"]), an.local(max(v["start"], v["end"] - timedelta(seconds=1)))
        d = ls.date()
        while d <= le.date() and (d - ls.date()).days < 60:
            cells[(round(v["lat"], 2), round(v["lon"], 2))].add(d)
            d += timedelta(days=1)
    for x in an.activities:
        for la, lo, t in ((x["slat"], x["slon"], x["start"]), (x["elat"], x["elon"], x["end"])):
            if la is not None:
                cells[(round(la, 2), round(lo, 2))].add(an.local(t).date())
    res = {k: defaultdict(set) for k in ("countries", "states", "counties")}
    names = {"countries": {}, "states": dict(state_names), "counties": {}}
    wid = {}
    for g in world_raw["objects"]["countries"]["geometries"]:
        wid[str(g.get("id"))] = (g.get("properties") or {}).get("name", "")
    us_country = {"72": "630", "78": "850", "66": "316", "60": "016", "69": "580"}
    for (la, lo), days in cells.items():
        c = None
        if 17 <= la <= 72 and (-180 <= lo <= -64 or lo >= 172):
            c = counties.find(lo, la)
            if c:
                res["counties"][c["id"]] |= days
                names["counties"][c["id"]] = c["name"]
                res["states"][c["id"][:2]] |= days
                cid = us_country.get(c["id"][:2], "840")
                if cid in wid:
                    res["countries"][cid] |= days
                    names["countries"][cid] = wid[cid]
                    continue
        f = world.find(lo, la)
        if f:
            res["countries"][f["id"]] |= days
            names["countries"][f["id"]] = f["name"]
    out = {}
    for k, m in res.items():
        out[k] = {i: dict(days=len(ds), years=sorted({d.year for d in ds}), first=min(ds).isoformat(),
                          last=max(ds).isoformat(), name=names[k].get(i, "")) for i, ds in m.items()}
    out["n_us_counties"] = len(counties.feats)
    out["n_us_states"] = len(state_names)
    out["has_us"] = bool(out["counties"])
    out["_us_raw"], out["_world_raw"] = us_raw, world_raw
    return out


# ==========================================================================
# Google Earth / GPS exports
# ==========================================================================

def _day_points(an, cap):
    by = defaultdict(list)
    for p in an.points:
        by[p[4].date()].append(p)
    out = {}
    for d, ps in by.items():
        if len(ps) > cap:
            step = len(ps) / cap
            ps = [ps[int(i * step)] for i in range(cap)] + [ps[-1]]
        out[d] = ps
    return out


def _iso_utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _kml_track(name, ps, color="ff2a78d6"):
    whens = "".join(f"<when>{_iso_utc(p[0])}</when>" for p in ps)
    coords = "".join(f"<gx:coord>{p[2]:.6f} {p[1]:.6f} 0</gx:coord>" for p in ps)
    return (f"<Placemark><name>{escape(name)}</name><styleUrl>#trk</styleUrl>"
            f"<gx:Track>{whens}{coords}</gx:Track></Placemark>")


KML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2" '
            'xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>{}</name>'
            '<Style id="trk"><LineStyle><color>ffd6782a</color><width>3</width></LineStyle>'
            '<IconStyle><scale>0.4</scale></IconStyle></Style>'
            '<Style id="stop"><IconStyle><color>ff3468eb</color><scale>0.8</scale></IconStyle></Style>')
KML_TAIL = "</Document></kml>"


def _kml_visits(an, visits):
    from locscope import display_name
    out = []
    for v in visits:
        p = an.places.get(v.get("place"))
        nm = display_name(an, p) if p else "Stop"
        out.append(f"<Placemark><name>{escape(nm)}</name><styleUrl>#stop</styleUrl>"
                   f"<TimeSpan><begin>{_iso_utc(v['start'].timestamp())}</begin><end>{_iso_utc(v['end'].timestamp())}</end></TimeSpan>"
                   f"<Point><coordinates>{v['lon']:.6f},{v['lat']:.6f},0</coordinates></Point></Placemark>")
    return "".join(out)


def export_earth(an, out_dir, cap=300):
    """Writes export/locscope_all.kml, export/locscope_all.gpx, export/places.kml and export/trips/*.kml."""
    from locscope import display_name
    ex = os.path.join(out_dir, "export")
    os.makedirs(os.path.join(ex, "trips"), exist_ok=True)
    days = _day_points(an, cap)
    vis_by_day = defaultdict(list)
    for v in an.visits:
        vis_by_day[an.local(v["start"]).date()].append(v)
    # everything, one folder per year -> Google Earth's time slider works
    with open(os.path.join(ex, "locscope_all.kml"), "w", encoding="utf-8") as f:
        f.write(KML_HEAD.format("LocScope - all history"))
        for y in sorted({d.year for d in set(days) | set(vis_by_day)}):
            f.write(f"<Folder><name>{y}</name>")
            for d in sorted(x for x in set(days) | set(vis_by_day) if x.year == y):
                f.write(f"<Folder><name>{d.isoformat()}</name>")
                if len(days.get(d, [])) > 1:
                    f.write(_kml_track(f"Path {d.isoformat()}", days[d]))
                f.write(_kml_visits(an, vis_by_day.get(d, [])))
                f.write("</Folder>")
            f.write("</Folder>")
        f.write(KML_TAIL)
    with open(os.path.join(ex, "locscope_all.gpx"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="LocScope" '
                'xmlns="http://www.topografix.com/GPX/1/1">')
        for d in sorted(days):
            if len(days[d]) < 2:
                continue
            f.write(f"<trk><name>{d.isoformat()}</name><trkseg>")
            for p in days[d]:
                f.write(f'<trkpt lat="{p[1]:.6f}" lon="{p[2]:.6f}"><time>{_iso_utc(p[0])}</time></trkpt>')
            f.write("</trkseg></trk>")
        f.write("</gpx>")
    with open(os.path.join(ex, "places.kml"), "w", encoding="utf-8") as f:
        f.write(KML_HEAD.format("LocScope - places"))
        f.write("<Folder><name>Top places</name>")
        for p in sorted(an.places.values(), key=lambda p: -p["hours"])[:500]:
            desc = f"{p['hours']:.0f} hours, {p['n']} visits. {p.get('address', '')}"
            f.write(f"<Placemark><name>{escape(display_name(an, p))}</name><description>{escape(desc)}</description>"
                    f"<Point><coordinates>{p['lon']:.6f},{p['lat']:.6f},0</coordinates></Point></Placemark>")
        f.write("</Folder>")
        pins = [p for p in getattr(an, "pins", []) if p["kind"] != "photo"]
        if pins:
            f.write("<Folder><name>Saved &amp; imported</name>")
            for p in pins:
                f.write(f"<Placemark><name>{escape(p['name'] or 'Pin')}</name><description>{escape(p['note'])}</description>"
                        f"<Point><coordinates>{p['lon']:.6f},{p['lat']:.6f},0</coordinates></Point></Placemark>")
            f.write("</Folder>")
        f.write(KML_TAIL)
    for t in an.trips:
        fn = os.path.join(ex, "trips", f"trip_{t['start'].isoformat()}_{t['days']}d.kml")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(KML_HEAD.format(f"Trip {t['start'].isoformat()} to {t['end'].isoformat()}"))
            d = t["start"]
            while d <= t["end"]:
                f.write(f"<Folder><name>{d.isoformat()}</name>")
                if len(days.get(d, [])) > 1:
                    f.write(_kml_track(d.isoformat(), days[d]))
                f.write(_kml_visits(an, vis_by_day.get(d, [])))
                f.write("</Folder>")
                d += timedelta(days=1)
            f.write(KML_TAIL)
    return ex


# ==========================================================================
# CSV outputs for the new sections
# ==========================================================================

def write_extra_csvs(an, out):
    try:
        import locscope_more
        locscope_more.write_csvs(an, out)
        import locscope_plus
        locscope_plus.write_csvs(an, out)
        if not getattr(an.args, "no_ics", False):
            files = locscope_plus.write_ics(an, os.path.join(out, "export", "calendar"))
            if files:
                print(f"Calendar files (.ics) for Outlook / Google Calendar: {os.path.dirname(files[0])}")
    except ImportError:
        pass
    _write_extra_csvs(an, out)


def _write_extra_csvs(an, out):
    def w(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f)
            cw.writerow(header)
            cw.writerows(rows)
    wh = an.x.get("work")
    if wh:
        w("work_timesheet.csv", ["date", "weekday", "arrive", "leave", "hours", "overnight", "place"],
          [[r["date"].isoformat(), r["arrive"].strftime("%a"), r["arrive"].strftime("%Y-%m-%d %H:%M"),
            r["leave"].strftime("%Y-%m-%d %H:%M"), f"{r['hours']:.2f}", "yes" if r["overnight"] else "", r["place"]]
           for r in wh["rows"]])
        w("work_weekly.csv", ["week_starting", "hours", "shifts", "overnights"],
          [[wk.isoformat(), f"{wh['weekly'][wk][0]:.1f}" if wk in wh["weekly"] else ("0" if wk in wh["data_weeks"] else "no data"), wh["weekly"][wk][1] if wk in wh["weekly"] else 0,
            wh["weekly"][wk][2] if wk in wh["weekly"] else 0] for wk in wh["weeks"]])
    if an.x.get("commutes"):
        w("commutes.csv", ["date", "weekday", "direction", "leave", "arrive", "minutes", "stops_on_the_way", "km", "mode"],
          [[c["leave"].date().isoformat(), c["leave"].strftime("%a"), c["dir"], c["leave"].strftime("%H:%M"),
            c["arrive"].strftime("%H:%M"), f"{c['mins']:.0f}", c["stops"], f"{c['km']:.1f}", c["mode"]] for c in an.x["commutes"]])
    if an.x.get("mileage"):
        w("mileage_log.csv", ["date", "start", "end", "from", "to", "miles", "km", "minutes", "category", "reimbursement",
                              "distance_estimated"],
          [[r["date"].isoformat(), r["start"].strftime("%H:%M"), r["end"].strftime("%H:%M"), r["frm"], r["to"],
            f"{r['miles']:.1f}", f"{r['km']:.1f}", f"{r['mins']:.0f}", r["cat"],
            "" if r["amount"] is None else f"{r['amount']:.2f}", "yes" if r["derived"] else ""] for r in an.x["mileage"]])
    vr = an.x.get("visited")
    if vr:
        rows = []
        for k, lab in (("countries", "country"), ("states", "US state"), ("counties", "US county")):
            for i, e in sorted(vr[k].items(), key=lambda kv: -kv[1]["days"]):
                rows.append([lab, e["name"] or i, i, e["days"], e["first"], e["last"]])
        w("visited_map_regions.csv", ["type", "name", "id", "days", "first", "last"], rows)


# ==========================================================================
# Run everything
# ==========================================================================

def _stage(text, pct):
    try:
        import sys
        m = sys.modules.get("locscope")
        if m is not None:
            m.stage(text, pct)
    except Exception:  # noqa: BLE001
        pass


def analyze(an, mapdir=None):
    an.x = {}
    home = home_place(an)
    wps = work_places(an)
    wh = work_hours(an, wps)
    comm = commutes(an, home, wps)
    rate = getattr(an.args, "mileage_rate", None)
    miles_rows = mileage(an, home, wps, rate)
    _stage("Mapping countries, states and counties", 71)
    visited = visited_regions(an, mapdir) if not getattr(an.args, "no_map_regions", False) else None
    an.x.update(work=wh, commutes=comm, mileage=miles_rows, visited=visited, work_places=wps,
                calendar=calendar(an, wh))
    an.x["years"] = year_review(an, wh, comm, miles_rows, visited)
    _stage("Nights, flights, exploring and weather", 74)
    try:
        import locscope_more
        locscope_more.analyze(an, mapdir, home, wps)
    except ImportError:
        pass
    _stage("Parks, holidays and data quality", 78)
    try:
        import locscope_plus
        locscope_plus.analyze(an, mapdir, home, wps)
    except ImportError:
        pass
    return an.x


# ==========================================================================
# Report payload pieces
# ==========================================================================

def _stats(vals):
    vals = sorted(vals)
    if not vals:
        return None
    return dict(n=len(vals), avg=round(sum(vals) / len(vals), 1), med=round(vals[len(vals) // 2], 1),
                min=round(vals[0], 1), max=round(vals[-1], 1))


def payload(an):
    x = an.x
    out = dict(calendar=x.get("calendar", {}), years=x.get("years", []), months=month_tops(an))
    try:
        import locscope_more
        out["more"] = locscope_more.payload(an)
    except ImportError:
        pass
    try:
        import locscope_plus
        out["plus"] = locscope_plus.payload(an)
    except ImportError:
        pass
    wh = x.get("work")
    if wh:
        out["work"] = dict(
            places=wh["places"], total=round(wh["total"]), avg_week=round(wh["avg_week"], 1), overnights=wh["overnights"],
            shifts=len(wh["rows"]),
            longest=dict(date=wh["longest"]["date"].isoformat(), hours=round(wh["longest"]["hours"], 1),
                         arrive=wh["longest"]["arrive"].strftime("%H:%M"), leave=wh["longest"]["leave"].strftime("%b %d %H:%M")),
            weeks=[[wk.isoformat(), round(wh["weekly"][wk][0], 1) if wk in wh["weekly"] else (0 if wk in wh["data_weeks"] else None),
                    wh["weekly"][wk][1] if wk in wh["weekly"] else 0, wh["weekly"][wk][2] if wk in wh["weekly"] else 0]
                   for wk in wh["weeks"]],
            months=[[m, round(v[0], 1), v[1], v[2]] for m, v in sorted(wh["monthly"].items())],
            rows=[[r["date"].isoformat(), r["arrive"].strftime("%H:%M"),
                   r["leave"].strftime("%H:%M" if not r["overnight"] else "%b %d %H:%M"), round(r["hours"], 2),
                   1 if r["overnight"] else 0, r["place"]] for r in wh["rows"]])
    cm = x.get("commutes") or []
    if cm:
        wd = []
        for d in range(7):
            wd.append([_stats([c["mins"] for c in cm if c["dir"] == "to work" and c["leave"].weekday() == d]),
                       _stats([c["mins"] for c in cm if c["dir"] == "to home" and c["leave"].weekday() == d])])
        months = defaultdict(lambda: [[], []])
        for c in cm:
            months[c["leave"].strftime("%Y-%m")][0 if c["dir"] == "to work" else 1].append(c["mins"])
        out["commute"] = dict(
            all=[_stats([c["mins"] for c in cm if c["dir"] == "to work"]), _stats([c["mins"] for c in cm if c["dir"] == "to home"])],
            weekday=wd,
            months=[[m, (_stats(v[0]) or {}).get("med"), (_stats(v[1]) or {}).get("med")] for m, v in sorted(months.items())],
            rows=[[c["leave"].date().isoformat(), c["dir"], c["leave"].strftime("%H:%M"), c["arrive"].strftime("%H:%M"),
                   round(c["mins"]), c["stops"], round(c["km"], 1), c["mode"]] for c in cm],
            leave=_stats([c["leave"].hour * 60 + c["leave"].minute for c in cm if c["dir"] == "to work"]))
    mi = x.get("mileage") or []
    if mi:
        out["mileage"] = dict(rate=getattr(an.args, "mileage_rate", None),
                              rows=[[r["date"].isoformat(), r["start"].strftime("%H:%M"), r["frm"], r["to"], round(r["miles"], 1),
                                     round(r["mins"]), r["cat"], r["amount"], 1 if r["derived"] else 0] for r in mi])
    vr = x.get("visited")
    if vr:
        out["visited"] = {k: {i: [e["days"], e["first"], e["last"], e["name"]] for i, e in vr[k].items()}
                          for k in ("countries", "states", "counties")}
        out["visited"]["n"] = [vr["n_us_states"], vr["n_us_counties"]]
    return out


# ==========================================================================
# Shareable copy: remove everything near home, work and private places
# ==========================================================================

def scrub(payload_dict, an, radius):
    zones = []
    if an.home is not None:
        zones.append((an.home["lat"], an.home["lon"]))
    for w in an.x.get("work_places", []):
        zones.append((w["lat"], w["lon"]))
    for n in an.named:
        if n.get("private") or n["name"].strip().upper() in ("HOME", "WORK"):
            zones.append((n["lat"], n["lon"]))

    dlat = radius / 111000.0 * 1.05
    boxes = [(z[0], z[1], dlat / max(0.01, math.cos(math.radians(z[0])))) for z in zones]

    def hidden(lat, lon):
        if lat is None or lon is None:
            return False
        for zl, zo, dlon in boxes:
            if abs(lat - zl) <= dlat and abs(lon - zo) <= dlon and hav(lat, lon, zl, zo) <= radius:
                return True
        return False

    D = json.loads(json.dumps(payload_dict, default=str))
    D["shared"] = True
    D["heat"] = [h for h in D["heat"] if not hidden(h[0], h[1])]
    for k, t in D["tracks"].items():
        t["p"] = [p for p in t["p"] if not hidden(p[0], p[1])]
        ev = []
        for e in t["e"]:
            if e[2] == "visit" and hidden(e[4], e[5]):
                ev.append([e[0], e[1], "visit", "Private place", None, None])
            elif e[2] == "move":
                e = list(e)
                if hidden(e[4], e[5]):
                    e[4] = e[5] = None
                if len(e) > 7 and hidden(e[6], e[7]):
                    e[6] = e[7] = None
                ev.append(e)
            else:
                ev.append(e)
        t["e"] = ev
    D["places"] = [p for p in D["places"] if not hidden(p["lat"], p["lon"])]
    for i, p in enumerate(D["places"]):
        p["rank"] = i + 1
    keep = [not hidden(v[0], v[1]) for v in D["fvisits"]]
    D["fvisits"] = [v for v, k in zip(D["fvisits"], keep) if k]
    D["named"] = [n for n in D["named"] if not hidden(n["lat"], n["lon"])]
    D["finds"] = []
    D["home"] = D["work"] = None
    D["summary"]["farthest_ll"] = None
    if D.get("pins"):
        D["pins"]["places"] = [p for p in D["pins"]["places"] if not hidden(p[2], p[3])]
        D["pins"]["photos"] = [p for p in D["pins"]["photos"] if not hidden(p[0], p[1])]
        D["pins"]["nplaces"] = len(D["pins"]["places"])
        D["pins"]["nphotos"] = len(D["pins"]["photos"])
    ex = D.get("x") or {}
    for k in ("work", "commute", "mileage"):
        ex.pop(k, None)
    for c in D.get("x", {}).get("calendar", {}).values():
        c[2] = 0
    for m, tops in ex.get("months", {}).items():
        ex["months"][m] = [t for t in tops if not hidden(t[2], t[3])]
    for y in ex.get("years", []):
        y["work"] = None
        y["commute"] = None
        y["top"] = [t for t in y["top"] if not hidden(t[2], t[3])]
        y["new_list"] = []
    D["sources"] = []
    try:
        import locscope_more
        locscope_more.scrub(D, hidden)
        import locscope_plus
        locscope_plus.scrub(D, hidden)
    except ImportError:
        pass
    return D
