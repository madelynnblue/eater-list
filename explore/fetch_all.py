#!/usr/bin/env python3
"""Fetch every archived capture of the Eater 38 page and extract its restaurant list.

Writes JSONL: {"ts": <cdx timestamp>, "eff_ts": <effective ts after redirect>,
               "digest": <cdx digest>, "source": ..., "n": ..., "items": [...]}
"""
import json, os, re, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import try_jsonld, try_cards, try_regex

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "captures.jsonl")
LOCK = threading.Lock()

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def load_cdx(path):
    rows = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 5:
                continue
            ts, url, status, mime, digest = parts[0], parts[1], parts[2], parts[3], parts[4]
            if status != "200" or not mime.startswith("text/html"):
                continue
            rows.append((ts, url, digest))
    return rows


def extract_doc(doc):
    for src, fn in (("jsonld", try_jsonld), ("cards", try_cards), ("regex", try_regex)):
        try:
            items = fn(doc)
        except Exception:
            items = []
        if len(items) >= 5:
            return src, items
    return "none", []


def fetch_one(ts, url, digest, tries=3):
    for attempt in range(tries):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
        tmp.close()
        try:
            wb = f"https://web.archive.org/web/{ts}id_/{url}"
            p = subprocess.run(
                ["curl", "-sL", "--compressed", "-m", "90", "-A", UA,
                 "-o", tmp.name, "-w", "%{url_effective}", wb],
                capture_output=True, text=True,
            )
            eff = p.stdout.strip()
            with open(tmp.name, "rb") as f:
                raw = f.read()
            if len(raw) < 5000:
                raise RuntimeError(f"short body {len(raw)}")
            doc = raw.decode("utf-8", "replace")
            src, items = extract_doc(doc)
            eff_ts = None
            m = re.search(r"/web/(\d{14})", eff)
            if m:
                eff_ts = m.group(1)
            return {
                "ts": ts, "eff_ts": eff_ts or ts, "digest": digest,
                "source": src, "n": len(items), "items": items,
            }
        except Exception as e:
            err = str(e)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        import time
        time.sleep(1.5 * (attempt + 1))
    return {"ts": ts, "eff_ts": ts, "digest": digest, "source": "error",
            "n": 0, "items": [], "error": err}


def main():
    cdx_files = sys.argv[1:]
    rows = []
    for cf in cdx_files:
        rows.extend(load_cdx(cf))
    # dedupe by timestamp+url
    seen = set()
    uniq = []
    for r in rows:
        k = (r[0], r[1])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    rows = sorted(uniq)
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["ts"])
                except Exception:
                    pass
    todo = [r for r in rows if r[0] not in done]
    print(f"total={len(rows)} done={len(done)} todo={len(todo)}", flush=True)

    out = open(OUT, "a")
    count = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_one, *r): r for r in todo}
        for fut in as_completed(futs):
            res = fut.result()
            with LOCK:
                out.write(json.dumps(res, ensure_ascii=False) + "\n")
                out.flush()
                count += 1
                if count % 25 == 0 or count == len(todo):
                    print(f"{count}/{len(todo)} last={res['ts']} n={res['n']} src={res['source']}", flush=True)
    out.close()


if __name__ == "__main__":
    main()
