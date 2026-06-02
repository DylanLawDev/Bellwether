"""Reference EXTERNAL producer: GDELT GKG 2.1 -> Bellwether ingest.

Fetches a GDELT GKG batch (local file or live feed URL), normalizes each TSV
row into the payload shape consumed by T10's ``GdeltGkgExtractor``, and submits
the batch via ``BellwetherClient``. Uses nothing privileged.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from bellweather.client import BellwetherClient
from bellweather.contracts import IngestResult, Submission

# GKG 2.1 tab-delimited column indices.
# VERIFY against current GDELT docs (GKG 2.1 codebook,
# http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf).
# The four list fields use the V2 *Enhanced* columns ("Name,offset;Name,offset"),
# NOT the V1 columns. Canonical 2.1 order:
#   0 GKGRECORDID  1 V2.1DATE  2 SourceCollectionId  3 SourceCommonName
#   4 DocumentIdentifier  5 V1Counts  6 V2.1Counts  7 V1Themes  8 V2EnhancedThemes
#   9 V1Locations  10 V2EnhancedLocations  11 V1Persons  12 V2EnhancedPersons
#   13 V1Organizations  14 V2EnhancedOrganizations  15 V1.5Tone  ...
COL_RECORD_ID = 0  # GKGRECORDID
COL_DATE = 1  # V2.1DATE (YYYYMMDDHHMMSS)
COL_V2THEMES = 8  # V2EnhancedThemes
COL_V2LOCATIONS = 10  # V2EnhancedLocations
COL_V2PERSONS = 12  # V2EnhancedPersons
COL_V2ORGS = 14  # V2EnhancedOrganizations
COL_TONE = 15  # V1.5Tone (comma list; first value = overall tone)


def _clean_enhanced(field: str) -> str:
    # Enhanced fields are "Name,offset;Name,offset" -- keep names, drop offsets.
    out = []
    for item in field.split(";"):
        name = item.split(",")[0].strip()
        if name:
            out.append(name)
    return ";".join(out)


def parse_gkg_line(line: str) -> dict:
    """Parse one GKG TSV row into our payload dict."""
    c = line.split("\t")
    dt = datetime.strptime(c[COL_DATE], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return {
        "gkg_record_id": c[COL_RECORD_ID],
        "date": dt.isoformat(),
        "v2_themes": _clean_enhanced(c[COL_V2THEMES]) if len(c) > COL_V2THEMES else "",
        "v2_persons": _clean_enhanced(c[COL_V2PERSONS]) if len(c) > COL_V2PERSONS else "",
        "v2_organizations": _clean_enhanced(c[COL_V2ORGS]) if len(c) > COL_V2ORGS else "",
        "v2_locations": _clean_enhanced(c[COL_V2LOCATIONS]) if len(c) > COL_V2LOCATIONS else "",
        "v15_tone": c[COL_TONE] if len(c) > COL_TONE else "",
    }


def rows_to_submissions(lines: Iterable[str]) -> list[Submission]:
    """Normalize GKG TSV rows into Bellwether submissions."""
    subs: list[Submission] = []
    for line in lines:
        if not line.strip():
            continue
        p = parse_gkg_line(line)
        subs.append(
            Submission(
                source="gdelt.gkg",
                kind="unstructured",
                content_type="gdelt-gkg-v2",
                fetched_at=datetime.fromisoformat(p["date"]),
                idempotency_key=p["gkg_record_id"],
                payload=p,
                provenance={"producer": "gdelt-reference", "record_id": p["gkg_record_id"]},
            )
        )
    return subs


def _fetch_lines(path_or_url: str) -> list[str]:
    if path_or_url.startswith("http"):
        resp = httpx.get(path_or_url, timeout=60)
        resp.raise_for_status()
        return resp.text.splitlines()
    with open(path_or_url, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _default_client() -> BellwetherClient:
    # An external producer has only BELLWEATHER_API_URL, never the server's DB /
    # storage settings. Build the client from the public URL directly so we don't
    # trip BellwetherClient()'s fallback to get_settings(), which requires
    # database_url and bellweather_bucket and would crash the producer before it
    # could reach the ingest API.
    return BellwetherClient(base_url=os.environ.get("BELLWEATHER_API_URL", "http://localhost:8000"))


def run(params: dict, client) -> dict:
    """Orchestrator template entrypoint (manifest: producers/gdelt/template.toml).

    Wraps the existing fetch+parse logic in the locked entrypoint contract
    ``def run(params: dict, client) -> dict | None``. ``params["url"]`` is a GKG
    file URL or local path (a master-file entry); ``client`` is injected by the
    run-harness (a real ``BellwetherClient`` on a scheduled run, a ``DryRunClient``
    for a preview). GDELT stays UNSTRUCTURED (``content_type="gdelt-gkg-v2"``),
    handled by the existing extractor — no numeric-series-v1 here.
    """
    subs = rows_to_submissions(_fetch_lines(params["url"]))
    results = client.ingest_batch(subs)
    return {"submitted": len(results)}


def run_path(path_or_url: str, client: BellwetherClient | None = None) -> list[IngestResult]:
    """Fetch a GKG batch, normalize it, and ingest via the Bellwether client.

    Manual/CLI helper (used by ``__main__`` below). The orchestrator template
    entrypoint is ``run(params, client)``.
    """
    client = client or _default_client()
    subs = rows_to_submissions(_fetch_lines(path_or_url))
    return client.ingest_batch(subs)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m producers.gdelt.producer <path-or-url>", file=sys.stderr)
        raise SystemExit(2)
    results = run_path(sys.argv[1])
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
    print(f"ingested {len(results)} record(s): {summary or 'none'}")
