"""Closure findings: a reusable, sourced record of which places have shut.

The findings come from a per-restaurant research pass (see ``research/closures``),
not from a heuristic: the archived list never records closures, and leaving the
list is not evidence of one — Gramercy Tavern and Cosme both left the list years
ago and are still open.

Each finding must carry a source. A place with no sourceable status is recorded
as ``unknown`` and is *not* treated as closed, because a wrong "closed" silently
deletes a restaurant from the history.
"""
from __future__ import annotations

import glob
import json
import os
import time

VALID_STATUS = {"open", "closed", "unknown"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def load_research(pattern: str) -> tuple[dict, list[str]]:
    """Merge every ``batch_*.out.json`` into {place_id: finding}."""
    findings: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{os.path.basename(path)}: unreadable ({exc})")
            continue
        if not isinstance(rows, list):
            problems.append(f"{os.path.basename(path)}: not a list")
            continue
        for row in rows:
            pid = row.get("id")
            if not pid:
                problems.append(f"{os.path.basename(path)}: entry without id")
                continue
            status = row.get("status")
            if status not in VALID_STATUS:
                problems.append(f"{pid}: bad status {status!r}")
                status = "unknown"
            source = row.get("source_url")
            if status != "unknown" and not source:
                # Never let an unsourced verdict through.
                problems.append(f"{pid}: {status} with no source -> demoted to unknown")
                status = "unknown"
            confidence = row.get("confidence")
            if confidence not in VALID_CONFIDENCE:
                confidence = "low"
            if status == "closed" and confidence == "low":
                # A low-confidence closure rests on aggregator listings rather
                # than reporting. Removing a restaurant on that basis is exactly
                # the silent-corruption case, so it stays recorded but inert.
                problems.append(f"{pid}: low-confidence closed -> recorded as unknown")
                status = "unknown"
            findings[pid] = {
                "status": status,
                "closed_date": row.get("closed_date") or None,
                "source_url": source or None,
                "evidence": (row.get("evidence") or "").strip(),
                "confidence": confidence,
            }
    return findings, problems


def write_closed(path: str, findings: dict, total_places: int) -> dict:
    """Write the reusable closed list, plus the full findings alongside it."""
    closed = {pid: f for pid, f in sorted(findings.items()) if f["status"] == "closed"}
    doc = {
        "_comment": (
            "Places confirmed permanently closed, with the source for each. "
            "Curated by a sourced research pass, not inferred: leaving the Eater "
            "list is not evidence of closure. Add entries by hand if you like; "
            "an entry needs status 'closed' and a source_url to take effect."
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": {
            "places_reviewed": len(findings),
            "places_on_list": total_places,
            "closed": len(closed),
            "open": sum(1 for f in findings.values() if f["status"] == "open"),
            "unknown": sum(1 for f in findings.values() if f["status"] == "unknown"),
        },
        "places": closed,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)

    detail = os.path.join(os.path.dirname(path) or ".", "closure_findings.json")
    with open(detail, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": doc["generated_at"], "findings": findings},
                  fh, indent=1, ensure_ascii=False)
    return doc


def load_closed(path: str) -> dict:
    """Read the closed list. Returns {place_id: finding} (possibly empty)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    places = doc.get("places", {})
    return {
        pid: f for pid, f in places.items()
        if isinstance(f, dict) and f.get("status") == "closed" and f.get("source_url")
    }
