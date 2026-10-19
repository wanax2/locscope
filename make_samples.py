"""Generate fake location exports in all four Google formats for testing."""
import json, random, os, zipfile, math
from datetime import datetime, timedelta, timezone

random.seed(7)
OUT = "samples"
os.makedirs(OUT, exist_ok=True)
ET = timezone(timedelta(hours=-4))
HOME = (38.8462, -77.3064)
WORK = (38.8977, -77.0365)
GYM = (38.8650, -77.2300)
BEACH = (38.3365, -75.0849)  # Ocean City MD


def path(a, b, n):
    return [(a[0] + (b[0]-a[0])*i/n + random.gauss(0, 2e-4), a[1] + (b[1]-a[1])*i/n + random.gauss(0, 2e-4)) for i in range(n+1)]


def day_plan(d):
    """Return list of ('visit', start, end, loc, name) / ('move', start, end, a, b, mode)."""
    t0 = datetime(d.year, d.month, d.day, tzinfo=ET)
    wd = d.weekday()
    if d.day in (12, 13, 14) and d.month == 7:  # beach trip
        if d.day == 12:
            return [("visit", t0, t0+timedelta(hours=9), HOME, "Home"), ("move", t0+timedelta(hours=9), t0+timedelta(hours=12), HOME, BEACH, "IN_PASSENGER_VEHICLE"),
                    ("visit", t0+timedelta(hours=12), t0+timedelta(hours=24), BEACH, "Ocean City Boardwalk")]
        if d.day == 13:
            return [("visit", t0, t0+timedelta(hours=24), BEACH, "Ocean City Boardwalk")]
        return [("visit", t0, t0+timedelta(hours=14), BEACH, "Ocean City Boardwalk"), ("move", t0+timedelta(hours=14), t0+timedelta(hours=17), BEACH, HOME, "IN_PASSENGER_VEHICLE"),
                ("visit", t0+timedelta(hours=17), t0+timedelta(hours=24), HOME, "Home")]
    if wd < 5:
        return [("visit", t0, t0+timedelta(hours=7, minutes=30), HOME, "Home"),
                ("move", t0+timedelta(hours=7, minutes=30), t0+timedelta(hours=8, minutes=20), HOME, WORK, "IN_SUBWAY"),
                ("visit", t0+timedelta(hours=8, minutes=20), t0+timedelta(hours=17), WORK, "NCUA HQ"),
                ("move", t0+timedelta(hours=17), t0+timedelta(hours=18), WORK, HOME, "IN_SUBWAY"),
                ("visit", t0+timedelta(hours=18), t0+timedelta(hours=24), HOME, "Home")]
    return [("visit", t0, t0+timedelta(hours=10), HOME, "Home"),
            ("move", t0+timedelta(hours=10), t0+timedelta(hours=10, minutes=20), HOME, GYM, "CYCLING"),
            ("visit", t0+timedelta(hours=10, minutes=20), t0+timedelta(hours=12), GYM, "Gym"),
            ("move", t0+timedelta(hours=12), t0+timedelta(hours=12, minutes=25), GYM, HOME, "WALKING"),
            ("visit", t0+timedelta(hours=12, minutes=25), t0+timedelta(hours=24), HOME, "Home")]


def hav(a, b):
    R = 6371000; p1, p2 = math.radians(a[0]), math.radians(b[0])
    x = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(math.radians(b[1]-a[1])/2)**2
    return 2*R*math.asin(math.sqrt(x))


def iso(t): return t.isoformat(timespec="milliseconds")
def zulu(t): return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
def e7(v): return int(round(v * 1e7))
def deg(ll): return f"{ll[0]:.7f}°, {ll[1]:.7f}°"
def geo(ll): return f"geo:{ll[0]:.6f},{ll[1]:.6f}"

start = datetime(2023, 6, 1).date()
days = [start + timedelta(days=i) for i in range(60)]
records, semantic = [], {}
for d in days:
    for ev in day_plan(d):
        if ev[0] == "visit":
            _, s, e, loc, name = ev
            t = s
            while t < e:
                records.append({"latitudeE7": e7(loc[0]+random.gauss(0, 1e-4)), "longitudeE7": e7(loc[1]+random.gauss(0, 1e-4)),
                                "accuracy": random.randint(5, 40), "timestamp": zulu(t)})
                t += timedelta(minutes=30)
            semantic.setdefault((s.year, s.month), []).append({"placeVisit": {"location": {"latitudeE7": e7(loc[0]), "longitudeE7": e7(loc[1]), "placeId": "ChIJ" + name.replace(" ", ""), "name": name, "address": name + ", somewhere", "semanticType": "TYPE_HOME" if name == "Home" else "TYPE_UNKNOWN"}, "duration": {"startTimestamp": zulu(s), "endTimestamp": zulu(e)}}})
        else:
            _, s, e, a, b, mode = ev
            pts = path(a, b, 20)
            for i, p in enumerate(pts):
                records.append({"latitudeE7": e7(p[0]), "longitudeE7": e7(p[1]), "accuracy": 10, "timestamp": zulu(s + (e-s)*i/20)})
            semantic.setdefault((s.year, s.month), []).append({"activitySegment": {"startLocation": {"latitudeE7": e7(a[0]), "longitudeE7": e7(a[1])}, "endLocation": {"latitudeE7": e7(b[0]), "longitudeE7": e7(b[1])}, "duration": {"startTimestamp": zulu(s), "endTimestamp": zulu(e)}, "distance": int(hav(a, b)*1.25), "activityType": mode}})
# a GPS glitch
records.append({"latitudeE7": e7(10.0), "longitudeE7": e7(10.0), "accuracy": 20, "timestamp": zulu(datetime(2023, 6, 5, 3, 1, tzinfo=ET))})
records.sort(key=lambda r: r["timestamp"])

MONTHS = ["JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE","JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"]
with zipfile.ZipFile(f"{OUT}/takeout-20230801.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Takeout/Location History (Timeline)/Records.json", json.dumps({"locations": records}))
    z.writestr("Takeout/Location History (Timeline)/Settings.json", json.dumps({"deviceSettings": []}))
    for (y, m), objs in semantic.items():
        z.writestr(f"Takeout/Location History (Timeline)/Semantic Location History/{y}/{y}_{MONTHS[m-1]}.json", json.dumps({"timelineObjects": objs}))

# Records-only copy (tests stay detection)
with open(f"{OUT}/Records.json", "w") as f:
    json.dump({"locations": records}, f)

# Android Timeline.json (Aug-Sep 2024), iOS (Oct 2024)
def segs(days, fmt_ll, style):
    out = []
    for d in days:
        for ev in day_plan(d):
            if ev[0] == "visit":
                _, s, e, loc, name = ev
                if style == "android":
                    out.append({"startTime": iso(s), "endTime": iso(e), "startTimeTimezoneUtcOffsetMinutes": -240,
                                "visit": {"hierarchyLevel": 0, "probability": 0.9, "topCandidate": {"placeId": "ChIJ" + name.replace(" ", ""), "semanticType": "HOME" if name == "Home" else "UNKNOWN", "probability": 0.8, "placeLocation": {"latLng": fmt_ll(loc)}}}})
                else:
                    out.append({"startTime": iso(s), "endTime": iso(e), "visit": {"hierarchyLevel": "0", "probability": "0.9", "topCandidate": {"placeID": "ChIJ" + name.replace(" ", ""), "semanticType": "Home" if name == "Home" else "Unknown", "probability": "0.8", "placeLocation": fmt_ll(loc)}}})
            else:
                _, s, e, a, b, mode = ev
                dist = hav(a, b) * 1.25
                if style == "android":
                    out.append({"startTime": iso(s), "endTime": iso(e), "activity": {"start": {"latLng": fmt_ll(a)}, "end": {"latLng": fmt_ll(b)}, "distanceMeters": dist, "topCandidate": {"type": mode, "probability": 0.9}}})
                    out.append({"startTime": iso(s), "endTime": iso(e), "timelinePath": [{"point": fmt_ll(p), "time": iso(s + (e-s)*i/10)} for i, p in enumerate(path(a, b, 10))]})
                else:
                    out.append({"startTime": iso(s), "endTime": iso(e), "activity": {"start": fmt_ll(a), "end": fmt_ll(b), "distanceMeters": f"{dist:.1f}", "probability": "0.9", "topCandidate": {"type": mode.lower().replace("_", " ").replace("in ", "in "), "probability": "0.9"}}})
                    out.append({"startTime": iso(s), "endTime": iso(e), "timelinePath": [{"point": fmt_ll(p), "durationMinutesOffsetFromStartTime": str(int((e-s).total_seconds()/60*i/10))} for i, p in enumerate(path(a, b, 10))]})
    return out

and_days = [datetime(2024, 7, 1).date() + timedelta(days=i) for i in range(45)]
with open(f"{OUT}/Timeline.json", "w", encoding="utf-8") as f:
    json.dump({"semanticSegments": segs(and_days, deg, "android"),
               "rawSignals": [{"position": {"LatLng": deg(HOME), "accuracyMeters": 12, "altitudeMeters": 90, "source": "WIFI", "timestamp": iso(datetime(2024, 7, 3, 2, tzinfo=ET))}}],
               "userLocationProfile": {"frequentPlaces": [{"placeId": "ChIJHome", "placeLocation": deg(HOME), "label": "HOME"}, {"placeId": "ChIJNCUAHQ", "placeLocation": deg(WORK), "label": "WORK"}]}},
              f, ensure_ascii=False)
ios_days = [datetime(2024, 10, 1).date() + timedelta(days=i) for i in range(20)]
with open(f"{OUT}/location-history.json", "w") as f:
    json.dump(segs(ios_days, geo, "ios"), f)
print("samples written:", os.listdir(OUT))
