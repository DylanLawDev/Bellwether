import json
import os
import subprocess
import time

from bellweather import schedules
from bellweather.config import get_settings
from bellweather.db import get_conn


def _run_subprocess(template: str, params: dict, *, timeout: int = 600) -> dict:
    proc = subprocess.run(
        [
            "bellweather",
            "run-template",
            "--template",
            template,
            "--params",
            json.dumps(params),
        ],
        env={
            "BELLWEATHER_API_URL": get_settings().bellweather_api_url,
            "PATH": os.environ["PATH"],
        },  # K4: ingest URL only — never DATABASE_URL / BELLWEATHER_BUCKET
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    proc.check_returncode()
    last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
    return json.loads(last)


def tick(conn) -> list[int]:
    """Run every due schedule once; return the started producer_runs ids."""
    started: list[int] = []
    for s in schedules.due_schedules(conn):
        claimed = schedules.claim(conn, s["id"])
        conn.commit()
        if not claimed:
            continue  # Another concurrent tick already claimed this schedule
        run_id = schedules.start_run(
            conn, schedule_id=s["id"], template=s["template"], params=s["params"]
        )
        conn.commit()
        try:
            summary = _run_subprocess(s["template"], s["params"])
            submitted = summary.get("submitted")
            if submitted is not None:
                try:
                    submitted = int(submitted)
                except (TypeError, ValueError):
                    submitted = None
            schedules.finish_run(conn, run_id, status="ok", submitted=submitted)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            schedules.finish_run(conn, run_id, status="error", error=str(e))
        conn.commit()
        started.append(run_id)
    return started


def run_orchestrator(once: bool = False) -> None:
    while True:
        with get_conn() as conn:
            started = tick(conn)
            conn.commit()
        if once:
            return
        if not started:
            time.sleep(2)
