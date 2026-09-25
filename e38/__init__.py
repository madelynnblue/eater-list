"""Eater 38 list-history pipeline.

A small, dependency-free toolkit that:

1. asks the Wayback Machine CDX API which captures of a page exist (cheap),
2. fetches only enough captures to determine when the list changed,
3. stores raw extractions in SQLite so nothing is ever fetched twice,
4. derives per-restaurant on-list periods and update history,
5. writes JSON ready for a web front-end plus CSV/Markdown for humans.
"""

__version__ = "1.0.0"

EXTRACTOR_VERSION = 4
