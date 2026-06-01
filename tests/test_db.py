from bellweather.db import get_conn


def test_can_select_one():
    with get_conn() as conn:
        cur = conn.execute("select 1")
        assert cur.fetchone()[0] == 1
