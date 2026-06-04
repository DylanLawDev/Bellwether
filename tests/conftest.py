import os
import socket

import pytest

from bellweather.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # get_settings() is a process-wide @lru_cache. Reset it around every test so a
    # test that monkeypatches the environment can never leak a stale Settings (e.g.
    # a throwaway DATABASE_URL) into a later test. Defense-in-depth alongside the
    # explicit fixture in test_config.py.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def clear_records(conn, source, keys=None):
    """Delete raw_records and their FK children (tags, work_queue) for `source`.

    If `keys` is given, restrict to those idempotency_keys; else clear the whole
    source. tags and work_queue both FK-reference raw_records, so children are
    deleted before the parent. This helper does NOT commit — the caller owns the
    transaction (it is typically called inside the same `with get_conn()` block
    that commits other setup).
    """
    keyed = " and idempotency_key = any(%s)" if keys is not None else ""
    params = (list(keys),) if keys is not None else ()
    child_select = "select id from raw_records where source=%s" + keyed
    for child in ("tags", "work_queue"):
        conn.execute(
            f"delete from {child} where raw_record_id in ({child_select})",
            (source, *params),
        )
    conn.execute(
        "delete from raw_records where source=%s" + keyed,
        (source, *params),
    )


def clear_observations(conn, symbol_keys):
    """Delete observations for the given tracked_symbols.key values.

    Used to reset shared coverage rows so value/sample_count assertions are
    deterministic. Does NOT commit — caller owns the transaction.
    """
    conn.execute(
        "delete from observations where tracked_symbol_id in"
        " (select id from tracked_symbols where key = any(%s))",
        (list(symbol_keys),),
    )


def _gcs_reachable() -> bool:
    host = os.environ.get("STORAGE_EMULATOR_HOST")
    if not host:
        return False
    netloc = host.split("//", 1)[-1]
    h, _, p = netloc.partition(":")
    try:
        socket.create_connection((h, int(p or 80)), timeout=1).close()
        return True
    except OSError:
        return False


requires_gcs = pytest.mark.skipif(not _gcs_reachable(), reason="GCS emulator not reachable")

requires_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)

requires_gemini = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"
)
