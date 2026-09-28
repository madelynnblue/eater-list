"""Configuration loading and path resolution."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class Config:
    root: str
    db_path: str
    out_dir: str
    csv_dir: str
    raw_dir: str
    aliases_path: str
    closed_path: str
    urls: list[str] = field(default_factory=list)
    user_agent: str = "eater38-history/1.0"
    requests_per_second: float = 1.5
    burst: int = 4
    max_concurrency: int = 4
    timeout: float = 45.0
    max_retries: int = 5
    cdx_timeout: float = 25.0
    cdx_retries: int = 3
    min_body_bytes: int = 5000
    since: str = "2017-01-01"
    strategy: str = "bisect"
    max_fetches_per_run: int = 500
    refetch: bool = False
    store_raw: bool = True


def _resolve(root: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(root, path)


def load(path: str | None = None) -> Config:
    """Load config.toml, resolving every path against the config's directory."""
    if path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "config.toml")
    root = os.path.dirname(os.path.abspath(path))
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    wb = raw.get("wayback", {})
    cr = raw.get("crawl", {})
    bd = raw.get("build", {})

    return Config(
        root=root,
        db_path=_resolve(root, bd.get("db", "data/eater38.sqlite")),
        out_dir=_resolve(root, bd.get("out_dir", "web/data")),
        csv_dir=_resolve(root, bd.get("csv_dir", "out")),
        raw_dir=_resolve(root, bd.get("raw_dir", "data/raw")),
        aliases_path=_resolve(root, bd.get("aliases", "aliases.json")),
        closed_path=_resolve(root, bd.get("closed", "closed.json")),
        urls=list(wb.get("urls", [])),
        user_agent=wb.get("user_agent", "eater38-history/1.0"),
        requests_per_second=float(wb.get("requests_per_second", 1.5)),
        burst=int(wb.get("burst", 4)),
        max_concurrency=int(wb.get("max_concurrency", 4)),
        timeout=float(wb.get("timeout", 45.0)),
        cdx_timeout=float(wb.get("cdx_timeout", 25.0)),
        cdx_retries=int(wb.get("cdx_retries", 3)),
        max_retries=int(wb.get("max_retries", 5)),
        min_body_bytes=int(wb.get("min_body_bytes", 5000)),
        since=str(cr.get("since", "2017-01-01")),
        strategy=cr.get("strategy", "bisect"),
        max_fetches_per_run=int(cr.get("max_fetches_per_run", 500)),
        refetch=bool(cr.get("refetch", False)),
        store_raw=bool(cr.get("store_raw", True)),
    )
