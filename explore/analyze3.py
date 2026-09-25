#!/usr/bin/env python3
"""Name-canonical analysis of the Eater 38 list history.

Identity is the restaurant's display name (normalised), because Eater's URL
anchors are unreliable: it used placeholder `article-N` anchors in 2024-25 and
re-slugged entries repeatedly.
"""
import json, os, csv, re, sys, unicodedata, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze2 import load, ts_of, fmt, norm_name, covid_timestamps

# Manual aliases: normalised name -> canonical normalised name.
# Populated from inspection of the transition diffs (renames / typo fixes /
# punctuation drift for the same restaurant).
ALIASES = {}


def canon(n):
    seen = set()
    while n in ALIASES and n not in seen:
        seen.add(n)
        n = ALIASES[n]
    return n


def nameset(d):
    out = {}
    for i in d["items"]:
        out.setdefault(canon(norm_name(i["name"])), i["name"])
    return out


def main():
    rows = load("main")
    print(f"captures: {len(rows)}  {fmt(ts_of(rows[0]))} .. {fmt(ts_of(rows[-1]))}")

    # ---- transitions ----
    trans = []
    for a, b in zip(rows, rows[1:]):
        na, nb = nameset(a), nameset(b)
        sa, sb = set(na), set(nb)
        if sa == sb:
            continue
        added = [(k, nb[k]) for k in sorted(sb - sa)]
        removed = [(k, na[k]) for k in sorted(sa - sb)]
        trans.append({"after": fmt(ts_of(a)), "before": fmt(ts_of(b)),
                      "after_ts": ts_of(a), "before_ts": ts_of(b),
                      "added": added, "removed": removed})
    with open(os.path.join(HERE, "data", "changelog_names.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["changed_between", "and", "n_added", "n_removed", "added", "removed"])
        for t in trans:
            w.writerow([t["after"], t["before"], len(t["added"]), len(t["removed"]),
                        "; ".join(n for _, n in t["added"]),
                        "; ".join(n for _, n in t["removed"])])

    print(f"\n=== {len(trans)} transitions (name-keyed) ===")
    for t in trans:
        print(f"\n{t['after']} -> {t['before']}   (+{len(t['added'])}/-{len(t['removed'])})")
        if t["added"]:
            print("   ADD: " + "; ".join(n for _, n in t["added"]))
        if t["removed"]:
            print("   DEL: " + "; ".join(n for _, n in t["removed"]))


if __name__ == "__main__":
    main()
