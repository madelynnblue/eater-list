# Eater 38 — list history pipeline

Reconstructs the full membership history of Eater NY's
[38 Best Restaurants](https://ny.eater.com/maps/best-new-york-restaurants-38-map)
page from Wayback Machine captures, and exports it as JSON for a web front-end.

The page is an evergreen URL whose contents are rewritten roughly quarterly, so
the only record of "who was on the list when" is the archived page itself. This
tool turns ~750 Wayback captures into:

* **`web/data/places.json`** — every restaurant that ever appeared, with the
  period(s) it was on the list, address, coordinates and stable id
* **`web/data/places.geojson`** — the same, ready to drop on a map
* **`web/data/current.json`** — the list as of the latest capture
* **`web/data/updates.json`** — every change, with what was added and removed
* **`web/data/meta.json`** — coverage and counts
* CSV/Markdown exports in `out/` for humans

---

## Why it is cheap

Two properties of the Wayback Machine make an incremental crawl very small.

**1. The CDX index is metadata-only.** `web.archive.org/cdx/search/cdx` returns
every capture's timestamp, HTTP status and content digest without transferring
any page. Listing 749 captures costs one small request.

**2. The list changes rarely.** It was rewritten 35 times across nine years, so
almost every capture is redundant. The crawler uses binary search:

```
compare list(first) with list(last)
  equal   -> nothing changed in between; stop
  differ  -> split the range and recurse; a range of two adjacent captures
             that differ is exactly one change
```

Measuring the real data: **128 of 749 captures (17%)** need downloading to find
all 35 changes — 621 fewer requests than fetching everything. Every payload is
also cached by content digest, so nothing is downloaded twice across runs, and
unchanged digests are never re-fetched.

Weekly runs are cheaper still: the crawler anchors on the last capture it
already verified, so a week in which the list did not move costs **one or two
page downloads** plus the CDX listing.

## Why it does not get rate limited

* A single **token bucket** shared by all worker threads caps the process at
  `requests_per_second` (default 1.5) with a small burst, independent of
  concurrency.
* A **semaphore** caps simultaneous connections (default 4).
* `429` / `503` / `5xx` and timeouts are retried with exponential backoff **plus
  jitter**, and HTTP `Retry-After` is honoured. A throttle response also applies
  a **global cooldown**, so parallel workers pause together instead of
  stampeding.
* The Internet Archive's "Temporarily Offline" HTML page is detected and treated
  as retryable — it arrives with HTTP 200, so it would otherwise be parsed as a
  page with no restaurants.
* `max_fetches_per_run` bounds any single run; progress is cached, so an
  interrupted run resumes cheaply.
* A CDX outage does not abort a run — it falls back to the index already stored.
* Set a **real contact** in `config.toml` → `[wayback] user_agent` before large
  crawls. That is what the Internet Archive asks of automated clients.

## Install and run

No third-party dependencies. Python 3.11+ (uses `tomllib`).

```bash
cd eater38
python -m e38 init                 # create the SQLite database
python -m e38 seed                 # optional: import the exploratory crawl
python -m e38 scan --mode full     # discover captures (CDX) + fetch what is needed
python -m e38 enrich               # backfill addresses / coordinates
python -m e38 build                # derive history, write web/ and out/
python -m e38 status               # coverage report
```

Typical first run on a clean checkout: `scan --mode full` downloads ~130 pages
and finishes in a few minutes at the default polite rate.

### Commands

| command | purpose |
|---|---|
| `init` | create the SQLite database |
| `scan --mode full` | index every capture since `[crawl] since`, then bisect for changes |
| `scan --mode incremental` | what the weekly job runs: only look at captures newer than the last one verified |
| `scan --strategy exhaustive` | fetch **every** capture — use to audit the bisect assumption |
| `status` | captures indexed, contents cached, staleness |
| `enrich` | backfill fields a previous extractor version did not capture |
| `build` | derive periods/changes and write `web/data/*.json` + `out/*` |
| `build --suggest-aliases` | propose rename candidates for `aliases.json` |
| `geocode` | fill coordinates for places the archive never geotagged (keyless, cached) |

## Scheduling the weekly re-scan

The scan is idempotent, so it is safe to run as often as you like. `cron`,
launchd or a systemd timer all work.

```cron
# Every Monday at 06:17 — one or two page downloads on a quiet week.
17 6 * * 1  cd /path/to/eater38 && /usr/bin/python3 -m e38 scan --mode incremental \
             >> data/weekly.log 2>&1 && /usr/bin/python3 -m e38 build >> data/weekly.log 2>&1
```

For a small server, run it from a systemd timer:

```ini
# /etc/systemd/system/eater38-scan.service
[Service]
Type=oneshot
WorkingDirectory=/srv/eater38
ExecStart=/usr/bin/python3 -m e38 scan --mode incremental
ExecStart=/usr/bin/python3 -m e38 build
```

```ini
# /etc/systemd/system/eater38-scan.timer
[Timer]
OnCalendar=Mon 06:17
Persistent=true

[Install]
WantedBy=timers.target
```

Keep the whole database on persistent storage: it is the resumable state. A
`scan` that dies halfway loses nothing.

## How the data is produced

```
e38/wayback.py    CDX listing + capture download (retries, gzip, offline detection)
e38/ratelimit.py  token bucket, concurrency cap, jittered backoff
e38/extract.py    archived HTML -> ordered entries (3 page eras)
e38/store.py      SQLite: captures, cached extractions, raw HTML, geo cache
e38/scan.py       bisect / exhaustive change detection
e38/build.py      periods, stretches, updates -> JSON/CSV/Markdown
e38/cli.py        command line
```

### Three page eras, one extractor

Eater changed its markup three times, so the extractor tries them in order and
merges what it finds:

| era | source of truth | gives |
|---|---|---|
| 2017–2018 | `section.c-mapstack__card` + JSON-LD `ItemList` | names, addresses, phone, website |
| 2019–2025‑06 | `section.c-mapstack__card` (`<h1>` headings) + JSON-LD | names, addresses, phone, website |
| 2025‑07 → | `__NEXT_DATA__` → `mapPoints[]` | names, addresses, phone, website, **lat/lng** |

Because the sources are merged on slug and then on name, the 2017 era keeps its
JSON-LD ordering *and* its mapstack addresses.

### Identity

URL anchors are unusable as identifiers: Eater used placeholder `article-N`
anchors during 2024–25 and re-slugged entries constantly. Identity is therefore
the **display name**, normalised (case, accents, punctuation, `&` → `and`), with
a small curated `aliases.json` joining entries Eater genuinely renamed
(*The Odeon* → *Odeon*, *S&P* → *S&P Lunch*, …). Run
`python -m e38 build --suggest-aliases` after a scan to get candidates.

### Geocoding the leftovers

`python -m e38 geocode` walks places that still have no coordinates and queries
**keyless public geocoders**, one request per second, caching every answer in
SQLite so repeat runs and rebuilds are free:

1. **NYC Planning Labs GeoSearch** — purpose-built for New York addresses
   (145 of 148 resolved here)
2. **US Census** one-line geocoder — federal fallback (3)
3. **Nominatim** — last, because it rejects User-Agents it does not consider
   properly identifying; it will 403 the placeholder contact that ships in
   `config.toml`, and start working the moment you put a real one there

`build` reads the geocode cache directly, so coordinates survive a rebuild
without re-querying anything.

### Raw archive

With `store_raw = true` the untouched HTML is kept gzipped under `data/raw/`.
That means a change to the extractor can be re-applied offline
(`python -m e38 enrich`) with **zero** extra load on the Internet Archive — the
extractor version is stored per payload, so stale payloads are re-parsed from
disk automatically. Weekly captures add roughly 300 KB each.

## The web demo

`web/index.html` is a dependency-free viewer over `web/data/`. Serve the folder
over HTTP (`make serve`) and open it. Three tabs drive the map:

| tab | sidebar | pins |
|---|---|---|
| **Current list** | the 39 restaurants listed today, in Eater's order | green |
| **Date range** | anything on the list at any point in the selected window | green = on today, amber = rotated out |
| **All 245** | every restaurant that ever appeared | green = on today, amber = rotated out |

The **Date range** tab has a double-ended slider spanning the whole archive
(2017‑08‑05 → 2026‑09‑21) and defaults to the previous year. Dragging either end
re-filters both the list and the pins; the map deliberately does not re-zoom
while dragging, only when the tab changes.

In every tab:

- **hover a row** → spotlight its pin
- **click a row** → centre the map on it and zoom to street level (the inline
  website link keeps its own behaviour)
- **hover a pin** → its details
- **click a pin** → open the restaurant's website, falling back to its Eater entry

`updates.json` is still exported for anyone who wants the change log, but the
UI no longer uses it.

## Known limits

* **The archive starts 2017‑08‑05.** The first capture already contains 38
  names, so anything added before that date is invisible. Earlier editions of
  the same guide live at separate dated URLs (`…/the-38-essential-new-york-restaurants-january-12`,
  `…/april-14`, and so on) and would need their own crawl.
* **Bisect assumes the list only moves forward.** If a restaurant were added and
  removed between two captures whose endpoints match, that is invisible. Run
  `scan --strategy exhaustive` to audit; it costs ~6× the requests.
  (In 2026 the archive is dense enough that this is a narrow window.)
* **Change dates are bracketed, not exact.** The list is only observed at
  capture times. `updates.json` gives the capture *before* and *after* each
  change; where the archive has a multi-month hole, the window is wide. Adding
  captures narrows it.
* **Coordinates come from three places.** Modern pages embed them from
  2025‑07 onward (~63 places); the very first 2017 capture turns out to carry
  them in each card's "Directions" link (~38 more); the remainder are geocoded
  from street addresses. Net result: **244 of 245 places are mapped**. The
  exception, Joe's of Avenue U, has no address anywhere in the archive, so
  there is nothing to geocode.
* **2018–2025.06 pages have neither coordinates nor a usable substitute** — they
  link Google *Place IDs* (`query_place_id`), which need a paid API to resolve.
  This is why the geocoding step exists at all.
* **Alias curation is manual-by-design.** Automatic name matching on restaurant
  names produces false merges (chains, branches); the tool suggests, a human
  decides.

## Tests

```bash
python -m unittest discover -s tests -t .
```

39 tests cover the extractor (against real gzipped captures from 2017 and 2026),
name/alias resolution, the bisect search, period derivation, and link splitting.

The web demo has its own headless check, which runs `web/index.html`'s real
script against a stub DOM and Leaflet using the real exported JSON:

```bash
make test-demo        # needs node
```

It drives the page's real event handlers, asserting that every tab renders the
right rows, that the range filter agrees with an independent computation of the
same window, that the slider defaults to the last year of coverage, that
clicking a row centres and zooms the map (while clicking the link inside it does
not), and that every update id resolves to a known place.
