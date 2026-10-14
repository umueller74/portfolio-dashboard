"""Moving-average crossover alerts for the ETFs currently held: a BUY when the 20-day average of the closes
rises above the 50-day one, a SELL when it falls below. The last side per ETF is kept in `signals`; an ETF
seen for the first time (or bought again after selling out) is recorded without an alert."""
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from portfolio import services

log = logging.getLogger("portfolio.signals")
SESSION_END_HOUR = 22      # gettex closes 22:00 Berlin; until then today's close is still moving
FAST, SLOW = 20, 50

Sender = Callable[[str], None]


@dataclass(frozen=True)
class Signal:
    isin: str
    name: str
    action: str          # BUY | SELL
    day: date
    close: float
    fast: float
    slow: float

    def text(self) -> str:
        icon, side = ("📈", "above") if self.action == "BUY" else ("📉", "below")
        return (f"{icon} {self.action} signal: {self.name} ({self.isin})\n"
                f"MA {FAST}d {self.fast:,.2f} crossed {side} MA {SLOW}d {self.slow:,.2f} "
                f"on {self.day.isoformat()} (close {self.close:,.2f})")


def _last_session(conn, isin: str, now: datetime) -> tuple[date, float] | None:
    local = now.astimezone(services.BERLIN)
    rows = [(d, p) for d, p in services._closes(conn).get(isin, [])
            if d < local.date() or (d == local.date() and local.hour >= SESSION_END_HOUR)]
    return (rows[-1][0], float(rows[-1][1])) if rows else None


def check(conn, now: datetime, send: Sender | None) -> list[Signal]:
    """Compare each held ETF's averages on its last completed session with the stored side and alert on a
    change. Without `send` changes are only recorded. A failed send leaves the old side, so the next check
    retries. Returns the signals that were sent."""
    ov = services.overview(conn, services._day(now))
    if ov.error:
        return []
    names = {h.isin: h.name for h in ov.holdings}
    stored = {r["isin"]: r["state"] for r in conn.execute("select isin, state from signals")}
    with conn:
        for isin in stored.keys() - names.keys():          # sold out: start silently if bought again
            conn.execute("delete from signals where isin=?", (isin,))
    sent: list[Signal] = []
    for isin, name in names.items():
        last = _last_session(conn, isin, now)
        if last is None:
            continue
        day, close = last
        ma = services.moving_averages(conn, isin, [day], (FAST, SLOW))
        fast, slow = ma[FAST][0], ma[SLOW][0]
        if fast is None or slow is None or fast == slow:
            continue
        side = "above" if fast > slow else "below"
        if stored.get(isin) == side:
            continue
        if isin in stored and send is not None:
            sig = Signal(isin, name, "BUY" if side == "above" else "SELL", day, close, fast, slow)
            try:
                send(sig.text())
            except Exception:
                log.exception("sending the %s signal for %s failed; retrying on the next check", sig.action, isin)
                continue
            sent.append(sig)
        with conn:
            conn.execute("insert into signals(isin,state,day) values(?,?,?)"
                         " on conflict(isin) do update set state=excluded.state, day=excluded.day",
                         (isin, side, day.isoformat()))
    return sent


def telegram_sender(token: str | None, chat_id: str | None, opener=urllib.request.urlopen) -> Sender | None:
    """A sender posting plain-text messages through the Telegram Bot API; None unless both values are set."""
    if not token or not chat_id:
        return None

    def send(text: str) -> None:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", method="POST",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        with opener(req, timeout=10) as resp:
            resp.read()

    return send
