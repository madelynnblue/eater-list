"""Derive the human-facing history and the web-facing JSON from cached content.

Runs on *observations* — the captures whose content has actually been fetched —
rather than on every capture. With the bisect strategy that is a few hundred
observations instead of ~700 fetches, and the resulting periods are identical
because a change is only ever reported between adjacent captures.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

from . import EXTRACTOR_VERSION
from .identity import entry_keys, load_aliases, normalize, resolver, slugify


def fmt(ts: str) -> str:
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if ts and len(ts) >= 8 else (ts or "?")


@dataclass
class Observation:
    ts: str
    digest: str
    keys: list[str]                 # alias-canonical, ordered, de-duplicated
    raw_keys: list[str]             # normalised only, for literal changelog
    names: dict = field(default_factory=dict)   # key -> display name
    raw_names: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)   # key -> item dict (address/coords)


class Builder:
    def __init__(self, cfg, store, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.aliases_raw = load_aliases(cfg.aliases_path)
        self.canonical = resolver(self.aliases_raw)

    # -- load --------------------------------------------------------------
    def observations(self, url: str) -> tuple[list[Observation], int]:
        rows = self.store.captures(url)
        obs: list[Observation] = []
        for row in rows:
            payload = self.store.get_content(url, row["digest"])
            if payload is None or not payload.get("items"):
                continue
            canonical_keys: list[str] = []
            names: dict[str, str] = {}
            items: dict[str, dict] = {}
            raw_keys: list[str] = []
            raw_names: dict[str, str] = {}
            for item in payload["items"]:
                name = item.get("name", "")
                raw = normalize(name)
                if raw and raw not in raw_keys:
                    raw_keys.append(raw)
                    raw_names[raw] = name
                key = self.canonical(name)
                if key and key not in canonical_keys:
                    canonical_keys.append(key)
                    names[key] = name
                    items[key] = item
            obs.append(
                Observation(
                    ts=row["ts"], digest=row["digest"], keys=canonical_keys,
                    raw_keys=raw_keys, names=names, raw_names=raw_names, items=items,
                )
            )
        obs.sort(key=lambda o: o.ts)
        return obs, len(rows)

    # -- derivation --------------------------------------------------------
    def runs(self, obs: list[Observation]) -> list[dict]:
        all_keys = {k for o in obs for k in o.keys}
        out: list[dict] = []
        last_index = len(obs) - 1
        for key in all_keys:
            i = 0
            while i < len(obs):
                if key in obs[i].keys:
                    j = i
                    while j + 1 < len(obs) and key in obs[j + 1].keys:
                        j += 1
                    out.append({"key": key, "i": i, "j": j,
                                "still_on": j == last_index})
                    i = j + 1
                else:
                    i += 1
        out.sort(key=lambda r: (obs[r["i"]].ts, r["key"]))
        return out

    def changes(self, obs: list[Observation]) -> list[dict]:
        out = []
        for a, b in zip(obs, obs[1:]):
            before, after = set(a.raw_keys), set(b.raw_keys)
            if before == after:
                continue
            added = [k for k in b.raw_keys if k in after - before]
            removed = [k for k in a.raw_keys if k in before - after]
            out.append(
                {
                    "after": a.ts,
                    "by": b.ts,
                    "added": [b.raw_names[k] for k in added],
                    "removed": [a.raw_names[k] for k in removed],
                    # stable place ids so a front-end can put these on a map
                    "added_ids": [slugify(self.canonical(k)) for k in added],
                    "removed_ids": [slugify(self.canonical(k)) for k in removed],
                }
            )
        return out

    def metadata_for(self, key: str, obs: list[Observation]) -> dict:
        """Best address/coordinates seen for a place (most recent wins)."""
        best: dict = {}
        for o in obs:
            item = o.items.get(key)
            if not item:
                continue
            merged = dict(best)
            for fld in ("address", "phone", "website", "eater_url", "url", "lat", "lng",
                        "open_for", "price", "drink", "tip", "good_for", "order", "blurb"):
                value = item.get(fld)
                if value not in (None, "", []):
                    merged[fld] = value
            best = merged
        return best

    @staticmethod
    def _split_links(meta: dict) -> tuple[str, str]:
        """Separate the restaurant's own site from Eater's anchor for the entry.

        Older extractor versions stored Eater's own map URL under a generic
        ``url`` key, which is not somewhere a "visit website" click should go.
        """
        website = meta.get("website") or ""
        eater_url = meta.get("eater_url") or ""
        legacy = meta.get("url") or ""
        if "eater.com" in website:
            eater_url = eater_url or website
            website = ""
        if not website and legacy and "eater.com" not in legacy:
            website = legacy
        if not eater_url and "eater.com" in legacy:
            eater_url = legacy
        return website, eater_url

    def build(self, url: str) -> dict:
        obs, indexed = self.observations(url)
        if not obs:
            raise SystemExit("no cached observations — run `e38 scan` first")

        runs = self.runs(obs)
        changes = self.changes(obs)
        names = {k: o.names[k] for o in obs for k in o.keys}
        aliases_seen: dict[str, set] = defaultdict(set)
        for o in obs:
            for key, name in o.names.items():
                aliases_seen[key].add(name)

        places: dict[str, dict] = {}
        for key in names:
            meta = self.metadata_for(key, obs)
            website, eater_url = self._split_links(meta)
            lat, lng = meta.get("lat"), meta.get("lng")
            address = meta.get("address", "")
            if lat is None and address:
                # Coordinates filled in by `e38 geocode` live in the cache, so
                # they survive rebuilds without re-querying the geocoder.
                hit = self.store.get_geo(address) if hasattr(self.store, "get_geo") else None
                if hit and hit.get("lat"):
                    lat, lng = hit["lat"], hit["lng"]
            places[key] = {
                "id": slugify(key),
                "name": names[key],
                "key": key,
                "names_seen": sorted(aliases_seen[key]),
                "address": meta.get("address", ""),
                "phone": meta.get("phone", ""),
                "website": website,
                "eater_url": eater_url,
                "blurb": meta.get("blurb", ""),
                "open_for": meta.get("open_for", ""),
                "price": meta.get("price", ""),
                "drink": meta.get("drink", ""),
                "tip": meta.get("tip", ""),
                "lat": lat,
                "lng": lng,
                "periods": [],
                "on_list_now": False,
            }

        for run in runs:
            place = places[run["key"]]
            a, b = obs[run["i"]], obs[run["j"]]
            add_before = obs[run["i"] - 1].ts if run["i"] > 0 else None
            rem_after = obs[run["j"] + 1].ts if run["j"] < len(obs) - 1 else None
            place["periods"].append(
                {
                    "from": a.ts,
                    "to": b.ts,
                    "from_date": fmt(a.ts),
                    "to_date": fmt(b.ts),
                    "added_between": [fmt(add_before), fmt(a.ts)] if add_before else None,
                    "removed_between": [fmt(b.ts), fmt(rem_after)] if rem_after else None,
                    "still_on": run["still_on"],
                }
            )
            place["on_list_now"] = place["on_list_now"] or run["still_on"]

        # capture index gaps give the resolution limit of each change
        index_ts = [r["ts"] for r in self.store.captures(url)]
        position = {ts: i for i, ts in enumerate(index_ts)}
        for change in changes:
            lo = position.get(change["after"])
            hi = position.get(change["by"])
            change["unsampled_captures"] = max((hi - lo - 1), 0) if lo is not None and hi is not None else None
            change["after_date"] = fmt(change["after"])
            change["by_date"] = fmt(change["by"])

        current = obs[-1]
        payload = {
            "url": url,
            "observations": obs,
            "indexed_captures": indexed,
            "runs": runs,
            "changes": changes,
            "places": places,
            "current": current,
            "index_ts": index_ts,
        }
        return payload

    # -- writers -----------------------------------------------------------
    def write(self, data: dict) -> dict:
        cfg = self.cfg
        os.makedirs(cfg.out_dir, exist_ok=True)
        os.makedirs(cfg.csv_dir, exist_ok=True)
        obs, places, changes = data["observations"], data["places"], data["changes"]
        current = data["current"]
        current_keys = [k for k in current.keys]

        place_list = sorted(places.values(), key=lambda p: p["name"].lower())
        with open(os.path.join(cfg.out_dir, "places.json"), "w", encoding="utf-8") as fh:
            json.dump(place_list, fh, ensure_ascii=False, indent=1)

        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lng"], p["lat"]]},
                "properties": {
                    "id": p["id"], "name": p["name"], "address": p["address"],
                    "on_list_now": p["on_list_now"],
                    "periods": [f"{x['from_date']}..{x['to_date']}" for x in p["periods"]],
                    "period_count": len(p["periods"]),
                },
            }
            for p in place_list
            if p.get("lat") is not None and p.get("lng") is not None
        ]
        with open(os.path.join(cfg.out_dir, "places.geojson"), "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": features}, fh,
                      ensure_ascii=False, indent=1)

        with open(os.path.join(cfg.out_dir, "updates.json"), "w", encoding="utf-8") as fh:
            json.dump(changes, fh, ensure_ascii=False, indent=1)

        with open(os.path.join(cfg.out_dir, "current.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "as_of": fmt(current.ts),
                    "as_of_ts": current.ts,
                    "source_url": data["url"],
                    "count": len(current_keys),
                    "entries": [
                        {
                            "position": i + 1,
                            "name": current.names[k],
                            "place_id": places[k]["id"],
                            "address": places[k]["address"],
                            "phone": places[k]["phone"],
                            "website": places[k]["website"],
                            "eater_url": places[k]["eater_url"],
                            "lat": places[k]["lat"],
                            "lng": places[k]["lng"],
                            "on_since": places[k]["periods"][-1]["from_date"] if places[k]["periods"] else None,
                        }
                        for i, k in enumerate(current_keys)
                    ],
                },
                fh,
                ensure_ascii=False,
                indent=1,
            )

        meta = {
            "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
            "source_url": data["url"],
            "extractor_version": EXTRACTOR_VERSION,
            "coverage": {
                "captures_indexed": data["indexed_captures"],
                "observations_used": len(obs),
                "first_observation": fmt(obs[0].ts),
                "last_observation": fmt(obs[-1].ts),
            },
            "counts": {
                "places": len(places),
                "stretches": len(data["runs"]),
                "changes": len(changes),
                "on_list_now": len(current_keys),
                "mapped": len(features),
            },
        }
        with open(os.path.join(cfg.out_dir, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)

        # ---- CSV ----
        with open(os.path.join(cfg.csv_dir, "eater38_places.csv"), "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["place", "stretches", "periods", "on_list_now", "lat", "lng", "address"])
            for p in place_list:
                periods = "; ".join(f"{x['from_date']} to {x['to_date']}" for x in p["periods"])
                writer.writerow([p["name"], len(p["periods"]), periods,
                                 "yes" if p["on_list_now"] else "no",
                                 p["lat"] if p["lat"] is not None else "",
                                 p["lng"] if p["lng"] is not None else "",
                                 p["address"]])

        with open(os.path.join(cfg.csv_dir, "eater38_history.csv"), "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["place", "on_list_from", "on_list_through", "added_between",
                             "removed_between", "still_on_list"])
            for run in data["runs"]:
                p = places[run["key"]]
                period = p["periods"][
                    [i for i, x in enumerate(p["periods"]) if x["from"] == obs[run["i"]].ts][0]
                ]
                writer.writerow([
                    p["name"], period["from_date"], period["to_date"],
                    " - ".join(period["added_between"]) if period["added_between"] else "before coverage",
                    " - ".join(period["removed_between"]) if period["removed_between"] else "still on list",
                    "yes" if period["still_on"] else "no",
                ])

        with open(os.path.join(cfg.csv_dir, "eater38_changelog.csv"), "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["change_after", "change_by", "unsampled_captures", "added", "removed"])
            for change in changes:
                writer.writerow([change["after_date"], change["by_date"],
                                 change["unsampled_captures"],
                                 "; ".join(change["added"]), "; ".join(change["removed"])])

        # ---- Markdown ----
        lines = [
            "# Eater NY \u201c38 Best Restaurants\u201d — membership history\n",
            f"Source: `{data['url']}`\n",
            f"- captures indexed: **{data['indexed_captures']}** "
            f"({fmt(data['index_ts'][0])} \u2192 {fmt(data['index_ts'][-1])})",
            f"- observations used to date the timeline: **{len(obs)}**",
            f"- distinct restaurants that have appeared: **{len(places)}**",
            f"- detected list changes: **{len(changes)}**",
            f"- list size at last observation: **{len(current_keys)}**\n",
            "| # | Restaurant | On list from | On list through | Added between | Status |",
            "|---|---|---|---|---|---|",
        ]
        n = 0
        for run in data["runs"]:
            p = places[run["key"]]
            period = next(x for x in p["periods"] if x["from"] == obs[run["i"]].ts)
            n += 1
            added = " - ".join(period["added_between"]) if period["added_between"] else "start of coverage"
            status = "on list" if period["still_on"] else "rotated out"
            lines.append(f"| {n} | {p['name']} | {period['from_date']} | {period['to_date']} | {added} | {status} |")
        lines.append("\n## Updates\n")
        for change in changes:
            lines.append(f"**after {change['after_date']} → by {change['by_date']}**")
            if change["added"]:
                lines.append(f"- added: {', '.join(change['added'])}")
            if change["removed"]:
                lines.append(f"- removed: {', '.join(change['removed'])}")
            lines.append("")
        with open(os.path.join(cfg.csv_dir, "eater38_history.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        return meta

    # -- alias curation ----------------------------------------------------
    def suggest_aliases(self, data: dict) -> list[tuple[str, str]]:
        """Guess renames: a removed name and an added name in the same update."""
        out = []
        for change in data["changes"]:
            if len(change["removed"]) == 1 and len(change["added"]) == 1:
                old, new = change["removed"][0], change["added"][0]
                a, b = normalize(old), normalize(new)
                if a == b:
                    continue
                if a in b or b in a or len(set(a.split()) & set(b.split())) >= 2:
                    out.append((old, new))
            elif change["removed"] and change["added"]:
                for old in change["removed"]:
                    for new in change["added"]:
                        a, b = normalize(old), normalize(new)
                        if a and b and (a in b or b in a) and abs(len(a) - len(b)) <= 12:
                            out.append((old, new))
        return out
