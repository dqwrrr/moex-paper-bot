"""Управление риском и правила вывода прибыли.

1. Стоп-кран по просадке: если капитал упал от своего пика больше чем на `dd_limit`,
   стратегия уходит в денежный рынок на `cooldown_days` торговых дней, пик сбрасывается.
2. Вывод прибыли: в конце каждого квартала, если капитал вырос больше чем на `trigger`
   относительно «отметки вывода», рекомендуется вывести `share` прибыли сверх отметки.
   После вывода отметка = капитал после вывода. Стартовая отметка = начальный капитал.

Бот сам деньги не выводит — формирует рекомендацию «сколько и когда».
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .rates import cash_daily_returns


@dataclass(frozen=True)
class RiskRules:
    dd_limit: float | None = 0.20
    cooldown_days: int = 21
    withdraw_trigger: float | None = 0.15
    withdraw_share: float = 0.5
    withdraw_freq: str = "QE"   # QE — квартал, ME — месяц, YE — год


@dataclass
class Withdrawal:
    date: pd.Timestamp
    amount: float
    equity_before: float
    reason: str


@dataclass
class RiskState:
    """Состояние для живой торговли (сохраняется в JSON)."""
    peak: float
    withdraw_mark: float
    cooldown_left: int = 0
    withdrawn_total: float = 0.0


def check_drawdown(equity: float, st: RiskState, rules: RiskRules) -> bool:
    """Обновляет пик; True — сработал стоп-кран (нужно уйти в кэш)."""
    if st.cooldown_left > 0:
        st.cooldown_left -= 1
        if st.cooldown_left == 0:
            st.peak = equity
        return st.cooldown_left > 0
    st.peak = max(st.peak, equity)
    if rules.dd_limit is not None and equity < st.peak * (1 - rules.dd_limit):
        st.cooldown_left = rules.cooldown_days
        return True
    return False


def withdrawal_recommendation(equity: float, st: RiskState, rules: RiskRules) -> float:
    """Сумма к выводу (0, если правило не сработало). Вызывать в конце периода."""
    if rules.withdraw_trigger is None:
        return 0.0
    if equity >= st.withdraw_mark * (1 + rules.withdraw_trigger):
        return round((equity - st.withdraw_mark) * rules.withdraw_share, 2)
    return 0.0


def apply_overlay(strategy_returns: pd.Series, rules: RiskRules, capital: float = 1.0):
    """Применяет правила к дневной доходности стратегии (для исторической проверки).

    Возвращает (капитал на счёте, накопленные выводы, список выводов)."""
    dates = strategy_returns.index
    cash = cash_daily_returns(dates)
    period_end = set(pd.Series(dates, index=dates).groupby(
        pd.Grouper(freq=rules.withdraw_freq)).max().dropna())
    st = RiskState(peak=capital, withdraw_mark=capital)
    eq, taken, log = capital, 0.0, []
    in_cash = False
    eq_s, taken_s = [], []
    for t in dates:
        r = cash.loc[t] if in_cash else strategy_returns.loc[t]
        eq *= 1 + r
        in_cash = check_drawdown(eq, st, rules)
        if t in period_end:
            amt = withdrawal_recommendation(eq, st, rules)
            if amt > 0:
                log.append(Withdrawal(t, amt, eq, "прибыль выше порога"))
                eq -= amt
                taken += amt
                st.withdraw_mark = eq
                st.peak -= amt          # вывод — не убыток: пик уменьшаем на выведенную сумму
                st.withdrawn_total += amt
        eq_s.append(eq)
        taken_s.append(taken)
    return pd.Series(eq_s, index=dates), pd.Series(taken_s, index=dates), log
