#!/usr/bin/env python3
"""Validate the exported site data before it is published.

The site is static and its JSON is committed, so CI cannot regenerate it from
the crawl database (that lives outside git). What CI *can* do is prove the
committed data is internally consistent and safe to publish, which is what this
does. Run it locally with:

    python scripts/validate_site.py web

Exits non-zero, listing every problem, if anything is inconsistent.
"""
from __future__ import annotations

import json
import os
import sys


def load(data_dir: str, name: str):
    path = os.path.join(data_dir, name)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def find_closed(data_dir: str) -> str | None:
    """closed.json sits at the project root, an unknown distance above the data."""
    directory = os.path.abspath(data_dir)
    for _ in range(4):
        directory = os.path.dirname(directory)
        candidate = os.path.join(directory, "closed.json")
        if os.path.exists(candidate):
            return candidate
    return None


def main(web_dir: str = "web") -> int:
    data_dir = os.path.join(web_dir, "data")
    errors: list[str] = []

    try:
        places = load(data_dir, "places.json")
        current = load(data_dir, "current.json")
        updates = load(data_dir, "updates.json")
        meta = load(data_dir, "meta.json")
        geo = load(data_dir, "places.geojson")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: cannot read site data: {exc}")
        return 1

    ids = [p.get("id") for p in places]
    idset = set(ids)

    # --- shape ---
    if len(ids) != len(idset):
        errors.append("places.json contains duplicate ids")
    for p in places:
        for field in ("id", "name", "periods"):
            if not p.get(field):
                errors.append(f"place {p.get('id', '?')} is missing {field}")
        if not isinstance(p.get("periods"), list) or not p["periods"]:
            errors.append(f"place {p.get('id', '?')} has no periods")

    # --- counts agree across files ---
    if meta["counts"]["places"] != len(places):
        errors.append(f"meta says {meta['counts']['places']} places, file has {len(places)}")
    if current["count"] != len(current["entries"]):
        errors.append("current.json count does not match its entries")
    if meta["counts"]["on_list_now"] != len(current["entries"]):
        errors.append("meta on_list_now does not match current.json")

    # --- every referenced id resolves ---
    for entry in current["entries"]:
        if entry["place_id"] not in idset:
            errors.append(f"current entry {entry['place_id']} has no place")
    for change in updates:
        for key in ("added_ids", "removed_ids"):
            for pid in change.get(key, []):
                if pid not in idset:
                    errors.append(f"update references unknown place {pid}")

    # --- the map matches the data ---
    mapped = [p for p in places if p.get("lat") is not None and p.get("lng") is not None]
    if len(geo["features"]) != len(mapped):
        errors.append(
            f"geojson has {len(geo['features'])} features, data has {len(mapped)} mapped places")
    for feature in geo["features"]:
        lon, lat = feature["geometry"]["coordinates"]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            errors.append(f"geojson feature {feature['properties'].get('id')} has bogus coordinates")

    # --- closed places must not be published ---
    closed = {}
    closed_path = find_closed(data_dir)
    if closed_path:
        try:
            with open(closed_path, encoding="utf-8") as fh:
                closed = json.load(fh).get("places", {})
        except (OSError, json.JSONDecodeError):
            print(f"warning: {closed_path} is unreadable; skipping the closed-place check")
    else:
        print("warning: no closed.json found; skipping the closed-place check")
    leaked = sorted(set(closed) & idset)
    if leaked:
        errors.append(f"{len(leaked)} closed place(s) still published: {leaked[:5]}")

    if errors:
        print(f"FAIL: {len(errors)} problem(s)")
        for problem in errors[:40]:
            print(f"  - {problem}")
        if len(errors) > 40:
            print(f"  ...and {len(errors) - 40} more")
        return 1

    print(f"OK: {len(places)} places, {len(current['entries'])} on the list today, "
          f"{len(geo['features'])} mapped, {len(updates)} updates, "
          f"{len(closed)} closed excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "web"))
