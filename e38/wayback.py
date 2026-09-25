"""Wayback Machine client: CDX discovery and polite capture fetching."""
from __future__ import annotations

import gzip
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

from .ratelimit import RateLimiter, backoff_delay

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
REPLAY = "https://web.archive.org/web/{ts}id_/{url}"

_OFFLINE_MARKERS = (
    b"Internet Archive: Temporarily Offline",
    b"services are temporarily offline",
    b"Temporarily Offline",
)


class Retryable(Exception):
    """A failure that is worth retrying later (network, throttle, outage)."""


class Permanent(Exception):
    """A failure that will not improve by retrying."""


@dataclass
class CaptureRow:
    url: str
    ts: str
    status: str
    mimetype: str
    digest: str
    length: int


@dataclass
class FetchResult:
    ts: str            # timestamp requested
    effective_ts: str  # timestamp actually served (Wayback may redirect)
    body: bytes
    http_status: int


class Wayback:
    def __init__(self, cfg, limiter: RateLimiter | None = None, log=print):
        self.cfg = cfg
        self.log = log
        self.limiter = limiter or RateLimiter(
            cfg.requests_per_second, cfg.burst, cfg.max_concurrency
        )

    # -- low level --------------------------------------------------------
    def _open(self, url: str, accept: str = "*/*", timeout: float | None = None):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": accept,
                "Accept-Encoding": "gzip, deflate",
            },
        )
        resp = urllib.request.urlopen(req, timeout=timeout or self.cfg.timeout)
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return resp.geturl(), raw, resp.status

    def _request(self, url: str, accept: str = "*/*", timeout: float | None = None,
                 retries: int | None = None):
        """Fetch with retries and rate limiting. Returns (final_url, body, status)."""
        last = None
        for attempt in range(retries or self.cfg.max_retries):
            try:
                with self.limiter.slot():
                    final, body, status = self._open(url, accept, timeout)
                if status in (429, 503) or status >= 500:
                    raise Retryable(f"HTTP {status}")
                if any(m in body[:4000] for m in _OFFLINE_MARKERS):
                    raise Retryable("Internet Archive reported itself offline")
                return final, body, status
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                if exc.code in (429, 503) or exc.code >= 500:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = float(retry_after) if (retry_after or "").isdigit() else None
                    last = f"HTTP {exc.code}"
                    self.limiter.penalize(wait or backoff_delay(attempt))
                    if wait:
                        continue
                elif exc.code in (403, 404):
                    raise Permanent(f"HTTP {exc.code} for {url}") from exc
                else:
                    last = f"HTTP {exc.code}"
            except Retryable as exc:
                last = str(exc)
                self.limiter.penalize(backoff_delay(attempt))
            except Exception as exc:  # network hiccups, timeouts, decode errors
                last = f"{type(exc).__name__}: {exc}"
                self.limiter.penalize(backoff_delay(attempt))
        raise Retryable(f"gave up after {retries or self.cfg.max_retries} attempts ({last})")

    # -- CDX --------------------------------------------------------------
    def cdx(self, url: str, since: str | None = None, until: str | None = None) -> list[CaptureRow]:
        """List archived captures of ``url`` (metadata only — no page fetches)."""
        params = {
            "url": url,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "filter": "statuscode:200",
            "limit": "50000",
        }
        if since:
            params["from"] = since
        if until:
            params["to"] = until
        final, body, status = self._request(
            CDX_ENDPOINT + "?" + urllib.parse.urlencode(params),
            accept="application/json",
            timeout=self.cfg.cdx_timeout,
            retries=self.cfg.cdx_retries,
        )
        if status != 200:
            raise Retryable(f"CDX HTTP {status}")
        try:
            rows = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise Retryable(f"CDX returned non-JSON ({exc})") from exc
        if not rows:
            return []
        header, *data = rows
        idx = {name: i for i, name in enumerate(header)}
        out = []
        for row in data:
            try:
                length = int(row[idx["length"]]) if row[idx["length"]].isdigit() else 0
            except (ValueError, IndexError):
                length = 0
            out.append(
                CaptureRow(
                    url=row[idx["original"]],
                    ts=row[idx["timestamp"]],
                    status=row[idx["statuscode"]],
                    mimetype=row[idx["mimetype"]],
                    digest=row[idx["digest"]],
                    length=length,
                )
            )
        return out

    # -- capture replay ---------------------------------------------------
    def fetch(self, url: str, ts: str) -> FetchResult:
        """Fetch one exact capture. Content is returned decompressed."""
        final, body, status = self._request(REPLAY.format(ts=ts, url=url), accept="text/html")
        if len(body) < self.cfg.min_body_bytes:
            raise Retryable(f"suspiciously small body ({len(body)} bytes) for {ts}")
        match = re.search(r"/web/(\d{14})", final or "")
        return FetchResult(
            ts=ts,
            effective_ts=match.group(1) if match else ts,
            body=body,
            http_status=status,
        )
