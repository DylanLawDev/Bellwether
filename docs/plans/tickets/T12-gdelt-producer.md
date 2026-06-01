# T12 — Reference GDELT producer (external script)

**Spec:** §7 GDELT reference producer, README §5.2. D1 (producers are external).
**Depends on:** T08, T11. **Branch:** `ticket/T12-gdelt-producer`. **PR, do not merge without approval.**

## Goal
A standalone script that demonstrates an **external** producer: it fetches a GDELT GKG batch, normalizes each row into our expected `payload`, and submits via `BellwetherClient`. It uses nothing privileged — exactly what a third-party scraper or LLM agent would do.

## ⚠️ Verify before building (README §5.2)
GDELT integration details drift. **Before writing fetch code, confirm the current GKG v2 file layout** at the GDELT master file list (`http://data.gdeltproject.org/gdeltv2/masterfilelist.txt`) and the GKG column order. Wire the real column indices into `parse_gkg_line` (documented inline). Keep the network fetch behind a function so tests use a local fixture file, not the live feed.

## Files
- Create: `producers/gdelt/__init__.py`, `producers/gdelt/producer.py`, `producers/gdelt/README.md`
- Test: `tests/test_gdelt_producer.py`, `tests/fixtures/gkg_sample.csv` (a few tab-separated GKG rows)

## Interface
```python
# producers/gdelt/producer.py
def parse_gkg_line(line: str) -> dict: ...           # one TSV row -> our payload dict
def rows_to_submissions(lines: Iterable[str]) -> list[Submission]: ...
def run(path_or_url: str, client: BellwetherClient | None = None) -> list[IngestResult]: ...
```
- `idempotency_key` = the GKG record id (column 0), guaranteeing dedup on re-runs.
- `content_type="gdelt-gkg-v2"`, `kind="unstructured"`, `source="gdelt.gkg"`.
- `fetched_at` parsed from the record date column (UTC).

## Steps

- [ ] **Step 1: Fixture** `tests/fixtures/gkg_sample.csv` — 2–3 tab-separated rows containing at least: record id, date, V2Themes, V2Persons, V2Organizations, V2Locations, V1.5Tone columns. (Use realistic but small values; document which column index maps to which field in a comment at the top of the test.)

- [ ] **Step 2: Failing test** `tests/test_gdelt_producer.py`
```python
import pathlib
from producers.gdelt.producer import parse_gkg_line, rows_to_submissions

LINES = (pathlib.Path(__file__).parent / "fixtures/gkg_sample.csv").read_text().splitlines()

def test_parse_extracts_payload_fields():
    p = parse_gkg_line(LINES[0])
    assert "v2_themes" in p and "v15_tone" in p and "gkg_record_id" in p and "date" in p

def test_rows_to_submissions_uses_record_id_as_idempotency_key():
    subs = rows_to_submissions(LINES)
    assert subs[0].idempotency_key == parse_gkg_line(LINES[0])["gkg_record_id"]
    assert subs[0].content_type == "gdelt-gkg-v2" and subs[0].source == "gdelt.gkg"
    assert subs[0].fetched_at.tzinfo is not None
```
- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement `producer.py`** (column indices are placeholders — set them from the verified GKG spec; the ones below match GKG v2.1 documentation at time of writing — re-confirm)
```python
from collections.abc import Iterable
from datetime import datetime, timezone
import httpx
from bellweather.client import BellwetherClient
from bellweather.contracts import Submission, IngestResult

# GKG v2.1 column indices — VERIFY against current GDELT docs (README §5.2)
COL_RECORD_ID = 0     # GKGRECORDID
COL_DATE = 1          # V2.1DATE  (YYYYMMDDHHMMSS)
COL_V2THEMES = 7      # V2EnhancedThemes
COL_V2PERSONS = 11    # V2EnhancedPersons
COL_V2ORGS = 13       # V2EnhancedOrganizations
COL_V2LOCATIONS = 9   # V2EnhancedLocations
COL_TONE = 15         # V1.5Tone

def _clean_enhanced(field: str) -> str:
    # Enhanced fields are "Name,offset;Name,offset" — keep names, drop offsets.
    out = []
    for item in field.split(";"):
        name = item.split(",")[0].strip()
        if name:
            out.append(name)
    return ";".join(out)

def parse_gkg_line(line: str) -> dict:
    c = line.split("\t")
    raw_date = c[COL_DATE]
    dt = datetime.strptime(raw_date, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
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
    subs = []
    for line in lines:
        if not line.strip():
            continue
        p = parse_gkg_line(line)
        subs.append(Submission(
            source="gdelt.gkg", kind="unstructured", content_type="gdelt-gkg-v2",
            fetched_at=datetime.fromisoformat(p["date"]), idempotency_key=p["gkg_record_id"],
            payload=p, provenance={"producer": "gdelt-reference", "record_id": p["gkg_record_id"]},
        ))
    return subs

def _fetch_lines(path_or_url: str) -> list[str]:
    if path_or_url.startswith("http"):
        return httpx.get(path_or_url, timeout=60).text.splitlines()
    with open(path_or_url, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()

def run(path_or_url: str, client: BellwetherClient | None = None) -> list[IngestResult]:
    client = client or BellwetherClient()
    subs = rows_to_submissions(_fetch_lines(path_or_url))
    return client.ingest_batch(subs)
```
- [ ] **Step 5: Run** → PASS (parsing tests; `run` is covered by the integration note below, not the live feed).
- [ ] **Step 6: `producers/gdelt/README.md`** — document: how to point at a local GKG file vs the live feed, the column-verification caveat, and the command `uv run python -m producers.gdelt.producer <path-or-url>` (add an `if __name__ == "__main__"` calling `run(sys.argv[1])`).
- [ ] **Step 7: Commit** (`feat: add reference GDELT producer`).

## Acceptance criteria
- `parse_gkg_line` yields the payload shape T10 expects (enhanced offsets stripped).
- `rows_to_submissions` uses the GKG record id as `idempotency_key` (re-runs dedup cleanly).
- Network fetch is isolated behind `_fetch_lines`; tests use the fixture, not the live feed.
- Column indices carry a visible "VERIFY against current GDELT docs" caveat.
