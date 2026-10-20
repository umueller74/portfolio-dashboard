"""The Scalable watchlist, mirrored from each fetch into `watchlist`, and the sell-out rules that edit it.
When a position goes to zero, the dashboard adds its ISIN to the Scalable watchlist once for that sell-out,
and removes it again 3 months after the last trade. The fetcher performs the adds and removes (`actions`)
before its next fetch; the list it then sends confirms them. `watch_auto` keeps one row per sold-out ISIN:
  pending_add    -> added (seen on the list)
  added          -> pending_remove (3 months after the sell-out) | done (the user removed it)
  pending_remove -> done (gone from the list)
An ISIN already on the list at its sell-out is the user's own: `done` at once, never touched. Sell-outs
before `watch_since` (the first fetch that read the list) are ignored, so the release writes nothing.
A new buy deletes the row; the next sell-out (a new day) starts over."""
import calendar
from datetime import date
from decimal import Decimal

from portfolio import services
from portfolio.fifo import OversellError, replay

HOLD_MONTHS = 3


def plus_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _held_and_sellouts(conn) -> tuple[set[str], dict[str, date]]:
    trades = services.load_trades(conn)
    lots, _ = replay(trades)  # raises OversellError
    left = {t.isin: Decimal(0) for t in trades}
    for lot in lots:
        left[lot.trade.isin] += lot.remaining
    last = {t.isin: services._day(t.executed_at) for t in trades}  # sorted by time: the last one wins
    return {i for i, q in left.items() if q > 0}, {i: last[i] for i, q in left.items() if q == 0}


def sellouts(conn) -> dict[str, date]:
    """ISIN -> Berlin day of its last trade, for every traded ISIN with nothing left."""
    try:
        return _held_and_sellouts(conn)[1]
    except OversellError:
        return {}


def _since(conn) -> date | None:
    row = conn.execute("select value from meta where key='watch_since'").fetchone()
    return date.fromisoformat(row[0]) if row else None


def reconcile(conn, today: date) -> None:
    since = _since(conn)
    try:
        held, sold = _held_and_sellouts(conn)
    except OversellError:
        return  # broken trade data: change nothing on the user's list
    on = {r[0] for r in conn.execute("select isin from watchlist")}
    rows = {r["isin"]: (r["sold_on"], r["state"]) for r in conn.execute("select * from watch_auto")}
    with conn:
        for isin in held & rows.keys():
            conn.execute("delete from watch_auto where isin=?", (isin,))
            del rows[isin]
        for isin, day in sold.items():
            if since is None or day < since or (isin in rows and rows[isin][0] == day.isoformat()):
                continue
            rows[isin] = (day.isoformat(), "done" if isin in on else "pending_add")
            conn.execute("insert or replace into watch_auto(isin,sold_on,state) values(?,?,?)",
                         (isin, *rows[isin]))
        for isin, (sold_on, st) in rows.items():
            new = st
            if st == "pending_add" and isin in on:
                new = "added"
            if new == "added" and isin not in on:
                new = "done"
            elif new == "added" and today >= plus_months(date.fromisoformat(sold_on), HOLD_MONTHS):
                new = "pending_remove"
            elif new == "pending_remove" and isin not in on:
                new = "done"
            if new != st:
                conn.execute("update watch_auto set state=? where isin=?", (new, isin))


def actions(conn) -> dict:
    rows = conn.execute("select isin, state from watch_auto where state in ('pending_add','pending_remove')"
                        " order by isin").fetchall()
    return {"add": [r[0] for r in rows if r[1] == "pending_add"],
            "remove": [r[0] for r in rows if r[1] == "pending_remove"]}


def watched(conn) -> dict[str, dict]:
    """ISINs for the Watchlist group (when not held): the Scalable list plus auto-adds not yet confirmed."""
    out = {r["isin"]: {"name": r["name"], "type": r["type"]}
           for r in conn.execute("select * from watchlist")}
    names = {t.isin: t.name for t in services.load_trades(conn)}
    for (isin,) in conn.execute("select isin from watch_auto where state in ('pending_add','added')"):
        out.setdefault(isin, {"name": names.get(isin, isin), "type": ""})
    return out


def remember(conn, isin: str, name: str, type: str) -> None:
    with conn:
        conn.execute("insert into watchlist(isin,name,type) values(?,?,?)"
                     " on conflict(isin) do update set name=excluded.name, type=excluded.type", (isin, name, type))


def manual(conn, isin: str, added: bool) -> None:
    """The user changed the entry in the popup: it is theirs now, never auto-removed."""
    with conn:
        conn.execute("update watch_auto set state='done' where isin=?", (isin,))
        if not added:
            conn.execute("delete from watchlist where isin=?", (isin,))
