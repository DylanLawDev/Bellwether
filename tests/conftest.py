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
