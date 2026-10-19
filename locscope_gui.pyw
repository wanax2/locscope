#!/usr/bin/env python3
"""
LocScope GUI - point-and-click front end for locscope.py.

Double-click this file on Windows (.pyw opens without a console window), or run:
    python locscope_gui.pyw
Needs only the Python that comes from python.org (Tkinter is included).
"""

import csv
import io
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from contextlib import redirect_stdout, redirect_stderr
from datetime import date

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import locscope  # noqa: E402

APP = "LocScope"
if os.name == "nt":
    DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    DOCS = os.path.join(os.path.expanduser("~"), "Documents")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".locscope")
    DOCS = os.path.expanduser("~")
SETTINGS = os.path.join(DATA_DIR, "settings.json")

TIMEZONES = ["Auto (each record's own / this PC)", "America/New_York", "America/Chicago", "America/Denver",
             "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Puerto_Rico",
             "Europe/London", "Europe/Paris", "Europe/Rome", "Europe/Berlin", "Asia/Jerusalem", "Asia/Tokyo",
             "Australia/Sydney", "UTC"]

FILE_TYPES = [
    ("All location files", "*.zip *.json *.geojson *.gpx *.kml *.kmz *.tcx *.csv *.rec *.jsonl *.nmea "
                           "*.jpg *.jpeg *.heic *.heif *.tif *.tiff *.dng"),
    ("Google Takeout zip", "*.zip"),
    ("Google Timeline / JSON", "*.json *.geojson"),
    ("GPS tracks", "*.gpx *.tcx *.kml *.kmz *.nmea"),
    ("Spreadsheets", "*.csv"),
    ("Photos", "*.jpg *.jpeg *.heic *.heif *.tif *.tiff *.dng"),
    ("All files", "*.*"),
]

HELP_TEXT = """HOW TO GET YOUR LOCATION DATA

Google moved Timeline onto your phone in 2024-2025, so grab both of these:

1) YOUR PHONE  (current Timeline: stops, trips, paths)
   Android: Settings > Location > Location services > Timeline >
            Export Timeline data  ->  Timeline.json
   iPhone:  Google Maps > your picture > Your Timeline > ... >
            Location & privacy settings > Export Timeline data
            ->  location-history.json
   Copy the file to this PC (USB, Drive or email).

2) GOOGLE TAKEOUT  (older history, saved and labeled places)
   takeout.google.com > Deselect all > check
     - Location History (Timeline)
     - Maps (your places)          (optional: saved / labeled places)
   Next step > .zip, largest size > Create export.
   Download the zip from the email. No need to unzip it.

ALSO WORKS:  GPX / TCX / KML / KMZ / GeoJSON / CSV / NMEA files from other
apps and GPS units, and folders of photos (uses their GPS tags).

Add everything on the Data tab and press Analyze. Overlaps are removed
automatically.
"""


def open_path(p):
    try:
        if os.name == "nt":
            os.startfile(p)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as ex:  # noqa: BLE001
        messagebox.showerror(APP, f"Couldn't open {p}\n\n{ex}")


class QueueWriter(io.TextIOBase):
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s)


def make_sortable(tree):
    """Click a Treeview column heading to sort by it; click again to reverse."""
    state = {"col": None, "rev": False}

    def key(v):
        t = str(v).replace(",", "").strip()
        try:
            return (0, float(t), "")
        except ValueError:
            return (1, 0.0, str(v).lower()) if t not in ("", "-", "–") else (2, 0.0, "")

    def sort(col):
        rev = not state["rev"] if state["col"] == col else False
        items = [(key(tree.set(i, col)), i) for i in tree.get_children("")]
        items.sort(key=lambda x: x[0], reverse=rev)
        for idx, (_, i) in enumerate(items):
            tree.move(i, "", idx)
        state.update(col=col, rev=rev)
        for c in tree["columns"]:
            text = tree.heading(c, "text").rstrip(" ▲▼")
            tree.heading(c, text=text + ((" ▼" if rev else " ▲") if c == col else ""))

    for c in tree["columns"]:
        tree.heading(c, command=lambda c=c: sort(c))


class CsvTab:
    """A Treeview editor for a small CSV file in DATA_DIR (fix-ups, family places)."""

    def __init__(self, app, title, intro, filename, cols, choices=None):
        self.app, self.file, self.cols, self.choices = app, os.path.join(DATA_DIR, filename), cols, choices or {}
        f = ttk.Frame(app.nb, padding=10)
        app.nb.add(f, text=f"  {title}  ")
        ttk.Label(f, text=intro, wraplength=920, justify="left").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(row, columns=[c for c, _, _ in cols], show="headings", selectmode="extended")
        for c, label, w in cols:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w)
        sy = ttk.Scrollbar(row, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.tree.identify_region(e.x, e.y) != "heading" and self.edit())
        make_sortable(self.tree)
        b = ttk.Frame(row, padding=(8, 0, 0, 0))
        b.pack(side="left", fill="y")
        for text, cmd in (("Add...", lambda: self.edit(True)), ("Edit...", self.edit), ("Delete", self.delete),
                          ("Import CSV...", self.import_csv), ("Export CSV...", self.export_csv)):
            ttk.Button(b, text=text, command=cmd, width=14).pack(pady=2)
        ttk.Label(f, text=f"Saved automatically to {self.file}", style="Hint.TLabel").pack(anchor="w")
        self.load(self.file)

    def load(self, path):
        if not os.path.exists(path):
            return 0
        n = 0
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                    if any(r.get(c) for c, _, _ in self.cols):
                        self.tree.insert("", "end", values=[r.get(c, "") for c, _, _ in self.cols])
                        n += 1
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't read {path}\n\n{ex}")
        return n

    def save(self, path=None):
        path = path or self.file
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([c for c, _, _ in self.cols])
                for iid in self.tree.get_children():
                    w.writerow(self.tree.item(iid, "values"))
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't save {path}\n\n{ex}")

    def has_rows(self):
        return bool(self.tree.get_children())

    def edit(self, new=False):
        sel = self.tree.selection()
        if not new and not sel:
            return
        vals = [""] * len(self.cols) if new else list(self.tree.item(sel[0], "values")) + [""] * len(self.cols)
        d = tk.Toplevel(self.app)
        d.title("Add" if new else "Edit")
        d.transient(self.app)
        d.grab_set()
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)
        vars_ = []
        for i, (c, label, _) in enumerate(self.cols):
            ttk.Label(fr, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            v = tk.StringVar(value=vals[i])
            if c in self.choices:
                ttk.Combobox(fr, textvariable=v, values=self.choices[c], state="readonly", width=30).grid(row=i, column=1, sticky="w")
            else:
                ttk.Entry(fr, textvariable=v, width=50).grid(row=i, column=1, sticky="w")
            vars_.append(v)

        def ok():
            row = [v.get().strip() for v in vars_]
            if new:
                self.tree.insert("", "end", values=row)
            else:
                self.tree.item(sel[0], values=row)
            self.save()
            d.destroy()
        b = ttk.Frame(fr)
        b.grid(row=len(self.cols), column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=ok).pack(side="right", padx=6)
        d.bind("<Escape>", lambda e: d.destroy())

    def delete(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
        self.save()

    def import_csv(self):
        p = filedialog.askopenfilename(title="Import CSV", filetypes=[("CSV", "*.csv")],
                                       initialdir=os.path.join(os.path.expanduser("~"), "Downloads"))
        if p:
            n = self.load(p)
            self.save()
            messagebox.showinfo(APP, f"Imported {n} rows. They'll be used in the next report.")

    def export_csv(self):
        p = filedialog.asksaveasfilename(title="Export", defaultextension=".csv", initialfile=os.path.basename(self.file),
                                         filetypes=[("CSV", "*.csv")])
        if p:
            self.save(p)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LocScope - location history analyzer")
        self.geometry("1060x760")
        self.minsize(760, 560)
        self.q = queue.Queue()
        self.running = False
        self.settings = self.load_settings()

        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 6))
        style.configure("Hint.TLabel", foreground="#6b6a66")

        self.build()
        self.restore()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll)
        self.after(1000, self.watch_changed)

    # ------------------------------------------------------------------ layout
    def build(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="LocScope", font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(top, text="  Analyze your Google location history and GPS data. Everything stays on this PC.",
                  style="Hint.TLabel").pack(side="left")
        ttk.Button(top, text="How do I get my data?", command=self.show_help).pack(side="right")

        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.pack(fill="both", expand=True, pady=(8, 0))

        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)
        self.build_data_tab()
        self.build_options_tab()
        self.build_places_tab()
        self.build_find_tab()
        self.build_notes_tab()
        self.fixes_tab = CsvTab(self, "Fix-ups",
            "Corrections applied to every report. In a report's day timeline, click ✎ next to a stop (Rename / Remove "
            "this stop / Ignore this whole day), then Download fix-ups in the report and Import here. Types: rename "
            "(give lat, lon and the new name), remove_stop (date + lat/lon, optional time HH:MM), ignore_area "
            "(lat/lon + radius, optional dates), ignore_dates (date range).",
            "my_fixes.csv", [("type", "Type", 110), ("date_from", "From", 95), ("date_to", "To", 95), ("lat", "Lat", 85),
                             ("lon", "Lon", 85), ("radius_m", "Radius m", 70), ("name", "New name", 170), ("note", "Note", 220),
                             ("time", "Time", 60)],
            choices={"type": ["rename", "remove_stop", "ignore_area", "ignore_dates"]})
        self.family_tab = CsvTab(self, "Family places",
            "Ancestral towns and family places. The report shows whether and when you've been to each. Give a place "
            "(e.g. \"Piacenza, Italy\") or lat/lon, and a radius in km (default 15).",
            "my_family_places.csv", [("name", "Family / name", 160), ("place", "Place", 220), ("lat", "Lat", 85),
                                      ("lon", "Lon", 85), ("radius_km", "Radius km", 75), ("note", "Note", 260)])
        self.build_together_tab()

        logf = ttk.Frame(paned)
        paned.add(logf, weight=1)
        bar = ttk.Frame(logf)
        bar.pack(fill="x", pady=(8, 4))
        self.run_btn = ttk.Button(bar, text="Analyze", style="Big.TButton", command=self.run)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=10)
        self.status = ttk.Label(bar, text="Add your files, then press Analyze.")
        self.status.pack(side="left")
        self.open_folder_btn = ttk.Button(bar, text="Open folder", command=self.open_folder, state="disabled")
        self.open_folder_btn.pack(side="right")
        self.share_btn = ttk.Button(bar, text="Open share copy", command=self.open_share)
        self.open_btn = ttk.Button(bar, text="Open report", command=self.open_report, state="disabled")
        self.open_btn.pack(side="right", padx=6)

        lf = ttk.Frame(logf)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=9, wrap="none", font=("Consolas", 9), state="disabled",
                           background="#fbfbfa", relief="solid", borderwidth=1)
        sy = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sy.set)
        self.log.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")

    def build_data_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  Data  ")
        ttk.Label(f, text="Files, folders and zips to analyze (Takeout zip, Timeline.json, GPX, KML, photos...)").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True, pady=6)
        self.inputs = tk.Listbox(row, selectmode="extended", activestyle="none", relief="solid", borderwidth=1)
        sy = ttk.Scrollbar(row, orient="vertical", command=self.inputs.yview)
        self.inputs.configure(yscrollcommand=sy.set)
        self.inputs.pack(side="left", fill="both", expand=True)
        sy.pack(side="left", fill="y")
        btns = ttk.Frame(row, padding=(8, 0, 0, 0))
        btns.pack(side="left", fill="y")
        for text, cmd in (("Add files...", self.add_files), ("Add folder...", self.add_folder),
                          ("Remove", self.remove_inputs), ("Clear", lambda: self.inputs.delete(0, "end"))):
            ttk.Button(btns, text=text, command=cmd, width=14).pack(pady=2)
        self.input_hint = ttk.Label(f, text="", style="Hint.TLabel")
        self.input_hint.pack(anchor="w")

        out = ttk.Frame(f)
        out.pack(fill="x", pady=(10, 0))
        ttk.Label(out, text="Save report to:").pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(out, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="Browse...", command=self.pick_out).pack(side="left")

    def build_options_tab(self):
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text="  Options  ")
        bg = ttk.Style(self).lookup("TFrame", "background") or None
        cv = tk.Canvas(outer, highlightthickness=0, borderwidth=0, background=bg)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        f = ttk.Frame(cv, padding=12)
        win = cv.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))

        def wheel(e):
            if str(self.nb.select()) == str(outer):
                cv.yview_scroll(int(-e.delta / 120) if e.delta else (1 if e.num == 5 else -1), "units")
        self.bind_all("<MouseWheel>", wheel, add="+")
        self.bind_all("<Button-4>", wheel, add="+")
        self.bind_all("<Button-5>", wheel, add="+")
        f.columnconfigure(1, minsize=250)
        f.columnconfigure(2, weight=1)
        r = 0

        def label(text, hint=""):
            nonlocal r
            ttk.Label(f, text=text).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
            if hint:
                ttk.Label(f, text=hint, style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)

        label("Time zone", "Used for days, hours and the travel-time chart")
        self.tz_var = tk.StringVar()
        ttk.Combobox(f, textvariable=self.tz_var, values=TIMEZONES, width=36).grid(row=r, column=1, sticky="w")
        r += 1
        label("Start date", "YYYY-MM-DD, blank = from the beginning")
        self.start_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.start_var, width=14).grid(row=r, column=1, sticky="w")
        r += 1
        label("End date", "YYYY-MM-DD, blank = to the end")
        self.end_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.end_var, width=14).grid(row=r, column=1, sticky="w")
        r += 1
        label("Trip distance (km)", "How far from home counts as a trip away")
        self.trip_var = tk.StringVar()
        ttk.Spinbox(f, textvariable=self.trip_var, from_=10, to=5000, increment=10, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        label("Ignore GPS worse than (m)", "Drops inaccurate fixes")
        self.acc_var = tk.StringVar()
        ttk.Spinbox(f, textvariable=self.acc_var, from_=20, to=5000, increment=50, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Separator(f).grid(row=r, column=0, columnspan=3, sticky="ew", pady=10)
        r += 1
        self.regions_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Cities, states and countries", variable=self.regions_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Downloads a free 10 MB city list once; offline after that", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.photos_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Read GPS tags from photos", variable=self.photos_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="JPEG / HEIC files and Google Photos Takeout", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.points_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Also export every GPS point (points.csv)", variable=self.points_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Can be a very large file", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.addr_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Show street addresses for my top", variable=self.addr_var).grid(row=r, column=0, sticky="w")
        af = ttk.Frame(f)
        af.grid(row=r, column=1, sticky="w")
        self.geo_var = tk.StringVar()
        ttk.Spinbox(af, textvariable=self.geo_var, from_=10, to=2000, increment=50, width=7).pack(side="left")
        ttk.Label(af, text=" places").pack(side="left")
        ttk.Label(f, text="Looks them up on OpenStreetMap (sends those coordinates), ~1 per second the first time, "
                          "then remembered", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        label("Mileage reimbursement ($/mile)", "For drives to/from work places; blank = don't show money")
        self.rate_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.rate_var, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        self.snow_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Snowy-week planner", variable=self.snow_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="~30 years of snowfall near home (Open-Meteo, sends a ~25 km grid point) -> which Monday-to-Monday "
                          "weeks usually get snow", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.cache_var = tk.BooleanVar()
        cf = ttk.Frame(f)
        cf.grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(cf, text="Remember files I've already read (much faster re-runs)", variable=self.cache_var).pack(side="left")
        ttk.Button(cf, text="Clear", command=self.clear_cache, width=6).pack(side="left", padx=6)
        ttk.Label(f, text="Unchanged files load instantly next time; a changed file is re-read automatically",
                  style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.weather_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Add the weather for every day", variable=self.weather_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="From Open-Meteo's free history archive (sends a rough location per day); the first run takes a "
                          "minute, then it's remembered", style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.export_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Export for Google Earth (KML / GPX)", variable=self.export_var).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Everything, each trip, and your places - in the report folder's 'export' subfolder",
                  style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.share_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Also make a shareable copy, hiding", variable=self.share_var).grid(row=r, column=0, sticky="w")
        sf = ttk.Frame(f)
        sf.grid(row=r, column=1, sticky="w")
        self.share_r_var = tk.StringVar()
        ttk.Spinbox(sf, textvariable=self.share_r_var, from_=200, to=10000, increment=100, width=7).pack(side="left")
        ttk.Label(sf, text=" m").pack(side="left")
        ttk.Label(f, text="around Home, Work and places marked Private (report_share.html; no work/commute/mileage)",
                  style="Hint.TLabel", wraplength=330).grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        self.watch_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Watch my files and folders - re-analyze automatically when something changes",
                        variable=self.watch_var, command=self.watch_changed).grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        r += 1
        ttk.Label(f, text="Tip: add a folder on the Data tab, then just drop new Timeline exports or GPX files into it. "
                          "An open report refreshes itself.", style="Hint.TLabel", wraplength=330).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.auto_open_var = tk.BooleanVar()
        ttk.Checkbutton(f, text="Open the report when finished", variable=self.auto_open_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def build_places_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  My places  ")
        ttk.Label(f, text="Name your own places. Stops within the radius are counted as that place. "
                          "Name one \"Home\" or \"Work\" to override the guess. Tick Work for every place you work "
                          "(hours, commute, mileage); tick Private to hide it in shareable copies.", wraplength=900).pack(anchor="w")
        ttk.Label(f, text="Tip: in Google Maps, right-click a spot and click the coordinates to copy them, "
                          "then paste into Latitude.", style="Hint.TLabel").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True, pady=6)
        cols = ("name", "lat", "lon", "radius", "address", "work", "private")
        self.places = ttk.Treeview(row, columns=cols, show="headings", selectmode="extended")
        for c, w, t in zip(cols, (170, 95, 95, 85, 260, 60, 65),
                           ("Name", "Latitude", "Longitude", "Radius (m)", "Address", "Work", "Private")):
            self.places.heading(c, text=t)
            self.places.column(c, width=w, anchor="e" if c in ("lat", "lon", "radius") else "center" if c in ("work", "private") else "w")
        sy = ttk.Scrollbar(row, orient="vertical", command=self.places.yview)
        self.places.configure(yscrollcommand=sy.set)
        self.places.pack(side="left", fill="both", expand=True)
        sy.pack(side="left", fill="y")
        self.places.bind("<Double-1>", lambda e: self.places.identify_region(e.x, e.y) != "heading" and self.edit_place())
        make_sortable(self.places)
        btns = ttk.Frame(row, padding=(8, 0, 0, 0))
        btns.pack(side="left", fill="y")
        for text, cmd in (("Add...", lambda: self.edit_place(new=True)), ("Edit...", self.edit_place),
                          ("Delete", self.delete_places), ("Import CSV...", self.import_places),
                          ("Export CSV...", self.export_places)):
            ttk.Button(btns, text=text, command=cmd, width=14).pack(pady=2)
        ttk.Label(f, text=f"Saved automatically to {os.path.join(DATA_DIR, 'my_places.csv')}",
                  style="Hint.TLabel").pack(anchor="w")

    def build_together_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  Together  ")
        ttk.Label(f, text="Add another person's location export (their Timeline.json, location-history.json or Takeout zip) "
                          "to see when and where you were together. Only the times together go into your report, "
                          "not their own movements. Ask them first.", wraplength=920).pack(anchor="w")
        r = ttk.Frame(f)
        r.pack(fill="x", pady=8)
        ttk.Label(r, text="Their name:").pack(side="left")
        self.tog_name = tk.StringVar()
        ttk.Entry(r, textvariable=self.tog_name, width=24).pack(side="left", padx=6)
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True)
        self.tog_files = tk.Listbox(row, selectmode="extended", relief="solid", borderwidth=1, activestyle="none")
        self.tog_files.pack(side="left", fill="both", expand=True)
        b = ttk.Frame(row, padding=(8, 0, 0, 0))
        b.pack(side="left", fill="y")

        def add():
            for p in filedialog.askopenfilenames(title="Their location files", filetypes=FILE_TYPES):
                if p not in self.tog_files.get(0, "end"):
                    self.tog_files.insert("end", os.path.normpath(p))
        ttk.Button(b, text="Add files...", command=add, width=14).pack(pady=2)
        ttk.Button(b, text="Remove", width=14, command=lambda: [self.tog_files.delete(i) for i in reversed(self.tog_files.curselection())]).pack(pady=2)
        ttk.Button(b, text="Clear", width=14, command=lambda: self.tog_files.delete(0, "end")).pack(pady=2)

    def build_notes_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  Notes  ")
        ttk.Label(f, text="Notes for particular days (\"Mom's birthday\", \"conference\"). They show in the report's calendar, "
                          "Browse, On this day and Ask. Notes you type in the report: click Download notes there, then Import here.",
                  wraplength=900).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="both", expand=True, pady=6)
        self.notes = ttk.Treeview(row, columns=("date", "note"), show="headings", selectmode="extended")
        self.notes.heading("date", text="Date")
        self.notes.heading("note", text="Note")
        self.notes.column("date", width=110)
        self.notes.column("note", width=700)
        sy = ttk.Scrollbar(row, orient="vertical", command=self.notes.yview)
        self.notes.configure(yscrollcommand=sy.set)
        self.notes.pack(side="left", fill="both", expand=True)
        sy.pack(side="left", fill="y")
        self.notes.bind("<Double-1>", lambda e: self.notes.identify_region(e.x, e.y) != "heading" and self.edit_note())
        make_sortable(self.notes)
        btns = ttk.Frame(row, padding=(8, 0, 0, 0))
        btns.pack(side="left", fill="y")
        for text, cmd in (("Add...", lambda: self.edit_note(new=True)), ("Edit...", self.edit_note), ("Delete", self.delete_notes),
                          ("Import CSV...", self.import_notes), ("Export CSV...", self.export_notes)):
            ttk.Button(btns, text=text, command=cmd, width=14).pack(pady=2)
        ttk.Label(f, text=f"Saved automatically to {os.path.join(DATA_DIR, 'my_notes.csv')}", style="Hint.TLabel").pack(anchor="w")

    def load_notes_file(self, path, merge=True):
        if not os.path.exists(path):
            return 0
        have = {self.notes.set(i, "date"): i for i in self.notes.get_children()}
        n = 0
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                    d, t = row.get("date", "")[:10], row.get("note", "")
                    try:
                        date.fromisoformat(d)
                    except ValueError:
                        continue
                    if d in have:
                        if t:
                            self.notes.set(have[d], "note", t)
                        else:
                            self.notes.delete(have[d])
                            del have[d]
                    elif t:
                        have[d] = self.notes.insert("", "end", values=(d, t))
                    n += 1
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't read {path}\n\n{ex}")
        return n

    def save_notes_file(self, path=None):
        path = path or os.path.join(DATA_DIR, "my_notes.csv")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["date", "note"])
                for iid in sorted(self.notes.get_children(), key=lambda i: self.notes.set(i, "date")):
                    w.writerow([self.notes.set(iid, "date"), self.notes.set(iid, "note")])
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't save {path}\n\n{ex}")

    def edit_note(self, new=False):
        sel = self.notes.selection()
        if not new and not sel:
            return
        d0, t0 = ("", "") if new else (self.notes.set(sel[0], "date"), self.notes.set(sel[0], "note"))
        dlg = tk.Toplevel(self)
        dlg.title("Day note")
        dlg.transient(self)
        dlg.grab_set()
        fr = ttk.Frame(dlg, padding=12)
        fr.pack(fill="both", expand=True)
        dv = tk.StringVar(value=d0 or date.today().isoformat())
        ttk.Label(fr, text="Date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        ttk.Entry(fr, textvariable=dv, width=14).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(fr, text="Note").grid(row=1, column=0, sticky="nw")
        txt = tk.Text(fr, width=60, height=4, wrap="word")
        txt.grid(row=1, column=1, pady=3)
        txt.insert("1.0", t0)
        txt.focus_set()

        def ok():
            try:
                date.fromisoformat(dv.get().strip())
            except ValueError:
                messagebox.showwarning(APP, "Date should look like 2024-07-12.", parent=dlg)
                return
            note = txt.get("1.0", "end").strip()
            if new:
                if note:
                    self.notes.insert("", "end", values=(dv.get().strip(), note))
            else:
                self.notes.item(sel[0], values=(dv.get().strip(), note))
            self.save_notes_file()
            dlg.destroy()
        b = ttk.Frame(fr)
        b.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=ok).pack(side="right", padx=6)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def delete_notes(self):
        for iid in self.notes.selection():
            self.notes.delete(iid)
        self.save_notes_file()

    def import_notes(self):
        p = filedialog.askopenfilename(title="Import notes (e.g. my_notes.csv downloaded from the report)",
                                       filetypes=[("CSV", "*.csv")], initialdir=os.path.join(os.path.expanduser("~"), "Downloads"))
        if p:
            n = self.load_notes_file(p)
            self.save_notes_file()
            messagebox.showinfo(APP, f"Imported {n} notes. They'll be in the next report.")

    def export_notes(self):
        p = filedialog.asksaveasfilename(title="Export notes", defaultextension=".csv", initialfile="my_notes.csv",
                                         filetypes=[("CSV", "*.csv")])
        if p:
            self.save_notes_file(p)

    def build_find_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  Find a location  ")
        ttk.Label(f, text="List every time you were somewhere. Enter coordinates (38.8977, -77.0365), "
                          "one of your named places, or an address.").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Combobox(row, textvariable=self.find_var)
        self.find_entry.pack(side="left", fill="x", expand=True)
        self.find_entry.bind("<Return>", lambda e: self.add_find())
        ttk.Label(row, text="  within").pack(side="left")
        self.radius_var = tk.StringVar()
        ttk.Spinbox(row, textvariable=self.radius_var, from_=25, to=10000, increment=25, width=7).pack(side="left", padx=4)
        ttk.Label(row, text="m").pack(side="left")
        ttk.Button(row, text="Add", command=self.add_find).pack(side="left", padx=(8, 0))

        mid = ttk.Frame(f)
        mid.pack(fill="x")
        self.finds = tk.Listbox(mid, height=4, relief="solid", borderwidth=1, activestyle="none")
        self.finds.pack(side="left", fill="x", expand=True)
        b = ttk.Frame(mid, padding=(8, 0, 0, 0))
        b.pack(side="left", fill="y")
        ttk.Button(b, text="Remove", command=lambda: [self.finds.delete(i) for i in reversed(self.finds.curselection())], width=12).pack(pady=2)
        ttk.Button(b, text="Clear", command=lambda: self.finds.delete(0, "end"), width=12).pack(pady=2)
        ttk.Label(f, text="Searches run with each Analyze. The report's Find box also works any time, and lets you click the map.",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 6))

        res = ttk.Frame(f)
        res.pack(fill="x")
        ttk.Label(res, text="Results for:").pack(side="left")
        self.result_pick = ttk.Combobox(res, state="readonly", width=50)
        self.result_pick.pack(side="left", padx=6)
        self.result_pick.bind("<<ComboboxSelected>>", lambda e: self.show_find_result())
        self.result_note = ttk.Label(f, text="", style="Hint.TLabel")
        self.result_note.pack(anchor="w", pady=(4, 0))
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True, pady=(6, 0))
        cols = ("kind", "date", "from", "until", "hours", "place")
        self.results = ttk.Treeview(tf, columns=cols, show="headings")
        for c, w, t in zip(cols, (90, 100, 70, 150, 70, 260), ("What", "Date", "From", "Until", "Hours", "Place")):
            self.results.heading(c, text=t)
            self.results.column(c, width=w, anchor="e" if c == "hours" else "w")
        sy = ttk.Scrollbar(tf, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=sy.set)
        self.results.pack(side="left", fill="both", expand=True)
        make_sortable(self.results)
        sy.pack(side="left", fill="y")
        self.find_results = []

    # ------------------------------------------------------------- settings
    def load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def save_settings(self):
        s = dict(inputs=list(self.inputs.get(0, "end")), out=self.out_var.get(), tz=self.tz_var.get(),
                 start=self.start_var.get(), end=self.end_var.get(), trip=self.trip_var.get(), acc=self.acc_var.get(),
                 regions=self.regions_var.get(), photos=self.photos_var.get(), points=self.points_var.get(),
                 geocode=self.geo_var.get(), addresses=self.addr_var.get(), auto_open=self.auto_open_var.get(), rate=self.rate_var.get(), export=self.export_var.get(), weather=self.weather_var.get(), snow=self.snow_var.get(), cache=self.cache_var.get(),
                 tog_name=self.tog_name.get(), tog_files=list(self.tog_files.get(0, "end")),
                 share=self.share_var.get(), share_r=self.share_r_var.get(), watch=self.watch_var.get(), finds=list(self.finds.get(0, "end")),
                 radius=self.radius_var.get(), geometry=self.geometry())
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=1)
        except OSError:
            pass

    def restore(self):
        s = self.settings
        for p in s.get("inputs", []):
            self.inputs.insert("end", p)
        self.out_var.set(s.get("out") or os.path.join(DOCS, "LocScope Report"))
        self.tz_var.set(s.get("tz") or TIMEZONES[0])
        self.start_var.set(s.get("start", ""))
        self.end_var.set(s.get("end", ""))
        self.trip_var.set(s.get("trip", "100"))
        self.acc_var.set(s.get("acc", "500"))
        self.regions_var.set(s.get("regions", True))
        self.photos_var.set(s.get("photos", True))
        self.points_var.set(s.get("points", False))
        g = str(s.get("geocode", "150"))
        self.geo_var.set("150" if g in ("", "0") else g)
        self.addr_var.set(s.get("addresses", True))
        self.auto_open_var.set(s.get("auto_open", True))
        self.rate_var.set(s.get("rate", ""))
        self.export_var.set(s.get("export", True))
        self.weather_var.set(s.get("weather", False))
        self.snow_var.set(s.get("snow", False))
        self.cache_var.set(s.get("cache", True))
        self.tog_name.set(s.get("tog_name", ""))
        for p in s.get("tog_files", []):
            self.tog_files.insert("end", p)
        self.share_var.set(s.get("share", False))
        self.share_r_var.set(s.get("share_r", "1000"))
        self.watch_var.set(s.get("watch", False))
        self.radius_var.set(s.get("radius", "150"))
        for q in s.get("finds", []):
            self.finds.insert("end", q)
        if s.get("geometry"):
            try:
                self.geometry(s["geometry"])
            except tk.TclError:
                pass
        self.load_places_file(os.path.join(DATA_DIR, "my_places.csv"))
        self.load_notes_file(os.path.join(DATA_DIR, "my_notes.csv"))
        self.update_hint()

    def on_close(self):
        if self.running and not messagebox.askyesno(APP, "Analysis is still running. Quit anyway?"):
            return
        self.save_places_file()
        self.save_notes_file()
        self.save_settings()
        if getattr(self, "_srv", None):
            self._srv.shutdown()
        self.destroy()

    # ---------------------------------------------------------------- data tab
    def add_paths(self, paths):
        have = set(self.inputs.get(0, "end"))
        for p in paths:
            p = os.path.normpath(p)
            if p and p not in have:
                self.inputs.insert("end", p)
                have.add(p)
        self.update_hint()

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(title="Add location files", filetypes=FILE_TYPES,
                                                   initialdir=os.path.join(os.path.expanduser("~"), "Downloads")))

    def add_folder(self):
        d = filedialog.askdirectory(title="Add a folder (all supported files inside are read)")
        if d:
            self.add_paths([d])

    def remove_inputs(self):
        for i in reversed(self.inputs.curselection()):
            self.inputs.delete(i)
        self.update_hint()

    def update_hint(self):
        n = self.inputs.size()
        self.input_hint.configure(text="Nothing added yet - click Add files... (see 'How do I get my data?')" if n == 0
                                  else f"{n} item{'s' if n != 1 else ''}. Folders are searched, including subfolders.")

    def pick_out(self):
        d = filedialog.askdirectory(title="Where should the report go?", initialdir=self.out_var.get() or DOCS)
        if d:
            self.out_var.set(os.path.normpath(d))

    def show_help(self):
        w = tk.Toplevel(self)
        w.title("How to get your location data")
        w.transient(self)
        t = tk.Text(w, width=78, height=30, wrap="word", font=("Consolas", 10), padx=12, pady=10, relief="flat")
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)
        b = ttk.Frame(w, padding=8)
        b.pack(fill="x")
        ttk.Button(b, text="Open Google Takeout", command=lambda: webbrowser.open("https://takeout.google.com")).pack(side="left")
        ttk.Button(b, text="Close", command=w.destroy).pack(side="right")

    # -------------------------------------------------------------- places tab
    def load_places_file(self, path):
        if not os.path.exists(path):
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                    if row.get("name"):
                        yes = lambda k: "yes" if (row.get(k) or "").lower() in ("y", "yes", "true", "1", "x") else ""
                        self.places.insert("", "end", values=(row.get("name"), row.get("lat", ""), row.get("lon", ""),
                                                               row.get("radius_m", "") or "150", row.get("address", ""),
                                                               yes("work"), yes("private")))
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror(APP, f"Couldn't read {path}\n\n{ex}")

    def save_places_file(self, path=None):
        path = path or os.path.join(DATA_DIR, "my_places.csv")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["name", "lat", "lon", "radius_m", "address", "work", "private"])
                for iid in self.places.get_children():
                    v = list(self.places.item(iid, "values")) + ["", ""]
                    w.writerow(v[:7])
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't save {path}\n\n{ex}")
        return path

    def edit_place(self, new=False):
        sel = self.places.selection()
        if not new and not sel:
            return
        vals = ("", "", "", "150", "", "", "") if new else tuple(self.places.item(sel[0], "values")) + ("", "")
        d = tk.Toplevel(self)
        d.title("Add place" if new else "Edit place")
        d.transient(self)
        d.grab_set()
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)
        vars_ = [tk.StringVar(value=v) for v in vals[:5]]
        work_v = tk.BooleanVar(value=str(vals[5]) == "yes")
        priv_v = tk.BooleanVar(value=str(vals[6]) == "yes")
        labels = ("Name", "Latitude", "Longitude", "Radius (m)", "Address (optional if you give coordinates)")
        for i, (lab, v) in enumerate(zip(labels, vars_)):
            ttk.Label(fr, text=lab).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            e = ttk.Entry(fr, textvariable=v, width=48 if i in (0, 4) else 20)
            e.grid(row=i, column=1, sticky="w")
            if i == 0:
                e.focus_set()

        def split_paste(*_):  # pasting "38.8977, -77.0365" into Latitude fills both boxes
            ll = locscope.parse_coords(vars_[1].get())
            if ll:
                vars_[1].set(f"{ll[0]:.6f}")
                vars_[2].set(f"{ll[1]:.6f}")
        vars_[1].trace_add("write", split_paste)

        def ok():
            name = vars_[0].get().strip()
            la, lo = vars_[1].get().strip(), vars_[2].get().strip()
            if not name:
                messagebox.showwarning(APP, "Give the place a name.", parent=d)
                return
            if (not la or not lo) and not vars_[4].get().strip():
                messagebox.showwarning(APP, "Enter latitude and longitude, or an address.", parent=d)
                return
            if la and lo and locscope.parse_coords(f"{la},{lo}") is None:
                messagebox.showwarning(APP, "Latitude/longitude don't look right (e.g. 38.8977 and -77.0365).", parent=d)
                return
            row = (name, la, lo, vars_[3].get().strip() or "150", vars_[4].get().strip(),
                   "yes" if work_v.get() else "", "yes" if priv_v.get() else "")
            if new:
                self.places.insert("", "end", values=row)
            else:
                self.places.item(sel[0], values=row)
            self.save_places_file()
            d.destroy()
        ttk.Checkbutton(fr, text="Work place (counts toward work hours, commute and work mileage)",
                        variable=work_v).grid(row=len(labels), column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(fr, text="Private (hidden in shareable copies)", variable=priv_v).grid(
            row=len(labels) + 1, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(fr)
        b.grid(row=len(labels) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=d.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=ok).pack(side="right", padx=6)
        d.bind("<Return>", lambda e: ok())
        d.bind("<Escape>", lambda e: d.destroy())

    def delete_places(self):
        for iid in self.places.selection():
            self.places.delete(iid)
        self.save_places_file()

    def import_places(self):
        p = filedialog.askopenfilename(title="Import places CSV", filetypes=[("CSV", "*.csv")])
        if p:
            self.load_places_file(p)
            self.save_places_file()

    def export_places(self):
        p = filedialog.asksaveasfilename(title="Export places", defaultextension=".csv", initialfile="my_places.csv",
                                         filetypes=[("CSV", "*.csv")])
        if p:
            self.save_places_file(p)

    # ---------------------------------------------------------------- find tab
    def add_find(self):
        q = self.find_var.get().strip()
        if q and q not in self.finds.get(0, "end"):
            self.finds.insert("end", q)
        self.find_var.set("")

    def show_find_result(self):
        self.results.delete(*self.results.get_children())
        i = self.result_pick.current()
        if i < 0 or i >= len(self.find_results):
            return
        r = self.find_results[i]
        for row in r["rows"]:
            self.results.insert("", "end", values=(row[0], row[1], row[2], row[3], row[4], row[5]))
        days = sorted({row[1] for row in r["rows"]})
        self.result_note.configure(text=(f"{r['visits']} visits ({r['hours']} h), {r['passes']} pass-by days"
                                         + (f"; first {days[0]}, last {days[-1]}" if days else "; never there")))

    # --------------------------------------------------------------------- run
    def build_args(self):
        inputs = list(self.inputs.get(0, "end"))
        if not inputs:
            messagebox.showinfo(APP, "Add at least one file or folder on the Data tab first.\n\n"
                                     "Click 'How do I get my data?' for where to find it.")
            self.nb.select(0)
            return None
        out = self.out_var.get().strip() or os.path.join(DOCS, "LocScope Report")
        args = list(inputs) + ["--out", out]
        for var, flag in ((self.start_var, "--start"), (self.end_var, "--end")):
            v = var.get().strip()
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    messagebox.showwarning(APP, f"{flag[2:].title()} date should look like 2023-06-01.")
                    self.nb.select(1)
                    return None
                args += [flag, v]
        tz = self.tz_var.get().strip()
        if tz and not tz.startswith("Auto"):
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:  # noqa: BLE001
                if os.name == "nt" and not getattr(sys, "frozen", False) and messagebox.askyesno(
                        APP, f"Python on this PC doesn't have time-zone data for '{tz}'.\n\n"
                             "Install it now? (runs: pip install tzdata - needs internet, takes a few seconds)"):
                    r = subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "tzdata"],
                                       capture_output=True, text=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if r.returncode != 0:
                        messagebox.showerror(APP, "pip install tzdata failed:\n\n" + (r.stderr or r.stdout)[-800:])
                        return None
                    messagebox.showinfo(APP, "Installed. Please close and reopen LocScope so Python picks it up.")
                    return None
                if not messagebox.askyesno(APP, f"Unknown time zone '{tz}'. Continue with automatic time zones?"):
                    return None
                tz = ""
            if tz:
                args += ["--tz", tz]
        geo = [(self.geo_var, "--geocode", "150")] if self.addr_var.get() else []
        for var, flag, default in [(self.trip_var, "--trip-km", "100"), (self.acc_var, "--max-accuracy", "500")] + geo:
            v = var.get().strip() or default
            try:
                float(v)
            except ValueError:
                v = default
            if flag == "--geocode":
                v = str(int(float(v)))
            args += [flag, v]
        if not self.regions_var.get():
            args.append("--no-regions")
        if not self.photos_var.get():
            args.append("--no-photos")
        if self.points_var.get():
            args.append("--export-points")
        rate = self.rate_var.get().strip().lstrip("$")
        if rate:
            try:
                args += ["--mileage-rate", str(float(rate))]
            except ValueError:
                messagebox.showwarning(APP, "Mileage rate should be a number like 0.70")
                return None
        if not self.export_var.get():
            args.append("--no-export")
        if self.share_var.get():
            args += ["--share", "--share-radius", self.share_r_var.get().strip() or "1000"]
        self.save_places_file()
        args += ["--places", os.path.join(DATA_DIR, "my_places.csv")]
        self.save_notes_file()
        args += ["--notes", os.path.join(DATA_DIR, "my_notes.csv")]
        self.fixes_tab.save()
        self.family_tab.save()
        args += ["--fixes", self.fixes_tab.file]
        if self.family_tab.has_rows():
            args += ["--family", self.family_tab.file]
        if self.snow_var.get():
            args.append("--snow")
        if not self.cache_var.get():
            args.append("--no-cache")
        tog = list(self.tog_files.get(0, "end"))
        if tog:
            args += ["--together-with"] + tog + ["--together-name", self.tog_name.get().strip() or "them"]
        if self.weather_var.get():
            args.append("--weather")
        args += ["--geodata", os.path.join(DATA_DIR, "geodata")]
        finds = list(self.finds.get(0, "end"))
        if self.find_var.get().strip():
            finds.append(self.find_var.get().strip())
        for q in finds:
            args += ["--find", q]
        if finds:
            args += ["--find-radius", self.radius_var.get().strip() or "150"]
        return args

    def run(self):
        if self.running:
            return
        args = self.build_args()
        if args is None:
            return
        self.save_settings()
        self.running = True
        self.run_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")
        self.progress.start(12)
        self.status.configure(text="Working... (big Takeout files can take a few minutes)")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self.worker, args=(args,), daemon=True).start()

    def worker(self, args):
        w = QueueWriter(self.q)
        code = 1
        try:
            with redirect_stdout(w), redirect_stderr(w):
                code = locscope.main(args)
        except SystemExit as ex:
            code = ex.code if isinstance(ex.code, int) else 1
        except MemoryError:
            self.q.put(("log", "\nOut of memory. For a huge Records.json, install ijson (pip install ijson) "
                               "or use a narrower date range.\n"))
        except Exception:  # noqa: BLE001
            self.q.put(("log", "\n" + traceback.format_exc()))
        self.q.put(("done", code))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", val)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "done":
                    self.finished(val)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def finished(self, code):
        self.running = False
        self.progress.stop()
        self.run_btn.configure(state="normal")
        lr = locscope.LAST_RUN
        if code == 0 and lr.get("report"):
            self.status.configure(text="Done.")
            self.open_btn.configure(state="normal")
            self.open_folder_btn.configure(state="normal")
            self.find_results = lr.get("finds") or []
            self.result_pick.configure(values=[f"{r['label']}  (within {r['radius']:g} m)" for r in self.find_results])
            self.find_entry.configure(values=lr.get("named", []))
            if self.find_results:
                self.result_pick.current(0)
                self.show_find_result()
            else:
                self.result_pick.set("")
                self.results.delete(*self.results.get_children())
                self.result_note.configure(text="")
            if lr.get("share"):
                self.share_btn.pack(side="right", padx=(0, 6))
            else:
                self.share_btn.pack_forget()
            self._last_sig = locscope.inputs_signature(list(self.inputs.get(0, "end")))
            if self.auto_open_var.get() and not getattr(self, "_auto_run", False):
                self.open_report()
            if lr.get("weak") and not getattr(self, "_auto_run", False):
                messagebox.showwarning(APP, "Very little location history was found - the report is mostly saved "
                                            "places, pins and photos.\n\nGoogle now keeps your Timeline on your phone, so "
                                            "Takeout often doesn't include it. Export it from the phone and add that file:\n\n"
                                            "Android: Settings > Location > Location services > Timeline > "
                                            "Export Timeline data\n\niPhone: Google Maps > your picture > Your Timeline > ... > "
                                            "Location & privacy settings > Export Timeline data")
        else:
            self.status.configure(text="Didn't finish - see the messages below.")
        self._auto_run = False
        if self.watch_var.get():
            self.status.configure(text=self.status.cget("text") + "  Watching for changes...")

    def clear_cache(self):
        p = os.path.join(DATA_DIR, "geodata", "cache.sqlite")
        try:
            if os.path.exists(p):
                os.remove(p)
            messagebox.showinfo(APP, "Cache cleared. The next run reads every file fresh.")
        except OSError as ex:
            messagebox.showerror(APP, f"Couldn't clear the cache:\n{ex}")

    def watch_changed(self):
        if self.watch_var.get() and not getattr(self, "_watch_job", None):
            self._watch_job = self.after(30000, self.watch_tick)

    def watch_tick(self):
        self._watch_job = None
        if not self.watch_var.get():
            return
        if not self.running and self.inputs.size():
            sig = locscope.inputs_signature(list(self.inputs.get(0, "end")))
            if getattr(self, "_last_sig", None) is not None and sig != self._last_sig:
                self._auto_run = True
                self.log_line(f"\nFiles changed - re-analyzing ({len(sig)} files)...\n")
                self.run()
            elif getattr(self, "_last_sig", None) is None:
                self._last_sig = sig
        self._watch_job = self.after(30000, self.watch_tick)

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_share(self):
        p = locscope.LAST_RUN.get("share")
        if p and os.path.exists(p):
            url = self.serve(os.path.dirname(p))
            webbrowser.open(url + "/" + os.path.basename(p) if url else "file:///" + p.replace("\\", "/").lstrip("/"))

    def open_report(self):
        p = locscope.LAST_RUN.get("report")
        if not (p and os.path.exists(p)):
            return
        url = self.serve(os.path.dirname(p))
        webbrowser.open(url + "/" + os.path.basename(p) if url else "file:///" + p.replace("\\", "/").lstrip("/"))

    def serve(self, folder):
        """Serve the report folder on a private local web address (127.0.0.1 only), so every map style works."""
        import functools
        import http.server
        if getattr(self, "_srv", None) and self._srv_dir == folder:
            return self._srv_url
        if getattr(self, "_srv", None):
            self._srv.shutdown()
            self._srv = None

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        try:
            srv = None
            for port in (47615, 47616, 47617, 0):  # a fixed port keeps notes you add in the report between runs
                try:
                    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=folder))
                    break
                except OSError:
                    continue
            if srv is None:
                return None
        except OSError:
            return None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._srv, self._srv_dir = srv, folder
        self._srv_url = f"http://127.0.0.1:{srv.server_address[1]}"
        return self._srv_url

    def open_folder(self):
        p = locscope.LAST_RUN.get("out")
        if p and os.path.isdir(p):
            open_path(p)


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    app = App()
    if len(sys.argv) > 1:  # files dropped onto the .pyw icon
        app.add_paths(sys.argv[1:])
    app.mainloop()


if __name__ == "__main__":
    main()
