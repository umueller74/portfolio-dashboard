"""The Scalable watchlist, mirrored from each fetch into `watchlist` (keeping popup adds the fetch could not
see yet), and the sell-out rules that edit it.
When a position goes to zero, the dashboard adds its ISIN to the Scalable watchlist once for that sell-out,
and removes it again 3 months after the last trade. The fetcher performs the adds and removes (`actions`)
before its next fetch; the list it then sends confirms them. `watch_auto` keeps one row per sold-out ISIN:
  pending_add    -> added (seen on the list)
  added          -> pending_remove (3 months after the sell-out) | done (the user removed it)
  pending_remove -> done (gone from the list)
An ISIN already on the list at its sell-out is the user's own: `done` at once, never touched. Only sell-outs
whose last trade is after `watch_since` (the instant of the first fetch that read the list) count, so the
release writes nothing. While an ISIN is held again, a pending add is cancelled and a `done` row dropped,
but an entry the dashboard added stays its own (`pending_remove` back to `added`, the clock paused); its next
sell-out restarts the 3 months (`added` again, or `pending_add` if the user removed it meanwhile)."""
import calendar
from datetime import date, datetime, timezone
from decimal import Decimal

from portfolio import services
from portfolio.fifo import OversellError, replay

HOLD_MONTHS = 3


def plus_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _held_and_sellouts(conn) -> tuple[set[str], dict[str, datetime]]:
    """Held ISINs, and ISIN -> time of the last trade for the sold-out ones. Raises OversellError."""
    trades = services.load_trades(conn)
    lots, _ = replay(trades)
    left = {t.isin: Decimal(0) for t in trades}
    for lot in lots:
        left[lot.trade.isin] += lot.remaining
    last: dict[str, datetime] = {}
    for t in trades:
        last[t.isin] = max(last.get(t.isin, t.executed_at), t.executed_at)
    return {i for i, q in left.items() if q > 0}, {i: last[i] for i, q in left.items() if q == 0}


def sellouts(conn) -> dict[str, date]:
    """ISIN -> Berlin day of its last trade, for every traded ISIN with nothing left."""
    try:
        return {i: services._day(at) for i, at in _held_and_sellouts(conn)[1].items()}
    except OversellError:
        return {}


def _since(conn) -> datetime | None:
    row = conn.execute("select value from meta where key='watch_since'").fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def reconcile(conn, today: date) -> None:
    since = _since(conn)
    try:
        held, sold = _held_and_sellouts(conn)
    except OversellError:
        return  # broken trade data: change nothing on the user's list
    on = {r[0] for r in conn.execute("select isin from watchlist")}
    rows = {r["isin"]: (r["sold_on"], r["state"]) for r in conn.execute("select * from watch_auto")}

    def put(isin, sold_on, state):
        rows[isin] = (sold_on, state)
        conn.execute("insert or replace into watch_auto(isin,sold_on,state) values(?,?,?)", (isin, sold_on, state))

    with conn:
        for isin in held & rows.keys():
            sold_on, st = rows[isin]
            if st in ("pending_add", "done"):
                conn.execute("delete from watch_auto where isin=?", (isin,))
                del rows[isin]
            elif st == "pending_remove":
                put(isin, sold_on, "added")
        for isin, at in sold.items():
            day = services._day(at).isoformat()
            if since is None or at <= since or (isin in rows and rows[isin][0] == day):
                continue
            ours = isin in rows and rows[isin][1] in ("added", "pending_remove")  # rebuy of our own entry
            put(isin, day, ("added" if ours else "done") if isin in on else "pending_add")
        for isin, (sold_on, st) in rows.items():
            if isin in held:
                continue
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


def remember(conn, isin: str, name: str, type: str, now: datetime | None = None) -> None:
    """The user added the entry in the popup. `added_at` (now, UTC) keeps it through the ingest of a fetch
    that read the Scalable list before the add (see `replace`)."""
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with conn:
        conn.execute("insert into watchlist(isin,name,type,added_at) values(?,?,?,?)"
                     " on conflict(isin) do update set name=excluded.name, type=excluded.type,"
                     " added_at=excluded.added_at", (isin, name, type, at))


def replace(conn, entries: list[tuple[str, str, str]], read_at: datetime | None) -> None:
    """The list a fetch read replaces the stored one (inside the caller's transaction). Entries added in the
    popup after `read_at` (when the fetch read the list) are kept; without `read_at` nothing is. The fetch's
    entries carry no `added_at`."""
    keep = set()
    if read_at is not None:
        read_at = read_at if read_at.tzinfo else read_at.replace(tzinfo=timezone.utc)
        keep = {r["isin"] for r in conn.execute("select isin, added_at from watchlist where added_at is not null")
                if datetime.fromisoformat(r["added_at"]) > read_at}
    for (isin,) in conn.execute("select isin from watchlist").fetchall():
        if isin not in keep:
            conn.execute("delete from watchlist where isin=?", (isin,))
    conn.executemany("insert into watchlist(isin,name,type,added_at) values(?,?,?,null)"
                     " on conflict(isin) do update set name=excluded.name, type=excluded.type, added_at=null",
                     entries)


def manual(conn, isin: str, added: bool) -> None:
    """The user changed the entry in the popup: it is theirs now, never auto-removed."""
    with conn:
        conn.execute("update watch_auto set state='done' where isin=?", (isin,))
        if not added:
            conn.execute("delete from watchlist where isin=?", (isin,))
