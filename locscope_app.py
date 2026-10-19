#!/usr/bin/env python3
"""
LocScope app - the window you use to build and read your location-history report.

    python locscope_app.py            (or double-click LocScope.bat / LocScope.exe)

The window is a small web page served from this computer only (127.0.0.1). If the optional
pywebview package is installed it opens in its own native window; otherwise it opens as an
app window in Microsoft Edge or Chrome (no address bar), or in your normal browser.
Nothing is sent anywhere unless you turn on the options that say so (addresses, weather, snow).
"""

import csv
import functools
import http.server
import io
import json
import mimetypes
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    HERE = getattr(sys, "_MEIPASS", HERE)
sys.path.insert(0, HERE)
import locscope  # noqa: E402

APP = "LocScope"
VERSION = getattr(locscope, "VERSION", "")
if os.name == "nt":
    DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    DOCS = os.path.join(os.path.expanduser("~"), "Documents")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".locscope")
    DOCS = os.path.expanduser("~")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
PORTS = (47615, 47616, 47617)  # fixed, so notes and choices made inside the report are kept between runs
TOKEN = secrets.token_urlsafe(18)

TIMEZONES = ["", "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
             "America/Anchorage", "Pacific/Honolulu", "America/Puerto_Rico", "America/Toronto", "America/Mexico_City",
             "Europe/London", "Europe/Dublin", "Europe/Paris", "Europe/Rome", "Europe/Berlin", "Europe/Madrid",
             "Europe/Athens", "Asia/Jerusalem", "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
             "Australia/Sydney", "Pacific/Auckland", "UTC"]

LOC_EXT = (".zip", ".json", ".geojson", ".gpx", ".kml", ".kmz", ".tcx", ".csv", ".rec", ".jsonl", ".nmea",
           ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".dng")

DEFAULTS = dict(inputs=[], out="", tz="", start="", end="", trip="100", acc="500", regions=True, photos=True,
                points=False, geocode="150", addresses=True, auto_open=True, rate="", export=True, weather=False,
                snow=False, cache=True, tog_name="", tog_files=[], share=False, share_r="1000", watch=False,
                finds=[], radius="150", units="imperial")

TABLES = {
    "places": dict(file="my_places.csv", cols=["name", "lat", "lon", "radius_m", "address", "work", "private"]),
    "notes": dict(file="my_notes.csv", cols=["date", "note"], key="date"),
    "fixes": dict(file="my_fixes.csv", cols=["type", "date_from", "date_to", "lat", "lon", "radius_m", "name", "note", "time"]),
    "family": dict(file="my_family_places.csv", cols=["name", "place", "lat", "lon", "radius_km", "note"]),
}


# ---------------------------------------------------------------------------- settings & tables
def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:  # noqa: BLE001
        pass
    if s.get("tz", "").startswith("Auto"):
        s["tz"] = ""
    if str(s.get("geocode", "")) in ("", "0"):
        s["geocode"] = "150"
    if not s.get("out"):
        s["out"] = os.path.join(DOCS, "LocScope Report")
    return s


def save_settings(s):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, SETTINGS)
    except OSError:
        pass


def table_path(name):
    return os.path.join(DATA_DIR, TABLES[name]["file"])


def read_csv_rows(path, cols):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            if "radius" in r and "radius_m" in cols and not r.get("radius_m"):
                r["radius_m"] = r["radius"]
            row = {c: r.get(c, "") for c in cols}
            if any(row.values()):
                rows.append(row)
    return rows


def yes(v):
    return "yes" if str(v).strip().lower() in ("y", "yes", "true", "1", "x") else ""


def clean_rows(name, rows):
    cols = TABLES[name]["cols"]
    out = []
    for r in rows or []:
        row = {c: str((r or {}).get(c, "") if (r or {}).get(c) is not None else "").strip() for c in cols}
        if name == "places":
            row["work"], row["private"] = yes(row["work"]), yes(row["private"])
            if not row["name"]:
                continue
        if name == "notes":
            try:
                date.fromisoformat(row["date"][:10])
            except ValueError:
                continue
            row["date"] = row["date"][:10]
            if not row["note"]:
                continue
        if any(row.values()):
            out.append(row)
    if name == "notes":
        out.sort(key=lambda r: r["date"])
    return out


def write_table(name, rows, path=None):
    cols = TABLES[name]["cols"]
    path = path or table_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    os.replace(tmp, path)


def merge_rows(name, have, new):
    key = TABLES[name].get("key")
    if not key:
        seen = {tuple(r.values()) for r in have}
        return have + [r for r in new if tuple(r.values()) not in seen]
    by = {r[key]: r for r in have}
    for r in new:
        by[r[key]] = r
    return list(by.values())


# ---------------------------------------------------------------------------- inputs
def describe_input(p):
    """A friendly label for a file or folder the user added."""
    d = dict(path=p, name=os.path.basename(os.path.normpath(p)) or p, exists=os.path.exists(p))
    if not d["exists"]:
        d.update(kind="missing", label="Not found - moved or deleted?")
        return d
    if os.path.isdir(p):
        n = 0
        size = 0
        t0 = time.time()
        for root, dirs, files in os.walk(p):
            for f in files:
                if f.lower().endswith(LOC_EXT):
                    n += 1
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            if time.time() - t0 > 1.5:
                d.update(kind="folder", label=f"Folder · {n:,}+ location files and photos", size=size)
                return d
        d.update(kind="folder", label=f"Folder · {n:,} location file{'s' if n != 1 else ''} and photos", size=size)
        return d
    low = d["name"].lower()
    try:
        d["size"] = os.path.getsize(p)
    except OSError:
        d["size"] = 0
    kinds = [
        (lambda: low.endswith(".zip") and "takeout" in low, "takeout", "Google Takeout download"),
        (lambda: low.endswith(".zip"), "zip", "Zip file"),
        (lambda: low == "timeline.json", "phone", "Phone Timeline export (Android)"),
        (lambda: low.startswith("location-history") and low.endswith(".json"), "phone", "Phone Timeline export (iPhone)"),
        (lambda: low == "records.json", "takeout", "Takeout Records.json (raw GPS)"),
        (lambda: low.startswith("timeline edits"), "takeout", "Takeout Timeline Edits"),
        (lambda: low.endswith((".json", ".geojson", ".jsonl")), "json", "Location JSON"),
        (lambda: low.endswith((".gpx", ".tcx", ".nmea")), "track", "GPS track"),
        (lambda: low.endswith((".kml", ".kmz")), "track", "Google Earth file"),
        (lambda: low.endswith(".csv"), "csv", "Spreadsheet (CSV)"),
        (lambda: low.endswith((".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".dng")), "photo", "Photo"),
    ]
    for test, kind, label in kinds:
        if test():
            d.update(kind=kind, label=label)
            break
    else:
        d.update(kind="file", label="File")
    return d


# ---------------------------------------------------------------------------- native dialogs
class Dialogs:
    """File pickers. Uses the pywebview window when there is one, otherwise Tk on its own thread."""

    def __init__(self):
        self.win = None
        self._q = None
        self._lock = threading.Lock()

    def _tk(self, fn):
        with self._lock:
            if self._q is None:
                self._q = queue.Queue()
                ready = threading.Event()
                threading.Thread(target=self._tk_loop, args=(ready,), daemon=True).start()
                ready.wait(5)
        res = queue.Queue()
        self._q.put((fn, res))
        return res.get()

    def _tk_loop(self, ready):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            root = filedialog = None
        ready.set()
        while True:
            fn, res = self._q.get()
            if root is None:
                res.put(None)
                continue
            try:
                root.lift()
                res.put(fn(filedialog, root))
            except Exception:  # noqa: BLE001
                res.put(None)

    def _wv(self, kind, **kw):
        import webview
        fd = getattr(webview, "FileDialog", None)
        kinds = {"open": getattr(fd, "OPEN", None) if fd else None, "folder": getattr(fd, "FOLDER", None) if fd else None,
                 "save": getattr(fd, "SAVE", None) if fd else None}
        k = kinds[kind]
        if k is None:
            k = {"open": webview.OPEN_DIALOG, "folder": webview.FOLDER_DIALOG, "save": webview.SAVE_DIALOG}[kind]
        r = self.win.create_file_dialog(k, **kw)
        if r is None:
            return None
        if isinstance(r, str):
            return [r]
        return list(r)

    def open_files(self, title, csv_only=False, initial=None):
        initial = initial or os.path.join(os.path.expanduser("~"), "Downloads")
        if self.win:
            types = ("CSV files (*.csv)", "All files (*.*)") if csv_only else (
                "Location files (" + ";".join("*" + e for e in LOC_EXT) + ")", "All files (*.*)")
            return self._wv("open", allow_multiple=not csv_only, file_types=types, directory=initial if os.path.isdir(initial) else "")
        ft = [("CSV", "*.csv")] if csv_only else [("Location files", " ".join("*" + e for e in LOC_EXT)), ("All files", "*.*")]
        r = self._tk(lambda fd, root: fd.askopenfilename(parent=root, title=title, filetypes=ft, initialdir=initial) if csv_only
                     else fd.askopenfilenames(parent=root, title=title, filetypes=ft, initialdir=initial))
        if not r:
            return None
        return [r] if isinstance(r, str) else list(r)

    def folder(self, title, initial=None):
        if self.win:
            r = self._wv("folder", directory=initial or "")
            return r[0] if r else None
        return self._tk(lambda fd, root: fd.askdirectory(parent=root, title=title, initialdir=initial or DOCS)) or None

    def save(self, title, filename):
        if self.win:
            r = self._wv("save", save_filename=filename, directory=os.path.join(os.path.expanduser("~"), "Documents"))
            return r[0] if r else None
        return self._tk(lambda fd, root: fd.asksaveasfilename(parent=root, title=title, initialfile=filename,
                                                              defaultextension=".csv", filetypes=[("CSV", "*.csv")])) or None


DIALOGS = Dialogs()


def open_path(p):
    try:
        if os.name == "nt":
            os.startfile(p)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------- the analysis job
class LogWriter(io.TextIOBase):
    def __init__(self, job):
        self.job = job

    def write(self, s):
        if s:
            with self.job.lock:
                self.job.log.append(s)
        return len(s)


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log = []
        self.stage = ""
        self.pct = 0
        self.started = 0
        self.finished = 0
        self.code = None
        self.result = None
        self.auto = False
        self.last_sig = None
        self.run_id = 0

    def snapshot(self, since=0):
        with self.lock:
            return dict(running=self.running, stage=self.stage, pct=round(self.pct), code=self.code, run_id=self.run_id,
                        elapsed=round((self.finished or time.time()) - self.started) if self.started else 0,
                        log="".join(self.log[since:]), n=len(self.log), result=self.result, auto=self.auto)


JOB = Job()
STATE = dict(settings=None)


def build_args(s):
    """Settings -> locscope command-line arguments. Returns (args, None) or (None, (message, field))."""
    inputs = [p for p in s.get("inputs", []) if p]
    if not inputs:
        return None, ("Add at least one file or folder first.", "inputs")
    out = (s.get("out") or "").strip() or os.path.join(DOCS, "LocScope Report")
    args = list(inputs) + ["--out", out]
    for key, flag, label in (("start", "--start", "Start date"), ("end", "--end", "End date")):
        v = (s.get(key) or "").strip()
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                return None, (f"{label} should look like 2023-06-01.", key)
            args += [flag, v]
    tz = (s.get("tz") or "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
            args += ["--tz", tz]
        except Exception:  # noqa: BLE001
            return None, (f"This computer's Python has no time-zone data for {tz}. Pick \"Automatic\", or run "
                          "\"pip install tzdata\" and restart LocScope.", "tz")

    def num(key, default, kind=float):
        try:
            return str(kind(float(str(s.get(key, "")).strip() or default)))
        except ValueError:
            return str(default)
    args += ["--units", "metric" if s.get("units") == "metric" else "imperial",
             "--trip-km", num("trip", 100), "--max-accuracy", num("acc", 500)]  # trip is stored in km
    if s.get("addresses"):
        args += ["--geocode", num("geocode", 150, int)]
    if not s.get("regions"):
        args.append("--no-regions")
    if not s.get("photos"):
        args.append("--no-photos")
    if s.get("points"):
        args.append("--export-points")
    rate = str(s.get("rate") or "").strip().lstrip("$")
    if rate:
        try:
            args += ["--mileage-rate", str(float(rate))]
        except ValueError:
            return None, ("Mileage rate should be a number like 0.70.", "rate")
    if not s.get("export"):
        args.append("--no-export")
    if s.get("share"):
        args += ["--share", "--share-radius", num("share_r", 1000)]
    args += ["--places", table_path("places"), "--notes", table_path("notes"), "--fixes", table_path("fixes")]
    if os.path.exists(table_path("family")) and read_csv_rows(table_path("family"), TABLES["family"]["cols"]):
        args += ["--family", table_path("family")]
    if s.get("snow"):
        args.append("--snow")
    if not s.get("cache"):
        args.append("--no-cache")
    tog = [p for p in s.get("tog_files", []) if p]
    if tog:
        args += ["--together-with"] + tog + ["--together-name", (s.get("tog_name") or "").strip() or "them"]
    if s.get("weather"):
        args.append("--weather")
    args += ["--geodata", os.path.join(DATA_DIR, "geodata")]
    finds = [q for q in s.get("finds", []) if q.strip()]
    for q in finds:
        args += ["--find", q]
    if finds:
        args += ["--find-radius", num("radius", 150)]
    for name in TABLES:  # make sure the files exist so locscope doesn't go looking elsewhere
        if not os.path.exists(table_path(name)) and name != "family":
            write_table(name, [])
    return args, None


def start_job(auto=False):
    with JOB.lock:
        if JOB.running:
            return dict(error="A report is already being built.")
    s = STATE["settings"]
    args, err = build_args(s)
    if err:
        return dict(error=err[0], field=err[1])
    with JOB.lock:
        JOB.running, JOB.log, JOB.stage, JOB.pct = True, [], "Starting", 1
        JOB.started, JOB.finished, JOB.code, JOB.result, JOB.auto = time.time(), 0, None, None, auto
        JOB.run_id += 1
    threading.Thread(target=_worker, args=(args,), daemon=True).start()
    return dict(ok=True)


def _progress(text, pct):
    with JOB.lock:
        JOB.stage = text
        JOB.pct = max(JOB.pct, pct)


def _worker(args):
    w = LogWriter(JOB)
    code = 1
    locscope.PROGRESS = _progress
    try:
        with redirect_stdout(w), redirect_stderr(w):
            code = locscope.main(args)
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else 1
    except MemoryError:
        w.write("\nOut of memory. For a huge Records.json, install ijson (pip install ijson) or use a narrower date range.\n")
    except Exception:  # noqa: BLE001
        w.write("\n" + traceback.format_exc())
    finally:
        locscope.PROGRESS = None
    lr = dict(locscope.LAST_RUN)
    result = None
    if code == 0 and lr.get("report"):
        result = dict(report=lr.get("report"), share=lr.get("share"), out=lr.get("out"), weak=bool(lr.get("weak")),
                      summary=lr.get("summary") or {}, finds=lr.get("finds") or [], named=lr.get("named") or [],
                      when=time.strftime("%Y-%m-%d %H:%M"))
        try:
            with open(os.path.join(DATA_DIR, "last_run.json"), "w", encoding="utf-8") as f:
                json.dump(result, f, default=str)
        except OSError:
            pass
        try:
            JOB.last_sig = locscope.inputs_signature(list(STATE["settings"].get("inputs", [])))
        except Exception:  # noqa: BLE001
            pass
    with JOB.lock:
        JOB.running, JOB.code, JOB.finished = False, code, time.time()
        JOB.pct = 100 if code == 0 else JOB.pct
        JOB.stage = "Done" if code == 0 else "Didn't finish"
        if result:
            JOB.result = json.loads(json.dumps(result, default=str))


def last_result():
    if JOB.result:
        return JOB.result
    try:
        with open(os.path.join(DATA_DIR, "last_run.json"), encoding="utf-8") as f:
            r = json.load(f)
        return r if r.get("report") and os.path.exists(r["report"]) else None
    except Exception:  # noqa: BLE001
        return None


def watch_loop():
    while True:
        time.sleep(30)
        s = STATE["settings"]
        if not s or not s.get("watch") or JOB.running or not s.get("inputs"):
            continue
        try:
            sig = locscope.inputs_signature(list(s["inputs"]))
        except Exception:  # noqa: BLE001
            continue
        if JOB.last_sig is None:
            JOB.last_sig = sig
        elif sig != JOB.last_sig:
            start_job(auto=True)


# ---------------------------------------------------------------------------- HTTP
LAST_PING = [time.time()]
BYE = [0.0]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocScope"

    def log_message(self, *a):
        pass

    # -- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")

    def _authed(self):
        return self.headers.get("X-LocScope") == TOKEN

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _static(self, path, root):
        root = os.path.realpath(root)
        full = os.path.realpath(os.path.join(root, urllib.parse.unquote(path).lstrip("/\\")))
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            return self._send(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                with open(full, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (OSError, ConnectionError):
            pass

    def _report_root(self):
        r = last_result()
        if r and r.get("out"):
            return r["out"]
        return STATE["settings"].get("out") or ""

    # -- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain")
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "locscope_app.html"), encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                return self._send(500, "locscope_app.html is missing - extract the whole zip again.", "text/plain")
            html = html.replace("__TOKEN__", TOKEN).replace("__MODE__", MODE[0])
            return self._send(200, html, "text/html; charset=utf-8")
        if p == "/hello":
            return self._send(200, {"app": APP})
        if p.startswith("/report/"):
            root = self._report_root()
            return self._static(p[len("/report/"):], root) if root else self._send(404, "No report yet", "text/plain")
        if p == "/api/bye":
            q = urllib.parse.parse_qs(u.query)
            if q.get("t", [""])[0] == TOKEN:
                BYE[0] = time.time()
            return self._send(204, b"")
        if not p.startswith("/api/"):
            return self._send(404, "Not found", "text/plain")
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        LAST_PING[0] = time.time()
        if p == "/api/ping":
            return self._send(200, {"ok": True})
        if p == "/api/state":
            s = STATE["settings"]
            return self._send(200, dict(
                settings=s, inputs=[describe_input(x) for x in s.get("inputs", [])],
                together=[describe_input(x) for x in s.get("tog_files", [])],
                timezones=TIMEZONES, data_dir=DATA_DIR, docs=DOCS, version=VERSION, mode=MODE[0],
                result=last_result(), job=JOB.snapshot(10 ** 9), files={k: table_path(k) for k in TABLES},
                cache_mb=round(os.path.getsize(os.path.join(DATA_DIR, "geodata", "cache.sqlite")) / 1e6, 1)
                if os.path.exists(os.path.join(DATA_DIR, "geodata", "cache.sqlite")) else 0))
        if p == "/api/progress":
            since = int((urllib.parse.parse_qs(u.query).get("since") or ["0"])[0])
            return self._send(200, JOB.snapshot(since))
        if p.startswith("/api/table/"):
            name = p.split("/")[3]
            if name not in TABLES:
                return self._send(404, {"error": "no such table"})
            try:
                rows = read_csv_rows(table_path(name), TABLES[name]["cols"])
            except OSError as ex:
                return self._send(200, dict(cols=TABLES[name]["cols"], rows=[], error=str(ex)))
            return self._send(200, dict(cols=TABLES[name]["cols"], rows=rows, file=table_path(name)))
        return self._send(404, {"error": "unknown"})

    def do_POST(self):
        if self.path.startswith("/api/bye?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("t", [""])[0] == TOKEN:
                BYE[0] = time.time()
            return self._send(204, b"")
        if not self._host_ok() or not self._authed():
            return self._send(403, {"error": "forbidden"})
        LAST_PING[0] = time.time()
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/upload":  # browser-window drag & drop: the file itself is sent, saved under LocScope's folder
            name = os.path.basename(urllib.parse.unquote(self.headers.get("X-Filename") or "upload.bin")) or "upload.bin"
            n = int(self.headers.get("Content-Length") or 0)
            d = os.path.join(DATA_DIR, "added files")
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, name)
            with open(dest, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return self._send(200, {"path": dest})
        body = self._json()
        s = STATE["settings"]
        if p == "/api/settings":
            for k, v in (body or {}).items():
                if k in DEFAULTS or k == "out":
                    s[k] = v
            save_settings(s)
            return self._send(200, {"ok": True})
        if p == "/api/inputs/add":
            which = "tog_files" if body.get("together") else "inputs"
            paths = body.get("paths")
            if paths is None:
                if body.get("folder"):
                    d = DIALOGS.folder("Add a folder - every location file and photo inside is read")
                    paths = [d] if d else []
                else:
                    paths = DIALOGS.open_files("Add location files") or []
            added = 0
            for x in paths:
                x = os.path.normpath(str(x).strip().strip('"'))
                if x and x not in s[which]:
                    s[which].append(x)
                    added += 1
            save_settings(s)
            return self._send(200, {"added": added, "items": [describe_input(x) for x in s[which]]})
        if p == "/api/inputs/remove":
            which = "tog_files" if body.get("together") else "inputs"
            drop = set(body.get("paths") or [])
            s[which] = [x for x in s[which] if x not in drop] if drop else []
            save_settings(s)
            return self._send(200, {"items": [describe_input(x) for x in s[which]]})
        if p == "/api/pick-out":
            d = DIALOGS.folder("Where should the report go?", s.get("out"))
            if d:
                s["out"] = os.path.normpath(d)
                save_settings(s)
            return self._send(200, {"out": s["out"]})
        if p.startswith("/api/table/"):
            parts = p.split("/")
            name = parts[3]
            if name not in TABLES:
                return self._send(404, {"error": "no such table"})
            action = parts[4] if len(parts) > 4 else "save"
            cols = TABLES[name]["cols"]
            try:
                have = read_csv_rows(table_path(name), cols)
                if action == "save":
                    rows = clean_rows(name, body.get("rows"))
                    write_table(name, rows)
                    return self._send(200, {"rows": rows})
                if action == "import":
                    got = DIALOGS.open_files("Import " + TABLES[name]["file"], csv_only=True)
                    if not got:
                        return self._send(200, {"rows": have, "imported": 0})
                    new = clean_rows(name, read_csv_rows(got[0], cols))
                    rows = clean_rows(name, merge_rows(name, have, new))
                    write_table(name, rows)
                    return self._send(200, {"rows": rows, "imported": len(new)})
                if action == "export":
                    dest = DIALOGS.save("Export", TABLES[name]["file"])
                    if dest:
                        write_table(name, have, dest)
                    return self._send(200, {"saved": dest})
            except (OSError, csv.Error) as ex:
                return self._send(200, {"error": str(ex), "rows": []})
        if p == "/api/run":
            return self._send(200, start_job())
        if p == "/api/open":
            what = body.get("what")
            r = last_result() or {}
            target = {"report": r.get("report"), "share": r.get("share"), "folder": r.get("out"),
                      "data": DATA_DIR}.get(what)
            if what == "url":
                u = str(body.get("url") or "")
                if u.startswith("https://") or u.startswith("http://127.0.0.1"):
                    webbrowser.open(u)
                return self._send(200, {"ok": True})
            if what in ("report", "share") and target and os.path.exists(target):
                webbrowser.open(f"{BASE[0]}/report/{urllib.parse.quote(os.path.basename(target))}")
                return self._send(200, {"ok": True})
            ok = bool(target) and os.path.exists(target) and open_path(target)
            return self._send(200, {"ok": ok})
        if p == "/api/cache/clear":
            c = os.path.join(DATA_DIR, "geodata", "cache.sqlite")
            try:
                if os.path.exists(c):
                    os.remove(c)
                return self._send(200, {"ok": True})
            except OSError as ex:
                return self._send(200, {"error": str(ex)})
        if p == "/api/quit":
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "unknown"})

    def do_PUT(self):
        return self.do_POST()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


MODE = ["browser"]
BASE = [""]


def already_running():
    import urllib.request
    for port in PORTS:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/hello", timeout=0.6) as r:
                if json.loads(r.read().decode()).get("app") == APP:
                    return port
        except Exception:  # noqa: BLE001
            continue
    return None


def start_server():
    for port in PORTS + (0,):
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE[0] = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv


def find_app_browser():
    if os.name == "nt":
        cands = []
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
        for c in cands:
            if os.path.exists(c):
                return c
        return None
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run_webview(url):
    try:
        import webview
    except Exception:  # noqa: BLE001
        return False
    try:
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        except Exception:  # noqa: BLE001
            pass
        win = webview.create_window("LocScope", url, width=1280, height=880, min_size=(900, 620), text_select=True,
                                    background_color="#f4f4f2")
        DIALOGS.win = win
        MODE[0] = "window"

        def setup(window):
            try:  # drag & drop of files and folders straight from Explorer
                from webview.dom import DOMEventHandler

                def on_drop(e):
                    files = (e.get("dataTransfer") or {}).get("files") or []
                    paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                    if paths:
                        window.evaluate_js(f"window.onNativeDrop && window.onNativeDrop({json.dumps(paths)})")
                window.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True)
                window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
            except Exception:  # noqa: BLE001
                pass
        storage = os.path.join(DATA_DIR, "window")
        os.makedirs(storage, exist_ok=True)
        webview.start(setup, win, private_mode=False, storage_path=storage)
        return True
    except Exception:  # noqa: BLE001
        MODE[0] = "browser"
        DIALOGS.win = None
        return False


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    os.makedirs(DATA_DIR, exist_ok=True)
    STATE["settings"] = load_settings()
    s = STATE["settings"]
    for p in sys.argv[1:]:  # files dropped on the program icon
        if not p.startswith("-") and os.path.exists(p):
            p = os.path.normpath(p)
            if p not in s["inputs"]:
                s["inputs"].append(p)
    save_settings(s)
    port = already_running()
    if port:  # LocScope is already open: just show it again
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    srv = start_server()
    threading.Thread(target=watch_loop, daemon=True).start()
    url = BASE[0] + "/"
    force_browser = "--browser" in sys.argv or os.environ.get("LOCSCOPE_BROWSER")
    if not force_browser and run_webview(url):
        while JOB.running:  # window closed mid-build: let it finish so the report isn't half-written
            time.sleep(1)
        srv.shutdown()
        return
    MODE[0] = "browser"
    exe = None if "--no-app-window" in sys.argv else find_app_browser()
    if exe:
        prof = os.path.join(DATA_DIR, "window-edge" if "edge" in exe.lower() else "window-chrome")
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,880", f"--user-data-dir={prof}",
                              "--no-first-run", "--no-default-browser-check"])
        except OSError:
            webbrowser.open(url)
    else:
        webbrowser.open(url)
    print(f"LocScope is running at {url}  (close its window to quit)")
    LAST_PING[0] = time.time()
    start = time.time()
    while True:
        time.sleep(2)
        idle = time.time() - LAST_PING[0]
        if JOB.running:
            continue
        if BYE[0] and BYE[0] > LAST_PING[0] and time.time() - BYE[0] > 6:
            break  # window closed
        if idle > 300 and time.time() - start > 60:
            break  # nobody has looked for 5 minutes
    srv.shutdown()


if __name__ == "__main__":
    main()
