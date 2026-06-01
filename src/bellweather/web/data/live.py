"""Live backend STUB — the "endpoints after" half of the seam.

Each function will issue an httpx call to a real Bellwether read-endpoint
(``GET /api/...``) and assemble the same DataFrame/dict shapes the mock backend
returns (see ``bellweather.web.data.source`` for the column/key contract). Filling these in,
together with the matching FastAPI endpoints, is future work; the screens do not
change when it lands — only ``BELLWEATHER_UI_SOURCE`` flips from ``mock`` to ``live``.
"""

from __future__ import annotations

_MSG = (
    "Live data backend is not implemented yet. Run with BELLWEATHER_UI_SOURCE=mock "
    "(the default) until the read-endpoints exist."
)


def get_tracked_symbols():
    raise NotImplementedError(_MSG)


def get_observations(keys, start=None, end=None):
    raise NotImplementedError(_MSG)


def query_raw_records(
    source=None,
    content_type=None,
    status=None,
    search=None,
    start=None,
    end=None,
    limit=100,
    offset=0,
):
    raise NotImplementedError(_MSG)


def query_tags(tag_type=None, search=None, start=None, end=None, limit=100, offset=0):
    raise NotImplementedError(_MSG)


def get_queue_stats():
    raise NotImplementedError(_MSG)


def get_worker_runs():
    raise NotImplementedError(_MSG)


def get_ingestion_rate():
    raise NotImplementedError(_MSG)


def get_settings_view():
    raise NotImplementedError(_MSG)
