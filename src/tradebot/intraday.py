"""Внутридневные стратегии на 10-минутных свечах фонда на индекс (TMOS).

Одна и та же функция `positions(bars)` используется в бэктесте и в живой торговле:
по закрытию свечи i решается позиция (0 — денежный рынок/рубли, 1 — в фонде),
исполнение — по открытию свечи i+1. Используются только закрытые свечи.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

MAIN_OPEN, MAIN_CLOSE = time(10, 0), time(18, 40)   # основная сессия Мосбиржи
LAST_ENTRY = time(18, 0)                             # после этого новые покупки не открываем
LAST_BAR = time(18, 20)                              # с этой свечи — к закрытию дня вне рынка


def main_session(bars: pd.DataFrame) -> pd.DataFrame:
    """Только свечи основной сессии 10:00–18:40."""
    t = bars.index.time
    return bars[(t >= MAIN_OPEN) & (t < MAIN_CLOSE)]


@dataclass(frozen=True)
class IntradayStrategy:
    name: str
    fn: Callable[[pd.DataFrame], pd.Series]   # bars -> желаемая позиция 0/1 по закрытию каждой свечи
    params: dict = field(default_factory=dict)
    flat_overnight: bool = True

    def positions(self, bars: pd.DataFrame) -> pd.Series:
        pos = self.fn(bars).reindex(bars.index).fillna(0.0).clip(0, 1)
        if self.flat_overnight:
            t = pd.Series(bars.index.time, index=bars.index)
            pos[t.apply(lambda x: x >= LAST_BAR).values] = 0.0    # к закрытию дня — вне рынка
            late = t.apply(lambda x: x >= LAST_ENTRY).values
            vals = pos.to_numpy(copy=True)
            for i in range(1, len(vals)):                         # после 18:00 позицию можно только закрыть
                if late[i]:
                    vals[i] = min(vals[i], vals[i - 1])
            pos = pd.Series(vals, index=pos.index)
        return pos


# ---------------- стратегии ----------------
def sma_cross(fast: int = 6, slow: int = 30, flat_overnight: bool = True) -> IntradayStrategy:
    def fn(b):
        c = b["close"]
        return (c.rolling(fast).mean() > c.rolling(slow).mean()).astype(float)
    return IntradayStrategy(f"Пересечение средних {fast}/{slow} свечей", fn,
                            {"fast": fast, "slow": slow}, flat_overnight)


def opening_range_breakout(n: int = 3) -> IntradayStrategy:
    """Покупка при пробое максимума первых n свечей дня, выход — пробой минимума или конец дня."""
    def fn(b):
        day = pd.Series(b.index.date, index=b.index)
        k = day.groupby(day).cumcount()
        hi = b["high"].where(k < n).groupby(day).transform("max")
        lo = b["low"].where(k < n).groupby(day).transform("min")
        c = b["close"]
        sig = pd.Series(np.nan, index=b.index)
        sig[(k >= n) & (c > hi)] = 1.0
        sig[(k >= n) & (c < lo)] = 0.0
        sig[k < n] = 0.0
        return sig.groupby(day).ffill().fillna(0.0)
    return IntradayStrategy(f"Пробой диапазона первых {n * 10} минут", fn, {"n": n})


def mean_reversion(n: int = 20, k: float = 2.0) -> IntradayStrategy:
    """Покупка на резком отклонении вниз от средней, продажа при возврате к средней."""
    def fn(b):
        c = b["close"]
        m, s = c.rolling(n).mean(), c.rolling(n).std()
        sig = pd.Series(np.nan, index=b.index)
        sig[c < m - k * s] = 1.0
        sig[c > m] = 0.0
        return sig.ffill().fillna(0.0)
    return IntradayStrategy(f"Возврат к средней {n} свечей, {k}σ", fn, {"n": n, "k": k})


def intraday_momentum(threshold: float = 0.003) -> IntradayStrategy:
    """В позиции, если с открытия дня рост больше порога."""
    def fn(b):
        day = pd.Series(b.index.date, index=b.index)
        first_open = b["open"].groupby(day).transform("first")
        return (b["close"] / first_open - 1 > threshold).astype(float)
    return IntradayStrategy(f"Импульс дня > {threshold:.1%}", fn, {"threshold": threshold})


def always_long(flat_overnight: bool = True) -> IntradayStrategy:
    return IntradayStrategy("Весь день в фонде" + (" (ночью вне рынка)" if flat_overnight else ""),
                            lambda b: pd.Series(1.0, index=b.index), {}, flat_overnight)


def with_daily_filter(strat: IntradayStrategy, daily_ok: pd.Series) -> IntradayStrategy:
    """Разрешать покупки только в дни, когда дневной фильтр (напр. индекс выше SMA150) включён.
    daily_ok индексирован датами; значение за день d должно быть известно ДО дня d."""
    def fn(b):
        day = pd.Series(pd.to_datetime(b.index.date), index=b.index)
        allow = day.map(daily_ok).fillna(False).astype(bool)
        return strat.fn(b).where(allow, 0.0)
    return IntradayStrategy(strat.name + " + дневной фильтр SMA150", fn, strat.params, strat.flat_overnight)


# ---------------- бэктест ----------------
@dataclass
class IntradayResult:
    name: str
    equity: pd.Series      # по свечам
    daily: pd.Series       # на конец дня
    trades: int
    exposure: float


def backtest(strat: IntradayStrategy, bars: pd.DataFrame, cost_per_side: float = 0.0005) -> IntradayResult:
    """Позиция по закрытию i → доходность свечи i+1 (open→close плюс гэп от прошлого close),
    издержки — при каждом изменении позиции (спред/проскальзывание, комиссия 0)."""
    pos = strat.positions(bars)
    held = pos.shift(1).fillna(0.0)                      # позиция в течение свечи i
    ret = bars["close"].pct_change().fillna(0.0)
    trade = held.diff().abs().fillna(held.abs())
    r = held * ret - trade * cost_per_side
    eq = (1 + r).cumprod()
    daily = eq.groupby(eq.index.date).last()
    daily.index = pd.to_datetime(daily.index)
    return IntradayResult(strat.name, eq, daily, int((trade > 0).sum()), float(held.mean()))
