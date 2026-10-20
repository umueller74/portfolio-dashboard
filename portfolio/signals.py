"""Moving-average crossover alerts for the ETFs currently held and the securities on the watchlist: a BUY
when the 20-day average of the closes rises above the 50-day one, a SELL when it falls below. A fall below
while the close is still above the 200-day average is a DIP instead: a pullback inside an uptrend, which
backtests showed to be a buying chance rather than an exit. With fewer than 200 closes it stays a SELL. The last side per ETF is kept in `signals`;
an ETF seen for the first time (or bought again after selling out, or put on the watchlist again) is recorded
without an alert."""
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from portfolio import services, watch

log = logging.getLogger("portfolio.signals")
SESSION_END_HOUR = 22      # gettex closes 22:00 Berlin; until then today's close is still moving
FAST, SLOW, TREND = 20, 50, 200

Sender = Callable[[str], None]


@dataclass(frozen=True)
class Signal:
    isin: str
    name: str
    action: str          # BUY | SELL | DIP
    day: date
    close: float
    fast: float
    slow: float
    trend: float | None = None

    def text(self) -> str:
        icon, side = {"BUY": ("📈", "above"), "SELL": ("📉", "below"), "DIP": ("🛒", "below")}[self.action]
        text = (f"{icon} {self.action} signal: {self.name} ({self.isin})\n"
                f"MA {FAST}d {self.fast:,.2f} crossed {side} MA {SLOW}d {self.slow:,.2f} "
                f"on {self.day.isoformat()} (close {self.close:,.2f})")
        if self.action != "BUY" and self.trend is not None:
            where = "above" if self.close > self.trend else "below"
            text += f", {where} MA {TREND}d {self.trend:,.2f}"
            if self.action == "DIP":
                text += "\nDip in an uptrend: a buying chance, not an exit"
        return text


def _last_session(conn, isin: str, now: datetime) -> tuple[date, float] | None:
    local = now.astimezone(services.BERLIN)
    rows = [(d, p) for d, p in services._closes(conn).get(isin, [])
            if d < local.date() or (d == local.date() and local.hour >= SESSION_END_HOUR)]
    return (rows[-1][0], float(rows[-1][1])) if rows else None


def check(conn, now: datetime, send: Sender | None) -> list[Signal]:
    """Compare each held or watched ETF's averages on its last completed session with the stored side and alert
    on a change. Without `send` changes are only recorded. A failed send leaves the old side, so the next check
    retries. Returns the signals that were sent."""
    ov = services.overview(conn, services._day(now))
    if ov.error:
        return []
    names = {h.isin: h.name for h in ov.holdings}
    for isin, entry in watch.watched(conn).items():
        names.setdefault(isin, entry["name"])
    stored = {r["isin"]: r["state"] for r in conn.execute("select isin, state from signals")}
    with conn:
        for isin in stored.keys() - names.keys():          # sold out or unwatched: start silently if back
            conn.execute("delete from signals where isin=?", (isin,))
    sent: list[Signal] = []
    for isin, name in names.items():
        last = _last_session(conn, isin, now)
        if last is None:
            continue
        day, close = last
        ma = services.moving_averages(conn, isin, [day], (FAST, SLOW, TREND))
        fast, slow, trend = ma[FAST][0], ma[SLOW][0], ma[TREND][0]
        if fast is None or slow is None or fast == slow:
            continue
        side = "above" if fast > slow else "below"
        if stored.get(isin) == side:
            continue
        if isin in stored and send is not None:
            if side == "above":
                action = "BUY"
            else:
                action = "DIP" if trend is not None and close > trend else "SELL"
            sig = Signal(isin, name, action, day, close, fast, slow, trend)
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
