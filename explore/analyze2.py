#!/usr/bin/env python3
"""Reconstruct membership intervals for every place on the Eater NY 38 list.

Outputs:
  data/intervals.csv   one row per presence-run per place
  data/changelog.csv   one row per detected list transition
  data/versions.txt    collapsed version timeline
"""
import json, os, csv, unicodedata, re
from collections import defaultdict, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(HERE, "data", "captures.jsonl")
URL_MAIN = "https://ny.eater.com/maps/best-new-york-restaurants-38-map"


# ---------- load ----------
def covid_timestamps():
    p = os.path.join(HERE, "data", "cdx_covid.txt")
    ts = set()
    if os.path.exists(p):
        for line in open(p):
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "200":
                ts.add(parts[0])
    return ts


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load(series="main", min_items=20):
    covid = covid_timestamps()
    best = {}
    for line in open(CAP):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("source") == "error" or d.get("n", 0) < min_items:
            continue
        is_covid = d["ts"] in covid
        if (series == "covid") != is_covid:
            continue
        key = (d.get("eff_ts") or d["ts"])
        best[key] = d          # last write wins for same effective timestamp
    return [best[k] for k in sorted(best)]


def ts_of(d):
    return d.get("eff_ts") or d["ts"]


def fmt(ts):
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" if ts and len(ts) >= 8 else (ts or "?")


def keyset(d):
    return frozenset(i["slug"] for i in d["items"])


# ---------- run detection ----------
def runs_for(rows):
    """Contiguous presence runs keyed by slug."""
    out = defaultdict(list)
    prev_ts = None
    for d in rows:
        ts = ts_of(d)
        present = {i["slug"]: i["name"] for i in d["items"]}
        for slug, name in present.items():
            r = out[slug]
            if r and r[-1]["last_ts"] == prev_ts:
                r[-1]["last_ts"] = ts
                r[-1]["captures"] += 1
                r[-1]["last_name"] = name
            else:
                r.append({"slug": slug, "first_name": name, "last_name": name,
                          "first_ts": ts, "last_ts": ts, "captures": 1})
        prev_ts = ts
    return out


def merge_by_name(runs):
    """Merge runs whose display names normalise to the same place (slug drift)."""
    by_name = defaultdict(list)
    for slug, rl in runs.items():
        for r in rl:
            by_name[norm_name(r["first_name"])].append(r)
    merged = {}
    for name, rl in by_name.items():
        rl.sort(key=lambda r: r["first_ts"])
        group = []
        for r in rl:
            if group and group[-1]["last_ts"] == r["first_ts"]:
                group[-1]["last_ts"] = r["last_ts"]
                group[-1]["captures"] += r["captures"]
                group[-1]["last_name"] = r["last_name"]
                group[-1]["slugs"].add(r["slug"])
            else:
                g = dict(r)
                g["slugs"] = {r["slug"]}
                group.append(g)
        merged[name] = group
    return merged


def main():
    rows = load("main")
    if not rows:
        print("no data yet")
        return
    print(f"main-series captures: {len(rows)}  {fmt(ts_of(rows[0]))} .. {fmt(ts_of(rows[-1]))}")
    src = defaultdict(int)
    for d in rows:
        src[d["source"]] += 1
    print("extraction sources:", dict(src))

    # sanity: distribution of list sizes
    sizes = defaultdict(int)
    for d in rows:
        sizes[d["n"]] += 1
    print("list sizes seen:", dict(sorted(sizes.items())))

    # version timeline
    vs = []
    for d in rows:
        k = keyset(d)
        ts = ts_of(d)
        if vs and vs[-1]["set"] == k:
            vs[-1]["last"] = ts
            vs[-1]["captures"] += 1
        else:
            vs.append({"set": k, "first": ts, "last": ts, "captures": 1, "sample": d})
    with open(os.path.join(HERE, "data", "versions.txt"), "w") as f:
        for i, v in enumerate(vs):
            f.write(f"v{i:03d}  {fmt(v['first'])} -> {fmt(v['last'])}  n={len(v['set'])}  captures={v['captures']}\n")
    print(f"distinct consecutive list versions: {len(vs)}")

    # changelog between consecutive *captures* (not versions)
    changelog = []
    for a, b in zip(rows, rows[1:]):
        sa, sb = keyset(a), keyset(b)
        if sa == sb:
            continue
        namea = {i["slug"]: i["name"] for i in a["items"]}
        nameb = {i["slug"]: i["name"] for i in b["items"]}
        added = [nameb[s] for s in sb - sa if s in nameb]
        removed = [namea[s] for s in sa - sb if s in namea]
        changelog.append({
            "after": fmt(ts_of(a)), "before": fmt(ts_of(b)),
            "after_ts": ts_of(a), "before_ts": ts_of(b),
            "added": added, "removed": removed,
        })
    with open(os.path.join(HERE, "data", "changelog.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["changed_between", "and", "added", "removed"])
        for c in changelog:
            w.writerow([c["after"], c["before"], "; ".join(c["added"]), "; ".join(c["removed"])])
    print(f"capture-to-capture transitions: {len(changelog)}")

    # intervals
    runs = runs_for(rows)
    merged = merge_by_name(runs)
    out = []
    for name, rl in merged.items():
        for g in rl:
            out.append((g["first_ts"], name, g))
    out.sort()
    with open(os.path.join(HERE, "data", "intervals.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["place", "slugs", "first_capture_present", "last_capture_present",
                    "captures", "still_on_list_at_end", "name_last_seen"])
        for _, name, g in out:
            w.writerow([g["first_name"], "|".join(sorted(g["slugs"])), fmt(g["first_ts"]),
                        fmt(g["last_ts"]), g["captures"],
                        "yes" if g["last_ts"] == ts_of(rows[-1]) else "no",
                        g["last_name"]])
    print(f"places: {len(merged)}   presence runs: {len(out)}")
    print("wrote data/intervals.csv, data/changelog.csv, data/versions.txt")


if __name__ == "__main__":
    main()
