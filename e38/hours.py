"""Fetch and parse real opening hours from each restaurant's own website.

Two extraction strategies, in order of reliability:

1. **schema.org JSON-LD** — ``openingHoursSpecification`` (structured days and
   times) or ``openingHours`` (strings like ``Mo-Fr 11:00-22:00``). Precise when
   present, and the only form worth trusting.
2. **Visible text** — a conservative day-range + time-range pattern, which
   catches plain "Mon-Fri 11am-10pm" markup but will miss hours rendered by
   JavaScript or baked into an image. Those are recorded as no-hours-found
   rather than guessed.

Everything is cached in SQLite keyed by place id, so a re-run costs nothing and
a parse improvement can be re-applied without refetching. Sites that are social
profiles (Instagram, Facebook) are skipped: they are not fetchable and never
carry hours in markup.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import urllib.error
import urllib.request

from .ratelimit import RateLimiter

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_DAY_WORDS = {
    "mo": "mon", "mon": "mon", "monday": "mon", "mondays": "mon",
    "tu": "tue", "tue": "tue", "tues": "tue", "tuesday": "tue", "tuesdays": "tue",
    "we": "wed", "wed": "wed", "weds": "wed", "wednesday": "wed", "wednesdays": "wed",
    "th": "thu", "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu", "thursdays": "thu",
    "fr": "fri", "fri": "fri", "friday": "fri", "fridays": "fri",
    "sa": "sat", "sat": "sat", "saturday": "sat", "saturdays": "sat",
    "su": "sun", "sun": "sun", "sunday": "sun", "sundays": "sun",
}

_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_SOCIAL_RE = re.compile(r"instagram\.com|facebook\.com|twitter\.com|linktr\.ee|tiktok\.com", re.I)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_LINK_RE = re.compile(r'<a[^>]+href="([^"#?]+)"[^>]*>(.*?)</a>', re.S | re.I)
_HOURS_LINK_RE = re.compile(r"hour|location|visit|contact|find us", re.I)
_TEXT_RE = re.compile(r"<[^>]+>")

# "Mon-Fri 11:00 am - 10:00 pm", "Tuesday: 5pm-11pm", "Sat 12–23"
_TIME = r"(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"
_DAYRANGE = (r"(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun"
             r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?")
_TEXT_HOURS_RE = re.compile(
    _DAYRANGE + r"\s*(?:[-–—]|through|to)\s*" + _DAYRANGE + r"\s*:?\s*"
    + _TIME + r"\s*(?:[-–—]|to)\s*" + _TIME, re.I)
_SINGLE_HOURS_RE = re.compile(
    _DAYRANGE + r"\s*:?\s*" + _TIME + r"\s*(?:[-–—]|to)\s*" + _TIME, re.I)


def _norm_time(value: str) -> str | None:
    """'5pm' / '17:00' / '11:30 am' -> 'HH:MM' on a 24-hour clock."""
    if not value:
        return None
    v = value.strip().lower().replace(".", "").replace(" ", "")
    pm = "pm" in v
    am = "am" in v
    v = v.replace("am", "").replace("pm", "")
    if ":" in v:
        hh, mm = v.split(":", 1)
    else:
        hh, mm = v, "00"
    try:
        hour = int(hh)
        minute = int(mm)
    except ValueError:
        return None
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    if pm and hour < 12:
        hour += 12
    if am and hour == 12:
        hour = 0
    if hour == 24:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _add(hours: dict, day: str, span: list[str]) -> None:
    day = _DAY_WORDS.get(day.lower())
    if not day or len(span) != 2 or not all(span):
        return
    hours.setdefault(day, [])
    if span not in hours[day]:
        hours[day].append(span)


def _expand_days(start: str, end: str) -> list[str]:
    a, b = _DAY_WORDS.get(start.lower()), _DAY_WORDS.get(end.lower())
    if not a or not b:
        return []
    i, j = DAYS.index(a), DAYS.index(b)
    if i <= j:
        return DAYS[i:j + 1]
    return DAYS[i:] + DAYS[:j + 1]          # wraps, e.g. Sat-Tue


def _walk_jsonld(node, out: dict):
    if isinstance(node, dict):
        spec = node.get("openingHoursSpecification")
        if spec:
            for entry in (spec if isinstance(spec, list) else [spec]):
                if not isinstance(entry, dict):
                    continue
                days = entry.get("dayOfWeek") or []
                days = days if isinstance(days, list) else [days]
                span = [_norm_time(str(entry.get("opens", ""))),
                        _norm_time(str(entry.get("closes", "")))]
                for day in days:
                    key = str(day).split("/")[-1].strip()
                    _add(out, key, span)
        raw = node.get("openingHours")
        if raw:
            for line in (raw if isinstance(raw, list) else [raw]):
                for start, end, opens, closes in _TEXT_HOURS_RE.findall(str(line)):
                    span = [_norm_time(opens), _norm_time(closes)]
                    for day in _expand_days(start, end):
                        _add(out, day, span)
        for value in node.values():
            _walk_jsonld(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_jsonld(value, out)


def parse_hours(doc: str) -> tuple[dict, str]:
    """Return ({day: [[open, close]], ...}, source) from a page's HTML."""
    hours: dict = {}
    for block in _LD_RE.findall(doc):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        _walk_jsonld(data, hours)
    if hours:
        return _sorted(hours), "jsonld"

    text = _TEXT_RE.sub(" ", _TAG_RE.sub(" ", doc))
    text = html_mod.unescape(re.sub(r"\s+", " ", text))
    for start, end, opens, closes in _TEXT_HOURS_RE.findall(text):
        span = [_norm_time(opens), _norm_time(closes)]
        for day in _expand_days(start, end):
            _add(hours, day, span)
    # Run the single-day pass too: "Mon-Fri 11-10, Sat 12-11" would otherwise
    # lose Saturday, because the range pattern matches the first clause only.
    for day, opens, closes in _SINGLE_HOURS_RE.findall(text):
        _add(hours, day, [_norm_time(opens), _norm_time(closes)])
    return (_sorted(hours), "text") if hours else ({}, "none")


def hours_links(doc: str, base: str, cap: int = 3) -> list[str]:
    """Internal links likely to lead to an hours page.

    Most restaurant sites keep hours on a sub-page ("Hours & Location"), so the
    homepage alone is not enough; follow the site's own navigation instead of
    guessing paths.
    """
    base = base.rstrip("/")
    out, seen = [], set()
    for href, label in _LINK_RE.findall(doc):
        label = _TEXT_RE.sub(" ", label)
        if not _HOURS_LINK_RE.search(f"{label} {href}"):
            continue
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = base + href
        else:
            continue
        if href.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp")):
            continue
        url = url.rstrip("/")
        if url in seen or url == base:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= cap:
            break
    return out


def _sorted(hours: dict) -> dict:
    return {d: sorted(hours[d]) for d in DAYS if d in hours}


# Meal windows used for the sidebar filter, as [start, end) on a 24h clock.
WINDOWS = {
    "breakfast": ("05:00", "11:00"),
    "lunch": ("11:00", "15:00"),
    "dinner": ("17:00", "22:00"),
    "late": ("22:00", "26:00"),
}


def _overlaps(span, window) -> bool:
    def mins(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    start, end = span
    if not start or not end:
        return False
    s, e = mins(start), mins(end)
    if e <= s:                      # closes after midnight
        e += 24 * 60
    return s < mins(window[1]) and e > mins(window[0])


def meal_periods(hours: dict | None) -> list[str]:
    """Which meal windows a place's real hours cover, on any day."""
    if not hours:
        return []
    out = []
    spans = [span for day in DAYS for span in hours.get(day, [])]
    for name, window in WINDOWS.items():
        if any(_overlaps(span, window) for span in spans):
            out.append(name)
    return out


_TEXT_PERIODS = {
    "breakfast": ("breakfast",),
    "brunch": ("brunch",),
    "lunch": ("lunch", "midday"),
    "dinner": ("dinner", "supper"),
    "late": ("late night", "late-night", "after hours"),
}


def periods_from_text(text: str) -> list[str]:
    """Fall back to Eater's "Open for: ..." copy when no hours were found."""
    blob = (text or "").lower()
    return [name for name, keys in _TEXT_PERIODS.items()
            if any(k in blob for k in keys)]


class HoursFetcher:
    def __init__(self, cfg, store, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.limiter = RateLimiter(1.5, 2, 2)      # be a good guest on small sites

    def _get(self, url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with self.limiter.slot():
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read(600_000)
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    import gzip, io
                    body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                return resp.status, body.decode("utf-8", "replace")

    def fetch(self, place: dict) -> dict:
        """Fetch one place's hours. Returns the row written to the store."""
        url = (place.get("website") or "").strip()
        if not url or _SOCIAL_RE.search(url):
            self.store.put_hours(place["id"], url, None, "skipped", None, "")
            return {"status": "skipped", "hours": None}

        base = url.rstrip("/")
        status, doc = self._try(url)
        if status == 200 and doc:
            hours, source = parse_hours(doc)
            if hours:
                self.store.put_hours(place["id"], url, status, source, hours, "")
                return {"status": "found", "hours": hours, "source": source}
            candidates = hours_links(doc, base) + [f"{base}/hours", f"{base}/contact"]
        else:
            candidates = [f"{base}/hours", f"{base}/contact"]

        tried = {url}
        for candidate in candidates:
            if candidate in tried:
                continue
            tried.add(candidate)
            sub_status, sub_doc = self._try(candidate)
            if sub_status == 200 and sub_doc:
                hours, source = parse_hours(sub_doc)
                if hours:
                    self.store.put_hours(place["id"], candidate, sub_status, source, hours, "")
                    return {"status": "found", "hours": hours, "source": source}
        self.store.put_hours(place["id"], url, status, "none", None, "")
        return {"status": "none", "hours": None}

    def _try(self, url: str) -> tuple[int | None, str]:
        try:
            return self._get(url)
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except Exception:  # noqa: BLE001 - network, TLS, DNS
            return None, ""

    def run(self, places: list[dict], limit: int, refresh: bool = False) -> dict:
        tally = {"found": 0, "none": 0, "skipped": 0, "cached": 0}
        todo = []
        for place in places:
            row = self.store.get_hours(place["id"])
            if row and not refresh and row["source"] not in (None, "none"):
                tally["cached"] += 1
                continue
            todo.append(place)
        self.log(f"{len(todo)} places to fetch ({len(places) - len(todo)} already cached)")
        for n, place in enumerate(todo, 1):
            if n > limit:
                self.log(f"limit {limit} reached; re-run to continue")
                break
            result = self.fetch(place)
            tally[result["status"]] = tally.get(result["status"], 0) + 1
            if n % 10 == 0:
                self.log(f"  {n}/{min(len(todo), limit)}  found={tally['found']} "
                         f"none={tally['none']} skipped={tally['skipped']}")
        return tally
