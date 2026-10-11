from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class OversellError(ValueError):
    pass


@dataclass(frozen=True)
class Trade:
    id: str
    isin: str
    name: str
    side: str
    kind: str
    executed_at: datetime
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal(0)


@dataclass
class Lot:
    trade: Trade
    remaining: Decimal

    @property
    def unit_cost(self) -> Decimal:
        t = self.trade
        return (t.quantity * t.price + t.fee) / t.quantity

    @property
    def cost(self) -> Decimal:
        return self.remaining * self.unit_cost


@dataclass(frozen=True)
class Sale:
    trade: Trade
    proceeds: Decimal
    cost_basis: Decimal

    @property
    def realized(self) -> Decimal:
        return self.proceeds - self.cost_basis


def replay(trades: list[Trade]) -> tuple[list[Lot], list[Sale]]:
    lots: list[Lot] = []
    open_lots: dict[str, list[Lot]] = {}  # FIFO queues are per ISIN: a sale never touches another ETF
    sales: list[Sale] = []
    for t in sorted(trades, key=lambda x: (x.executed_at, x.id)):
        if t.side == "BUY":
            lot = Lot(t, t.quantity)
            lots.append(lot)
            open_lots.setdefault(t.isin, []).append(lot)
            continue
        need, basis = t.quantity, Decimal(0)
        for lot in open_lots.get(t.isin, []):
            if need == 0:
                break
            take = min(lot.remaining, need)
            basis += take * lot.unit_cost
            lot.remaining -= take
            need -= take
        if need > 0:
            raise OversellError(f"{t.isin}: sale {t.id} exceeds holdings by {need}")
        sales.append(Sale(t, t.quantity * t.price - t.fee, basis))
    return lots, sales
