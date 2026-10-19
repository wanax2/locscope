# LocScope: Google location history analyzer

LocScope reads your Google location exports and writes an interactive `report.html` along with some CSV files. The report has a heat map, a day-by-day path replay, your top places, distance per month, travel modes, a chart of when you travel, trips away from home, and a year-by-year table. It uses only the Python standard library, and all processing happens on your own PC.

## 1. Get your data

Google moved Timeline off its servers in 2024–2025, so where the data lives depends on how old it is. Collect everything you can find. LocScope merges all the files and removes duplicates.

| What | How to get it | File(s) |
|---|---|---|
| **Current Timeline (Android)** | Phone **Settings → Location → Location services → Timeline → Export Timeline data**. Save it, then copy it to your PC. | `Timeline.json` |
| **Current Timeline (iPhone)** | Google Maps app → profile picture → **Your Timeline** → **⋯** → **Location & privacy settings** → **Export Timeline data** | `location-history.json` |
| **Older history (pre-migration)** | https://takeout.google.com → *Deselect all* → check **Location History (Timeline)** → export. If Google has already deleted the server copy, this item will be empty or missing. | `takeout-*.zip` (contains `Records.json` and `Semantic Location History/`) |
| **Encrypted cloud backup** | If you turned on Timeline backup, Google Maps can restore it to a phone. After that, export it from the phone as shown above. | — |

You don't need to unzip the Takeout archive, because LocScope reads the zip directly.

## 2. Run it: the easy way (the LocScope app)

1. Install Python 3.9 or newer from https://www.python.org/downloads/. On the first installer screen, check **"Add python.exe to PATH"**. (Or build `LocScope.exe` once with `build_exe.bat` and skip Python on other PCs.)
2. Extract the whole zip (right-click → **Extract All…**), then double-click **`LocScope.pyw`** or **`LocScope.bat`**.
3. **Build report** page:
   - **Your location data:** drag files or folders onto the box, or click **Add files…** / **Add a folder…**. Each item shows what LocScope thinks it is (phone Timeline export, Takeout download, GPS track, photo folder…). You can also paste a path and press Enter.
   - **What to include:** switch features on or off. Anything that contacts the internet says so on its card. **More settings** has the rarely changed options.
   - Click **Build report** (or press Ctrl+Enter). A progress bar shows each step; **Details** shows the full log.
4. When it's done, the report opens inside the app on the **Report** page. **Open in browser**, **Folder** and **Shareable copy** are along the top.
5. The pages on the left under **Your additions** hold your own information. Each one is saved as it changes and used in every build:
   - **My places:** name your places, using a small map to pick each spot. Mark them Work or Private.
   - **Day notes**, **Find a place**, **Fix-ups**, **Family places** and **Together** (someone else's export).
   - Each page can import and export CSV files, so files downloaded from the report (notes, fix-ups) go in with **Import CSV…**.

**The window.** If the optional `pywebview` package is installed (`pip install pywebview`), LocScope opens in its own native window and you can drop files on it straight from Explorer. Otherwise it opens as an app window in Microsoft Edge or Chrome (no address bar). That works the same, except that dropped files are copied into LocScope's folder and whole folders must be added with **Add a folder…**. pywebview needs `pythonnet`, which can lag behind the newest Python. If `pip install pywebview` fails on your Python version, just use the Edge window.

Your file list, settings, places and notes are kept in `%APPDATA%\LocScope`. The report goes to `Documents\LocScope Report` unless you choose another folder. Starting LocScope again while it's open just brings up the existing window. The older Tk window, `locscope_gui.pyw`, still works and uses the same settings.

## 2b. Run it from the command line

Install Python 3.8 or newer from python.org or the Microsoft Store. Then run:

```powershell
cd C:\path\to\locscope
python locscope.py "$HOME\Downloads\takeout-20260929T000000Z-001.zip" "$HOME\Downloads\Timeline.json" --tz America/New_York
start .\locscope_report\report.html
```

You can pass any mix of files, folders and zips.

## What's in the report

The report opens on an **Overview**: your headline numbers, the Ask box, and a tile for every section with a key number on it. Tap a tile to open that section.

- **Tabs** along the top group the sections: Days & map, Years, Places, Travel, Work, Data & tools, and **All** (everything on one long page, like older versions). The small number on each tab is how many sections it holds. A row of chips under the tabs jumps within the tab.
- **Find a section** (top right, or press `/`): type a word such as *flights*, *snow*, *irs* or *hotel* and press Enter.
- **⚙ Customize:** untick sections you don't use and they disappear from the tabs and the Overview. This is saved in your browser only. Sections with nothing to show for your data are hidden automatically and listed at the bottom of the Overview.
- Links such as *show on map* or *open trip journal* switch to the right tab for you. You can bookmark a tab or section, e.g. `report.html#travel` or `report.html#flightCard`.

- **Map:** a heat map of where you spend time, your top places, your named places, saved pins and photos (with thumbnails). Pick a day to see its path, then use **▶ Replay** to animate it. Each day can also be downloaded as KML or GPX.
- **Calendar:** one square per day, colored by distance, by how far you got from home, or by hours at work. Click a square to replay that day.
- **Browse by date:** pick a year, month or day from the lists, or click down from all years to a year, a month and then a single day. Each level shows its stats: days with data, distance in km and miles, stops and trips, farthest from home, hours at work, photos and towns. Each table lists the months or days inside it.
  - Sort by newest, oldest, most distance, most stops, farthest from home or most days. Clicking a column heading also sorts.
  - Use **◀ Previous / Next ▶** to step through days, months or years.
  - At the day level you also get that day's timeline and photos, plus buttons to show it on the map, download it as KML/GPX, or open its trip journal.
- **Year in review:** one page per year. It shows days, distance, new places, trips, towns, states and countries visited, photos, work hours, typical commute, miles driven, your most-visited places and the year's highlights.
- **Trips and trip journal:** click any trip away from home to open its journal. It has a map of the route with stops and photos, a day-by-day log, an animated replay of the whole trip, and KML/GPX downloads.
- **Work hours:** hours per week or per month at your work places, with a 40-hour reference line, plus overnight shifts, your longest shift and a full timesheet. Saved as `work_timesheet.csv` and `work_weekly.csv`.
  - Your work places are the inferred Work place, any place named "Work", and every place you mark **Work** on the app's My places page. On the command line, use the `work` column in my_places.csv or `--work-place`.
  - Stops at work less than 45 minutes apart count as one shift.
- **Commute:** door-to-door times from home to work and back, by weekday and by month, with your best and worst times and when you usually leave. Saved as `commutes.csv`.
- **Mileage log:** every drive, with date, time, where it started and ended, miles, and a type of **Commute**, **Work** or **Personal**. You can filter by year and type and download the rows. If you set a $/mile rate, Work drives get a reimbursement column. Saved as `mileage_log.csv`.
  - Distances marked ≈ were estimated from GPS points. That happens for data such as Records.json and GPX files, where Google didn't record the trip itself.
  - Check the log before using it for taxes or reimbursement. Commutes usually aren't deductible.
- **Where you've been:** world, US-state and US-county maps shaded by days spent in each, plus town/state/country tables. The borders ship with LocScope and work offline. Saved as `visited_map_regions.csv`.
- **Find a location**, **Saved & imported places**, **Top places** and the travel charts are as before.

## Even more in the report

- **Ask:** type questions in plain English, for example:
  - "last time at Costco"
  - "how many times at the gym in 2024"
  - "days I drove over 100 miles"
  - "weekends at Ocean City in 2023"
  - "nights away in 2024"
  - "flights", "call-ins in 2025", "farthest from home"
  - a town name, or "notes about conference"

  Answers are worked out inside the report, so nothing is sent anywhere. Click any date to see that day on the map.
- **On this day:** what you were doing on today's date (or any date you pick) in every past year, with the stops, photos, weather and your note for each.
- **Compare two periods:** this year so far against the same dates last year, last year against the year before, the last 30 days or 12 months against the period before, or any two date ranges you choose.
  - It compares distance, stops, places, towns, trips, nights away, flights, hours at work, call-ins, miles driven and photos.
- **Where you slept:** where you were at 3 a.m. each night. You get nights at home and away per year, the places you've slept most often away from home, and your longest time away. Saved as `nights.csv`.
- **Flights:** flights Google recorded, plus ones detected from big jumps between GPS fixes. Each is matched to the nearest airport using a bundled list of about 4,500 airports from OurAirports.
  - It shows a map of the routes, total distance, the airports you've used and your most-flown route. Saved as `flights.csv`.
- **After-hours call-ins:** work visits that started at night (6 p.m.–6 a.m.), on a weekend, or after you'd already gone home for the day. They're grouped into on-call weeks running Monday to Monday.
  - It charts what time you arrived and the call-ins per week. Saved as `call_ins.csv`.
- **Driving speeds and when to leave:** average driving speed by weekday and hour, and how long your commute typically takes for each 15-minute departure time. It also points out the quickest time to leave.
- **Weather:** tick "Add the weather for every day" (or add `--weather`) and LocScope gets each day's weather where you spent most of the day, from Open-Meteo's free history archive.
  - This sends a rough location for each day to Open-Meteo. The first run takes a minute, and the results are cached in `geodata\weather_cache.json` after that.
  - The weather then shows in the day view, Browse, On this day and the calendar tooltips. The Weather section lists your hottest, coldest, wettest and snowiest days. Saved as `weather.csv`.
  - Without the option, each day has a "Get the weather for this day" link that looks up just that day when you click it.
- **Exploration map:** a "fog of war" map that clears every roughly 200 m square you've been in. It also gives the percentage you've explored within 2, 5 and 10 km of home, and of the counties you've spent the most time in.
- **Maidenhead grid squares:** every amateur-radio grid square (4-character) and subsquare (6-character) you've been in, with your home grid, on a map and in a table. Saved as `grid_squares.csv`.
- **Day notes:** click "＋ Add a note" on any day (in the map's day view, Browse or On this day). Notes are searchable and show in the calendar (a small dot), Browse and Ask.
  - Notes you type in the report are kept in that browser. To keep them in every future report, click **Download notes** in the report, then **Import CSV…** on the app's **Day notes** page.
  - You can also add notes straight on the Day notes page. On the command line, use `--notes my_notes.csv`.
- **Places you stopped going and new regulars:** places you used to visit regularly but haven't been to in 6 months or more, and places that became regulars in the last year. Saved as `places_stopped_and_new.csv`.
- **Carbon footprint:** a rough estimate of CO₂ per year by travel mode, with the factors used listed in the report. Saved as `carbon_kg_co2e.csv`.

The shareable copy leaves out where you slept, call-ins, notes, the commute and departure times, 6-character grid squares, and any explored areas near home, work or private places.

## Round 4 additions

- **Fix-ups:** in any day's timeline (on the map or in Browse), click ✎ next to a stop and choose:
  - **Rename this place:** for example, give a mislabeled stop its right name.
  - **Remove this stop:** for a stop that never happened.
  - **Ignore this whole day:** for example, a day you lent your phone to someone.

  Your changes collect in the report's **Fix-ups** list. Click **Download fix-ups**, then **Import CSV…** on the app's **Fix-ups** page, and they're applied to every future report. You can also add fix-ups straight on that page, and the `ignore_area` type there removes everything within a radius of a point. On the command line, use `--fixes my_fixes.csv`.
- **Faster re-runs:** every file LocScope reads is remembered in `geodata\cache.sqlite`. Next time, unchanged files load instantly and only new or changed ones are read. Turn this off under **More settings** or with `--no-cache`. **Clear the memory** empties the cache.
- **Data quality and gaps:** how much of each day your phone recorded, average coverage by month, and every stretch of two or more days with no data. In the Calendar, choose "Data coverage" to see it day by day. Saved as `data_quality.csv` and `data_gaps.csv`.
- **Parks and collections:**
  - **POTA parks:** the Parks on the Air parks you've visited, plus parks near home you haven't been to yet (activation ideas). The official POTA park list downloads once and is refreshed twice a year.
  - **National parks and sites:** national parks, monuments, historic sites, seashores, forests and other national sites, taken from the same POTA list.
  - **State capitals:** all 50 plus Washington, DC.
  - A park counts if you were within about 1 km of its listed point (3 km for big parks and forests). Very large parks can be missed.
- **Holidays:** where you were on each holiday, every year, with a ✈ marking holidays spent away from home. Saved as `holidays.csv`.
- **Family-history places:** list ancestral towns on the **Family places** page, by town name or lat/lon, with a radius in km. The report shows which you've been to, when, and how far each is from home.
- **Snowy-week planner:** switch it on under **What to include** (or add `--snow`). LocScope downloads about 30 years of daily snowfall near home from Open-Meteo, sending only a ~25 km grid point, not your address.
  - The report then shows, for each coming Monday-to-Monday week, the chance of a day with an inch of snow or more, plus a typical year week by week. This is handy for choosing on-call weeks. Saved as `snowy_weeks.csv`.
- **Place types:** with street addresses turned on, each top place gets its OpenStreetMap category (restaurant, grocery, gas, medical, park, fitness, hotel and so on). The report shows hours per type per year, top places of each type, and gas stops per month.
  - Places looked up before this version are looked up once more to get their category.
- **Calendar files:** `export\calendar\` holds .ics files for your trips, work shifts, call-ins, flights and overnight stays. Double-click one, or import it into Outlook or Google Calendar. Add `--no-ics` to skip them.
- **Printable year book:** the **🖨 Print year book** button in Year in review prints one page per year, with tiles, a map, monthly distance, highlights, top places and photos. Choose "Save as PDF" as the printer to keep a PDF.
- **Year video:** **🎬 Make a video of this year** in Year in review animates your year on a map.
  - **Preview** plays it in the page.
  - **Record** saves a 30- or 60-second video, as MP4 when Chrome supports it, otherwise WebM. Keep the tab visible while it records.
- **Time together:** on the **Together** page, add another person's export and their name (or use `--together-with FILES --together-name NAME`).
  - The report shows the hours and days you were within 250 m of each other, where, and your longest stretch together. Saved as `time_together.csv`.
  - Only the times together are kept, not their own movements.
  - Ask them before using their data.

The shareable copy also leaves out Together, Holidays, Family places, Snowy weeks, Place types and Fix-ups.

## Extras

- **Google Earth:** the `export` folder holds `locscope_all.kml` (every day, with timestamps, so Google Earth's time slider works), `locscope_all.gpx`, `places.kml`, and a KML file for each trip in `trips\`. Uncheck the option in the window, or add `--no-export`, to skip this.
- **Photo thumbnails:** LocScope uses the small preview that phones and cameras store inside each photo. If a photo has none and Pillow is installed (`pip install pillow`, plus `pillow-heif` for iPhone HEIC files), it makes one.
- **Shareable copy:** turn on "Also make a shareable copy" (or add `--share`) and LocScope also writes `report_share.html`.
  - It leaves out everything within 1 km (adjustable) of Home, your work places and any place ticked **Private**.
  - It also drops the work, commute and mileage sections, your place names near those areas, and the list of data sources.
  - Check it before sending it to anyone.
- **Watch mode:** in the app, switch on **Rebuild automatically when my files change** under More settings. Then drop a new Timeline export or GPX file into a folder you've added, and LocScope rebuilds within about 30 seconds. An open report refreshes itself. On the command line, add `--watch` to do the same.
- **Standalone .exe (no Python needed):**
  - On one PC with Python, double-click `build_exe.bat`. It makes `dist\LocScope.exe`, which you can copy to any of your PCs. It also tries to include pywebview for a native window; if that can't install on your Python, the exe uses an Edge app window instead.
  - Or push this folder to GitHub and run the included **Build LocScope.exe** workflow from the Actions tab (or push a tag like `v1.0`). GitHub builds the exe on Windows and attaches it to the run, and to the Release when you push a tag.

## Import other map and GPS data

LocScope also reads the files below. You can pass any of them, or a whole folder or zip of them, alongside your Google data. Everything is merged by time, and duplicates are removed.

| Source | Files | What LocScope does with it |
|---|---|---|
| Garmin, Strava, Komoot, AllTrails, GPS units, GPSLogger, Gaia | `.gpx`, `.tcx` | Adds the timed track points to your history. Waypoints become pins. |
| Google Earth, Google My Maps, the old Google Timeline KML export | `.kml`, `.kmz` | Tracks with times are added to your history, and old Timeline KML visits and drives are recognized. Placemarks with no time become pins. |
| Google Maps saved places, from Takeout "Maps (your places)" | `Saved Places.json` | Pins, each with a count of how many times you've actually stopped there |
| Google Maps labeled places, from Takeout "Maps" | `Labeled places.json` | Treated like entries in `my_places.csv`. For example, your labeled "Home" is used as Home. |
| GeoJSON from Arc, Overland, geojson.io and similar | `.geojson`, `.json` | Timed points and lines are added to your history. Other points become pins. |
| Photos | `.jpg`, `.jpeg`, `.heic`, `.tif`, `.dng`, or a Google Photos Takeout zip | The GPS tag and time taken become history points. Photos also appear on the map under **Photos**, and clicking one replays that day. |
| OwnTracks and other JSON-lines loggers | `.rec`, `.jsonl` | Adds the points to your history |
| Spreadsheets and app exports (Life360, GPSLogger CSV, etc.) | `.csv` | Needs lat/lon columns. Rows with a time go into your history, and rows without one become pins. |
| Raw GPS receiver logs | `.nmea` | Adds the RMC fixes to your history |

Some examples:

```powershell
python locscope.py takeout.zip "D:\Garmin\Activities" "$HOME\Pictures\2024" mymaps.kmz
python locscope.py takeout.zip --no-photos
```

- **Times without a time zone** are read as `--tz` if you gave one, otherwise as your PC's time zone. This applies to CSV timestamps and to photos without a GPS time. A GPS time on a photo, where there is one, is used first.
- **Photo folders:** LocScope reads only the first 1 MB of each photo, so large folders go quickly. HEIC files from iPhones work in most cases. If an iPhone photo isn't picked up, export it as JPEG. Add `--no-photos` to skip photos.
- **Saved-place CSVs from Takeout (the "Saved" folder)** contain no coordinates, so they can't be imported. Use `Saved Places.json` instead.
- **Garmin `.fit` files** aren't supported. Export them as GPX from Garmin Connect.
- The imported pins are listed in the report's **Saved & imported places** table, where you can filter to show only places you've never visited, and in `imported_places.csv`.

## Name your own places

The `work` and `private` columns (yes/blank) mark work places and places to hide in shareable copies. In the window, these are the Work and Private checkboxes.

Run `python locscope.py --make-places-template`. This writes a `my_places.csv` file that you can edit in Excel or Notepad:

```csv
name,lat,lon,radius_m,address
Home,38.8462,-77.3064,150,
Office,38.8977,-77.0365,200,
Mom's house,,,150,"456 Oak Ave, Springfield, VA"
```

- For each place, give either `lat`/`lon` or an `address`. Addresses are looked up once through OpenStreetMap and then cached.
- Every stop within `radius_m` meters of a place is counted as that place. This merges the stops and replaces Google's name for them.
- A place named `Home` or `Work` overrides the guess that LocScope would otherwise make.
- LocScope picks up `my_places.csv` automatically if it's in the current folder or next to the script. To use a different file, add `--places file.csv`.
- To get coordinates, right-click a spot in Google Maps and click the numbers at the top of the menu to copy them.

## Look up a location

```powershell
python locscope.py Timeline.json --find "38.8977, -77.0365"
python locscope.py Timeline.json --find "Mom's house" --find "Dulles Airport, VA" --find-radius 500
```

This prints every stop within the radius (with the date, arrival time, departure time and hours) and every day you only passed by. It also writes `find_<name>.csv`. The report has the same tool in its **Find a location** box. There you can type coordinates, one of your named places or an address, or click **Pick on map** and then click anywhere on the map.

## Cities, states and countries

LocScope labels every stop with its town, state and country. It also adds a **Where you've been** table with the days, stops and hours for each place, and the first and last date you were there. Clicking a row replays that day.

On the first run it downloads the free GeoNames city list (about 10 MB) into a `geodata` folder next to the script. After that, no internet is needed and your coordinates never leave your PC. Without internet, download these three files from https://download.geonames.org/export/dump/ into the `geodata` folder yourself: `cities1000.zip`, `admin1CodesASCII.txt` and `countryInfo.txt`. Add `--no-regions` to skip this feature.

The town is the nearest town or city with 1,000 or more people. In suburbs and rural areas this can be a neighboring town rather than your mailing address.

## Miles or kilometers

Reports use miles, feet and °F by default. The **mi | km** switch at the top of the report changes every distance, speed, temperature and chart. It reloads the report and remembers your choice in that browser. To change the default for new reports, use **Units** on the app's Build page, or `--units metric` on the command line. Small distances such as search radii and place radii are still entered in meters, with feet shown alongside. The CSV files keep kilometers, except the mileage log, which is in miles.

## Big histories

LocScope is tuned for many years of data. On an 8-year, 860,000-point test history, a full run takes about 35 seconds (under 30 when files are unchanged and come from the parse cache), and the report is about 13 MB. Day paths and the exploration map are packed into compact number lists and unpacked in the browser when you open them. The report draws its first screen straight away and builds the remaining sections in the background.

If the report still feels heavy, `--day-points` lowers the number of path points kept per day, and `--no-explore` skips the exploration map.

## Useful options

| Option | What it does |
|---|---|
| `--out DIR` | Sets the output folder. The default is `locscope_report`. |
| `--start 2019-01-01 --end 2019-12-31` | Analyzes only a date range. |
| `--tz America/New_York` | Puts all times in one time zone. On Windows this may first need `pip install tzdata`. Without this option, each timestamp uses its own offset, or this PC's zone if it has none. |
| `--geocode 50` | Looks up names for your top 50 places through OpenStreetMap, at about one per second, and caches the results. The newer exports contain only coordinates and Google place IDs, with no place names. **This option sends those coordinates to OpenStreetMap.** |
| `--trip-km 150` | Sets how far from home counts as a trip away. The default is 100 km. |
| `--export-points` | Also writes every GPS point to `points.csv`, which you can import into Google Earth, QGIS or Excel. |
| `--max-accuracy 200` | Drops GPS fixes that are less accurate than this many meters. The default is 500. |
| `--work-place "Hospital,Clinic"` | Counts these places from my_places.csv as work places, in addition to Work and any place marked work. |
| `--mileage-rate 0.70` | Adds a reimbursement column, at this many dollars per mile, for Work drives. |
| `--share` / `--share-radius 1500` | Also writes `report_share.html`, with everything near Home, Work and Private places removed. The default radius is 1000 m. |
| `--no-export` | Skips the Google Earth KML/GPX export. |
| `--no-map-regions` | Skips the countries, US states and counties maps. |
| `--weather` | Adds historical weather for every day, from Open-Meteo. |
| `--notes my_notes.csv` | Day notes to show in the report, with `date,note` columns. |
| `--no-explore` | Skips the exploration map. |
| `--fixes my_fixes.csv` | Applies corrections: renames, removed stops, ignored areas and ignored dates. |
| `--family family.csv` | Family-history places, with `name,place,lat,lon,radius_km,note` columns. |
| `--snow` | Adds the snowy-week planner, using about 30 years of snowfall near home. |
| `--together-with FILES --together-name NAME` | Finds the times you were together with another person, from their export. |
| `--no-cache` / `--cache FILE` | Turns off, or relocates, the cache of files already read. |
| `--no-collections` | Skips the POTA parks, national parks and state capitals section. |
| `--no-ics` | Skips the calendar (.ics) files. |
| `--watch` | Keeps running, and re-analyzes whenever the input files change. It checks every 60 s, or `--watch 300` for every 5 minutes. |

For a Takeout `Records.json` of several GB, run `pip install ijson` first so the file is streamed instead of loaded into memory all at once.

## Outputs

- `report.html` is the interactive report. The map needs internet access to load Leaflet and the OpenStreetMap tiles. Everything else in the report works offline.
- `places.csv` lists every place with its total hours, visit count, first and last dates, and a Google Maps link.
- `visits.csv` has every stop, with its start and end times.
- `trips_by_mode.csv` has every drive, walk or flight, with its distance.
- `daily.csv` has the distance and GPS point count for each day, plus how far you got from home that day.
- `regions.csv` lists every country, state and town, with its days, stops, hours and first and last dates.
- `work_timesheet.csv`, `work_weekly.csv`, `commutes.csv`, `mileage_log.csv` and `visited_map_regions.csv` hold the data behind the new sections described above.
- `export\` holds the Google Earth KML/GPX files, `thumbs\` holds the photo thumbnails, and `report_share.html` is the shareable copy.
- `imported_places.csv` lists every saved place, waypoint, pin and geotagged photo, with how many times you visited each.
- `find_*.csv` holds the results of each `--find`.
- `places.csv`, `visits.csv` and `daily.csv` also include town, state and country columns.
- `away_trips.csv` lists trips of more than `--trip-km` from home.

## Notes

- **Addresses:** the window's **Show street addresses** option is on by default and looks up your top 150 places. On the command line, use `--geocode 150`. Each place's coordinates are sent to OpenStreetMap, one lookup per second. The first run takes a few minutes, and after that the addresses are saved in `geodata\address_cache.json` and reused. The addresses appear in the report, in the window's Find results, and in `places.csv` and `visits.csv`. A place with no address shows as "Near <town>" instead of coordinates.
- **Sorting:** click any column heading in the report or the window to sort by it, and click again to reverse.

- **The map** uses Esri tiles, which work even when you open `report.html` straight from a folder. Use the layers button at the top right to switch to Light, Dark, Satellite or Topographic. If you open the report with the window's **Open report** button, it is served from a private local web address (127.0.0.1) instead, which adds an OpenStreetMap style. OpenStreetMap and CARTO refuse to load in pages opened as plain files.
- **"Very little location history was found"** means your inputs contained only pins and photos, with no real Timeline. Export Timeline from your phone and add that file (see step 1).
- **Takeout's `Timeline Edits.json`** is read too. It holds a small amount of raw position data and Google's "frequent places".

- **Home and work** come from the labels in your export when it has them. Otherwise, home is the place where you spent the most time between midnight and 6 AM, and work is the place where you spent the most time on weekdays between 9 AM and 5 PM.
- **Distance** uses Google's own trip distances when a day has them. Otherwise it adds up the GPS points, skipping jumps faster than an airliner and small movements that are just GPS noise.
- If the input has only raw GPS points (only `Records.json`), LocScope works out stops itself. A stop is staying within 200 m of one spot for at least 15 minutes.
