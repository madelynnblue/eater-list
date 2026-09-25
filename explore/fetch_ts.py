#!/usr/bin/env python3
"""Fetch a specific list of capture timestamps for one URL.

usage: fetch_ts.py <timestamps-file> <url>
"""
import json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_all import fetch_one

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "captures.jsonl")
LOCK = threading.Lock()


def main():
    tsf, url = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    ts_list = [l.strip() for l in open(tsf) if l.strip()]
    done = {}
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("source") != "error":
                done[d["ts"]] = d
    todo = [t for t in ts_list if t not in done]
    print(f"requested={len(ts_list)} already={len(ts_list)-len(todo)} todo={len(todo)}", flush=True)
    if not todo:
        return
    # rewrite file without the rows we are about to replace
    tmp = OUT + ".tmp"
    out = open(tmp, "w")
    for ts, d in done.items():
        if ts not in set(todo):
            out.write(json.dumps(d, ensure_ascii=False) + "\n")
    out.flush()
    count = [0]
    results = {}

    def work(ts):
        r = fetch_one(ts, url, "")
        with LOCK:
            count[0] += 1
            results[ts] = r
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            if count[0] % 20 == 0 or count[0] == len(todo):
                print(f"{count[0]}/{len(todo)} last={ts} n={r['n']} src={r['source']}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for f in as_completed(futs):
            f.result()
    out.close()
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
