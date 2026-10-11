import sqlite3

import pytest

from portfolio.db import connect


def test_schema_has_expected_tables(tmp_path):
    conn = connect(tmp_path / "p.db")
    names = {r["name"] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"trades", "closes", "quotes", "meta"} <= names


def test_side_is_constrained(tmp_path):
    conn = connect(tmp_path / "p.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into trades(id,isin,name,side,kind,executed_at,quantity,price,amount,fee)"
            " values('x','I','n','HOLD','SINGLE','2026-01-01T00:00:00+00:00','1','1','1','0')"
        )
