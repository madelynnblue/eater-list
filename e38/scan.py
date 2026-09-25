"""The crawler.

Strategy, and why it is cheap
-----------------------------
The CDX index tells us *when* every capture happened without downloading any
page. A page only needs downloading when we want to know what the list said at
that moment. So:

* ``bisect`` fetches the endpoints of a range and recurses only while the two
  ends disagree. Finding every change among N captures costs roughly
  ``2 * changes * log2(N / changes)`` fetches instead of N. On this page that is
  ~170 instead of ~680.
* Weekly incremental runs anchor on the last capture already checked. If the
  list has not moved, that is a single fetch.
* Every payload is cached by content digest, so a capture is never downloaded
  twice even across runs.

The bisect assumption is that the list only changes (it does not change and
change back inside a range whose endpoints agree). ``--strategy exhaustive``
lifts that assumption by fetching every capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import EXTRACTOR_VERSION
from .extract import extract
from .identity import entry_keys, resolver
from .wayback import Permanent, Retryable, Wayback


class BudgetExhausted(Exception):
    """Raised when a run hits ``max_fetches_per_run``; the next run resumes."""


@dataclass
class ScanReport:
    url: str
    new_captures: int = 0
    fetches: int = 0
    cache_hits: int = 0
    changes: list = field(default_factory=list)
    complete: bool = True
    note: str = ""


class Scanner:
    def __init__(self, cfg, store, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log
        self.wayback = Wayback(cfg, log=log)
        self.aliases = resolver({})  # raw names for signatures; aliases applied at build
        self._fetches = 0
        self._cache_hits = 0

    # -- capture catalogue -------------------------------------------------
    def sync(self, url: str, since: str | None = None) -> int:
        """Refresh the capture index for one URL. No page downloads."""
        rows = self.wayback.cdx(url, since=since)
        if not rows:
            return 0
        added = self.store.upsert_captures(rows)
        return added

    # -- content -----------------------------------------------------------
    def _budget(self) -> None:
        if self._fetches >= self.cfg.max_fetches_per_run:
            raise BudgetExhausted()

    def _reparse(self, url: str, ts: str, digest: str, min_version: int) -> dict | None:
        """Re-run the extractor on archived HTML, without touching the network."""
        raw = self.store.get_raw(url, digest)
        if raw is None:
            return None
        payload = extract(raw.decode("utf-8", "replace"))
        payload["fetched_for_ts"] = ts
        payload["reparsed"] = True
        self.store.put_content(url, digest, payload, 200)
        self.log(f"  re-parsed {ts} from archive (extractor v{payload['extractor_version']})")
        return payload

    def content(self, url: str, ts: str, digest: str, min_version: int = 0) -> dict:
        """Parsed payload for a capture, downloading only when necessary.

        ``min_version`` upgrades a cached payload that came from an older
        extractor — from the local raw archive when possible, otherwise with a
        single download.
        """
        cached = self.store.get_content(url, digest)
        stale = cached is not None and cached.get("extractor_version", 0) < min_version
        if cached is not None and not self.cfg.refetch and not stale:
            self._cache_hits += 1
            return cached
        if stale:
            reparsed = self._reparse(url, ts, digest, min_version)
            if reparsed is not None:
                self._cache_hits += 1
                return reparsed
        self._budget()
        result = self.wayback.fetch(url, ts)
        # Wayback occasionally replays a neighbouring capture; key the payload
        # under the digest of the capture actually served.
        effective = result.effective_ts
        true_digest = self.store.digest_for(url, effective) or digest
        self.store.put_raw(url, true_digest, result.body)
        payload = extract(result.body.decode("utf-8", "replace"))
        payload["fetched_for_ts"] = ts
        self.store.put_content(url, true_digest, payload, result.http_status, effective)
        if true_digest != digest:
            self.store.put_raw(url, digest, result.body)
            self.store.put_content(url, digest, payload, result.http_status, effective)
        self._fetches += 1
        return payload

    def _signature(self, url: str, index: int, ts_list, digest_list) -> frozenset:
        payload = self.content(url, ts_list[index], digest_list[index])
        return frozenset(entry_keys(payload, self.aliases))

    # -- change detection --------------------------------------------------
    def find_changes(self, url, ts_list, digest_list, lo, hi) -> list[tuple[int, int]]:
        """Indices (i, i+1) bracketing every detected change in [lo, hi]."""
        changes: list[tuple[int, int]] = []
        stack = [(lo, hi)]
        while stack:
            i, j = stack.pop()
            if i >= j:
                continue
            left = self._signature(url, i, ts_list, digest_list)
            right = self._signature(url, j, ts_list, digest_list)
            if left == right:
                continue
            if j == i + 1:
                changes.append((i, j))
                continue
            mid = (i + j) // 2
            stack.append((i, mid))
            stack.append((mid, j))
        changes.sort()
        return changes

    # -- entry points ------------------------------------------------------
    def scan_url(self, url: str, mode: str = "incremental", strategy: str | None = None) -> ScanReport:
        strategy = strategy or self.cfg.strategy
        report = ScanReport(url=url)

        since = None if mode == "full" else self.store.last_capture_ts(url)
        if mode == "full":
            since = self.cfg.since
        cdx_ok = True
        try:
            report.new_captures = self.sync(url, since=since)
        except (Retryable, Permanent) as exc:
            # A CDX outage must not stop us using the index we already have.
            cdx_ok = False
            report.note = f"CDX unavailable, reused cached index ({exc})"
            self.log(f"  ! {report.note}")

        rows = self.store.captures(url)
        if not rows:
            report.complete = False
            report.note = report.note or "no captures indexed"
            return report
        ts_list = [r["ts"] for r in rows]
        digest_list = [r["digest"] for r in rows]
        index_of = {ts: i for i, ts in enumerate(ts_list)}

        if mode == "full":
            lo = index_of.get(ts_list[0], 0)
        else:
            anchor = self.store.get_meta(f"last_checked_ts:{url}")
            if not anchor or anchor not in index_of:
                lo = 0
            else:
                lo = index_of[anchor]
        hi = len(ts_list) - 1

        try:
            if strategy == "exhaustive":
                for i in range(lo, hi + 1):
                    self.content(url, ts_list[i], digest_list[i])
                report.changes = [(i, i + 1) for i in range(lo, hi)
                                  if set(entry_keys(self.content(url, ts_list[i], digest_list[i]), self.aliases))
                                  != set(entry_keys(self.content(url, ts_list[i + 1], digest_list[i + 1]), self.aliases))]
            else:
                self.content(url, ts_list[lo], digest_list[lo])
                report.changes = self.find_changes(url, ts_list, digest_list, lo, hi)
        except BudgetExhausted:
            report.complete = False
            report.note = (
                f"fetch budget of {self.cfg.max_fetches_per_run} reached; "
                "progress is cached, re-run to continue"
            )
        except (Retryable, Permanent) as exc:
            report.complete = False
            report.note = f"stopped: {exc}"

        if report.complete:
            # The probe verified the list up to this capture; record it so the
            # next run only has to look at what came after. This is safe even if
            # the CDX refresh failed, because the next run re-lists from here.
            self.store.set_meta(f"last_checked_ts:{url}", ts_list[hi])
        if not cdx_ok and report.complete:
            report.complete = False
            report.note = (report.note + " (probe completed)").strip()
        report.fetches = self._fetches
        report.cache_hits = self._cache_hits
        return report

    def check_bracket(self, url: str, ts_a: str, ts_b: str) -> list[tuple[str, str]]:
        """Re-verify one window (used by audits): returns precise change brackets."""
        rows = self.store.captures(url)
        ts_list = [r["ts"] for r in rows]
        digest_list = [r["digest"] for r in rows]
        index_of = {ts: i for i, ts in enumerate(ts_list)}
        if ts_a not in index_of or ts_b not in index_of:
            return []
        pairs = self.find_changes(url, ts_list, digest_list, index_of[ts_a], index_of[ts_b])
        return [(ts_list[i], ts_list[j]) for i, j in pairs]
