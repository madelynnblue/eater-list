#!/usr/bin/env python3
"""Final reconstruction with resolved identities.

Writes:
  eater38_history.csv   one row per place per on-list run
  eater38_changelog.csv one row per detected list transition
  eater38_history.md    readable report
"""
import json, os, csv, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze2 import load, ts_of, fmt, norm_name

# Variant key -> canonical key.  Eater renamed / re-slugged these entries; they
# are the same restaurant, so their runs are joined.
ALIASES = {
    "the odeon": "odeon",
    "roberta s": "roberto s",
    "russ and daughters": "russ and daughters cafe",
    "charles country pan fried chicken": "charles pan fried chicken",
    "s and p": "s and p lunch",
    "haidilao": "haidilao hot pot",
    "adda indian canteen": "adda",
}


def canon(k):
    return ALIASES.get(k, k)


def rowmap(d):
    """canonical key -> display name, for one capture."""
    out = {}
    for i in d["items"]:
        k = canon(norm_name(i["name"]))
        out.setdefault(k, i["name"])
    return out


def main():
    rows = load("main")
    maps = [rowmap(d) for d in rows]
    tss = [ts_of(d) for d in rows]
    last_ts = tss[-1]

    # most recent display name per key
    display = {}
    for m in maps:
        for k, n in m.items():
            display[k] = n  # later captures win

    # ---- runs ----
    runs = []
    n = len(rows)
    for k in display:
        i = 0
        while i < n:
            if k in maps[i]:
                j = i
                while j + 1 < n and k in maps[j + 1]:
                    j += 1
                runs.append({"key": k, "i": i, "j": j})
                i = j + 1
            else:
                i += 1
    runs.sort(key=lambda r: (tss[r["i"]], r["key"]))

    # ---- changelog ----
    trans = []
    for a, b in zip(range(n), range(1, n)):
        sa, sb = set(maps[a]), set(maps[b])
        if sa == sb:
            continue
        trans.append({
            "after": tss[a], "before": tss[b],
            "added": [maps[b][k] for k in sorted(sb - sa)],
            "removed": [maps[a][k] for k in sorted(sa - sb)],
        })

    # ---- csv: runs ----
    p = os.path.join(HERE, "eater38_history.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["place", "on_list_from_capture", "on_list_through_capture",
                    "added_between", "removed_between", "still_on_list",
                    "note"])
        for r in runs:
            k = r["key"]
            i, j = r["i"], r["j"]
            if i == 0:
                add = ("on list at the very first archived capture (%s); "
                       "actual start earlier" % fmt(tss[0]))
            else:
                add = f"after {fmt(tss[i-1])} and by {fmt(tss[i])}"
            if j == n - 1:
                rem = "still on list at last capture (%s)" % fmt(last_ts)
            else:
                rem = f"after {fmt(tss[j])} and by {fmt(tss[j+1])}"
            w.writerow([display[k], fmt(tss[i]), fmt(tss[j]), add, rem,
                        "yes" if j == n - 1 else "no", ""])

    # ---- csv: changelog ----
    p2 = os.path.join(HERE, "eater38_changelog.csv")
    with open(p2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["change_happened_after", "and_by", "added", "removed"])
        for t in trans:
            w.writerow([fmt(t["after"]), fmt(t["before"]),
                        "; ".join(t["added"]), "; ".join(t["removed"])])

    # ---- gaps ----
    def days(a, b):
        import datetime
        da = datetime.date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        db = datetime.date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        return (db - da).days
    coarse = [(t, days(t["after"], t["before"])) for t in trans
              if days(t["after"], t["before"]) > 30]
    coarse.sort(key=lambda x: -x[1])

    # ---- consolidated per-place file ----
    per_place = collections.defaultdict(list)
    still = set()
    for r in runs:
        per_place[r["key"]].append(r)
        if r["j"] == n - 1:
            still.add(r["key"])
    with open(os.path.join(HERE, "eater38_places.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["place", "stretches_on_list", "periods", "still_on_list_at_last_capture"])
        for k in sorted(per_place, key=lambda x: display[x].lower()):
            rs = sorted(per_place[k], key=lambda r: r["i"])
            periods = "; ".join(f"{fmt(tss[r['i']])} to {fmt(tss[r['j']])}" for r in rs)
            w.writerow([display[k], len(rs), periods, "yes" if k in still else "no"])

    # ---- report ----
    lines = []
    lines.append("# Eater NY \u201c38 Best Restaurants\u201d \u2014 membership history\n")
    lines.append("Source: Wayback Machine captures of "
                 "`https://ny.eater.com/maps/best-new-york-restaurants-38-map`.\n")
    lines.append(f"- archived captures analysed: **{n}** ({fmt(tss[0])} \u2192 {fmt(last_ts)})")
    lines.append(f"- distinct restaurants that ever appeared on the list: **{len(display)}**")
    lines.append(f"- detected list changes: **{len(trans)}**")
    lines.append(f"- list size at last capture: **{len(maps[-1])}** "
                 f"(the page is titled \u201cThe 38\u201d but ran 39 entries in 2018 and from Aug 2026)\n")
    lines.append("Each row below is one continuous stretch on the list. `on list from` / `on list through` "
                 "are the earliest and latest archived captures in which the place appears. Because the "
                 "list is only observed when a capture is taken, a change is known only to fall between "
                 "those captures and their neighbours; `eater38_history.csv` gives that bracket "
                 "(`added_between` / `removed_between`), and `eater38_places.csv` lists every stretch "
                 "per restaurant on one line.\n")

    lines.append("\n## Changes whose exact date is uncertain\n")
    lines.append("Where the archive has no capture for a while, a change can only be bounded:\n")
    lines.append("| change happened after | and by | window (days) |")
    lines.append("|---|---|---|")
    for t, dd in coarse:
        lines.append(f"| {fmt(t['after'])} | {fmt(t['before'])} | {dd} |")
    if not coarse:
        lines.append("| \u2014 | \u2014 | \u2014 |")
    lines.append("\nEvery other detected change is bracketed by captures less than 30 days apart; "
                 "in 2026 the archive is dense enough that most changes are pinned to the day.\n")

    lines.append("\n## Every restaurant, with the period(s) it was on the list\n")
    lines.append("| # | Restaurant | On list from | On list through | Status |")
    lines.append("|---|---|---|---|---|")
    for idx, r in enumerate(runs, 1):
        k = r["key"]
        i, j = r["i"], r["j"]
        lines.append(f"| {idx} | {display[k]} | {fmt(tss[i])} | {fmt(tss[j])} | "
                     f"{'on list at last capture' if j == n-1 else 'rotated out'} |")

    lines.append("\n## Detected changes (the quarterly updates)\n")
    for t in trans:
        lines.append(f"**after {fmt(t['after'])} \u2192 by {fmt(t['before'])}**")
        if t["added"]:
            lines.append(f"- added: {', '.join(t['added'])}")
        if t["removed"]:
            lines.append(f"- removed: {', '.join(t['removed'])}")
        lines.append("")

    lines.append("\n## Name changes folded into one entry\n")
    for variant, target in sorted(ALIASES.items()):
        lines.append(f"- \u201c{variant}\u201d \u2192 {display.get(target, target)}")

    lines.append("\n## Not covered here (earlier era)\n")
    lines.append("The Wayback Machine has **no captures of this URL before 2017-08-05**. Before then "
                 "the same guide lived at dated URLs (for example "
                 "`/maps/the-38-essential-new-york-restaurants-january-12`, `.../january-13`, "
                 "`.../april-14`, and the base `/maps/the-38-essential-new-york-restaurants`), each a "
                 "frozen edition rather than an evergreen page. Extending the timeline back to those "
                 "editions is a separate crawl.\n")

    with open(os.path.join(HERE, "eater38_history.md"), "w") as f:
        f.write("\n".join(lines))

    print(f"captures={n} places={len(display)} runs={len(runs)} transitions={len(trans)} coarse={len(coarse)}")
    print("wrote eater38_history.csv, eater38_changelog.csv, eater38_history.md")


if __name__ == "__main__":
    main()
