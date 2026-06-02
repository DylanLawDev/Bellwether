from datetime import datetime

from bellweather.normalizers import NormalizedPoint, register


class NumericSeriesNormalizer:
    content_type = "numeric-series-v1"

    def normalize(self, envelope: dict) -> list[NormalizedPoint]:
        p = envelope["payload"]
        symbol_key = p["symbol_key"]
        symbol_kind = p["symbol_kind"]
        unit = p.get("unit")
        description = p.get("description")
        points: list[NormalizedPoint] = []
        for point in p.get("points") or []:
            points.append(
                NormalizedPoint(
                    symbol_key=symbol_key,
                    symbol_kind=symbol_kind,
                    ts=datetime.fromisoformat(point["ts"]),
                    value=float(point["value"]),
                    unit=unit,
                    description=description,
                )
            )
        return points


register(NumericSeriesNormalizer())
