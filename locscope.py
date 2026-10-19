#!/usr/bin/env python3
"""
LocScope - analyze your Google location history.

Reads every Google location export format and writes an interactive HTML
report plus CSV files:

  * Google Takeout "Records.json"                 (raw GPS points, pre-2024 Takeout)
  * Google Takeout "Semantic Location History"    (YYYY/YYYY_MONTH.json, visits + trips)
  * Android on-device export "Timeline.json"      (semanticSegments / rawSignals, 2024+)
  * iPhone Google Maps export "location-history.json" (list of visit/activity/path, 2024+)
  * .zip files (e.g. takeout-2026...zip) containing any of the above

Standard library only (Python 3.8+). Optional: `pip install ijson` lets it stream
multi-gigabyte Records.json files without loading them into memory.

Usage (Windows PowerShell):
  python locscope.py "C:\\Users\\Chris\\Downloads\\takeout-20260929.zip"
  python locscope.py Timeline.json --out report --tz America/New_York
  python locscope.py .\\Takeout\\ --start 2020-01-01 --end 2022-12-31 --geocode 25

Nothing is uploaded anywhere. The only network use is (a) the report page
loading map tiles/Leaflet when you open it, and (b) --geocode, which asks
OpenStreetMap Nominatim for the names of your top N places.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone, date

VERSION = "2.0"

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

EARTH_R = 6371008.8
_FLOAT_PAIR = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*,\s*(-?\d+(?:\.\d+)?)")
_FRAC = re.compile(r"\.(\d+)")


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def parse_time(v):
    """Parse ISO-8601 strings ('...Z', '+05:00', any fraction length) or epoch ms."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # older Pythons: normalize fractional seconds to 6 digits
        s2 = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def e7(v):
    if v is None:
        return None
    v = int(v)
    if v > 1800000000:  # Takeout overflow bug on some records
        v -= 4294967296
    return v / 1e7


def parse_latlng(v):
    """Accepts '38.12°, -77.4°', 'geo:38.12,-77.4', or dicts with *E7 keys."""
    if v is None:
        return None
    if isinstance(v, dict):
        for a, b in (("latitudeE7", "longitudeE7"), ("latE7", "lngE7")):
            if a in v and b in v:
                return e7(v[a]), e7(v[b])
        for k in ("latLng", "LatLng", "point", "placeLocation"):
            if k in v:
                return parse_latlng(v[k])
        return None
    m = _FLOAT_PAIR.search(str(v))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


MODE_MAP = [
    ("FLY", "Flying"),
    ("PLANE", "Flying"),
    ("PASSENGER_VEHICLE", "Driving"),
    ("IN_VEHICLE", "Driving"),
    ("DRIVING", "Driving"),
    ("MOTORCYCL", "Motorcycle"),
    ("TAXI", "Driving"),
    ("BUS", "Bus"),
    ("SUBWAY", "Rail"),
    ("TRAIN", "Rail"),
    ("TRAM", "Rail"),
    ("RAIL", "Rail"),
    ("FERRY", "Boat"),
    ("BOAT", "Boat"),
    ("SAIL", "Boat"),
    ("CYCL", "Cycling"),
    ("BICYCLE", "Cycling"),
    ("RUN", "Running"),
    ("WALK", "Walking"),
    ("ON_FOOT", "Walking"),
    ("HIK", "Walking"),
    ("SKI", "Skiing"),
]


def norm_mode(t):
    if not t:
        return "Unknown"
    u = str(t).upper().replace(" ", "_")
    for key, name in MODE_MAP:
        if key in u:
            return name
    return "Other"


def norm_semantic(t):
    if not t:
        return ""
    u = str(t).upper()
    if "HOME" in u:
        return "HOME"
    if "WORK" in u:
        return "WORK"
    return ""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.points = []      # (epoch, lat, lon, accuracy_m or None)
        self.visits = []      # dict(start, end, lat, lon, place_id, name, address, semantic, src)
        self.activities = []  # dict(start, end, mode, dist, slat, slon, elat, elon, src)
        self.labeled = {}     # 'HOME'/'WORK' -> (lat, lon) from export profile
        self.sources = Counter()
        self.skipped = []
        self.pins = []            # saved places, waypoints, placemarks, photos: dict(kind,name,lat,lon,when,note,src)
        self.labeled_places = []  # Google "Labeled places" -> treated like my_places.csv entries
        self.photos = True

    def add_point(self, dt, ll, acc=None):
        if dt is None or ll is None:
            return
        self.points.append((dt.timestamp(), ll[0], ll[1], acc, dt))

    def add_visit(self, start, end, ll, place_id=None, name=None, address=None, semantic="", src=""):
        if start is None or end is None or ll is None or end < start:
            return
        self.visits.append(dict(start=start, end=end, lat=ll[0], lon=ll[1], place_id=place_id or "",
                                name=name or "", address=address or "", semantic=semantic, src=src))

    def add_activity(self, start, end, mode, dist, sll, ell, src=""):
        if start is None or end is None or end < start:
            return
        if dist is None and sll and ell:
            dist = haversine(sll[0], sll[1], ell[0], ell[1]) * 1.2
        self.activities.append(dict(start=start, end=end, mode=norm_mode(mode), dist=dist or 0.0,
                                    slat=sll[0] if sll else None, slon=sll[1] if sll else None,
                                    elat=ell[0] if ell else None, elon=ell[1] if ell else None, src=src))


# --------------------------------------------------------------------------
# Format parsers
# --------------------------------------------------------------------------

def parse_records(obj, st, name):
    n = 0
    for loc in obj.get("locations", []):
        n += _record(loc, st)
    st.sources["Records.json (raw points)"] += n


def _record(loc, st):
    ll = parse_latlng(loc)
    if ll is None:
        return 0
    dt = parse_time(loc.get("timestamp") or loc.get("timestampMs"))
    st.add_point(dt, ll, to_float(loc.get("accuracy")))
    return 1


def stream_records(fh, st):
    import ijson  # optional
    n = 0
    for loc in ijson.items(fh, "locations.item"):
        n += _record(loc, st)
    st.sources["Records.json (raw points)"] += n


def _dur(d):
    d = d or {}
    return (parse_time(d.get("startTimestamp") or d.get("startTimestampMs")),
            parse_time(d.get("endTimestamp") or d.get("endTimestampMs")))


def parse_semantic(obj, st, name):
    nv = na = 0
    for o in obj.get("timelineObjects", []):
        if "placeVisit" in o:
            pv = o["placeVisit"]
            loc = pv.get("location", {}) or {}
            s, e = _dur(pv.get("duration"))
            ll = parse_latlng(loc) or parse_latlng({"latE7": pv.get("centerLatE7"), "lngE7": pv.get("centerLngE7")}
                                                    if pv.get("centerLatE7") is not None else None)
            st.add_visit(s, e, ll, loc.get("placeId"), loc.get("name"), loc.get("address"),
                         norm_semantic(loc.get("semanticType")), "semantic")
            nv += 1
        elif "activitySegment" in o:
            a = o["activitySegment"]
            s, e = _dur(a.get("duration"))
            sll = parse_latlng(a.get("startLocation"))
            ell = parse_latlng(a.get("endLocation"))
            st.add_activity(s, e, a.get("activityType"), to_float(a.get("distance")), sll, ell, "semantic")
            na += 1
            # path detail
            raw = (a.get("simplifiedRawPath") or {}).get("points") or []
            for p in raw:
                st.add_point(parse_time(p.get("timestamp") or p.get("timestampMs")), parse_latlng(p),
                             to_float(p.get("accuracyMeters")))
            wps = (a.get("waypointPath") or {}).get("waypoints") or []
            if wps and s and e and not raw:
                span = (e - s).total_seconds()
                for i, w in enumerate(wps):
                    frac = i / max(1, len(wps) - 1)
                    st.add_point(s + timedelta(seconds=span * frac), parse_latlng(w), None)
    st.sources["Semantic Location History (visits)"] += nv
    st.sources["Semantic Location History (trips)"] += na


def parse_android_timeline(obj, st, name):
    nv = na = npnt = 0
    for seg in obj.get("semanticSegments", []) or []:
        s, e = parse_time(seg.get("startTime")), parse_time(seg.get("endTime"))
        if "visit" in seg:
            v = seg["visit"]
            tc = v.get("topCandidate", {}) or {}
            ll = parse_latlng(tc.get("placeLocation"))
            if str(v.get("hierarchyLevel", "0")) not in ("0", "", "None"):
                continue  # nested child visits duplicate the parent
            st.add_visit(s, e, ll, tc.get("placeId") or tc.get("placeID"), None, None,
                         norm_semantic(tc.get("semanticType")), "timeline")
            nv += 1
        if "activity" in seg:
            a = seg["activity"]
            tc = a.get("topCandidate", {}) or {}
            st.add_activity(s, e, tc.get("type"), to_float(a.get("distanceMeters")),
                            parse_latlng(a.get("start")), parse_latlng(a.get("end")), "timeline")
            na += 1
        for p in seg.get("timelinePath", []) or []:
            st.add_point(parse_time(p.get("time")), parse_latlng(p.get("point")), None)
            npnt += 1
    for sig in obj.get("rawSignals", []) or []:
        pos = sig.get("position")
        if pos:
            st.add_point(parse_time(pos.get("timestamp")), parse_latlng(pos.get("LatLng") or pos.get("latLng")),
                         to_float(pos.get("accuracyMeters")))
            npnt += 1
    prof = obj.get("userLocationProfile") or {}
    for fp in prof.get("frequentPlaces", []) or []:
        lab = norm_semantic(fp.get("label"))
        ll = parse_latlng(fp.get("placeLocation"))
        if lab and ll and lab not in st.labeled:
            st.labeled[lab] = ll
    st.sources["Timeline.json (visits)"] += nv
    st.sources["Timeline.json (trips)"] += na
    st.sources["Timeline.json (path points)"] += npnt


def parse_ios(lst, st, name):
    nv = na = npnt = 0
    for seg in lst:
        if not isinstance(seg, dict):
            continue
        s, e = parse_time(seg.get("startTime")), parse_time(seg.get("endTime"))
        if "visit" in seg:
            v = seg["visit"]
            tc = v.get("topCandidate", {}) or {}
            if str(v.get("hierarchyLevel", "0")) not in ("0", ""):
                continue
            st.add_visit(s, e, parse_latlng(tc.get("placeLocation")), tc.get("placeID") or tc.get("placeId"),
                         None, None, norm_semantic(tc.get("semanticType")), "ios")
            nv += 1
        elif "activity" in seg:
            a = seg["activity"]
            tc = a.get("topCandidate", {}) or {}
            st.add_activity(s, e, tc.get("type"), to_float(a.get("distanceMeters")),
                            parse_latlng(a.get("start")), parse_latlng(a.get("end")), "ios")
            na += 1
        elif "timelinePath" in seg and s:
            for p in seg["timelinePath"] or []:
                off = to_float(p.get("durationMinutesOffsetFromStartTime"), 0.0)
                st.add_point(s + timedelta(minutes=off), parse_latlng(p.get("point")), None)
                npnt += 1
    st.sources["iPhone export (visits)"] += nv
    st.sources["iPhone export (trips)"] += na
    st.sources["iPhone export (path points)"] += npnt


# --------------------------------------------------------------------------
# Other map / GPS formats
# --------------------------------------------------------------------------

import xml.etree.ElementTree as ET
import struct

NAIVE_TZ = None  # set from --tz; timestamps without a zone are taken as this zone (else this PC's)


def naive_local(dt):
    if dt.tzinfo is not None:
        return dt
    if NAIVE_TZ is not None:
        return dt.replace(tzinfo=NAIVE_TZ)
    return dt.astimezone()  # interpret as this computer's local time


def parse_time_any(v):
    """Like parse_time, but also 'YYYY:MM:DD HH:MM:SS', 'YYYY-MM-DD HH:MM', 'MM/DD/YYYY HH:MM[:SS] [AM]'
    and epoch seconds. Naive times are taken as local (--tz or this PC)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{9,13}(\.\d+)?", v.strip())):
        x = float(v)
        return datetime.fromtimestamp(x / 1000.0 if x > 1e11 else x, tz=timezone.utc)
    s = str(v).strip()
    m = re.fullmatch(r"(\d{4}):(\d\d):(\d\d)[ T](\d\d:\d\d:\d\d)(.*)", s)
    if m:
        s = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}{m.group(5).strip()}"
    has_zone = bool(re.search(r"(Z|z|[+-]\d\d:?\d\d)$", s))
    if not has_zone:
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
                    "%d.%m.%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return naive_local(datetime.strptime(s, fmt))
            except ValueError:
                pass
    s2 = s.replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", s) else s
    if not has_zone:
        try:
            dt = datetime.fromisoformat(_FRAC.sub(lambda mm: "." + (mm.group(1) + "000000")[:6], s2, count=1))
            return naive_local(dt)
        except ValueError:
            return None
    return parse_time(s2)


def _pin(st, kind, name, ll, when=None, note="", src="", **extra):
    if ll is None or (abs(ll[0]) < 1e-6 and abs(ll[1]) < 1e-6):
        return
    st.pins.append(dict(kind=kind, name=(name or "").strip(), lat=ll[0], lon=ll[1], when=when, note=note or "", src=src,
                        **extra))


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _ctext(el, name):
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None and c.text else ""


# ---- GPX ------------------------------------------------------------------
def parse_gpx(data, st, name):
    npt = nwp = nuntimed = 0
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        t = _local(el.tag)
        if t in ("trkpt", "rtept", "wpt"):
            ll = (to_float(el.get("lat")), to_float(el.get("lon")))
            if ll[0] is None or ll[1] is None:
                el.clear()
                continue
            when = parse_time_any(_ctext(el, "time"))
            if t == "wpt":
                _pin(st, "waypoint", _ctext(el, "name"), ll, when, _ctext(el, "desc"), name)
                nwp += 1
            if when:
                acc = to_float(_ctext(el, "hdop"))
                st.add_point(when, ll, acc * 5 if acc else None)
                npt += 1
            elif t != "wpt":
                nuntimed += 1
            el.clear()
    st.sources["GPX track points"] += npt
    st.sources["GPX waypoints"] += nwp
    if nuntimed:
        st.skipped.append(f"{name}: {nuntimed} route/track points without times (can't place them in your history)")
    return True


# ---- TCX (Garmin, Strava, etc.) -------------------------------------------
def parse_tcx(data, st, name):
    n = 0
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local(el.tag) == "Trackpoint":
            pos = _child(el, "Position")
            if pos is not None:
                ll = (to_float(_ctext(pos, "LatitudeDegrees")), to_float(_ctext(pos, "LongitudeDegrees")))
                when = parse_time_any(_ctext(el, "Time"))
                if when and ll[0] is not None and ll[1] is not None:
                    st.add_point(when, ll)
                    n += 1
            el.clear()
    st.sources["TCX track points"] += n
    return True


# ---- KML / KMZ (Google Earth, My Maps, old Timeline KML) --------------------
def _kml_coords(text):
    out = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            lo, la = to_float(parts[0]), to_float(parts[1])
            if la is not None and lo is not None:
                out.append((la, lo))
    return out


def parse_kml(data, st, name):
    root = ET.fromstring(data)
    nv = npt = npin = na = nuntimed = 0
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        title = _ctext(pm, "name")
        desc = _ctext(pm, "description") or _ctext(pm, "address")
        ts, span = _child(pm, "TimeStamp"), _child(pm, "TimeSpan")
        when = parse_time_any(_ctext(ts, "when")) if ts is not None else None
        begin = parse_time_any(_ctext(span, "begin")) if span is not None else None
        end = parse_time_any(_ctext(span, "end")) if span is not None else None
        for g in pm.iter():
            gt = _local(g.tag)
            if gt == "Point":
                c = _kml_coords(_ctext(g, "coordinates"))
                if not c:
                    continue
                if begin and end:
                    st.add_visit(begin, end, c[0], None, title, desc, norm_semantic(title), "kml")
                    nv += 1
                elif when:
                    st.add_point(when, c[0])
                    npt += 1
                    _pin(st, "placemark", title, c[0], when, desc, name)
                    npin += 1
                else:
                    _pin(st, "placemark", title, c[0], None, desc, name)
                    npin += 1
            elif gt == "LineString":
                c = _kml_coords(_ctext(g, "coordinates"))
                if len(c) < 2:
                    continue
                if begin and end and end >= begin:
                    span_s = (end - begin).total_seconds()
                    dist = sum(haversine(*c[i], *c[i + 1]) for i in range(len(c) - 1))
                    for i, ll in enumerate(c):
                        st.add_point(begin + timedelta(seconds=span_s * i / (len(c) - 1)), ll)
                    npt += len(c)
                    if norm_mode(title) not in ("Other", "Unknown"):
                        st.add_activity(begin, end, title, dist, c[0], c[-1], "kml")
                        na += 1
                else:
                    nuntimed += len(c)
            elif gt == "Track":  # gx:Track
                whens = [parse_time_any((w.text or "").strip()) for w in g if _local(w.tag) == "when"]
                coords = []
                for w in g:
                    if _local(w.tag) == "coord":
                        p = (w.text or "").split()
                        coords.append((to_float(p[1]), to_float(p[0])) if len(p) >= 2 else None)
                for t, ll in zip(whens, coords):
                    if t and ll and ll[0] is not None:
                        st.add_point(t, ll)
                        npt += 1
    st.sources["KML points"] += npt
    st.sources["KML visits"] += nv
    st.sources["KML pins"] += npin
    st.sources["KML trips"] += na
    if nuntimed:
        st.skipped.append(f"{name}: {nuntimed} line points without times (drawn nowhere; add times to use them)")
    return True


def parse_kmz(data, st, name):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.lower().endswith(".kml"):
                parse_kml(z.read(n), st, f"{name}:{n}")
    return True


# ---- GeoJSON (Google Maps saved/labeled places, My Maps, Arc, etc.) ---------
TIME_KEYS = ("time", "timestamp", "datetime", "date_time", "recorded_at", "recordedAt", "tst", "startTime", "when")


def parse_geojson(obj, st, name):
    feats = obj.get("features") if obj.get("type") == "FeatureCollection" else [obj]
    base = os.path.basename(name).lower()
    labeled_file = "labeled" in base
    npin = npt = 0
    for f in feats or []:
        if not isinstance(f, dict):
            continue
        g = f.get("geometry") or {}
        props = f.get("properties") or {}
        gtype, coords = g.get("type"), g.get("coordinates")
        loc = props.get("location") if isinstance(props.get("location"), dict) else {}
        title = props.get("name") or props.get("Title") or props.get("title") or loc.get("name") or ""
        addr = loc.get("address") or props.get("address") or props.get("description") or ""
        if gtype == "Point" and coords and len(coords) >= 2:
            ll = (to_float(coords[1]), to_float(coords[0]))
            if ll[0] is None:
                continue
            if labeled_file:
                _pin(st, "labeled", title, ll, None, addr, name)
                if title:
                    st.labeled_places.append(dict(name=title, lat=ll[0], lon=ll[1], address=addr))
            elif "google_maps_url" in props or loc:
                _pin(st, "saved", title, ll, None, addr, name)   # 'date' here is when you saved it, not a visit
            else:
                when = next((parse_time_any(props[k]) for k in TIME_KEYS if props.get(k)), None)
                if when:
                    st.add_point(when, ll)
                    npt += 1
                else:
                    _pin(st, "placemark", title, ll, None, addr, name)
            npin += 1
        elif gtype in ("LineString", "MultiLineString") and coords:
            lines = [coords] if gtype == "LineString" else coords
            times = props.get("coordTimes") or (props.get("coordinateProperties") or {}).get("times")
            if gtype == "LineString" and times:
                times = [times]
            for li, line in enumerate(lines):
                tl = times[li] if times and li < len(times) else None
                if not tl:
                    continue
                for c, t in zip(line, tl):
                    dt = parse_time_any(t)
                    if dt and len(c) >= 2:
                        st.add_point(dt, (c[1], c[0]))
                        npt += 1
    st.sources["GeoJSON points"] += npt
    st.sources["GeoJSON places/pins"] += npin
    return True


# ---- generic JSON: Google Photos sidecars, OwnTracks, simple point lists ---
LAT_KEYS = ("lat", "latitude", "Latitude", "LAT")
LON_KEYS = ("lon", "lng", "long", "longitude", "Longitude", "LON")


def _generic_point(d, st):
    la = next((to_float(d[k]) for k in LAT_KEYS if k in d), None)
    lo = next((to_float(d[k]) for k in LON_KEYS if k in d), None)
    if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return 0
    when = next((parse_time_any(d[k]) for k in TIME_KEYS if d.get(k) not in (None, "")), None)
    if not when:
        return 0
    st.add_point(when, (la, lo), to_float(d.get("acc") or d.get("accuracy")))
    return 1


def parse_photo_sidecar(obj, st, name):
    gd = obj.get("geoData") or {}
    la, lo = to_float(gd.get("latitude")), to_float(gd.get("longitude"))
    if (la is None or abs(la) < 1e-6) and obj.get("geoDataExif"):
        gd = obj["geoDataExif"]
        la, lo = to_float(gd.get("latitude")), to_float(gd.get("longitude"))
    ts = (obj.get("photoTakenTime") or {}).get("timestamp")
    if la is None or lo is None or (abs(la) < 1e-6 and abs(lo) < 1e-6) or not ts:
        return True
    when = parse_time_any(ts)
    st.add_point(when, (la, lo))
    _pin(st, "photo", obj.get("title") or os.path.basename(name).replace(".json", ""), (la, lo), when, "", name)
    st.sources["Google Photos (with location)"] += 1
    return True


def parse_json_lines(text, st, name):
    n = 0
    for line in text.splitlines():
        i = line.find("{")
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("_type", "location") == "location":
            n += _generic_point(d, st)
    st.sources["JSON-lines points (OwnTracks etc.)"] += n
    return n > 0


# ---- CSV (GPSLogger, Life360/Arc/other app exports, spreadsheets) -----------
OUTPUT_CSVS = {"imported_places.csv", "places.csv", "visits.csv", "trips_by_mode.csv", "daily.csv", "away_trips.csv", "regions.csv",
               "points.csv", "my_places.csv", "tombstones.csv"}


def parse_csv(data, st, name):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = csv.reader(io.StringIO(text), dialect)
    header = next(rows, None)
    if not header:
        return False
    low = [h.strip().lower() for h in header]

    def col(*names):
        for n in names:
            if n in low:
                return low.index(n)
        return None
    ila = col("lat", "latitude", "lat_deg", "y")
    ilo = col("lon", "lng", "long", "longitude", "lon_deg", "x")
    if ila is None or ilo is None:
        return False
    it = col("time", "timestamp", "datetime", "date_time", "date time", "recorded_at", "utc", "time (utc)",
             "tst", "gps_time", "created", "date")
    idate = col("date") if it is not None and low[it] != "date" else None
    iname = col("name", "title", "label", "place", "location", "description")
    iacc = col("accuracy", "acc", "accuracy(m)", "horizontal_accuracy")
    npt = npin = 0
    for r in rows:
        if len(r) <= max(ila, ilo):
            continue
        la, lo = to_float(r[ila]), to_float(r[ilo])
        if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
            continue
        when = None
        if it is not None and it < len(r) and r[it].strip():
            tv = r[it].strip()
            if idate is not None and re.fullmatch(r"\d\d?:\d\d(:\d\d)?(\.\d+)?", tv):
                tv = r[idate].strip() + " " + tv
            when = parse_time_any(tv)
        if when:
            st.add_point(when, (la, lo), to_float(r[iacc]) if iacc is not None and iacc < len(r) else None)
            npt += 1
        else:
            _pin(st, "placemark", r[iname] if iname is not None and iname < len(r) else "", (la, lo), None, "", name)
            npin += 1
    st.sources["CSV points"] += npt
    st.sources["CSV pins"] += npin
    return bool(npt or npin)


# ---- NMEA logs --------------------------------------------------------------
def _nmea_deg(v, hemi):
    if not v:
        return None
    x = float(v)
    d = int(x // 100)
    val = d + (x - d * 100) / 60
    return -val if hemi in ("S", "W") else val


def parse_nmea(data, st, name):
    n = 0
    for line in data.decode("ascii", errors="ignore").splitlines():
        if not line.startswith("$") or line[3:6] != "RMC":
            continue
        f = line.split("*")[0].split(",")
        if len(f) < 10 or f[2] != "A":
            continue
        try:
            t, dd = f[1], f[9]
            when = datetime(2000 + int(dd[4:6]), int(dd[2:4]), int(dd[0:2]), int(t[0:2]), int(t[2:4]),
                            int(float(t[4:])), tzinfo=timezone.utc)
            st.add_point(when, (_nmea_deg(f[3], f[4]), _nmea_deg(f[5], f[6])))
            n += 1
        except (ValueError, IndexError):
            continue
    st.sources["NMEA fixes"] += n
    return n > 0


# ---- photo EXIF (JPEG, HEIC, TIFF) -----------------------------------------
def read_exif_gps(data):
    """-> (lat, lon, datetime) from EXIF, or None. Pure-Python, reads GPS + DateTimeOriginal."""
    i = data.find(b"Exif\x00\x00")
    if i < 0:
        return None
    t = data[i + 6:]
    if t[:2] == b"II":
        e = "<"
    elif t[:2] == b"MM":
        e = ">"
    else:
        return None
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

    def ifd(off):
        out = {}
        if off + 2 > len(t):
            return out
        n = struct.unpack_from(e + "H", t, off)[0]
        for k in range(n):
            p = off + 2 + 12 * k
            if p + 12 > len(t):
                break
            tag, typ, cnt = struct.unpack_from(e + "HHI", t, p)
            sz = sizes.get(typ, 1) * cnt
            vo = p + 8 if sz <= 4 else struct.unpack_from(e + "I", t, p + 8)[0]
            if vo + sz > len(t):
                continue
            if typ == 2:
                out[tag] = t[vo:vo + cnt].split(b"\x00")[0].decode("ascii", "ignore")
            elif typ == 3:
                out[tag] = struct.unpack_from(e + "H" * cnt, t, vo) if cnt < 64 else None
            elif typ == 4:
                out[tag] = struct.unpack_from(e + "I" * cnt, t, vo) if cnt < 64 else None
            elif typ in (5, 10) and cnt < 16:
                vals = struct.unpack_from(e + ("I" if typ == 5 else "i") * (2 * cnt), t, vo)
                out[tag] = tuple(vals[j] / vals[j + 1] if vals[j + 1] else 0.0 for j in range(0, 2 * cnt, 2))
        return out
    try:
        ifd0 = ifd(struct.unpack_from(e + "I", t, 4)[0])
        gps = ifd(ifd0[0x8825][0]) if ifd0.get(0x8825) else {}
        exif = ifd(ifd0[0x8769][0]) if ifd0.get(0x8769) else {}
    except (struct.error, KeyError, TypeError, IndexError):
        return None
    if 2 not in gps or 4 not in gps:
        return None

    def dms(v):
        return v[0] + (v[1] if len(v) > 1 else 0) / 60 + (v[2] if len(v) > 2 else 0) / 3600
    lat, lon = dms(gps[2]), dms(gps[4])
    if gps.get(1, "N").upper().startswith("S"):
        lat = -lat
    if gps.get(3, "E").upper().startswith("W"):
        lon = -lon
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    when = None
    if gps.get(29) and gps.get(7):
        try:
            y, mo, d = (int(x) for x in gps[29].split(":"))
            h, mi, s = gps[7]
            when = datetime(y, mo, d, int(h), int(mi), int(s), tzinfo=timezone.utc)
        except (ValueError, TypeError):
            when = None
    if when is None and exif.get(0x9003):
        when = parse_time_any(exif[0x9003] + (exif.get(0x9011) or ""))
    return lat, lon, when


THUMB_DIR = None  # set by main(): where photo thumbnails are written


def exif_thumbnail(data):
    """The small JPEG preview most cameras/phones embed in EXIF (IFD1), or None."""
    i = data.find(b"Exif\x00\x00")
    if i < 0:
        return None
    t = data[i + 6:]
    e = "<" if t[:2] == b"II" else ">" if t[:2] == b"MM" else None
    if e is None:
        return None
    try:
        off0 = struct.unpack_from(e + "I", t, 4)[0]
        n0 = struct.unpack_from(e + "H", t, off0)[0]
        off1 = struct.unpack_from(e + "I", t, off0 + 2 + 12 * n0)[0]
        if not off1 or off1 + 2 > len(t):
            return None
        n1 = struct.unpack_from(e + "H", t, off1)[0]
        start = length = None
        for k in range(n1):
            tag, typ, cnt, val = struct.unpack_from(e + "HHII", t, off1 + 2 + 12 * k)
            if typ == 3:
                val = struct.unpack_from(e + "H", t, off1 + 2 + 12 * k + 8)[0]
            if tag == 0x0201:
                start = val
            elif tag == 0x0202:
                length = val
        if start and length and start + length <= len(t) and t[start:start + 2] == b"\xff\xd8":
            return t[start:start + length]
    except struct.error:
        return None
    return None


def save_thumb(data, name, when):
    if not THUMB_DIR:
        return ""
    th = exif_thumbnail(data)
    if not th and os.path.exists(name):  # no embedded preview: make one if Pillow is installed (pip install pillow)
        try:
            from PIL import Image
            try:
                import pillow_heif  # optional, for iPhone .heic
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            im = Image.open(name)
            im.thumbnail((240, 240))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
            th = buf.getvalue()
        except Exception:  # noqa: BLE001
            th = None
    if not th:
        return ""
    import hashlib
    fn = hashlib.sha1(f"{name}|{when}".encode("utf-8", "ignore")).hexdigest()[:16] + ".jpg"
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        path = os.path.join(THUMB_DIR, fn)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(th)
        return "thumbs/" + fn
    except OSError:
        return ""


def parse_photo(data, st, name):
    r = read_exif_gps(data)
    if not r:
        st.sources["Photos without GPS"] += 1
        return True
    lat, lon, when = r
    if when:
        st.add_point(when, (lat, lon))
    full = os.path.abspath(name) if os.path.exists(name) else ""
    _pin(st, "photo", os.path.basename(name.split(":")[-1]), (lat, lon), when, "", name,
         thumb=save_thumb(data, name, when), path=full)
    st.sources["Photos with GPS"] += 1
    return True


PHOTO_EXT = (".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".dng")
XML_HANDLERS = {".gpx": parse_gpx, ".tcx": parse_tcx, ".kml": parse_kml, ".kmz": parse_kmz}
SUPPORTED_EXT = (".json", ".geojson", ".csv", ".rec", ".jsonl", ".ndjson", ".nmea") + tuple(XML_HANDLERS) + PHOTO_EXT


def dispatch(obj, st, name):
    if isinstance(obj, dict):
        if "locations" in obj:
            return parse_records(obj, st, name) or True
        if "timelineObjects" in obj:
            return parse_semantic(obj, st, name) or True
        if "semanticSegments" in obj or "rawSignals" in obj:
            return parse_android_timeline(obj, st, name) or True
        if obj.get("type") in ("FeatureCollection", "Feature"):
            return parse_geojson(obj, st, name)
        if "photoTakenTime" in obj or "geoData" in obj:
            return parse_photo_sidecar(obj, st, name) if st.photos else True
        if _generic_point(obj, st):
            st.sources["JSON points"] += 1
            return True
        for v in obj.values():  # e.g. {"points": [...]} / {"data": [...]}
            if isinstance(v, list) and v and isinstance(v[0], dict) and any(k in v[0] for k in LAT_KEYS):
                return dispatch(v, st, name)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        if "startTime" in obj[0] and any(k in obj[0] for k in ("visit", "activity", "timelinePath")):
            return parse_ios(obj, st, name) or True
        if any(k in obj[0] for k in LAT_KEYS):
            n = sum(_generic_point(d, st) for d in obj if isinstance(d, dict))
            st.sources["JSON points"] += n
            return n > 0
    return scan_e7(obj, st, name)


E7_PAIRS = (("latE7", "lngE7"), ("latitudeE7", "longitudeE7"))
E7_TIME = ("timestamp", "timestampMs", "time", "startTime", "deviceTimestamp")


def scan_e7(obj, st, name):
    """Fallback for Google formats we don't know by name (e.g. Takeout's 'Timeline Edits.json'):
    find every {latE7, lngE7} with a timestamp beside it (or one level up) anywhere in the file."""
    found = dict(pts=0, pins=0)

    def ll_of(d):
        for a, b in E7_PAIRS:
            if a in d and b in d:
                return e7(d[a]), e7(d[b])
        return None

    def t_of(d):
        for k in E7_TIME:
            if d.get(k) not in (None, ""):
                t = parse_time(d[k])
                if t:
                    return t
        return None

    def acc_of(d):
        if d.get("accuracyMm") is not None:
            return to_float(d["accuracyMm"], 0) / 1000
        return to_float(d.get("accuracyMeters") or d.get("accuracy"))

    def walk(o, depth=0):
        if depth > 40:
            return
        if isinstance(o, list):
            for x in o:
                walk(x, depth + 1)
            return
        if not isinstance(o, dict):
            return
        ll = ll_of(o)
        holder = o
        if ll is None:
            for k in ("point", "placePoint", "latLng", "location", "position"):
                c = o.get(k)
                if isinstance(c, dict):
                    ll = ll_of(c)
                    if ll:
                        break
        if ll is not None and -90 <= ll[0] <= 90 and -180 <= ll[1] <= 180:
            t = t_of(holder)
            if t:
                st.add_point(t, ll, acc_of(holder))
                found["pts"] += 1
            elif o.get("placeId") or "placePoint" in o:
                _pin(st, "google", "", ll, None, "Frequent place (from Google)", name)
                found["pins"] += 1
            return
        for v in o.values():
            if isinstance(v, (dict, list)):
                walk(v, depth + 1)

    walk(obj)
    label = "Timeline Edits.json" if "timeline edits" in name.lower() else "other Google JSON"
    if found["pts"]:
        st.sources[f"{label} (raw points)"] += found["pts"]
    if found["pins"]:
        st.sources[f"{label} (frequent places)"] += found["pins"]
    return bool(found["pts"] or found["pins"])


SKIP_NAMES = ("settings.json", "archive_browser.html", "address_cache.json",
              "geocode_cache.json", "print-subscriptions.json", "shared_album_comments.json", "user-generated-memory-titles.json")
BIG = 400 * 1024 * 1024
PHOTO_READ = 1 << 20  # EXIF lives in the first part of the file


def load_stream(fh, size, name, st):
    base = os.path.basename(name).lower()
    ext = os.path.splitext(base)[1]
    if base in SKIP_NAMES or ext not in SUPPORTED_EXT:
        return
    try:
        if ext in PHOTO_EXT:
            if st.photos:
                parse_photo(fh.read(PHOTO_READ), st, name)
            return
        if ext == ".csv":
            if base in OUTPUT_CSVS or base.startswith("find_"):
                return
            ok = parse_csv(fh.read(), st, name)
            (print(f"  read {name}") if ok else st.skipped.append(f"{name}: no lat/lon columns"))
            return
        if ext in XML_HANDLERS:
            XML_HANDLERS[ext](fh.read(), st, name)
            print(f"  read {name}")
            return
        if ext == ".nmea":
            ok = parse_nmea(fh.read(), st, name)
            (print(f"  read {name}") if ok else st.skipped.append(f"{name}: no valid NMEA RMC sentences"))
            return
        if ext in (".rec", ".jsonl", ".ndjson"):
            ok = parse_json_lines(fh.read().decode("utf-8", "replace"), st, name)
            (print(f"  read {name}") if ok else st.skipped.append(f"{name}: no location lines"))
            return
    except (ET.ParseError, zipfile.BadZipFile, UnicodeDecodeError, ValueError) as ex:
        st.skipped.append(f"{name}: couldn't parse ({ex.__class__.__name__}: {str(ex)[:80]})")
        return
    # JSON / GeoJSON
    if size > BIG and base == "records.json":
        try:
            import ijson  # noqa: F401
            print(f"  streaming {name} ({size/1e6:.0f} MB) with ijson...")
            stream_records(fh, st)
            return
        except ImportError:
            print(f"  note: {name} is {size/1e6:.0f} MB; `pip install ijson` would use far less memory.")
    try:
        obj = json.load(io.TextIOWrapper(fh, encoding="utf-8-sig") if not isinstance(fh, io.TextIOBase) else fh)
    except Exception as ex:  # noqa: BLE001
        st.skipped.append(f"{name}: not readable JSON ({ex.__class__.__name__})")
        return
    quiet = "photoTakenTime" in obj if isinstance(obj, dict) else False
    if not dispatch(obj, st, name):
        st.skipped.append(f"{name}: not a location format")
    elif not quiet:
        print(f"  read {name}")


PARSE_CACHE = None  # set by main(): locscope_plus.ParseCache


def load_path(path, st):
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != "geodata" and not os.path.exists(os.path.join(root, d, "report.html"))]
            for f in sorted(files):
                load_path(os.path.join(root, f), st)
        return
    low = path.lower()
    if PARSE_CACHE is not None and (low.endswith(".zip") or low.endswith(SUPPORTED_EXT)) and \
            (not low.endswith(PHOTO_EXT) or os.path.getsize(path) > 0):
        import locscope_plus as LP
        try:
            key = LP.ParseCache.key(path, f"photos={st.photos}")
        except OSError:
            key = None
        snap = PARSE_CACHE.get(key) if key else None
        if snap is not None:
            LP.store_merge(st, snap)
            PARSE_CACHE.hits += 1
            if not low.endswith(PHOTO_EXT):
                print(f"  {path}: unchanged since last run - loaded from cache")
            return
        sub = Store()
        sub.photos = st.photos
        _load_file(path, low, sub)
        snap = LP.store_snapshot(sub)
        if key:
            PARSE_CACHE.put(key, snap)
        PARSE_CACHE.misses += 1
        LP.store_merge(st, snap)
        return
    _load_file(path, low, st)


def _load_file(path, low, st):
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            nphotos = sum(1 for i in infos if i.filename.lower().endswith(PHOTO_EXT))
            if nphotos and st.photos:
                print(f"  {os.path.basename(path)}: checking {nphotos:,} photos for GPS tags...")
            for info in infos:
                with zf.open(info) as fh:
                    load_stream(fh, info.file_size, f"{os.path.basename(path)}:{info.filename}", st)
        return
    if low.endswith(SUPPORTED_EXT):
        with open(path, "rb") as fh:
            load_stream(fh, os.path.getsize(path), path, st)


# --------------------------------------------------------------------------
# Forward geocoding (addresses -> coordinates), cached
# --------------------------------------------------------------------------

class Geocoder:
    """OpenStreetMap Nominatim lookups with an on-disk cache. Only used when you
    give an address (in my_places.csv or --find) or ask for --geocode."""

    def __init__(self, cache_path):
        self.path = cache_path
        self.cache = {}
        self.last = 0.0
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:  # noqa: BLE001
                self.cache = {}

    def _get(self, url):
        wait = 1.1 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": f"LocScope/{VERSION} (personal location-history analyzer)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def search(self, q):
        key = "search:" + q.strip().lower()
        if key not in self.cache:
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(dict(q=q, format="jsonv2", limit=1))
            try:
                j = self._get(url)
                self.cache[key] = [float(j[0]["lat"]), float(j[0]["lon"]), j[0].get("display_name", "")] if j else None
            except Exception as ex:  # noqa: BLE001
                print(f"  address lookup failed for '{q}': {ex}")
                return None
            self.save()
        return self.cache[key]

    def save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Your own named places (my_places.csv)
# --------------------------------------------------------------------------

EXAMPLE_PLACES = """name,lat,lon,radius_m,address,work,private
Home,,,150,"123 Main St, Fairfax, VA",,yes
Hospital,38.8977,-77.0365,300,,yes,
Mom's house,,,150,"456 Oak Ave, Springfield, VA",,yes
"""


def load_named_places(path, geo):
    """CSV columns: name, lat, lon, radius_m, address. Give lat/lon or an address."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name = row.get("name")
            if not name or name.startswith("#"):
                continue
            lat, lon = to_float(row.get("lat")), to_float(row.get("lon") or row.get("lng"))
            addr = row.get("address", "")
            if (lat is None or lon is None) and addr:
                r = geo.search(addr)
                if not r:
                    print(f"  my places: couldn't find the address for '{name}' - add lat/lon instead")
                    continue
                lat, lon = r[0], r[1]
            if lat is None or lon is None:
                print(f"  my places: '{name}' needs lat/lon or an address")
                continue
            yes = lambda k: (row.get(k) or "").strip().lower() in ("y", "yes", "true", "1", "x", "work", "private")
            out.append(dict(name=name, lat=lat, lon=lon, radius=to_float(row.get("radius_m"), 150.0) or 150.0,
                            address=addr, work=yes("work"), private=yes("private")))
    print(f"  loaded {len(out)} named places from {path}")
    return out


# --------------------------------------------------------------------------
# City / state / country lookup (offline, GeoNames)
# --------------------------------------------------------------------------

GEONAMES_URL = "https://download.geonames.org/export/dump/"
GEONAMES_FILES = ("cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt")


def ensure_geonames(folder, allow_download=True):
    """Make sure the free GeoNames city list is on disk (~10 MB download, once)."""
    os.makedirs(folder, exist_ok=True)
    for fn in GEONAMES_FILES:
        dest = os.path.join(folder, fn)
        if fn == "cities1000.zip" and os.path.exists(os.path.join(folder, "cities1000.txt")):
            continue
        if os.path.exists(dest):
            continue
        if not allow_download:
            return False
        print(f"  downloading {fn} from GeoNames (one time)...")
        try:
            req = urllib.request.Request(GEONAMES_URL + fn, headers={"User-Agent": f"LocScope/{VERSION}"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(dest + ".part", dest)
        except Exception as ex:  # noqa: BLE001
            print(f"  couldn't download {fn}: {ex}")
            print(f"  (you can download it yourself from {GEONAMES_URL}{fn} into {folder})")
            return False
    return True


class RegionLookup:
    """Nearest-town reverse geocoder over GeoNames cities1000 (every place with 1,000+ people)."""

    def __init__(self, folder):
        self.countries, self.admin1 = {}, {}
        with open(os.path.join(folder, "countryInfo.txt"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                c = line.rstrip("\n").split("\t")
                if len(c) > 4:
                    self.countries[c[0]] = c[4]
        with open(os.path.join(folder, "admin1CodesASCII.txt"), encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) > 1:
                    self.admin1[c[0]] = c[1]
        txt = os.path.join(folder, "cities1000.txt")
        if os.path.exists(txt):
            fh = open(txt, encoding="utf-8")
        else:
            zf = zipfile.ZipFile(os.path.join(folder, "cities1000.zip"))
            fh = io.TextIOWrapper(zf.open("cities1000.txt"), encoding="utf-8")
        self.names, self.cc, self.a1 = [], [], []
        self.grid = defaultdict(list)
        with fh:
            for line in fh:
                c = line.split("\t")
                if len(c) < 15:
                    continue
                lat, lon = float(c[4]), float(c[5])
                i = len(self.names)
                self.names.append(c[1])
                self.cc.append(c[8])
                self.a1.append(c[10])
                self.grid[(int(math.floor(lat)), int(math.floor(lon)))].append((lat, lon, i))
        self.cache = {}
        print(f"  city database: {len(self.names):,} towns and cities")

    def lookup(self, lat, lon):
        """-> (city, state, country, km_to_town_center) or None."""
        key = (round(lat, 2), round(lon, 2))
        r = self.cache.get(key)
        if r is not None or key in self.cache:
            return r
        cy, cx = int(math.floor(lat)), int(math.floor(lon))
        coslat = max(0.05, math.cos(math.radians(lat)))
        best, bd = None, float("inf")
        for ring in range(0, 6):
            for dy in range(-ring, ring + 1):
                for dx in range(-ring, ring + 1):
                    if max(abs(dy), abs(dx)) != ring:
                        continue
                    for (la, lo, i) in self.grid.get((cy + dy, ((cx + dx + 180) % 360) - 180), ()):
                        dlo = (lo - lon + 540) % 360 - 180
                        d = (la - lat) ** 2 + (dlo * coslat) ** 2
                        if d < bd:
                            best, bd = i, d
            # a hit in ring r is only guaranteed nearest after also checking ring r+1
            if best is not None and ring >= 1 and math.sqrt(bd) * 111 < ring * 111 * coslat:
                break
        if best is None:
            r = None
        else:
            km = math.sqrt(bd) * 111.2
            cc = self.cc[best]
            r = (self.names[best] if km < 50 else "", self.admin1.get(f"{cc}.{self.a1[best]}", ""),
                 self.countries.get(cc, cc), round(km, 1))
        self.cache[key] = r
        return r


def parse_coords(s):
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", s or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

class Analyzer:
    def __init__(self, st, args, named=None, regions=None):
        self.st = st
        self.args = args
        self.named = named or []
        self.regions = regions
        self.tz = None
        if args.tz:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(args.tz)
            except Exception:  # noqa: BLE001
                print(f"warning: time zone '{args.tz}' not available (on Windows: pip install tzdata). "
                      "Using timestamps' own offsets / this PC's zone.")

    def local(self, dt):
        if self.tz is not None:
            return dt.astimezone(self.tz)
        off = dt.utcoffset()
        if off is not None and off != timedelta(0):
            return dt  # source carried the local offset of where you were
        return dt.astimezone()  # this computer's zone

    # ---- cleaning -------------------------------------------------------
    def prepare(self):
        st, a = self.st, self.args
        start = datetime.combine(a.start, datetime.min.time()).replace(tzinfo=timezone.utc) if a.start else None
        end = datetime.combine(a.end + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc) if a.end else None

        def ok(dt):
            return (start is None or dt >= start - timedelta(hours=14)) and (end is None or dt < end + timedelta(hours=14))

        # points: filter, dedupe, sort
        seen = set()
        pts = []
        for ts, lat, lon, acc, dt in st.points:
            if not ok(dt):
                continue
            if acc is not None and acc > a.max_accuracy:
                continue
            k = (int(ts), round(lat, 5), round(lon, 5))
            if k in seen:
                continue
            seen.add(k)
            ld = self.local(dt)
            if a.start and ld.date() < a.start or a.end and ld.date() > a.end:
                continue
            pts.append((ts, lat, lon, acc, ld))
        pts.sort(key=lambda p: p[0])
        self.points = pts

        def dd(items, key):
            out, seen2 = [], set()
            for it in sorted(items, key=lambda x: x["start"]):
                if not ok(it["start"]):
                    continue
                ls = self.local(it["start"]).date()
                if a.start and ls < a.start or a.end and ls > a.end:
                    continue
                k = key(it)
                if k in seen2:
                    continue
                seen2.add(k)
                out.append(it)
            return out

        self.visits = dd(st.visits, lambda v: (int(v["start"].timestamp() // 60), round(v["lat"], 3), round(v["lon"], 3)))
        self.activities = dd(st.activities, lambda x: (int(x["start"].timestamp() // 60), x["mode"]))
        self.derived_visits = False
        if not self.visits and self.points:
            self.visits = self.detect_stays()
            self.derived_visits = True

    def detect_stays(self, radius=200.0, min_minutes=15):
        """Stay-point detection for raw-point-only exports (Records.json)."""
        pts, out, i, n = self.points, [], 0, len(self.points)
        while i < n:
            j = i + 1
            while j < n and haversine(pts[i][1], pts[i][2], pts[j][1], pts[j][2]) < radius:
                j += 1
            if pts[j - 1][0] - pts[i][0] >= min_minutes * 60:
                seg = pts[i:j]
                lat = sum(p[1] for p in seg) / len(seg)
                lon = sum(p[2] for p in seg) / len(seg)
                out.append(dict(start=datetime.fromtimestamp(seg[0][0], timezone.utc),
                                end=datetime.fromtimestamp(seg[-1][0], timezone.utc),
                                lat=lat, lon=lon, place_id="", name="", address="", semantic="", src="derived"))
                i = j
            else:
                i += 1
        return out

    # ---- places ---------------------------------------------------------
    def build_places(self):
        places = {}
        pid_index = {}
        for v in self.visits:
            nm = self._named_match(v["lat"], v["lon"])
            if nm is not None:
                key = "n:" + nm["name"]
            elif v["place_id"]:
                key = pid_index.get(v["place_id"])
                if key is None:
                    key = pid_index[v["place_id"]] = "p:" + v["place_id"]
            else:
                key = self._grid_key(places, v["lat"], v["lon"])
            p = places.get(key)
            if p is None:
                p = places[key] = dict(key=key, place_id=v["place_id"], name=v["name"], address=v["address"],
                                       lat_sum=0.0, lon_sum=0.0, n=0, hours=0.0, first=v["start"], last=v["end"],
                                       semantic=Counter(), night=0.0, workhrs=0.0)
            dur = (v["end"] - v["start"]).total_seconds() / 3600.0
            p["lat_sum"] += v["lat"]
            p["lon_sum"] += v["lon"]
            p["n"] += 1
            p["hours"] += dur
            p["first"] = min(p["first"], v["start"])
            p["last"] = max(p["last"], v["end"])
            if nm is not None:
                p["name"], p["address"], p["named"] = nm["name"], nm["address"], nm
            elif v["name"] and not p["name"]:
                p["name"], p["address"] = v["name"], v["address"]
            if v["semantic"]:
                p["semantic"][v["semantic"]] += 1
            n, w = self._night_work_hours(v["start"], v["end"])
            p["night"] += n
            p["workhrs"] += w
            v["place"] = key
        for p in places.values():
            p["lat"] = p["lat_sum"] / p["n"]
            p["lon"] = p["lon_sum"] / p["n"]
            if p.get("named"):
                p["lat"], p["lon"] = p["named"]["lat"], p["named"]["lon"]
        self.places = places

        def pick(label, score_key, exclude=None, min_hours=20):
            mine = [p for p in places.values() if p.get("named") and p["name"].strip().upper() == label]
            if mine:
                return mine[0]
            nm = [n for n in self.named if n["name"].strip().upper() == label]
            if nm:  # named but never visited in this date range
                return dict(key="n:" + nm[0]["name"], lat=nm[0]["lat"], lon=nm[0]["lon"], name=nm[0]["name"],
                            hours=0, n=0, place_id="", address=nm[0]["address"])
            if label in self.st.labeled:
                lat, lon = self.st.labeled[label]
                best = min(places.values(), key=lambda p: haversine(lat, lon, p["lat"], p["lon"]), default=None)
                if best and haversine(lat, lon, best["lat"], best["lon"]) < 300:
                    return best
                return dict(key=label, lat=lat, lon=lon, name="", hours=0, n=0, place_id="", address="")
            tagged = [p for p in places.values() if p["semantic"].get(label)]
            if tagged:
                return max(tagged, key=lambda p: p["hours"])
            cands = [p for p in places.values() if p is not exclude and p[score_key] >= min_hours]
            return max(cands, key=lambda p: p[score_key], default=None)

        self.home = pick("HOME", "night")
        work = pick("WORK", "workhrs", exclude=self.home, min_hours=100)
        if work is not None and self.home is not None and haversine(work["lat"], work["lon"], self.home["lat"], self.home["lon"]) < 300:
            work = None
        self.work = work

    def _named_match(self, lat, lon):
        best, bd = None, None
        for n in self.named:
            d = haversine(lat, lon, n["lat"], n["lon"])
            if d <= n["radius"] and (bd is None or d < bd):
                best, bd = n, d
        return best

    @staticmethod
    def _grid_key(places, lat, lon, radius=150.0):
        """Reuse an existing coordinate-only place within `radius` m (checks neighbouring cells)."""
        cy, cx = round(lat * 500), round(lon * 500)   # ~220 m cells
        best, bd = None, radius
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                k = "g:%d,%d" % (cy + dy, cx + dx)
                p = places.get(k)
                if p:
                    d = haversine(lat, lon, p["lat_sum"] / p["n"], p["lon_sum"] / p["n"])
                    if d < bd:
                        best, bd = k, d
        return best or "g:%d,%d" % (cy, cx)

    def _night_work_hours(self, s, e):
        """Hours of a visit falling 0:00-6:00 local, and weekday 9:00-17:00 local."""
        ls, le = self.local(s), self.local(e)
        if (le - ls) > timedelta(days=60):
            le = ls + timedelta(days=60)
        night = work = 0.0
        d = ls.date()
        while d <= le.date():
            tzinfo = ls.tzinfo
            for (h0, h1, isnight) in ((0, 6, True), (9, 17, False)):
                if not isnight and d.weekday() >= 5:
                    continue
                w0 = datetime(d.year, d.month, d.day, h0, tzinfo=tzinfo)
                w1 = datetime(d.year, d.month, d.day, h1, tzinfo=tzinfo)
                ov = (min(le, w1) - max(ls, w0)).total_seconds()
                if ov > 0:
                    if isnight:
                        night += ov / 3600
                    else:
                        work += ov / 3600
            d += timedelta(days=1)
        return night, work

    # ---- movement -------------------------------------------------------
    def movement(self):
        a = self.args
        day_act = defaultdict(float)
        day_pts = defaultdict(float)
        self.mode_dist = defaultdict(float)
        self.mode_time = defaultdict(float)
        self.mode_count = Counter()
        travel_grid_act = defaultdict(float)   # (date, weekday, hour) -> minutes
        travel_grid_pts = defaultdict(float)
        for x in self.activities:
            ls = self.local(x["start"])
            day_act[ls.date()] += x["dist"]
            self.mode_dist[x["mode"]] += x["dist"]
            secs = (x["end"] - x["start"]).total_seconds()
            self.mode_time[x["mode"]] += secs / 3600
            self.mode_count[x["mode"]] += 1
            t, le = ls, self.local(x["end"])
            guard = 0
            while t < le and guard < 48:
                nxt = (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                seg = (min(nxt, le) - t).total_seconds() / 60
                travel_grid_act[(t.date(), t.weekday(), t.hour)] += seg
                t = nxt
                guard += 1
        # distance from raw points (filtered for GPS jumps)
        pts = self.points
        prev, glitches = None, 0
        gap = a.max_gap_minutes * 60
        bad = set()
        for p in pts:
            if prev is None:
                prev = p
                continue
            dtsec = p[0] - prev[0]
            if dtsec <= 0:
                continue
            if dtsec > gap:
                prev, glitches = p, 0
                continue
            d = haversine(prev[1], prev[2], p[1], p[2])
            spd = d / dtsec
            if spd >= 320:          # faster than an airliner: GPS glitch
                bad.add(p[0])
                glitches += 1
                if glitches >= 3:   # the anchor was the bad one; re-anchor
                    bad.add(prev[0])
                    prev, glitches = p, 0
                continue
            glitches = 0
            if d < max(15.0, (p[3] or 0) * 0.5):
                continue            # jitter while standing still
            day = p[4].date()
            day_pts[day] += d
            if spd > 1.0:
                travel_grid_pts[(day, p[4].weekday(), p[4].hour)] += dtsec / 60
            prev = p
        if bad:
            self.points = [p for p in pts if p[0] not in bad]
            self.glitches = len(bad)
        # merge: prefer Google's trip distances on days that have them
        days = set(day_act) | set(day_pts)
        self.day_dist = {d: (day_act[d] if day_act.get(d) else day_pts.get(d, 0.0)) for d in days}
        self.day_source = {d: ("trips" if day_act.get(d) else "gps") for d in days}
        pts_only = sum(v for d, v in day_pts.items() if not day_act.get(d))
        if pts_only > 0:
            self.mode_dist["Unclassified (GPS)"] += pts_only
        grid = [[0.0] * 24 for _ in range(7)]
        act_days = {k[0] for k in travel_grid_act}
        for (d, wd, h), m in travel_grid_act.items():
            grid[wd][h] += m
        for (d, wd, h), m in travel_grid_pts.items():
            if d not in act_days:
                grid[wd][h] += m
        self.travel_grid = grid

    # ---- days / trips ---------------------------------------------------
    def days_and_trips(self):
        a = self.args
        info = defaultdict(lambda: dict(pts=0, visits=0, far=0.0, far_ll=None, places=set()))
        home = self.home
        def touch(ld, lat, lon):
            dinfo = info[ld.date()]
            if home:
                d = haversine(home["lat"], home["lon"], lat, lon)
                if d > dinfo["far"]:
                    dinfo["far"], dinfo["far_ll"] = d, (lat, lon)
            return dinfo
        for p in self.points:
            touch(p[4], p[1], p[2])["pts"] += 1
        for v in self.visits:
            ls = self.local(v["start"])
            le = self.local(max(v["start"], v["end"] - timedelta(seconds=1)))
            d = ls.date()
            while d <= le.date() and (d - ls.date()).days < 60:
                di = touch(datetime(d.year, d.month, d.day, tzinfo=ls.tzinfo), v["lat"], v["lon"])
                di["places"].add(v.get("place"))
                d += timedelta(days=1)
            info[ls.date()]["visits"] += 1
        for x in self.activities:
            for key in (("slat", "slon", "start"), ("elat", "elon", "end")):
                if x[key[0]] is not None:
                    touch(self.local(x[key[2]]), x[key[0]], x[key[1]])
        self.days = info
        # trips: consecutive days where farthest distance from home > threshold
        trips = []
        if home:
            thr = a.trip_km * 1000
            cur = None
            for d in sorted(info):
                di = info[d]
                if di["far"] >= thr:
                    if cur and (d - cur["end"]).days <= 1:
                        cur["end"] = d
                        if di["far"] > cur["far"]:
                            cur["far"], cur["far_ll"] = di["far"], di["far_ll"]
                        cur["dist"] += self.day_dist.get(d, 0)
                    else:
                        cur = dict(start=d, end=d, far=di["far"], far_ll=di["far_ll"], dist=self.day_dist.get(d, 0))
                        trips.append(cur)
        for t in trips:
            t["days"] = (t["end"] - t["start"]).days + 1
        self.trips = trips

    # ---- cities / states / countries --------------------------------------
    def label_regions(self):
        """Tag every visit and place with its town/state/country and tally where you've been."""
        self.region_stats = None
        R = self.regions
        if R is None:
            return
        stats = {lvl: {} for lvl in ("country", "state", "city")}

        def keys(r):
            city, state, country, _ = r
            out = [("country", country, country)]
            if state:
                out.append(("state", f"{state}, {country}", state))
            if city:
                out.append(("city", f"{city}|{state}|{country}", city))
            return out

        def rec(lvl, key, label, r):
            e = stats[lvl].get(key)
            if e is None:
                e = stats[lvl][key] = dict(name=label, state=r[1], country=r[2], days=set(), visits=0, hours=0.0,
                                           first=None, last=None)
            return e

        def touch(r, day):
            for lvl, key, label in keys(r):
                e = rec(lvl, key, label, r)
                e["days"].add(day)
                e["first"] = day if e["first"] is None or day < e["first"] else e["first"]
                e["last"] = day if e["last"] is None or day > e["last"] else e["last"]

        self.day_cities = defaultdict(set)
        for v in self.visits:
            r = R.lookup(v["lat"], v["lon"])
            v["region"] = r
            if not r:
                continue
            ls = self.local(v["start"])
            le = self.local(max(v["start"], v["end"] - timedelta(seconds=1)))
            d = ls.date()
            while d <= le.date() and (d - ls.date()).days < 60:
                touch(r, d)
                if r[0]:
                    self.day_cities[d].add(r[0])
                d += timedelta(days=1)
            for lvl, key, label in keys(r):
                e = stats[lvl][key]
                e["visits"] += 1
                e["hours"] += (v["end"] - v["start"]).total_seconds() / 3600
        # places you passed through (GPS points and trip end-points)
        seen = set()
        for p in self.points:
            d = p[4].date()
            k = (d, round(p[1], 2), round(p[2], 2))
            if k in seen:
                continue
            seen.add(k)
            r = R.lookup(p[1], p[2])
            if r:
                touch(r, d)
                if r[0]:
                    self.day_cities[d].add(r[0])
        for x in self.activities:
            for la, lo, t in ((x["slat"], x["slon"], x["start"]), (x["elat"], x["elon"], x["end"])):
                if la is not None:
                    r = R.lookup(la, lo)
                    if r:
                        touch(r, self.local(t).date())
        for p in self.places.values():
            p["region"] = R.lookup(p["lat"], p["lon"])
        self.region_stats = stats

    # ---- find a location ----------------------------------------------------
    def find(self, lat, lon, radius):
        """Every visit within `radius` m, plus days you only passed by (GPS points within radius)."""
        visits = []
        for v in self.visits:
            if haversine(lat, lon, v["lat"], v["lon"]) <= radius + 50:
                visits.append(v)
        visit_days = set()
        for v in visits:
            visit_days.add(self.local(v["start"]).date())
        passes = {}
        for p in self.points:
            if abs(p[1] - lat) > 0.05:
                continue
            if haversine(lat, lon, p[1], p[2]) <= radius:
                d = p[4].date()
                if d in visit_days:
                    continue
                e = passes.get(d)
                if e is None:
                    passes[d] = [p[4], p[4]]
                else:
                    e[1] = p[4]
        return visits, sorted(passes.items())

    def run(self):
        stage("Cleaning up GPS points", 42)
        self.prepare()
        if getattr(self, "fixes", None):
            import locscope_plus as LP
            LP.apply_fixes(self, self.fixes)
        try:
            import locscope_extras as X
            n = X.derive_trips(self)
            if n:
                print(f"  worked out {n:,} trips between stops from GPS points")
        except ImportError:
            X = None
        stage("Finding your places", 50)
        self.build_places()
        if self.args.geocode:
            stage("Looking up street addresses", 55)  # addresses + place types, before anything is labeled
            top = sorted(self.places.values(), key=lambda p: -p["hours"])
            cdir = self.args.geodata or os.path.join(os.path.dirname(os.path.abspath(__file__)), "geodata")
            try:
                os.makedirs(cdir, exist_ok=True)
            except OSError:
                cdir = self.args.out
            geocode_places(top, self.args.geocode, os.path.join(cdir, "address_cache.json"))
        stage("Measuring trips and days", 60)
        self.movement()
        self.days_and_trips()
        stage("Naming towns, states and countries", 64)
        self.label_regions()
        self.pin_stats()
        self.x = {}
        stage("Work, commutes, flights and the rest", 68)
        if X is not None:
            X.analyze(self, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mapdata"))

    # ---- saved places, waypoints, photos -------------------------------------
    def pin_stats(self):
        a = self.args
        pins = []
        for p in self.st.pins:
            if p["when"] is not None:
                ld = self.local(p["when"]).date()
                if a.start and ld < a.start or a.end and ld > a.end:
                    continue
            pins.append(p)
        seen, out = set(), []
        for p in pins:  # a photo in both the JPEG and its Google Photos sidecar counts once
            k = (p["kind"] == "photo", int(p["when"].timestamp()) if p["when"] else p["name"], round(p["lat"], 4), round(p["lon"], 4))
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        grid = defaultdict(list)
        for v in self.visits:
            grid[(int(v["lat"] * 100), int(v["lon"] * 100))].append(v)
        for p in out:
            p["visits"], p["hours"], p["last"] = 0, 0.0, None
            if p["kind"] == "photo":
                continue
            cy, cx = int(p["lat"] * 100), int(p["lon"] * 100)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    for v in grid.get((cy + dy, cx + dx), ()):
                        if haversine(p["lat"], p["lon"], v["lat"], v["lon"]) <= 150:
                            p["visits"] += 1
                            p["hours"] += (v["end"] - v["start"]).total_seconds() / 3600
                            if p["last"] is None or v["start"] > p["last"]:
                                p["last"] = v["start"]
            if self.regions is not None:
                p["region"] = self.regions.lookup(p["lat"], p["lon"])
        self.pins = out


# --------------------------------------------------------------------------
# Optional reverse geocoding (OpenStreetMap Nominatim, 1 request/second)
# --------------------------------------------------------------------------

def _short_address(j):
    a = j.get("address") or {}
    street = " ".join(x for x in (a.get("house_number"), a.get("road") or a.get("pedestrian") or a.get("footway")) if x)
    town = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("suburb") or a.get("county") or ""
    state = a.get("state") or a.get("region") or ""
    parts = [x for x in (street or a.get("neighbourhood") or "", town, state) if x]
    if a.get("country_code") and a["country_code"].lower() != "us" and a.get("country"):
        parts.append(a["country"])
    return ", ".join(parts), street


def geocode_places(places, n, cache_path):
    """Reverse-geocode the top `n` places that have no address yet (OpenStreetMap Nominatim, ~1/sec, cached)."""
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = {}
    todo = list(places)[:n]  # also places that already have a name/address: we want their OpenStreetMap category
    need = lambda k: k not in cache or (cache[k] is not None and "cat" not in cache[k])
    new = sum(1 for p in todo if need("%.4f,%.4f" % (p["lat"], p["lon"])))
    if todo:
        print(f"Street addresses for {len(todo)} places ({new} new lookups on OpenStreetMap, ~1 per second"
              f"{', about %d min' % max(1, round(new * 1.1 / 60)) if new > 60 else ''})...")
    fails = 0
    for i, p in enumerate(todo):
        key = "%.4f,%.4f" % (p["lat"], p["lon"])
        if need(key) and fails < 3:
            url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
                dict(lat=p["lat"], lon=p["lon"], format="jsonv2", zoom=18, addressdetails=1))
            req = urllib.request.Request(url, headers={"User-Agent": f"LocScope/{VERSION} (personal location-history analyzer)"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    j = json.load(r)
                short, street = _short_address(j)
                cache[key] = dict(name=j.get("name") or "", address=short or j.get("display_name") or "",
                                  street=street, full=j.get("display_name") or "", cat=j.get("category") or "",
                                  typ=j.get("type") or "")
                fails = 0
            except Exception as ex:  # noqa: BLE001
                fails += 1
                print(f"  address lookup failed ({ex})" + ("; giving up for this run" if fails >= 3 else ""))
            time.sleep(1.1)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(todo)}")
                _save_json(cache_path, cache)
        c = cache.get(key)
        if c:
            if c.get("cat"):
                try:
                    from locscope_plus import type_group
                    p["ptype"] = type_group(c["cat"], c.get("typ", ""))
                except ImportError:
                    pass
            if not p.get("address"):
                p["address"] = c.get("address") or c.get("full", "")
            if not p.get("name"):
                p["name"] = c.get("name") or c.get("street") or p["address"].split(", ")[0]
    _save_json(cache_path, cache)


def _save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except OSError:
        pass


def display_name(an, p):
    """What to call a place on screen: your name / Google's / looked-up address, else 'Near <town>'."""
    if p.get("name"):
        return p["name"]
    if an.home is p:
        return "Home"
    if an.work is p:
        return "Work"
    r = p.get("region")
    if r:
        return "Near " + region_label(r)
    return "%.4f, %.4f" % (p["lat"], p["lon"])


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def fmt_km(m):
    return f"{m/1000:,.0f} km" if m >= 10000 else f"{m/1000:,.1f} km"


def maps_link(lat, lon, pid=""):
    q = dict(api=1, query=f"{lat:.6f},{lon:.6f}")
    if pid and not pid.startswith("g:"):
        q["query_place_id"] = pid
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(q)


def region_parts(r):
    return list(r[:3]) if r else ["", "", ""]


def region_label(r, short=True):
    if not r:
        return ""
    city, state, country, _ = r
    parts = [x for x in (city, state) if x] if short else [x for x in (city, state, country) if x]
    return ", ".join(parts) or country


def write_csvs(an, out):
    def w(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f)
            cw.writerow(header)
            cw.writerows(rows)

    places = sorted(an.places.values(), key=lambda p: -p["hours"])
    w("places.csv", ["name", "address", "city", "state", "country", "lat", "lon", "visits", "hours", "first_seen",
                     "last_seen", "place_id", "google_maps"],
      [[p["name"], p["address"], *region_parts(p.get("region")), f"{p['lat']:.6f}", f"{p['lon']:.6f}", p["n"],
        f"{p['hours']:.1f}", an.local(p["first"]).isoformat(), an.local(p["last"]).isoformat(), p["place_id"],
        maps_link(p["lat"], p["lon"], p["place_id"])] for p in places])
    w("visits.csv", ["start_local", "end_local", "hours", "lat", "lon", "place_name", "address", "city", "state", "country",
                     "place_id", "source"],
      [[an.local(v["start"]).isoformat(), an.local(v["end"]).isoformat(),
        f"{(v['end']-v['start']).total_seconds()/3600:.2f}", f"{v['lat']:.6f}", f"{v['lon']:.6f}",
        display_name(an, an.places[v["place"]]), an.places[v["place"]]["address"], *region_parts(v.get("region")),
        v["place_id"], v["src"]] for v in an.visits])
    if an.region_stats:
        rows = []
        for lvl in ("country", "state", "city"):
            for e in sorted(an.region_stats[lvl].values(), key=lambda e: (-len(e["days"]), e["name"])):
                rows.append([lvl, e["name"], e["state"] if lvl == "city" else "", e["country"], len(e["days"]), e["visits"],
                             f"{e['hours']:.1f}", e["first"], e["last"]])
        w("regions.csv", ["level", "name", "state", "country", "days", "visits", "hours_at_stops", "first_date", "last_date"], rows)
    w("trips_by_mode.csv", ["start_local", "end_local", "mode", "km", "minutes", "start_lat", "start_lon", "end_lat", "end_lon"],
      [[an.local(x["start"]).isoformat(), an.local(x["end"]).isoformat(), x["mode"], f"{x['dist']/1000:.2f}",
        f"{(x['end']-x['start']).total_seconds()/60:.0f}", x["slat"], x["slon"], x["elat"], x["elon"]]
       for x in an.activities])
    dc = getattr(an, "day_cities", {})
    w("daily.csv", ["date", "km", "distance_source", "gps_points", "visits", "max_km_from_home", "towns"],
      [[d.isoformat(), f"{an.day_dist.get(d, 0)/1000:.2f}", an.day_source.get(d, ""), an.days[d]["pts"],
        an.days[d]["visits"], f"{an.days[d]['far']/1000:.1f}", "; ".join(sorted(dc.get(d, ())))] for d in sorted(an.days)])
    w("away_trips.csv", ["start", "end", "days", "farthest_km_from_home", "farthest_lat", "farthest_lon", "km_traveled"],
      [[t["start"], t["end"], t["days"], f"{t['far']/1000:.0f}", f"{t['far_ll'][0]:.5f}", f"{t['far_ll'][1]:.5f}",
        f"{t['dist']/1000:.0f}"] for t in an.trips])
    pins = getattr(an, "pins", [])
    if pins:
        w("imported_places.csv", ["kind", "name", "lat", "lon", "taken_or_timestamp", "visits", "hours", "last_visit",
                                  "note", "source_file"],
          [[KIND_LABEL.get(p["kind"], p["kind"]), p["name"], f"{p['lat']:.6f}", f"{p['lon']:.6f}",
            an.local(p["when"]).isoformat() if p["when"] else "", p["visits"], f"{p['hours']:.1f}",
            an.local(p["last"]).isoformat() if p["last"] else "", p["note"], p["src"]] for p in pins])
    if an.args.export_points:
        w("points.csv", ["time_local", "lat", "lon", "accuracy_m"],
          [[p[4].isoformat(), f"{p[1]:.6f}", f"{p[2]:.6f}", "" if p[3] is None else f"{p[3]:.0f}"] for p in an.points])


def downsample(seq, cap):
    if len(seq) <= cap:
        return seq
    step = len(seq) / cap
    return [seq[int(i * step)] for i in range(cap)] + [seq[-1]]


def build_payload(an):
    a = an.args
    places = sorted(an.places.values(), key=lambda p: -p["hours"])
    # heat map cells
    heat = Counter()
    for p in an.points:
        heat[(round(p[1], 4), round(p[2], 4))] += 1
    for v in an.visits:
        heat[(round(v["lat"], 4), round(v["lon"], 4))] += max(1, int((v["end"] - v["start"]).total_seconds() / 900))
    if len(heat) > a.heat_cells:
        coarse = Counter()
        for (la, lo), c in heat.items():
            coarse[(round(la, 3), round(lo, 3))] += c
        heat = coarse
    heat_list = [[la, lo, c] for (la, lo), c in heat.most_common(a.heat_cells)]

    # per-day tracks & timelines
    day_pts = defaultdict(list)
    for p in an.points:
        day_pts[p[4].date()].append((round(p[1], 5), round(p[2], 5), p[4].hour * 60 + p[4].minute))
    day_events = defaultdict(list)
    for v in an.visits:
        ls, le = an.local(v["start"]), an.local(v["end"])
        pl = an.places[v["place"]]
        label = display_name(an, pl)
        extra = pl.get("address") or (region_label(v["region"]) if v.get("region") else "")
        if extra and extra != label and not label.startswith("Near "):
            label += " · " + extra
        day_events[ls.date()].append([ls.strftime("%H:%M"), le.strftime("%H:%M" if le.date() == ls.date() else "%b %d %H:%M"),
                                      "visit", label, round(v["lat"], 5), round(v["lon"], 5)])
    for x in an.activities:
        ls, le = an.local(x["start"]), an.local(x["end"])
        day_events[ls.date()].append([ls.strftime("%H:%M"), le.strftime("%H:%M"), "move",
                                      f"{x['mode']} · {x['dist']/1000:.1f} km", x["slat"], x["slon"], x["elat"], x["elon"]])
    day_off = {}
    for p in an.points:
        day_off.setdefault(p[4].date(), int(p[4].utcoffset().total_seconds() // 60) if p[4].utcoffset() is not None else 0)
    tracks = {}
    for d in set(day_pts) | set(day_events):
        tracks[d.isoformat()] = dict(p=downsample(day_pts.get(d, []), a.day_points),
                                     e=sorted(day_events.get(d, []), key=lambda e: e[0]),
                                     km=round(an.day_dist.get(d, 0) / 1000, 1), o=day_off.get(d, 0))

    monthly = defaultdict(float)
    for d, m in an.day_dist.items():
        monthly[d.strftime("%Y-%m")] += m
    if monthly:
        ks = sorted(monthly)
        y, mth = int(ks[0][:4]), int(ks[0][5:])
        full = []
        while (y, mth) <= (int(ks[-1][:4]), int(ks[-1][5:])):
            k = f"{y:04d}-{mth:02d}"
            full.append([k, round(monthly.get(k, 0) / 1000, 1)])
            mth += 1
            if mth == 13:
                y, mth = y + 1, 1
    else:
        full = []

    years = defaultdict(lambda: dict(km=0.0, days=0, places=set(), away=0))
    for d, di in an.days.items():
        y = years[d.year]
        y["days"] += 1
        y["km"] += an.day_dist.get(d, 0)
        y["places"] |= {x for x in di["places"] if x}
        if an.home and di["far"] >= a.trip_km * 1000:
            y["away"] += 1

    all_dates = sorted(an.days)
    total_m = sum(an.day_dist.values())
    top_days = sorted(an.day_dist.items(), key=lambda kv: -kv[1])[:15]
    far_day = max(an.days.items(), key=lambda kv: kv[1]["far"], default=None)

    # compact visit list for the report's "Find a location" tool
    pidx = {k: i for i, k in enumerate(an.places)}
    all_places = [[display_name(an, p), p["address"] or region_label(p.get("region"))] for p in an.places.values()]
    find_visits = []
    for v in an.visits:
        ls, le = an.local(v["start"]), an.local(v["end"])
        find_visits.append([round(v["lat"], 5), round(v["lon"], 5), ls.strftime("%Y-%m-%d"), ls.strftime("%H:%M"),
                            le.strftime("%H:%M" if le.date() == ls.date() else "%b %d %H:%M"),
                            round((v["end"] - v["start"]).total_seconds() / 3600, 2), pidx[v["place"]]])

    regions = None
    if an.region_stats:
        def rl(lvl, cap):
            return [[e["name"], e["state"] if lvl == "city" else "", e["country"], len(e["days"]), e["visits"],
                     round(e["hours"]), str(e["first"]), str(e["last"])]
                    for e in sorted(an.region_stats[lvl].values(), key=lambda e: (-len(e["days"]), e["name"]))[:cap]]
        regions = dict(country=rl("country", 400), state=rl("state", 1000), city=rl("city", 3000),
                       n=[len(an.region_stats[k]) for k in ("country", "state", "city")])

    def place_json(p, i):
        return dict(rank=i + 1, name=display_name(an, p), addr=p["address"] or region_label(p.get("region"), short=False),
                    named=bool(p.get("named")), lat=round(p["lat"], 5), lon=round(p["lon"], 5),
                    n=p["n"], h=round(p["hours"], 1), first=an.local(p["first"]).strftime("%Y-%m-%d"),
                    last=an.local(p["last"]).strftime("%Y-%m-%d"), url=maps_link(p["lat"], p["lon"], p["place_id"]),
                    tag="Home" if p is an.home else "Work" if p is an.work else "")

    return dict(
        version=VERSION,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        sources=[[k, v] for k, v in an.st.sources.items() if v],
        derived_visits=an.derived_visits,
        weak=history_is_thin(an),
        units=getattr(a, "units", "imperial"),
        summary=dict(first=all_dates[0].isoformat() if all_dates else "", last=all_dates[-1].isoformat() if all_dates else "",
                     days=len(all_dates), points=len(an.points), visits=len(an.visits), places=len(an.places),
                     trips=len(an.activities), km=round(total_m / 1000),
                     earth=round(total_m / 40075017, 2),
                     away_trips=len(an.trips), away_days=sum(t["days"] for t in an.trips),
                     farthest_km=round(far_day[1]["far"] / 1000) if far_day and an.home else None,
                     farthest_date=far_day[0].isoformat() if far_day and an.home else None,
                     farthest_ll=far_day[1]["far_ll"] if far_day and an.home else None),
        home=dict(lat=an.home["lat"], lon=an.home["lon"]) if an.home else None,
        work=dict(lat=an.work["lat"], lon=an.work["lon"]) if an.work else None,
        places=[place_json(p, i) for i, p in enumerate(places[:a.top_places])],
        monthly=full,
        modes=sorted([[m, round(d / 1000, 1), round(an.mode_time.get(m, 0), 1), an.mode_count.get(m, 0)]
                      for m, d in an.mode_dist.items() if d > 0], key=lambda r: -r[1]),
        grid=[[round(v) for v in row] for row in an.travel_grid],
        years=[[y, round(v["km"] / 1000), v["days"], len(v["places"]), v["away"]] for y, v in sorted(years.items())],
        top_days=[[d.isoformat(), round(m / 1000, 1), an.day_source.get(d, "")] for d, m in top_days],
        away=[dict(start=t["start"].isoformat(), end=t["end"].isoformat(), days=t["days"], far=round(t["far"] / 1000),
                   lat=round(t["far_ll"][0], 4), lon=round(t["far_ll"][1], 4), km=round(t["dist"] / 1000))
              for t in sorted(an.trips, key=lambda t: t["start"], reverse=True)],
        trip_km=a.trip_km,
        heat=heat_list,
        tracks=tracks,
        regions=regions,
        fvisits=find_visits,
        fplaces=all_places,
        named=[dict(name=n["name"], lat=n["lat"], lon=n["lon"], r=n["radius"]) for n in an.named],
        finds=getattr(an, "find_results", []),
        pins=pins_payload(an),
    )


HISTORY_SOURCES = ("Records.json", "Semantic Location History", "Timeline.json", "iPhone export",
                   "Timeline Edits.json (raw", "KML visits", "GPX track", "TCX", "NMEA", "JSON-lines", "CSV points")


def history_is_thin(an):
    """True when the inputs had no real location-history source (only pins/photos) or almost no points."""
    srcs = {k: v for k, v in an.st.sources.items() if v}
    has_history = any(k.startswith(HISTORY_SOURCES) for k in srcs)
    return (not has_history) or (len(an.points) < 200 and len(an.visits) < 20)


KIND_LABEL = {"google": "Frequent place (Google)", "saved": "Saved in Google Maps", "labeled": "Labeled place", "waypoint": "GPX waypoint",
              "placemark": "Imported pin", "photo": "Photo"}


def pins_payload(an):
    pins = getattr(an, "pins", [])
    places = [p for p in pins if p["kind"] != "photo"]
    photos = [p for p in pins if p["kind"] == "photo"]
    places.sort(key=lambda p: (-p["hours"], p["name"]))
    photos.sort(key=lambda p: p["when"] or datetime.min.replace(tzinfo=timezone.utc))
    photos = downsample(photos, 5000)
    return dict(
        places=[[p["name"], KIND_LABEL.get(p["kind"], p["kind"]), round(p["lat"], 5), round(p["lon"], 5), p["visits"],
                 round(p["hours"], 1), an.local(p["last"]).strftime("%Y-%m-%d") if p["last"] else "",
                 p["note"][:120] or region_label(p.get("region"), short=False), maps_link(p["lat"], p["lon"])]
                for p in places[:5000]],
        photos=[[round(p["lat"], 5), round(p["lon"], 5), p["name"][:60],
                 an.local(p["when"]).strftime("%Y-%m-%d %H:%M") if p["when"] else "", p.get("thumb", ""),
                 p.get("path", "")] for p in photos],
        nplaces=len(places), nphotos=len([p for p in pins if p["kind"] == "photo"]))


def pack_payload(D):
    """Smaller report: per-day paths and explored cells become delta-encoded integer lists (decoded in the page)."""
    D = dict(D)
    tr = {}
    for k, t in D.get("tracks", {}).items():
        t = dict(t)
        q, pl, po, pm = [], 0, 0, 0
        for p in t.pop("p", []):
            la, lo, m = int(round(p[0] * 1e5)), int(round(p[1] * 1e5)), int(p[2] if len(p) > 2 and p[2] is not None else 0)
            q += (la - pl, lo - po, m - pm)
            pl, po, pm = la, lo, m
        t["q"] = q
        tr[k] = t
    D["tracks"] = tr
    x = D.get("x")
    if isinstance(x, dict) and isinstance(x.get("more"), dict) and isinstance(x["more"].get("explore"), dict):
        x = dict(x); mo = dict(x["more"]); ex = dict(mo["explore"])
        cells = ex.pop("cells", None)
        if cells:
            q, pa, pb = [], 0, 0
            for a, b in cells:
                q += (a - pa, b - pb)
                pa, pb = a, b
            ex["cq"] = q
        mo["explore"] = ex; x["more"] = mo; D["x"] = x
    return D


def write_report(an, out):
    here = os.path.dirname(os.path.abspath(__file__))
    tpl_path = os.path.join(here, "report_template.html")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    topo_js = ""
    try:
        with open(os.path.join(here, "mapdata", "topojson-client.min.js"), encoding="utf-8") as f:
            topo_js = f.read()
    except OSError:
        pass
    tpl = tpl.replace("/*__TOPOJSON__*/", topo_js.replace("</", "<\\/"))
    D = build_payload(an)
    try:
        import locscope_extras as X
        D["x"] = X.payload(an)
        vr = an.x.get("visited")
        if vr:
            D["maps"] = dict(world=vr["_world_raw"], us=vr["_us_raw"] if vr["has_us"] else None)
    except ImportError:
        X = None

    def emit(payload, fname):
        data = json.dumps(pack_payload(payload), separators=(",", ":"), default=str).replace("</", "<\\/")
        p = os.path.join(out, fname)
        with open(p, "w", encoding="utf-8") as f:
            f.write(tpl.replace("/*__DATA__*/null", data))
        return p
    path = emit(D, "report.html")
    if getattr(an.args, "share", False) and X is not None:
        sp = emit(X.scrub(D, an, an.args.share_radius), "report_share.html")
        LAST_RUN["share"] = os.path.abspath(sp)
        print(f"Shareable copy (nothing within {an.args.share_radius:g} m of home, work or private places, no work/commute/"
              f"mileage sections): {os.path.abspath(sp)}")
    try:
        with open(os.path.join(out, "version.txt"), "w") as f:
            f.write(datetime.now().isoformat())
    except OSError:
        pass
    return path


def print_summary(an):
    s = an
    dates = sorted(s.days)
    print()
    print("=" * 60)
    if not dates:
        print("No location data found in the given files.")
        return
    total = sum(s.day_dist.values())
    print(f"Date range     : {dates[0]}  ->  {dates[-1]}  ({len(dates):,} days with data)")
    print(f"GPS points     : {len(s.points):,}")
    print(f"Visits         : {len(s.visits):,}{' (detected from GPS points)' if s.derived_visits else ''}")
    print(f"Distinct places: {len(s.places):,}")
    print(f"Distance       : {fmt_km(total)}  ({total/40075017:.2f} x around the Earth)")
    if s.home:
        print(f"Home           : {s.home['lat']:.5f}, {s.home['lon']:.5f}")
    if s.work:
        print(f"Work           : {s.work['lat']:.5f}, {s.work['lon']:.5f}")
    if s.mode_dist:
        print("By travel mode :")
        for m, d in sorted(s.mode_dist.items(), key=lambda kv: -kv[1])[:8]:
            print(f"   {m:<20} {fmt_km(d):>12}")
    print("Top places by time spent:")
    for p in sorted(s.places.values(), key=lambda p: -p["hours"])[:10]:
        tag = " [home]" if p is s.home else " [work]" if p is s.work else ""
        nm = display_name(s, p)
        print(f"   {p['hours']:>9,.0f} h  {p['n']:>5} visits  {nm}{tag}")
    if s.region_stats:
        rs = s.region_stats
        print(f"Been to        : {len(rs['country'])} countries, {len(rs['state'])} states/provinces, {len(rs['city'])} towns/cities")
        for e in sorted(rs["country"].values(), key=lambda e: -len(e["days"]))[:8]:
            print(f"   {e['name']:<28} {len(e['days']):>6,} days")
    pins = getattr(s, "pins", [])
    if pins:
        np_ = sum(1 for p in pins if p["kind"] == "photo")
        other = [p for p in pins if p["kind"] != "photo"]
        print(f"Imported pins  : {len(other):,} places/pins ({sum(1 for p in other if p['visits']):,} you've visited), {np_:,} geotagged photos")
    if s.trips:
        print(f"Trips away (>{an.args.trip_km:g} km from home): {len(s.trips)}, {sum(t['days'] for t in s.trips)} days")
    print("=" * 60)


def run_finds(an, a, named, geo):
    results = []
    for q in a.find:
        ll, label = parse_coords(q), q
        if ll is None:
            nm = [n for n in named if n["name"].lower() == q.strip().lower()]
            if nm:
                ll, label = (nm[0]["lat"], nm[0]["lon"]), nm[0]["name"]
            else:
                r = geo.search(q)
                if r:
                    ll, label = (r[0], r[1]), r[2] or q
        if ll is None:
            print(f"  --find: couldn't locate '{q}'")
            continue
        visits, passes = an.find(ll[0], ll[1], a.find_radius)
        rows = []
        for v in visits:
            ls, le = an.local(v["start"]), an.local(v["end"])
            rows.append(["visit", ls.strftime("%Y-%m-%d"), ls.strftime("%H:%M"), le.strftime("%Y-%m-%d %H:%M"),
                         round((v["end"] - v["start"]).total_seconds() / 3600, 2), display_name(an, an.places[v["place"]])])
        for d, (t0, t1) in passes:
            rows.append(["passed by", d.isoformat(), t0.strftime("%H:%M"), t1.strftime("%Y-%m-%d %H:%M"), "", ""])
        rows.sort(key=lambda r: (r[1], r[2]))
        slug = re.sub(r"[^A-Za-z0-9]+", "_", q).strip("_")[:40] or "location"
        fn = os.path.join(a.out, f"find_{slug}.csv")
        with open(fn, "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f)
            cw.writerow(["kind", "date", "from", "until", "hours", "place_name"])
            cw.writerows(rows)
        results.append(dict(query=q, label=label, lat=ll[0], lon=ll[1], radius=a.find_radius, rows=rows, file=fn,
                            visits=len(visits), hours=round(sum((v["end"] - v["start"]).total_seconds() for v in visits) / 3600, 1),
                            passes=len(passes)))
    return results


def print_finds(results):
    for r in results:
        print()
        print(f"FIND: {r['label']}  ({r['lat']:.5f}, {r['lon']:.5f}, within {r['radius']:g} m)")
        if not r["rows"]:
            print("   never there in this data")
            continue
        days = sorted({row[1] for row in r["rows"]})
        print(f"   {r['visits']} visits ({r['hours']:,} h), {r['passes']} pass-by days; first {days[0]}, last {days[-1]}")
        for row in r["rows"][:15]:
            extra = f"  {row[4]} h" if row[4] != "" else ""
            print(f"   {row[0]:<9} {row[1]} {row[2]} -> {row[3]}{extra}")
        if len(r["rows"]) > 15:
            print(f"   ... {len(r['rows']) - 15} more")
        print(f"   full list: {os.path.abspath(r['file'])}")


LAST_RUN = {}  # filled by main() for the GUI: report path, output folder, find results
PROGRESS = None  # optional callback(text, percent) set by the app window


def stage(text, pct):
    if PROGRESS is not None:
        try:
            PROGRESS(text, pct)
        except Exception:  # noqa: BLE001
            pass


def inputs_signature(paths):
    sig = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    try:
                        st_ = os.stat(os.path.join(root, f))
                        sig.append((os.path.join(root, f), st_.st_size, int(st_.st_mtime)))
                    except OSError:
                        pass
        elif os.path.exists(p):
            st_ = os.stat(p)
            sig.append((p, st_.st_size, int(st_.st_mtime)))
    return sorted(sig)


def watch_loop(argv, a):
    """Re-run whenever files in the inputs change (new Timeline export dropped into a folder, etc.)."""
    clean, skip = [], False
    for i, x in enumerate(argv):
        if skip:
            skip = False
            continue
        if x == "--watch":
            if i + 1 < len(argv) and re.fullmatch(r"\d+(\.\d+)?", argv[i + 1]):
                skip = True
            continue
        if x.startswith("--watch="):
            continue
        clean.append(x)
    last = None
    print(f"Watching {', '.join(a.inputs)} - checking every {a.watch:g} s. Press Ctrl+C to stop.")
    try:
        while True:
            sig = inputs_signature(a.inputs)
            if sig != last:
                if last is not None:
                    print(f"\n{datetime.now():%Y-%m-%d %H:%M} files changed - re-analyzing...")
                main(clean)
                last = inputs_signature(a.inputs)
            time.sleep(a.watch)
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Analyze Google location history (Takeout, Timeline.json, iPhone export).")
    ap.add_argument("inputs", nargs="+", help="files, folders or .zip files to read")
    ap.add_argument("--out", default="locscope_report", help="output folder (default: locscope_report)")
    ap.add_argument("--start", type=date.fromisoformat, help="only include dates on/after YYYY-MM-DD")
    ap.add_argument("--end", type=date.fromisoformat, help="only include dates on/before YYYY-MM-DD")
    ap.add_argument("--tz", help="IANA time zone for all local times, e.g. America/New_York "
                                 "(default: each timestamp's own offset, else this PC's zone)")
    ap.add_argument("--trip-km", type=float, default=100, help="distance from home that counts as a trip (default 100)")
    ap.add_argument("--max-accuracy", type=float, default=500, help="drop GPS points less accurate than this, meters (default 500)")
    ap.add_argument("--max-gap-minutes", type=float, default=60, help="don't join GPS points further apart in time (default 60)")
    ap.add_argument("--top-places", type=int, default=100, help="places listed in the report (default 100)")
    ap.add_argument("--geocode", type=int, default=0, metavar="N",
                    help="look up names for your top N unnamed places via OpenStreetMap (sends their coordinates)")
    ap.add_argument("--units", choices=["imperial", "metric"], default="imperial",
                    help="starting units in the report: imperial (miles, feet, °F; default) or metric (km, m, °C). "
                         "The report has a mi/km switch either way.")
    ap.add_argument("--day-points", type=int, default=250, help="max points per day in the day viewer (default 250)")
    ap.add_argument("--heat-cells", type=int, default=60000, help="max heat-map cells (default 60000)")
    ap.add_argument("--export-points", action="store_true", help="also write every GPS point to points.csv")
    ap.add_argument("--places", metavar="CSV", help="your own named places (name,lat,lon,radius_m,address). "
                                                   "Default: my_places.csv in the current or script folder, if present")
    ap.add_argument("--make-places-template", action="store_true", help="write an example my_places.csv and exit")
    ap.add_argument("--find", action="append", default=[], metavar="WHERE",
                    help='list every time you were at a location: "lat,lon", an address, or one of your named places. '
                         "Can be repeated.")
    ap.add_argument("--find-radius", type=float, default=150, help="meters around a --find location (default 150)")
    ap.add_argument("--no-regions", action="store_true", help="skip the city/state/country breakdown")
    ap.add_argument("--no-photos", action="store_true", help="don't read GPS tags from photos (JPEG/HEIC/Google Photos)")
    ap.add_argument("--work-place", metavar="NAMES", help="comma-separated names from my_places.csv to count as work "
                                                          "(in addition to Work / places marked work)")
    ap.add_argument("--mileage-rate", type=float, metavar="DOLLARS", help="reimbursement per mile for work drives, e.g. 0.70")
    ap.add_argument("--share", action="store_true", help="also write report_share.html with home/work/private areas removed")
    ap.add_argument("--share-radius", type=float, default=1000, help="meters hidden around those places (default 1000)")
    ap.add_argument("--no-export", action="store_true", help="skip the Google Earth KML / GPX export")
    ap.add_argument("--no-map-regions", action="store_true", help="skip the countries / US states / counties map")
    ap.add_argument("--weather", action="store_true", help="add historical weather for every day (Open-Meteo; sends rounded "
                                                           "daily locations; cached)")
    ap.add_argument("--notes", metavar="CSV", help="day notes (date,note). Default: my_notes.csv in the current or script folder")
    ap.add_argument("--no-explore", action="store_true", help="skip the exploration (fog of war) map")
    ap.add_argument("--fixes", metavar="CSV", help="fix-ups: renames, removed stops, ignored areas/dates (my_fixes.csv)")
    ap.add_argument("--family", metavar="CSV", help="family-history places (name,place,lat,lon,radius_km,note)")
    ap.add_argument("--snow", action="store_true", help="snowy-week planner: ~30 years of snowfall near home from Open-Meteo")
    ap.add_argument("--together-with", nargs="+", metavar="FILE", help="another person's export(s), to find time spent together")
    ap.add_argument("--together-name", metavar="NAME", help="their name, for the report")
    ap.add_argument("--cache", metavar="FILE", help="parse cache (default: cache.sqlite in the geodata folder)")
    ap.add_argument("--no-cache", action="store_true", help="always re-read every input file")
    ap.add_argument("--no-collections", action="store_true", help="skip POTA parks / national parks / state capitals")
    ap.add_argument("--no-ics", action="store_true", help="skip the calendar (.ics) files")
    ap.add_argument("--watch", type=float, nargs="?", const=60, metavar="SECONDS",
                    help="keep running; re-analyze whenever the input files change (checks every 60 s)")
    ap.add_argument("--geodata", help="folder for the GeoNames city list (default: geodata next to this script)")
    a = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))

    if a.make_places_template:
        dest = "my_places.csv"
        if os.path.exists(dest):
            print(f"{dest} already exists - not overwriting.")
        else:
            with open(dest, "w", encoding="utf-8", newline="") as f:
                f.write(EXAMPLE_PLACES)
            print(f"Wrote {os.path.abspath(dest)} - edit it, then run LocScope again.")
        return 0

    global NAIVE_TZ
    NAIVE_TZ = None
    LAST_RUN.clear()
    if a.tz:
        try:
            from zoneinfo import ZoneInfo
            NAIVE_TZ = ZoneInfo(a.tz)
        except Exception:  # noqa: BLE001
            pass
    if a.watch:
        return watch_loop(argv if argv is not None else sys.argv[1:], a)
    global THUMB_DIR, PARSE_CACHE
    PARSE_CACHE = None
    gdir0 = a.geodata or os.path.join(here, "geodata")
    THUMB_DIR = None if a.no_photos else os.path.join(a.out, "thumbs")
    if not a.no_cache:
        try:
            import locscope_plus as LP
            PARSE_CACHE = LP.ParseCache(a.cache or os.path.join(gdir0, "cache.sqlite"))
            if not a.no_photos:
                THUMB_DIR = os.path.join(os.path.dirname(PARSE_CACHE.path), "thumbs")  # survives between runs
        except Exception as ex:  # noqa: BLE001
            print(f"  (cache unavailable: {ex})")
            PARSE_CACHE = None
    st = Store()
    st.photos = not a.no_photos
    print("Reading...")
    for i, p in enumerate(a.inputs):
        stage("Reading " + (os.path.basename(os.path.normpath(p)) or p), 3 + 30 * i / max(1, len(a.inputs)))
        if not os.path.exists(p):
            print(f"  not found: {p}")
            continue
        load_path(p, st)
    for s in st.skipped[:20]:
        print(f"  skipped {s}")
    if len(st.skipped) > 20:
        print(f"  ... and {len(st.skipped) - 20} more skipped files")
    if not (st.points or st.visits or st.activities):
        print("\nNo location history recognized. Point this at the Takeout .zip, Timeline.json (Android),")
        print("location-history.json (iPhone), or GPX/KML/KMZ/GeoJSON/TCX/CSV/NMEA files or photo folders.")
        if st.pins:
            print(f"(Found {len(st.pins)} places/pins but no timed locations, so there's no history to analyze.)")
        return 1

    os.makedirs(a.out, exist_ok=True)
    geo = Geocoder(os.path.join(a.out, "address_cache.json"))

    named = []
    places_file = a.places
    if not places_file:
        for cand in (os.path.join(os.getcwd(), "my_places.csv"), os.path.join(here, "my_places.csv")):
            if os.path.exists(cand):
                places_file = cand
                break
    if places_file:
        if os.path.exists(places_file):
            named = load_named_places(places_file, geo)
        else:
            print(f"  places file not found: {places_file}")
    for lp in st.labeled_places:  # Google Maps "Labeled places" (Home, Work, ...), unless you named it yourself
        if not any(n["name"].lower() == lp["name"].lower() or haversine(n["lat"], n["lon"], lp["lat"], lp["lon"]) < 100
                   for n in named):
            named.append(dict(name=lp["name"], lat=lp["lat"], lon=lp["lon"], radius=150.0, address=lp["address"]))

    regions = None
    stage("Loading the city list", 34)
    if not a.no_regions:
        gdir = a.geodata or os.path.join(here, "geodata")
        if ensure_geonames(gdir):
            try:
                regions = RegionLookup(gdir)
            except Exception as ex:  # noqa: BLE001
                print(f"  city database unreadable ({ex}); skipping cities/states/countries")
        else:
            print("  skipping cities/states/countries (city list not available)")

    if not a.notes:
        for cand in (os.path.join(os.getcwd(), "my_notes.csv"), os.path.join(here, "my_notes.csv")):
            if os.path.exists(cand):
                a.notes = cand
                break
    other = None
    if a.together_with:
        print(f"Reading {a.together_name or 'the other person'}'s data...")
        stage(f"Reading {a.together_name or 'the other person'}'s data", 37)
        other = Store()
        other.photos = False
        for p in a.together_with:
            if os.path.exists(p):
                load_path(p, other)
            else:
                print(f"  not found: {p}")
    fixes, fam = [], []
    try:
        import locscope_plus as LP
        if not a.fixes:
            for cand in (os.path.join(os.getcwd(), "my_fixes.csv"), os.path.join(here, "my_fixes.csv")):
                if os.path.exists(cand):
                    a.fixes = cand
                    break
        fixes = LP.load_fixes(a.fixes)
        named = named + LP.fixes_to_named(fixes)
        fam = LP.load_family(a.family, geo)
    except ImportError:
        pass
    if PARSE_CACHE is not None:
        if PARSE_CACHE.hits:
            print(f"  {PARSE_CACHE.hits} file(s) came from the cache, {PARSE_CACHE.misses} read fresh")
        PARSE_CACHE.close()
    an = Analyzer(st, a, named, regions)
    an.fixes, an.family_places, an.other_store = fixes, fam, other
    print("Analyzing...")
    stage("Analyzing", 40)
    an.run()
    stage("Searching your places", 80)
    an.find_results = run_finds(an, a, named, geo)
    if THUMB_DIR and os.path.abspath(THUMB_DIR) != os.path.abspath(os.path.join(a.out, "thumbs")):
        import shutil
        for p in getattr(an, "pins", []):
            t = p.get("thumb")
            if t:
                src, dst = os.path.join(THUMB_DIR, os.path.basename(t)), os.path.join(a.out, t)
                if os.path.exists(src) and not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
    stage("Writing spreadsheets", 82)
    write_csvs(an, a.out)
    try:
        import locscope_extras as X
        X.write_extra_csvs(an, a.out)
        stage("Exporting for Google Earth", 86)
        if not a.no_export:
            ex = X.export_earth(an, a.out)
            print(f"Google Earth / GPS files: {os.path.abspath(ex)}")
    except ImportError:
        pass
    stage("Writing the report", 90)
    path = write_report(an, a.out)
    stage("Done", 100)
    print_summary(an)
    print_finds(an.find_results)
    if history_is_thin(an):
        print()
        print("NOTE: very little location history was found - mostly pins and photos.")
        print("      Google now keeps Timeline on your phone. Export it there (Timeline > Export Timeline data)")
        print("      and add that Timeline.json / location-history.json file. See 'How do I get my data?'.")
    ds = sorted(an.days)
    LAST_RUN.update(weak=history_is_thin(an), report=os.path.abspath(path), out=os.path.abspath(a.out), finds=an.find_results,
                    named=[n["name"] for n in named],
                    summary=dict(days=len(ds), first=ds[0].isoformat() if ds else "", last=ds[-1].isoformat() if ds else "",
                                 km=round(sum(an.day_dist.values()) / 1000), places=len(an.places), points=len(an.points),
                                 trips=len(an.trips)))
    print(f"Report: {os.path.abspath(path)}")
    print(f"CSVs  : {os.path.abspath(a.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
