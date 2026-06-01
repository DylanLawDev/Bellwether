import json
import pathlib

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
    payload = {
        "v2_themes": "",
        "v2_persons": "",
        "v2_organizations": "",
        "v2_locations": "",
        "v15_tone": "",
        "date": "2026-05-31T14:15:00Z",
    }
    tags = GdeltGkgExtractor().extract({"payload": payload})
    assert tags == []


def test_extractor_is_registered_on_import():
    from bellweather.extractors import get_extractor

    ex = get_extractor("gdelt-gkg-v2")
    assert ex is not None
    assert ex.content_type == "gdelt-gkg-v2"
