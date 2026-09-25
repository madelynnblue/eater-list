"""SQLite state: captured metadata, cached extractions, and derived bookkeeping.

Two ideas keep re-runs cheap:

* ``captures`` records *every* Wayback capture (timestamp + content digest) from
  the CDX index. Listing captures costs one small HTTP request.
* ``contents`` stores the parsed payload keyed by ``(url, digest)``. A page is
  downloaded at most once per unique digest, ever.

Because captures are known without downloading them, the pipeline can reason
about *when* to look without paying to look everywhere.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS captures (
  url        TEXT NOT NULL,
  ts         TEXT NOT NULL,
  status     TEXT,
  mimetype   TEXT,
  digest     TEXT,
  length     INTEGER,
  first_seen TEXT,
  PRIMARY KEY (url, ts)
);
CREATE INDEX IF NOT EXISTS captures_digest_idx ON captures (url, digest);
CREATE TABLE IF NOT EXISTS contents (
  url               TEXT NOT NULL,
  digest            TEXT NOT NULL,
  fetched_at        TEXT,
  http_status       INTEGER,
  extractor_version INTEGER,
  effective_ts      TEXT,
  n_items           INTEGER,
  payload           TEXT,
  PRIMARY KEY (url, digest)
);
CREATE TABLE IF NOT EXISTS geocache (
  query      TEXT PRIMARY KEY,
  lat        REAL,
  lng        REAL,
  source     TEXT,
  fetched_at TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self, path: str, raw_dir: str | None = None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.raw_dir = raw_dir
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- meta -------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- captures ---------------------------------------------------------
    def upsert_captures(self, rows) -> int:
        """Insert capture metadata. Returns the number of newly discovered rows."""
        before = self.conn.total_changes
        stamp = now()
        self.conn.executemany(
            """INSERT INTO captures(url,ts,status,mimetype,digest,length,first_seen)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(url,ts) DO UPDATE SET
                 status=excluded.status, mimetype=excluded.mimetype,
                 digest=excluded.digest, length=excluded.length""",
            [(r.url, r.ts, r.status, r.mimetype, r.digest, r.length, stamp) for r in rows],
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def captures(self, url: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT ts,digest,mimetype,length FROM captures WHERE url=? ORDER BY ts", (url,)
            )
        )

    def last_capture_ts(self, url: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM captures WHERE url=?", (url,)
        ).fetchone()
        return row["ts"] if row and row["ts"] else None

    def digest_for(self, url: str, ts: str) -> str | None:
        row = self.conn.execute(
            "SELECT digest FROM captures WHERE url=? AND ts=?", (url, ts)
        ).fetchone()
        return row["digest"] if row else None

    # -- contents ---------------------------------------------------------
    def get_content(self, url: str, digest: str):
        row = self.conn.execute(
            "SELECT payload, extractor_version FROM contents WHERE url=? AND digest=?",
            (url, digest),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def have_digests(self, url: str) -> set[str]:
        return {
            r["digest"]
            for r in self.conn.execute(
                "SELECT digest FROM contents WHERE url=?", (url,)
            )
        }

    def put_content(self, url, digest, payload, http_status=200, effective_ts=None):
        self.conn.execute(
            """INSERT INTO contents(url,digest,fetched_at,http_status,extractor_version,
                                    effective_ts,n_items,payload)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(url,digest) DO UPDATE SET
                 fetched_at=excluded.fetched_at, http_status=excluded.http_status,
                 extractor_version=excluded.extractor_version,
                 effective_ts=excluded.effective_ts, n_items=excluded.n_items,
                 payload=excluded.payload""",
            (
                url,
                digest,
                now(),
                http_status,
                payload.get("extractor_version"),
                effective_ts,
                payload.get("n", 0),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def stale_contents(self, url: str, below_version: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM contents WHERE url=? AND extractor_version<?",
            (url, below_version),
        ).fetchone()
        return row["c"]

    # -- raw page archive -------------------------------------------------
    def _raw_path(self, url: str, digest: str) -> str:
        bucket = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        return os.path.join(self.raw_dir, bucket, f"{digest}.html.gz")

    def put_raw(self, url: str, digest: str, body: bytes) -> None:
        """Archive the untouched HTML so the extractor can be re-run offline."""
        if not self.raw_dir:
            return
        path = self._raw_path(url, digest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def get_raw(self, url: str, digest: str) -> bytes | None:
        if not self.raw_dir:
            return None
        path = self._raw_path(url, digest)
        if not os.path.exists(path):
            return None
        with gzip.open(path, "rb") as fh:
            return fh.read()

    def raw_count(self, url: str) -> int:
        if not self.raw_dir:
            return 0
        bucket = os.path.join(self.raw_dir, hashlib.sha1(url.encode("utf-8")).hexdigest()[:12])
        return len(os.listdir(bucket)) if os.path.isdir(bucket) else 0

    # -- geocoding cache --------------------------------------------------
    def get_geo(self, query: str):
        row = self.conn.execute(
            "SELECT lat,lng,source FROM geocache WHERE query=?", (query,)
        ).fetchone()
        return dict(row) if row else None

    def put_geo(self, query: str, lat: float, lng: float, source: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO geocache(query,lat,lng,source,fetched_at) VALUES(?,?,?,?,?)",
            (query, lat, lng, source, now()),
        )
        self.conn.commit()

    # -- reporting --------------------------------------------------------
    def stats(self, url: str) -> dict:
        caps = self.conn.execute(
            "SELECT COUNT(*) c, MIN(ts) lo, MAX(ts) hi FROM captures WHERE url=?", (url,)
        ).fetchone()
        digests = self.conn.execute(
            "SELECT COUNT(DISTINCT digest) c FROM captures WHERE url=?", (url,)
        ).fetchone()["c"]
        have = self.conn.execute(
            "SELECT COUNT(*) c FROM contents WHERE url=?", (url,)
        ).fetchone()["c"]
        return {
            "captures": caps["c"],
            "first": caps["lo"],
            "last": caps["hi"],
            "distinct_digests": digests,
            "contents_cached": have,
            "contents_missing": max(digests - have, 0),
        }
