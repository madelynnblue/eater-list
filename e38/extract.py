"""Turn an archived Eater map page into a structured, ordered list of entries.

Eater has shipped three page shapes since 2017, so the extractor tries them in
order of reliability:

1. ``__NEXT_DATA__``  — modern Next.js pages embed the full ordered map point
   list, including address, latitude/longitude, phone and per-entry copy.
2. JSON-LD ``ItemList`` — every era carries schema.org markup with the ordered
   restaurant names and anchors.
3. ``section.c-mapstack__card`` — the original 2017-era markup, which has no
   embedded JSON but does carry addresses.

The result is deliberately a plain dict so it can be stored as JSON verbatim.
"""
from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any

from . import EXTRACTOR_VERSION

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I
)
_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_CARD_TAG_RE = re.compile(r'<section[^>]*class="[^"]*c-mapstack__card[^"]*"[^>]*>', re.I)
_SLUG_ATTR_RE = re.compile(r'data-slug="([^"]+)"')
_HEADING_RE = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.S | re.I)
_INDEX_SPAN_RE = re.compile(r'<span[^>]*c-mapstack__card-index[^>]*>.*?</span>', re.S | re.I)
_ADDRESS_RE = re.compile(r'<div[^>]*class="[^"]*c-mapstack__address[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_VISIT_SITE_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>\s*Visit website\s*</a>', re.I
)
# The 2017-era cards carry a "Directions" link whose query holds real coordinates.
_DIRECTIONS_RE = re.compile(r"google\.com/maps\?q=(-?\d+\.\d+),(-?\d+\.\d+)")
_RESTAURANT_RE = re.compile(
    r'"@type"\s*:\s*"Restaurant"\s*,\s*"url"\s*:\s*"([^"]*)"\s*,\s*"name"\s*:\s*"([^"]*)"', re.S
)

# Fields Eater prints as bold labels inside an entry's copy.
_INFO_LABELS = {
    "open for": "open_for",
    "price range": "price",
    "drink up": "drink",
    "insider tip": "tip",
    "good for": "good_for",
    "order": "order",
    "address": "address_text",
}


def clean(text: Any) -> str:
    if text is None:
        return ""
    text = html_mod.unescape(str(text))
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def slug_from_url(url: str) -> str:
    if not url:
        return ""
    if "#" in url:
        return url.split("#", 1)[1].strip("/")
    return url.rstrip("/").split("/")[-1]


# --------------------------------------------------------------------------
# 1. modern __NEXT_DATA__
# --------------------------------------------------------------------------
def next_data(doc: str) -> dict | None:
    match = _NEXT_DATA_RE.search(doc)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def find_map_points(node: Any, best: list | None = None) -> list | None:
    """Find the longest list of dicts that look like map entries."""
    if isinstance(node, dict):
        for value in node.values():
            best = find_map_points(value, best)
    elif isinstance(node, list):
        if len(node) >= 5 and all(
            isinstance(x, dict) and "name" in x and ("location" in x or "address" in x)
            for x in node[:5]
        ):
            if best is None or len(node) > len(best):
                best = node
        for value in node:
            best = find_map_points(value, best)
    return best


def parse_description(blocks: Any) -> dict:
    """Pull the labelled fields out of an entry's copy."""
    fields: dict[str, str] = {}
    texts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("plaintext"):
                texts.append(block["plaintext"])
    for text in texts:
        matched = False
        for label, key in _INFO_LABELS.items():
            prefix = f"{label}:"
            if text.lower().startswith(prefix):
                fields[key] = text[len(prefix):].strip()
                matched = True
                break
        if not matched:
            fields.setdefault("blurb", text)
    return fields


def items_from_map_points(points: list) -> list[dict]:
    items = []
    for index, point in enumerate(points, 1):
        location = point.get("location") or {}
        venue = point.get("venue") or {}
        fields = parse_description(point.get("description"))
        items.append(
            {
                "position": index,
                "name": clean(point.get("name")),
                "slug": (venue.get("slug") or "").strip(),
                "website": point.get("url") or "",
                "eater_url": "",
                "address": clean(point.get("address")),
                "lat": location.get("latitude"),
                "lng": location.get("longitude"),
                "phone": clean(point.get("phone")),
                **fields,
            }
        )
    return items


# --------------------------------------------------------------------------
# 2. JSON-LD ItemList
# --------------------------------------------------------------------------
def _walk_itemlists(node: Any):
    if isinstance(node, dict):
        if str(node.get("@type", "")).lower() == "itemlist":
            yield node
        for value in node.values():
            yield from _walk_itemlists(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_itemlists(value)


def items_from_jsonld(doc: str) -> list[dict]:
    best: list[dict] = []
    for match in _LD_RE.finditer(doc):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        for item_list in _walk_itemlists(data):
            items = []
            for element in item_list.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                item = element.get("item") if isinstance(element.get("item"), dict) else element
                name = clean(item.get("name"))
                if not name:
                    continue
                url = item.get("url") or ""
                items.append(
                    {
                        "position": element.get("position") or len(items) + 1,
                        "name": name,
                        "slug": slug_from_url(url),
                        "website": "",
                        "eater_url": html_mod.unescape(url),
                        "address": "",
                        "lat": None,
                        "lng": None,
                        "phone": "",
                    }
                )
            if len(items) > len(best):
                best = items
    return best


# --------------------------------------------------------------------------
# 3. original mapstack markup
# --------------------------------------------------------------------------
def items_from_mapstack(doc: str) -> list[dict]:
    # NB: split() would discard the opening tag, and the data-slug lives on it,
    # so the tag is captured and each card is the slice up to the next tag.
    tags = list(_CARD_TAG_RE.finditer(doc))
    items = []
    for position, tag in enumerate(tags):
        end = tags[position + 1].start() if position + 1 < len(tags) else len(doc)
        chunk = doc[tag.end():end]
        slug_match = _SLUG_ATTR_RE.search(tag.group(0))
        slug = slug_match.group(1) if slug_match else ""
        if not slug or slug == "intro":
            continue
        heading = _HEADING_RE.search(chunk)
        name = clean(_INDEX_SPAN_RE.sub(" ", heading.group(1))) if heading else ""
        if not name:
            continue
        addresses = [clean(a) for a in _ADDRESS_RE.findall(chunk)]
        address = addresses[0] if addresses else ""
        phone = ""
        for value in addresses[1:]:
            if re.search(r"\d{3}", value) and "@" not in value:
                phone = value
                break
        site = _VISIT_SITE_RE.search(chunk)
        point = _DIRECTIONS_RE.search(chunk)
        items.append(
            {
                "position": len(items) + 1,
                "name": name,
                "slug": slug,
                "website": html_mod.unescape(site.group(1)) if site else "",
                "eater_url": "",
                "address": address,
                "lat": float(point.group(1)) if point else None,
                "lng": float(point.group(2)) if point else None,
                "phone": phone,
            }
        )
    return items


# --------------------------------------------------------------------------
_MERGE_FIELDS = ("address", "phone", "website", "eater_url", "url", "lat", "lng",
                 "open_for", "price", "drink", "tip", "good_for", "order")


def _merge_items(primary: list[dict], others: list[list[dict]]) -> list[dict]:
    """Fill empty fields on the primary list from the other representations.

    E.g. the 2017-era JSON-LD carries the ordered names but only the mapstack
    markup carries street addresses, so the two are joined on slug, then name.
    """
    by_slug: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for items in others:
        for item in items:
            if item.get("slug"):
                by_slug.setdefault(item["slug"], item)
            if item.get("name"):
                by_name.setdefault(normalise_key(item["name"]), item)

    for item in primary:
        donor = by_slug.get(item.get("slug") or "") or by_name.get(
            normalise_key(item.get("name", ""))
        )
        if not donor:
            continue
        for fld in _MERGE_FIELDS:
            if item.get(fld) in (None, "", []) and donor.get(fld) not in (None, "", []):
                item[fld] = donor[fld]
    return primary


def normalise_key(name: str) -> str:
    return _WS_RE.sub(" ", re.sub(r"[^a-z0-9]+", " ", (name or "").lower())).strip()


def extract(doc: str, min_items: int = 5) -> dict:
    """Extract the ordered entry list from one archived page."""
    candidates: list[tuple[str, list[dict]]] = []

    data = next_data(doc)
    if data:
        points = find_map_points(data)
        if points:
            candidates.append(("next_data", items_from_map_points(points)))

    candidates.append(("jsonld", items_from_jsonld(doc)))
    candidates.append(("mapstack", items_from_mapstack(doc)))

    # Highest-fidelity source that yields a plausible list, in priority order.
    for source, items in candidates:
        if len(items) >= min_items:
            others = [other for name, other in candidates if name != source]
            return {
                "extractor_version": EXTRACTOR_VERSION,
                "source": source,
                "n": len(items),
                "items": _merge_items(items, others),
            }

    best = max(candidates, key=lambda c: len(c[1]), default=("none", []))
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "source": "none" if not best[1] else best[0],
        "n": len(best[1]),
        "items": best[1],
    }
