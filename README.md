# Eater 38 — list history pipeline

Reconstructs the full membership history of Eater NY's
[38 Best Restaurants](https://ny.eater.com/maps/best-new-york-restaurants-38-map)
page from Wayback Machine captures, and exports it for a web front-end.

The page is an evergreen URL whose contents Eater rewrites roughly quarterly, so
the only record of *who was on the list when* is the archived page itself. This
project turns ~750 Wayback captures into:

* **`web/data/places.json`** — all **245** restaurants that ever appeared, each
  with the period(s) it was on the list, address, coordinates and stable id
* **`web/data/places.geojson`** — the same as map points
* **`web/data/current.json`** — the **39** listed at the latest capture
* **`web/data/updates.json`** — all **35** changes, with adds and removals
* **`web/data/meta.json`** — coverage and counts
* `out/*.csv`, `out/*.md` — the same history in human-readable form

No third-party dependencies. Python 3.11+ (uses `tomllib`). Node is needed only
for the optional browser-demo test.

---

## Quick start

```bash
cd ~/code/eater-list
make init          # create the SQLite database
make full          # crawl: index captures, fetch what's needed, build outputs
make serve         # http://127.0.0.1:8787
```

`make full` on a clean checkout downloads ~130 pages and takes a few minutes at
the default politeness settings. After that, `make weekly` keeps it current.

---

## How it works, step by step

```
  (1) CDX index ──▶ captures table        one small request, no pages fetched
                          │
  (4) bisect ─────────────┘               which captures actually matter?
                          │
  (2) fetch ◀─────────────┘               rate-limited, cached by digest
        ├──────────────────▶ data/raw/    gzipped HTML, so re-parsing is free
        ▼
  (3) extract ──▶ contents table          parsed entries, keyed by digest
        │
        ▼
  (5) identify ──┐                        normalise names, apply aliases
                 ├──▶ (6) periods ──▶ (8) export ──▶ (9) serve
  (7) locations ─┘                        coords from pages, then geocoding
```

### 0. State lives in SQLite

Everything below reads and writes one database (`data/eater38.sqlite`). It is the
resumable state — a run that dies halfway loses nothing.

| table | holds |
|---|---|
| `captures` | one row per Wayback capture: timestamp, status, content digest |
| `contents` | the parsed payload for each **distinct digest**, with its extractor version |
| `geocache` | address → coordinates, so geocoding is never repeated |
| `meta` | bookkeeping, notably the last capture verified per URL |

Untouched HTML lives beside it on disk under `data/raw/`, not in a table.

The split between `captures` and `contents` is the heart of the design: we know
about **749** captures but only ever download each of the **670 distinct pages**
among them once.

### 1. Index the captures (metadata only)

The CDX API returns every capture's timestamp, status and content digest
**without transferring any page**. One small request lists the whole history.

→ The CDX index returns 751 rows, stored as **749 capture records** spanning
2017‑08‑05 → 2026‑09‑21. Of those, **728 carry HTTP 200**; the other 21 are
Wayback revisit markers, and among the 200s there are **670 distinct pages**.

This is why the crawler can reason about *when* to look without paying to look
everywhere.

### 2. Fetch a page (politely, once)

When the crawler decides it needs a specific capture, `fetch`:

1. acquires a slot from a **shared token bucket** (1.5 req/s, burst 4) and a
   **concurrency semaphore** (4), so adding threads cannot outrun the limit;
2. downloads with gzip, retries `429`/`503`/`5xx`/timeouts with **exponential
   backoff plus jitter**, honours `Retry-After`, and applies a **global cooldown**
   on throttle so parallel workers pause together;
3. rejects bodies under 5 KB, and detects the Internet Archive's
   **"Temporarily Offline" page — which arrives with HTTP 200** and would
   otherwise parse as a page with zero restaurants;
4. archives the raw HTML gzipped, then extracts and stores the payload **keyed by
   content digest**, so that page is never downloaded twice, ever.

A CDX outage does not abort a run — it falls back to the index already stored.
`max_fetches_per_run` (500) bounds any single run.

**Why the raw archive matters:** because the bytes are kept and each payload
records its extractor version, improving the parser costs *zero* network. The
last extractor upgrade re-parsed **32 captures offline and fetched one page**.

### 3. Extract the entries

Eater changed its markup three times, so the extractor tries each shape and
merges what it finds:

| era | source of truth | yields |
|---|---|---|
| 2017–2018 | `section.c-mapstack__card` + JSON-LD `ItemList` | names, addresses, phone, website |
| 2019 – 2025‑06 | same cards, `<h1>` headings | names, addresses, phone, website |
| 2025‑07 → | `__NEXT_DATA__` → `mapPoints[]` | the above **plus lat/lng** |

Sources are merged on slug and then on name, so the 2017 era keeps its JSON-LD
*ordering* and its mapstack *addresses*. Every entry is normalised to the same
shape, which is what lets later steps ignore the era entirely.

### 4. Find the changes (bisection, not brute force)

The list was rewritten only **35 times in nine years**, so almost every capture
is redundant. A signature for each capture — the set of restaurant names — is
enough to binary-search the change points:

```
compare signature(i) with signature(j)
  equal   → nothing changed in between; stop
  differ  → j == i+1 ? that pair is one change : split at the midpoint and recurse
```

→ **128 of 749 captures (17%)** are needed to find all 35 changes: 621 fewer
downloads than fetching everything.

The assumption is that the list only ever changes (it does not change *and change
back* between two captures whose endpoints agree). `scan --strategy exhaustive`
lifts that assumption by fetching every capture, at roughly 6× the requests.

### 5. Identify the restaurants

Eater's URL anchors are unusable as IDs — it used placeholder `article-N` anchors
through 2024–25 and re-slugs entries constantly. (Keying on slugs produced ~40
fake add/remove events in an early version.)

Identity is the **display name, normalised**: case, accents and punctuation
folded, `&` → `and`. A small curated `aliases.json` joins entries Eater genuinely
renamed, with chain-safe resolution — *The Odeon* → *Odeon*, *S&P* → *S&P Lunch*,
*Adda Indian Canteen* → *Adda*.

Subtlety worth knowing: **the crawler compares un-aliased names, `build` applies
the aliases.** So a rename still registers as a detected change, but does not
split one restaurant into two. `build --suggest-aliases` proposes candidates for
review; merges stay manual because automatic name matching fuses chains and
branches.

→ **245 places** from **293 stints** on the list.

### 6. Derive the periods

Walking the observations in time order gives, per place, the maximal runs during
which it was present. For each run we record the first and last capture that
showed it, plus the neighbouring captures that did *not* — so every start and end
is reported as a **bracket** ("after 2019‑12‑05 and by 2020‑03‑18") rather than a
false precision.

The same walk yields the change log: consecutive observations whose name sets
differ are one update, with its adds and removes.

### 7. Attach locations

Coordinates come from three sources, in the order the archive offers them:

| source | places | note |
|---|---|---|
| `__NEXT_DATA__` `location` | ~63 | modern pages only, from 2025‑07 |
| the 2017 cards' "Directions" link (`maps?q=<lat>,<lng>`) | ~38 | found only in that one capture |
| geocoding from the street address | the rest | 148 resolved |

2018 – 2025.06 pages carry neither — they link Google *Place IDs*, which need a
paid API to resolve. That is exactly why geocoding is a permanent pipeline step
rather than a one-off backfill.

`e38 geocode` walks the leftovers at 1 request/second through a **keyless
provider chain**, caching every answer in SQLite:

1. **NYC Planning Labs GeoSearch** — built for New York addresses (145 of 148)
2. **US Census** one-line geocoder — federal fallback (3)
3. **Nominatim** — last, and it 403s the placeholder contact that ships in
   `config.toml`; it starts working the moment you put a real one there

`build` reads the geocode cache directly, so coordinates survive a rebuild
without re-querying anything. → **244 of 245 places mapped.**

### 8. Export

One pass over the cached observations writes the JSON above plus CSV/Markdown.
Per place, the most recent non-empty value wins for address, phone, website and
coordinates — so a field discovered in a 2024 capture is used even if the 2026
page omits it.

Websites and Eater's own anchors are kept in **separate fields** (`website` vs
`eater_url`). They were once the same field, which meant 184 "restaurant
websites" were actually links back to Eater.

### 9. Serve

`web/index.html` is a dependency-free viewer over `web/data/`. Three tabs, all
driving one Leaflet map:

| tab | sidebar | pins |
|---|---|---|
| **Current list** | the 39 listed today, in Eater's order | green |
| **Date range** | anything on the list at any point in the window | green = on today, amber = rotated out |
| **All 245** | every restaurant that ever appeared | as above |

The **Date range** tab has a double-ended slider spanning the whole archive and
defaulting to the previous year; each place is included if any of its periods
overlaps the window.

In every tab: **hover a row** to spotlight its pin, **click a row** to centre and
zoom the map to it, **hover a pin** for a short card (name, address, website) —
the sidebar scrolls to highlight that entry — and **click a pin** to open the
restaurant's website, falling back to its Eater entry.

Sidebar rows carry the full Eater blurb, open hours / price and every stint on
the list; the hover card is deliberately just enough to identify a pin.

### 10. Keep it current

`scan --mode incremental` is the weekly job. It re-lists CDX *from the last
capture already indexed*, then anchors on `last_checked_ts` — the newest capture
already verified — and bisects only `[anchor, newest]`. If the list has not
moved, that terminates after a single probe.

→ A quiet week costs **0 page fetches**. Verified.

```cron
# Every Monday at 06:17
17 6 * * 1  cd ~/code/eater-list && make weekly >> data/weekly.log 2>&1
```

<details>
<summary>systemd timer equivalent</summary>

```ini
# /etc/systemd/system/eater38-scan.service
[Service]
Type=oneshot
WorkingDirectory=/srv/eater-list
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

</details>

Keep the database on persistent storage — it *is* the progress.

---

## Commands

| command | purpose |
|---|---|
| `init` | create the SQLite database |
| `seed` | import artefacts from the original exploratory crawl (optional) |
| `scan --mode full` | index every capture since `[crawl] since`, then bisect for changes |
| `scan --mode incremental` | the weekly job: only look past the last verified capture |
| `scan --strategy exhaustive` | fetch **every** capture — audits the bisect assumption |
| `status` | captures indexed, contents cached, staleness |
| `enrich` | re-extract fields an older extractor version did not capture |
| `build` | derive periods/changes, write `web/data/*` and `out/*` |
| `build --suggest-aliases` | propose renames for `aliases.json` |
| `geocode` | fill remaining coordinates from keyless public geocoders |

Configuration lives in `config.toml`: tracked URLs, rate limits, crawl strategy,
and the User-Agent. **Set a real contact in `[wayback] user_agent`** before large
crawls — that is what the Internet Archive asks of automated clients. A Nominatim
key is not required, but that placeholder is why Nominatim sits last.

## Layout

```
e38/wayback.py    CDX listing + capture download (retries, gzip, offline detection)
e38/ratelimit.py  token bucket, concurrency cap, jittered backoff
e38/extract.py    archived HTML → ordered entries (three page eras)
e38/identity.py   name normalisation + alias resolution
e38/store.py      SQLite: captures, contents, raw archive, geocode cache
e38/scan.py       bisect / exhaustive change detection
e38/build.py      periods, stints, updates → JSON / CSV / Markdown
e38/cli.py        command line
web/              static viewer + exported data
tests/            39 unit tests + a headless browser-demo check
explore/          the original throwaway scripts, kept for provenance
```

## Known limits

* **The archive starts 2017‑08‑05.** The first capture already holds 38 names, so
  anything added earlier is invisible. Earlier editions live at separate dated
  URLs (`…/the-38-essential-new-york-restaurants-january-12`, `…/april-14`, …)
  and would need their own crawl.
* **Bisect assumes the list only moves forward.** A place added and removed
  between two captures with matching endpoints is invisible. Use
  `scan --strategy exhaustive` to audit; in 2026 the archive is dense enough that
  the blind window is a day or less.
* **Change dates are bracketed, not exact** — the list is only observed when a
  capture is taken. Where the archive has a multi-month hole the bracket is wide.
* **Joe's of Avenue U has no coordinates**: it has no address anywhere in the
  archive, so there is nothing to geocode. The UI labels it "no pin" rather than
  hiding the gap.
* **Closure status is not tracked.** The list page never records closures — Eater
  removes a place rather than annotating it — so leaving the list is *not* a
  signal that a restaurant closed. (Gramercy Tavern and Cosme are both open and
  left the list in 2017–18.)
* **Alias curation is manual by design.**

## Tests

```bash
make test         # 39 unit tests (Python)
make test-demo    # 13 headless checks of web/index.html (needs node)
```

The unit tests cover the extractor against real gzipped captures from 2017 and
2026, name/alias resolution, the bisect search, period derivation, geocode-cache
durability and link splitting.

The demo check runs `web/index.html`'s real script against a stub DOM and Leaflet
using the real exported JSON, and drives its real event handlers: it asserts each
tab renders the right rows, that the range filter agrees with an independent
computation of the same window, that the slider defaults to the last year of
coverage, that clicking a row centres and zooms the map (while clicking the link
inside it does not), and that every update id resolves to a known place.
