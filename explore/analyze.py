#!/usr/bin/env python3
"""Turn the per-capture restaurant lists into (a) a version timeline and
(b) per-restaurant membership intervals."""
import json, os, sys, csv, datetime
from collections import defaultdict, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(HERE, "data", "captures.jsonl")

COVID_MARK = "coronavirus-delivery-takeout"


def covid_timestamps():
    """Timestamps that belong to the COVID-era variant URL (joined from CDX)."""
    p = os.path.join(HERE, "data", "cdx_covid.txt")
    ts = set()
    if os.path.exists(p):
        for line in open(p):
            parts = line.split()
            if len(parts) >= 2 and parts[2] == "200":
                ts.add(parts[0])
    return ts


def load(series="main"):
    rows = []
    covid = covid_timestamps()
    with open(CAP) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            is_covid = d["ts"] in covid
            if (series == "covid") != is_covid:
                continue
            if d.get("n", 0) < 5:
                continue
            rows.append(d)
    # dedupe on effective timestamp + content key (use last write wins)
    best = {}
    for d in rows:
        key = (d.get("eff_ts") or d["ts"], d.get("n"), tuple(i["slug"] for i in d["items"]))
        best[key] = d
    rows = sorted(best.values(), key=lambda d: d.get("eff_ts") or d["ts"])
    return rows


def fmt(ts):
    if not ts or len(ts) < 8:
        return ts or "?"
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


def content_key(d):
    return tuple(i["slug"] for i in d["items"])


def versions(rows):
    """Collapse consecutive captures with an identical membership set."""
    vs = []
    for d in rows:
        key = frozenset(content_key(d))
        ts = d.get("eff_ts") or d["ts"]
        if vs and vs[-1]["keyset"] == key:
            vs[-1]["last"] = ts
            vs[-1]["captures"] += 1
        else:
            vs.append({"keyset": key, "first": ts, "last": ts, "captures": 1, "sample": d})
    return vs


def intervals(rows, by="slug"):
    """For each restaurant, find contiguous runs of presence across captures."""
    out = defaultdict(list)
    for d in rows:
        ts = d.get("eff_ts") or d["ts"]
        present = OrderedDict()
        for i in d["items"]:
            present[i[by]] = i["name"]
        for k, name in present.items():
            run = out[k]
            if run and run[-1]["last_ts"] == prev_ts:
                run[-1]["last_ts"] = ts
                run[-1]["captures"] += 1
                run[-1]["last_name"] = name
            else:
                run.append({
                    "slug": k, "first_name": name, "last_name": name,
                    "first_ts": ts, "last_ts": ts, "captures": 1,
                })
        prev_ts = ts
    return out


def main():
    rows = load("main")
    if not rows:
        print("no captures yet")
        return
    print(f"captures analysed: {len(rows)}  ({fmt(rows[0].get('eff_ts'))} .. {fmt(rows[-1].get('eff_ts'))})")
    print(f"golden source breakdown: ", end="")
    src = defaultdict(int)
    for d in rows:
        src[d.get("source")] += 1
    print(dict(src))

    vs = versions(rows)
    print(f"\n=== distinct list versions (consecutive-run collapse): {len(vs)} ===")
    for i, v in enumerate(vs):
        print(f"{i:3d}  {fmt(v['first'])} -> {fmt(v['last'])}  n={len(v['keyset'])}  captures={v['captures']}")

    iv = intervals(rows)
    rows_out = []
    for slug, runs in iv.items():
        for r in runs:
            rows_out.append(r)
    rows_out.sort(key=lambda r: (r["first_ts"], r["slug"]))

    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    with open(os.path.join(HERE, "data", "intervals.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["restaurant", "slug", "first_capture", "last_capture", "captures", "last_name_seen"])
        for r in rows_out:
            w.writerow([r["first_name"], r["slug"], fmt(r["first_ts"]), fmt(r["last_ts"]),
                        r["captures"], r["last_name"]])
    print(f"\nwrote data/intervals.csv with {len(rows_out)} presence-runs across {len(iv)} distinct places")


if __name__ == "__main__":
    main()
