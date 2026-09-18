"""Бумажная (виртуальная) торговля по реальным котировкам.

Портфель хранится в JSON. Сделки исполняются по текущей цене ISS с учётом лотов,
комиссии и проскальзывания. Деньги не реальные.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .risk import RiskRules, RiskState, check_drawdown, withdrawal_recommendation

# Как виртуальные «INDEX» и «CASH» стратегии превращаются в реальные бумаги Мосбиржи
INSTRUMENTS = {
    "INDEX": {"ticker": "TMOS", "board": "TQTF", "fee": 0.0, "slip": 0.0003,
              "name": "Т-Капитал Индекс МосБиржи (TMOS@)"},
    "CASH": {"ticker": "TMON", "board": "TQTF", "fee": 0.0, "slip": 0.0002,
             "name": "Т-Капитал Денежный рынок (TMON@)"},
}
STOCK_FEE, STOCK_SLIP = 0.0005, 0.0005   # тариф «Трейдер» + проскальзывание


def resolve(key: str) -> dict:
    """Параметры инструмента по ключу стратегии (INDEX/CASH/тикер акции)."""
    if key in INSTRUMENTS:
        return INSTRUMENTS[key]
    return {"ticker": key, "board": "TQBR", "fee": STOCK_FEE, "slip": STOCK_SLIP, "name": key}


@dataclass
class Trade:
    time: str
    ticker: str
    side: str          # BUY / SELL
    qty: int
    price: float
    cost: float        # комиссия + проскальзывание, ₽
    reason: str


@dataclass
class Portfolio:
    id: str
    title: str
    strategy: str
    start_capital: float
    cash_rub: float                       # свободные рубли (не в фондах/акциях)
    positions: dict[str, int] = field(default_factory=dict)   # тикер -> штук
    target: dict[str, float] = field(default_factory=dict)    # последние целевые веса
    pending: dict[str, float] | None = None                   # веса, ждущие исполнения
    last_signal_date: str | None = None
    history: list[dict] = field(default_factory=list)         # [{t, equity, withdrawn}]
    trades: list[dict] = field(default_factory=list)
    withdrawals: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    last_withdraw_period: str | None = None
    kind: str = "daily"                   # daily — долгосрочный, intraday — активный
    body: float = 0.0                     # «тело» для правила вывода прибыли (активный режим)
    day: str | None = None                # текущий торговый день (для дневной статистики)
    day_start_equity: float = 0.0
    day_trades: int = 0
    day_stopped: bool = False             # сработал дневной лимит убытка
    last_comment: str | None = None

    # ---------- состояние ----------
    @classmethod
    def load(cls, path: Path) -> Portfolio:
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    def risk_state(self) -> RiskState:
        return RiskState(**self.risk) if self.risk else RiskState(self.start_capital, self.start_capital)

    # ---------- оценка ----------
    def equity(self, prices: dict[str, float]) -> float:
        return self.cash_rub + sum(q * prices[t] for t, q in self.positions.items() if q)

    def withdrawn(self) -> float:
        return round(sum(w["amount"] for w in self.withdrawals), 2)

    def log(self, t: str, text: str, kind: str = "info") -> None:
        self.events.append({"t": t, "kind": kind, "text": text})
        self.events = self.events[-300:]

    # ---------- исполнение ----------
    def rebalance(self, target: dict[str, float], prices: dict[str, float], lots: dict[str, int],
                  now: str, reason: str) -> list[Trade]:
        """Приводит портфель к целевым весам. Сначала продажи, потом покупки."""
        eq = self.equity(prices)
        want: dict[str, int] = {}
        for key, w in target.items():
            ins = resolve(key)
            t, lot = ins["ticker"], max(int(lots.get(ins["ticker"], 1)), 1)
            budget = eq * w / (1 + ins["fee"] + ins["slip"])
            want[t] = int(math.floor(budget / prices[t] / lot)) * lot
        fees = {resolve(k)["ticker"]: resolve(k)["fee"] + resolve(k)["slip"] for k in target}
        trades: list[Trade] = []
        for t in sorted(set(self.positions) | set(want), key=lambda x: want.get(x, 0) - self.positions.get(x, 0)):
            diff = want.get(t, 0) - self.positions.get(t, 0)
            if diff == 0:
                continue
            rate = fees.get(t, STOCK_FEE + STOCK_SLIP if t not in ("TMOS", "TMON") else 0.0003)
            px = prices[t]
            if diff > 0:  # покупка — проверяем, хватает ли денег
                affordable = int(self.cash_rub / (px * (1 + rate)))
                lot = max(int(lots.get(t, 1)), 1)
                diff = min(diff, affordable // lot * lot)
                if diff <= 0:
                    continue
            gross = abs(diff) * px
            cost = round(gross * rate, 2)
            self.cash_rub += -gross - cost if diff > 0 else gross - cost
            self.positions[t] = self.positions.get(t, 0) + diff
            if self.positions[t] == 0:
                del self.positions[t]
            tr = Trade(now, t, "BUY" if diff > 0 else "SELL", abs(diff), px, cost, reason)
            trades.append(tr)
            self.trades.append(asdict(tr))
        self.cash_rub = round(self.cash_rub, 2)
        self.target = target
        return trades

    # ---------- риск и вывод ----------
    def apply_risk(self, eq: float, rules: RiskRules, period: str, now: str,
                   prices: dict[str, float], lots: dict[str, int]) -> tuple[bool, float]:
        """Возвращает (стоп-кран активен?, сумма рекомендованного вывода)."""
        st = self.risk_state()
        was_cooldown = st.cooldown_left > 0
        stop = check_drawdown(eq, st, rules)
        if stop and not was_cooldown:
            self.log(now, f"Стоп-кран: просадка больше {rules.dd_limit:.0%} от пика. "
                          f"Уходим в денежный рынок на {rules.cooldown_days} торговых дней.", "risk")
        amount = 0.0
        if self.last_withdraw_period is not None and period != self.last_withdraw_period:
            amount = withdrawal_recommendation(eq, st, rules)
            if amount > 0:
                amount = self._raise_cash(amount, prices, lots, now)
                self.withdrawals.append({"t": now, "amount": amount, "equity_before": round(eq, 2)})
                st.withdraw_mark = eq - amount
                st.peak -= amount
                st.withdrawn_total += amount
                self.log(now, f"РЕКОМЕНДАЦИЯ: вывести {amount:,.2f} ₽ — капитал вырос больше чем на "
                              f"{rules.withdraw_trigger:.0%} с прошлой отметки. В бумажном режиме вывод выполнен автоматически.",
                         "withdraw")
        self.last_withdraw_period = period
        self.risk = asdict(st)
        return stop, amount

    def _raise_cash(self, amount: float, prices: dict[str, float], lots: dict[str, int], now: str) -> float:
        """Продаёт часть позиций пропорционально, чтобы освободить сумму для вывода."""
        need = amount - self.cash_rub
        if need > 0:
            eq_pos = sum(q * prices[t] for t, q in self.positions.items())
            for t, q in list(self.positions.items()):
                lot = max(int(lots.get(t, 1)), 1)
                sell = math.ceil(q * min(1.0, need / eq_pos) / lot) * lot
                sell = min(sell, q)
                if sell:
                    gross = sell * prices[t]
                    rate = 0.0003 if t in ("TMOS", "TMON") else STOCK_FEE + STOCK_SLIP
                    cost = round(gross * rate, 2)
                    self.cash_rub += gross - cost
                    self.positions[t] -= sell
                    if not self.positions[t]:
                        del self.positions[t]
                    self.trades.append(asdict(Trade(now, t, "SELL", sell, prices[t], cost, "вывод прибыли")))
        amount = round(min(amount, self.cash_rub), 2)
        self.cash_rub = round(self.cash_rub - amount, 2)
        return amount
