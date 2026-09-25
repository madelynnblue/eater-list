"""Command line interface: ``python -m e38 <command>``."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import EXTRACTOR_VERSION
from .build import Builder, fmt
from .config import load as load_config
from .extract import extract
from .scan import Scanner
from .store import Store
from .wayback import CaptureRow, Wayback


# --------------------------------------------------------------------------
def cmd_init(args, cfg):
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    store.close()
    print(f"database ready: {cfg.db_path}")


def cmd_seed(args, cfg):
    """Import the exploratory crawl's artefacts so nothing is re-downloaded."""
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    legacy = args.legacy_dir or os.path.join(cfg.root, "data")
    ts_to_url: dict[str, str] = {}
    total = 0
    for name in sorted(os.listdir(legacy)):
        if not (name.startswith("cdx_") and name.endswith(".txt")):
            continue
        path = os.path.join(legacy, name)
        rows = []
        for line in open(path, encoding="utf-8", errors="replace"):
            if line.startswith("<"):
                print(f"  {name}: looks like an error page, skipped")
                break
            parts = line.split()
            if len(parts) < 5:
                continue
            ts, url, status, mimetype, digest = parts[0], parts[1], parts[2], parts[3], parts[4]
            length = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
            ts_to_url[ts] = url
            rows.append(CaptureRow(url=url, ts=ts, status=status,
                                   mimetype=mimetype, digest=digest, length=length))
        if rows:
            store.upsert_captures(rows)
            total += len(rows)
            print(f"  {name}: {len(rows)} captures")
    print(f"indexed {total} captures")

    payloads = os.path.join(legacy, "captures.jsonl")
    seeded = 0
    unattributed = 0
    if os.path.exists(payloads):
        for line in open(payloads, encoding="utf-8"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("items"):
                continue
            # Only import rows whose capture we can attribute to a known URL.
            url = ts_to_url.get(row["ts"])
            if not url:
                unattributed += 1
                continue
            effective = row.get("eff_ts") or row["ts"]
            digest = store.digest_for(url, effective) or row.get("digest") or row["ts"]
            payload = {
                "extractor_version": 1,
                "source": row.get("source", "jsonld"),
                "n": row.get("n", len(row["items"])),
                "items": row["items"],
                "fetched_for_ts": row["ts"],
            }
            store.put_content(url, digest, payload, 200, effective)
            if digest != row.get("digest") and row.get("digest"):
                store.put_content(url, row["digest"], payload, 200, effective)
            seeded += 1
        print(f"seeded {seeded} cached extractions (extractor v1)")
    if unattributed:
        print(f"skipped {unattributed} payload(s) from URLs with no capture index")
    store.close()


def cmd_scan(args, cfg):
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    scanner = Scanner(cfg, store)
    urls = [args.url] if args.url else cfg.urls
    started = time.time()
    for url in urls:
        try:
            report = scanner.scan_url(url, mode=args.mode, strategy=args.strategy)
        except KeyboardInterrupt:
            print("\ninterrupted — progress is cached, re-run to continue")
            break
        stats = store.stats(url)
        print(f"\n{url}")
        print(f"  new captures indexed : {report.new_captures}")
        print(f"  page fetches         : {report.fetches}  (cache hits: {report.cache_hits})")
        print(f"  changes detected     : {len(report.changes)}")
        print(f"  captures indexed     : {stats['captures']} "
              f"({fmt(stats['first'])} .. {fmt(stats['last'])})")
        print(f"  contents cached      : {stats['contents_cached']}/{stats['distinct_digests']} digests")
        if not report.complete:
            print(f"  INCOMPLETE: {report.note}")
    print(f"\nelapsed {time.time() - started:.1f}s")
    store.close()


def cmd_status(args, cfg):
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    for url in cfg.urls:
        stats = store.stats(url)
        if not stats["captures"]:
            continue
        checked = store.get_meta(f"last_checked_ts:{url}", "-")
        print(f"{url}")
        print(f"  captures indexed   : {stats['captures']}  ({fmt(stats['first'])} .. {fmt(stats['last'])})")
        print(f"  distinct digests   : {stats['distinct_digests']}")
        print(f"  contents cached    : {stats['contents_cached']}")
        print(f"  contents missing   : {stats['contents_missing']}")
        print(f"  last checked ts    : {checked}")
        stale = store.stale_contents(url, EXTRACTOR_VERSION)
        if stale:
            print(f"  older extractor    : {stale} (run `e38 enrich`)")
    store.close()


def cmd_build(args, cfg):
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    builder = Builder(cfg, store)
    url = args.url or (cfg.urls[0] if cfg.urls else None)
    if not url:
        raise SystemExit("no URL configured")
    data = builder.build(url)
    meta = builder.write(data)
    print(json.dumps(meta, indent=1))
    if args.suggest_aliases:
        print("\nCandidate renames (add to aliases.json after review):")
        for old, new in builder.suggest_aliases(data):
            print(f'  "{old}"  ->  "{new}"')
    store.close()


def cmd_enrich(args, cfg):
    """Backfill address/coordinate fields the old extractor did not keep."""
    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    builder = Builder(cfg, store)
    scanner = Scanner(cfg, store)
    url = args.url or (cfg.urls[0] if cfg.urls else None)
    data = builder.build(url)
    obs = data["observations"]

    wanted: dict[str, str] = {}   # place key -> capture ts to fetch
    for key, place in data["places"].items():
        if (place.get("lat") is not None and place.get("address")
                and place.get("website") and place.get("blurb")):
            continue
        for o in reversed(obs):
            if key in o.items and o.ts >= (args.min_ts or ""):
                wanted[key] = o.ts
                break

    targets = sorted(set(wanted.values()))
    print(f"{len(data['places'])} places, {len(wanted)} need enrichment, "
          f"{len(targets)} captures to fetch (limit {args.limit})")
    rows = {r["ts"]: r for r in store.captures(url)}
    fetched = 0
    for ts in targets:
        if fetched >= args.limit:
            print("limit reached; re-run to continue")
            break
        row = rows.get(ts)
        if not row:
            continue
        before = scanner._fetches
        try:
            scanner.content(url, ts, row["digest"], min_version=EXTRACTOR_VERSION)
            if scanner._fetches > before:
                fetched += 1
                if fetched % 10 == 0:
                    print(f"  fetched {fetched}/{len(targets)}")
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            print(f"  {ts}: {exc}")
    print(f"fetched {fetched} captures")

    data = builder.build(url)
    builder.write(data)
    mapped = sum(1 for p in data["places"].values() if p.get("lat") is not None)
    print(f"{mapped}/{len(data['places'])} places now have coordinates")
    store.close()


def _http_json(url, user_agent, timeout=25):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _geo_nyc(address, user_agent):
    """NYC Planning Labs GeoSearch — purpose-built for New York addresses."""
    import urllib.parse
    q = urllib.parse.urlencode({"text": address, "size": 1})
    data = _http_json(f"https://geosearch.planninglabs.nyc/v2/search?{q}", user_agent)
    features = data.get("features") or []
    if not features:
        return None
    lon, lat = features[0]["geometry"]["coordinates"][:2]
    return float(lat), float(lon)


def _geo_census(address, user_agent):
    """US Census one-line geocoder — federal, keyless, US-wide."""
    import urllib.parse
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current",
                                "format": "json"})
    data = _http_json(
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + q, user_agent)
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    c = matches[0]["coordinates"]
    return float(c["y"]), float(c["x"])


def _geo_nominatim(address, user_agent):
    """OpenStreetMap Nominatim — best coverage outside NYC, but it rejects
    User-Agents it does not consider properly identifying."""
    import urllib.parse
    q = urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    data = _http_json(f"https://nominatim.openstreetmap.org/search?{q}", user_agent)
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


# Tried in order; the first that resolves the address wins. Nominatim is last
# because it will 403 the placeholder contact that ships in config.toml.
GEOCODERS = [("nyc-geosearch", _geo_nyc), ("us-census", _geo_census),
             ("nominatim", _geo_nominatim)]


def cmd_geocode(args, cfg):
    """Fill coordinates for places the archived pages never geotagged.

    Keyless public geocoders, one request per second, results cached in SQLite
    so repeat runs and rebuilds are free. The User-Agent is exactly whatever
    config.toml specifies.
    """
    from .ratelimit import RateLimiter

    store = Store(cfg.db_path, cfg.raw_dir if cfg.store_raw else None)
    builder = Builder(cfg, store)
    url = args.url or (cfg.urls[0] if cfg.urls else None)
    data = builder.build(url)
    limiter = RateLimiter(1.0, 1, 1)

    providers = GEOCODERS
    if args.provider:
        providers = [p for p in GEOCODERS if p[0] == args.provider]
        if not providers:
            raise SystemExit(f"unknown provider {args.provider!r}")

    todo = [p for p in data["places"].values()
            if p.get("lat") is None and p.get("address")]
    print(f"{len(todo)} places to geocode via {' -> '.join(n for n, _ in providers)} "
          f"(1 request/second, cached)")

    done = skipped = failed = 0
    tally: dict[str, int] = {}
    for place in todo:
        if done + skipped >= args.limit:
            print("limit reached; re-run to continue")
            break
        if store.get_geo(place["address"]):
            skipped += 1
            continue
        resolved = None
        for name, fn in providers:
            try:
                with limiter.slot():
                    resolved = fn(place["address"], cfg.user_agent)
            except Exception as exc:  # noqa: BLE001 - try the next provider
                print(f"  ! {name} on {place['name']}: {type(exc).__name__}: {exc}")
                limiter.penalize(3)
                continue
            if resolved:
                tally[name] = tally.get(name, 0) + 1
                store.put_geo(place["address"], resolved[0], resolved[1], name)
                break
        if resolved is None:
            store.put_geo(place["address"], 0.0, 0.0, "notfound")
            failed += 1
        done += 1
        if done % 10 == 0:
            print(f"  {done} looked up ({skipped} cached, {failed} unresolved)")

    if tally:
        print("resolved by: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"{done} looked up, {skipped} already cached, {failed} unresolved")

    data = builder.build(url)      # build() now reads the geocache itself
    builder.write(data)
    mapped = sum(1 for p in data["places"].values() if p.get("lat") is not None)
    print(f"{mapped}/{len(data['places'])} places have coordinates")
    store.close()


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="e38", description="Reconstruct the Eater 38 list history from the Wayback Machine."
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the SQLite database").set_defaults(func=cmd_init)

    seed = sub.add_parser("seed", help="import artefacts from the exploratory crawl")
    seed.add_argument("--legacy-dir", default=None)
    seed.set_defaults(func=cmd_seed)

    scan = sub.add_parser("scan", help="discover captures and fetch what is needed")
    scan.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    scan.add_argument("--strategy", choices=["bisect", "exhaustive"], default=None)
    scan.add_argument("--url", default=None)
    scan.set_defaults(func=cmd_scan)

    sub.add_parser("status", help="show crawl coverage").set_defaults(func=cmd_status)

    build = sub.add_parser("build", help="derive history and write web/CSV outputs")
    build.add_argument("--url", default=None)
    build.add_argument("--suggest-aliases", action="store_true")
    build.set_defaults(func=cmd_build)

    enrich = sub.add_parser("enrich", help="backfill address/coordinate fields")
    enrich.add_argument("--url", default=None)
    enrich.add_argument("--limit", type=int, default=120)
    enrich.add_argument("--min-ts", default="", help="skip captures older than this (coords only exist in modern pages)")
    enrich.set_defaults(func=cmd_enrich)

    geo = sub.add_parser("geocode", help="fill remaining coordinates from public geocoders")
    geo.add_argument("--url", default=None)
    geo.add_argument("--limit", type=int, default=400)
    geo.add_argument("--provider", default=None,
                     help="force one provider (nyc-geosearch, us-census, nominatim)")
    geo.set_defaults(func=cmd_geocode)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    args.func(args, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
