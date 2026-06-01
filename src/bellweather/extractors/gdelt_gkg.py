from bellweather.extractors import ExtractedTag, register

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
                tags.append(
                    ExtractedTag(tag_type="tone", raw_value="tone", score={"tone": float(first)})
                )
        return tags


register(GdeltGkgExtractor())
