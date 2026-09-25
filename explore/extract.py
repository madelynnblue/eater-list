#!/usr/bin/env python3
"""Extract the ordered restaurant list from an archived Eater 38 map page.

Reads HTML on stdin, writes one JSON object on stdout:
  {"source": "...", "n": <int>, "items": [{"position":1,"name":"...","slug":"...","url":"..."}]}

Tries, in order:
  1. JSON-LD ItemList (present in both old mapstack and modern duet layouts)
  2. HTML map cards (data-slug + h2 title)
  3. Loose JSON-LD Restaurant regex fallback
"""
import sys, json, re, html

LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def slug_from_url(url):
    if not url:
        return ""
    if "#" in url:
        return url.split("#", 1)[1].strip("/")
    return url.rstrip("/").split("/")[-1]


def walk_itemlist(obj):
    """Yield ItemList dicts found anywhere in a JSON-LD structure."""
    if isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, str) and t.lower() == "itemlist":
            yield obj
        for v in obj.values():
            yield from walk_itemlist(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_itemlist(v)


def clean_name(s):
    if s is None:
        return ""
    s = html.unescape(str(s))
    return re.sub(r"\s+", " ", s).strip()


def try_jsonld(doc):
    best = []
    for m in LD_RE.finditer(doc):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # some snapshots have trailing commas / stray control chars
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        for il in walk_itemlist(data):
            elems = il.get("itemListElement") or []
            items = []
            for e in elems:
                if not isinstance(e, dict):
                    continue
                it = e.get("item") if isinstance(e.get("item"), dict) else e
                name = clean_name(it.get("name"))
                url = it.get("url") or ""
                if name:
                    items.append(
                        {
                            "position": e.get("position") or len(items) + 1,
                            "name": name,
                            "slug": slug_from_url(url),
                            "url": url,
                        }
                    )
            if len(items) > len(best):
                best = items
    return best


CARD_RE = re.compile(
    r'<div[^>]*class="[^"]*duet--article--map-card[^"]*"[^>]*data-slug="([^"]+)"(.*?)(?=<div[^>]*class="[^"]*duet--article--map-card|$)',
    re.S,
)
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
SLUG_RE = re.compile(r'data-slug="([^"]+)"')


def try_cards(doc):
    items = []
    seen = set()
    # modern: card div carries data-slug, title in first h2 inside the card
    for m in CARD_RE.finditer(doc):
        slug = m.group(1)
        body = m.group(2)
        h = H2_RE.search(body)
        if not h:
            continue
        name = clean_name(re.sub(r"<[^>]+>", " ", h.group(1)))
        if name and slug not in seen:
            seen.add(slug)
            items.append({"position": len(items) + 1, "name": name, "slug": slug, "url": ""})
    if len(items) >= 5:
        return items

    # older mapstack layout: cards have data-slug and a title element
    items = []
    seen = set()
    for m in re.finditer(r'data-slug="([^"]+)"', doc):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        items.append({"position": len(items) + 1, "name": "", "slug": slug, "url": ""})
    if len(items) >= 5:
        return items
    return []


REST_RE = re.compile(
    r'"@type"\s*:\s*"Restaurant"\s*,\s*"url"\s*:\s*"([^"]*)"\s*,\s*"name"\s*:\s*"([^"]*)"',
    re.S,
)


def try_regex(doc):
    items = []
    for url, name in REST_RE.findall(doc):
        nm = clean_name(name)
        if nm:
            items.append(
                {
                    "position": len(items) + 1,
                    "name": nm,
                    "slug": slug_from_url(html.unescape(url)),
                    "url": html.unescape(url),
                }
            )
    return items


def main():
    raw = sys.stdin.buffer.read()
    doc = raw.decode("utf-8", "replace")
    for src, fn in (("jsonld", try_jsonld), ("cards", try_cards), ("regex", try_regex)):
        try:
            items = fn(doc)
        except Exception:
            items = []
        if len(items) >= 5:
            out = {"source": src, "n": len(items), "items": items}
            json.dump(out, sys.stdout, ensure_ascii=False)
            return
    json.dump({"source": "none", "n": 0, "items": []}, sys.stdout)
    sys.exit(3)


if __name__ == "__main__":
    main()
