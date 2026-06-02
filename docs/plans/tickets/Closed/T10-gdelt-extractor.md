# T10 — GDELT GKG v2 extractor → tags

**Spec:** §7 (GDELT reference), D8 (borrowed extraction, no bespoke NLP), README §5.2.
**Depends on:** T09. **Branch:** `ticket/T10-gdelt-extractor`. **PR, do not merge without approval.**

## Goal
Parse GDELT's **already-extracted** GKG v2 fields (themes, persons, organizations, locations, tone) into `ExtractedTag`s. This is "borrowed extraction" — no NLP of our own.

## Background (verify against current GDELT docs at build time — README §5.2)
A GKG v2.1 record (one row) has tab-separated columns; the producer (T12) will pre-parse the relevant columns into a JSON `payload`. **This ticket defines the payload shape we expect and parses it.** Expected `payload` (already normalized by the producer):
```json
{
  "gkg_record_id": "20260531141500-42",
  "v2_themes": "TAX_FNCACT;ECON_STOCKMARKET;EPU_POLICY",
  "v2_persons": "Joe Biden;Jerome Powell",
  "v2_organizations": "Federal Reserve;Treasury",
  "v2_locations": "Washington;New York",
  "v15_tone": "-2.13,4.5,6.6,11.1,...",
  "date": "2026-05-31T14:15:00Z"
}
```
- `v2_themes/persons/organizations/locations` are `;`-separated lists (may be empty strings).
- `v15_tone` is a comma list whose **first value is the overall tone** (float).

## Mapping to tags
| Source field | `tag_type` | `raw_value` | `score` |
|---|---|---|---|
| each `v2_themes` item | `theme` | the theme code | `{}` |
| each `v2_persons` item | `person` | the name | `{}` |
| each `v2_organizations` item | `org` | the name | `{}` |
| each `v2_locations` item | `location` | the place | `{}` |
| `v15_tone` (first value) | `tone` | `"tone"` | `{"tone": <float>}` |

## Files
- Create: `src/bellweather/extractors/gdelt_gkg.py`
- Test: `tests/test_gdelt_extractor.py`, `tests/fixtures/gkg_payload.json`

## Steps

- [ ] **Step 1: Fixture** `tests/fixtures/gkg_payload.json` — use the example payload above.
- [ ] **Step 2: Failing test** `tests/test_gdelt_extractor.py`
```python
import json, pathlib
from bellweather.extractors.gdelt_gkg import GdeltGkgExtractor

PAYLOAD = json.loads((pathlib.Path(__file__).parent / "fixtures/gkg_payload.json").read_text())

def test_extracts_all_tag_types():
    tags = GdeltGkgExtractor().extract({"payload": PAYLOAD})
    by_type = {}
    for t in tags:
        by_type.setdefault(t.tag_type, []).append(t.raw_value)
    assert "ECON_STOCKMARKET" in by_type["theme"]
    assert "Joe Biden" in by_type["person"]
    assert "Federal Reserve" in by_type["org"]
    assert "Washington" in by_type["location"]
    tone = [t for t in tags if t.tag_type == "tone"][0]
    assert tone.score["tone"] == -2.13

def test_empty_fields_produce_no_tags():
    payload = {"v2_themes": "", "v2_persons": "", "v2_organizations": "",
               "v2_locations": "", "v15_tone": "", "date": "2026-05-31T14:15:00Z"}
    tags = GdeltGkgExtractor().extract({"payload": payload})
    assert tags == []
```
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement `extractors/gdelt_gkg.py`**
```python
from bellweather.extractors import ExtractedTag, Extractor, register

_FIELD_MAP = {
    "v2_themes": "theme",
    "v2_persons": "person",
    "v2_organizations": "org",
    "v2_locations": "location",
}

class GdeltGkgExtractor:
    content_type = "gdelt-gkg-v2"

    def extract(self, envelope: dict) -> list[ExtractedTag]:
        p = envelope["payload"]
        tags: list[ExtractedTag] = []
        for field, tag_type in _FIELD_MAP.items():
            raw = (p.get(field) or "").strip()
            if not raw:
                continue
            for item in raw.split(";"):
                item = item.strip()
                if item:
                    tags.append(ExtractedTag(tag_type=tag_type, raw_value=item, score={}))
        tone_raw = (p.get("v15_tone") or "").strip()
        if tone_raw:
            first = tone_raw.split(",")[0].strip()
            if first:
                tags.append(ExtractedTag(tag_type="tone", raw_value="tone", score={"tone": float(first)}))
        return tags

register(GdeltGkgExtractor())
```
- [ ] **Step 5: Run** → PASS. Commit (`feat: add GDELT GKG v2 extractor`).

## Acceptance criteria
- Themes/persons/orgs/locations split on `;`; tone parsed from the first comma value.
- Empty fields yield no tags (no crashes, no empty-string tags).
- `register()` runs on import so the registry knows `gdelt-gkg-v2`.
