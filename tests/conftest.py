import os
import socket

import pytest


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
