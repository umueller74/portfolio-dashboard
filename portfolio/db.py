import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists trades(
  id text primary key,
  isin text not null,
  name text not null,
  side text not null check (side in ('BUY','SELL')),
  kind text not null,
  executed_at text not null,
  quantity text not null,
  price text not null,
  amount text not null,
  fee text not null default '0'
);
create index if not exists trades_isin on trades(isin);
create table if not exists closes(
  isin text not null, day text not null, price text not null,
  primary key (isin, day)
);
create table if not exists quotes(
  isin text primary key, price text not null, quoted_at text not null, outdated integer not null
);
create table if not exists meta(key text primary key, value text not null);
create table if not exists signals(
  isin text primary key, state text not null check (state in ('above','below')), day text not null
);
create table if not exists ticks(isin text not null, at text not null, price text not null, primary key (isin, at));
create table if not exists backfills(isin text primary key, fetched_at text not null, points integer not null);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    return conn
