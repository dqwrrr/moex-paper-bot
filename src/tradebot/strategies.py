"""Стратегии: по данным ДО даты включительно возвращают целевые веса портфеля.

Ключи весов: тикеры акций, "INDEX" (фонд на индекс полной доходности, напр. TMOS@),
"CASH" (фонд денежного рынка, напр. TMON@). Сумма весов = 1.
Каждая стратегия использует только market.upto(asof) — одна и та же функция
работает в бэктесте и в живой (бумажной) торговле.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .data import Market

Weights = dict[str, float]


@dataclass(frozen=True)
class Strategy:
    name: str
    rebalance: str                     # "D" ежедневно, "W" раз в неделю, "M" раз в месяц
    fn: Callable[[Market], Weights]
    params: dict = field(default_factory=dict)

    def weights(self, m: Market) -> Weights:
        return self.fn(m)


def _cash() -> Weights:
    return {"CASH": 1.0}


def _liquid_universe(m: Market, top: int, min_history: int = 260) -> list[str]:
    """Top-N самых ликвидных акций по медианному обороту за 60 дней (на дату среза)."""
    close = m.close.iloc[-min_history:]
    has_hist = close.notna().sum() >= min_history * 0.95
    alive = m.close.iloc[-1].notna()
    liq = m.value.iloc[-60:].median()
    ok = liq[has_hist & alive].dropna().sort_values(ascending=False)
    return list(ok.index[:top])


# ---------- 1. Эталоны ----------
def buy_hold_index() -> Strategy:
    return Strategy("Купить и держать индекс", "M", lambda m: {"INDEX": 1.0})


def cash_only() -> Strategy:
    return Strategy("Только денежный рынок", "M", lambda m: _cash())


# ---------- 2. Тайминг индекса ----------
def index_sma(n: int = 200, band: float = 0.0) -> Strategy:
    """Держим индексный фонд, пока индекс выше скользящей средней, иначе — денежный рынок.

    band — «коридор» против ложных переключений: вход при цене > SMA·(1+band),
    выход при цене < SMA·(1−band); внутри коридора — сохраняем прошлое решение
    (упрощённо: внутри коридора — индекс, если цена выше SMA).
    """
    def fn(m: Market) -> Weights:
        s = m.index["MCFTR"].dropna()
        if len(s) < n:
            return _cash()
        sma = s.iloc[-n:].mean()
        p = s.iloc[-1]
        if p > sma * (1 + band):
            return {"INDEX": 1.0}
        if p < sma * (1 - band):
            return _cash()
        return {"INDEX": 1.0} if p > sma else _cash()
    return Strategy(f"Индекс выше SMA{n}" + (f" ±{band:.0%}" if band else ""), "D", fn,
                    {"n": n, "band": band})


def index_abs_momentum(lookback: int = 252) -> Strategy:
    """Абсолютный моментум: индекс, если его доходность за период выше денежного рынка."""
    from .rates import cash_daily_returns

    def fn(m: Market) -> Weights:
        s = m.index["MCFTR"].dropna()
        if len(s) <= lookback:
            return _cash()
        r_idx = s.iloc[-1] / s.iloc[-lookback - 1] - 1
        cr = cash_daily_returns(s.index[-lookback - 1:])
        r_cash = float((1 + cr.iloc[1:]).prod() - 1)
        return {"INDEX": 1.0} if r_idx > r_cash else _cash()
    return Strategy(f"Моментум индекса {lookback}д vs кэш", "M", fn, {"lookback": lookback})


# ---------- 3. Акции ----------
def stock_momentum(lookback: int = 126, skip: int = 21, top: int = 5, universe: int = 30,
                   trend_filter: int | None = 200) -> Strategy:
    """Кросс-секционный моментум: N лучших акций по доходности (без последнего месяца).
    Фильтр тренда: если индекс ниже SMA — всё в денежный рынок."""
    def fn(m: Market) -> Weights:
        if trend_filter:
            s = m.index["MCFTR"].dropna()
            if len(s) < trend_filter or s.iloc[-1] < s.iloc[-trend_filter:].mean():
                return _cash()
        uni = _liquid_universe(m, universe)
        c = m.close[uni]
        if len(c) < lookback + skip + 1:
            return _cash()
        mom = c.iloc[-1 - skip] / c.iloc[-1 - skip - lookback] - 1
        mom = mom.dropna()
        winners = mom[mom > 0].sort_values(ascending=False).index[:top]
        if len(winners) == 0:
            return _cash()
        w = {t: 1.0 / top for t in winners}
        if len(winners) < top:
            w["CASH"] = 1.0 - len(winners) / top
        return w
    tf = f", фильтр SMA{trend_filter}" if trend_filter else ""
    return Strategy(f"Моментум акций {lookback}д top{top}{tf}", "M", fn,
                    {"lookback": lookback, "skip": skip, "top": top, "trend_filter": trend_filter})


def stock_low_vol(top: int = 10, universe: int = 30, window: int = 126) -> Strategy:
    """Портфель из наименее волатильных ликвидных акций, равные веса."""
    def fn(m: Market) -> Weights:
        uni = _liquid_universe(m, universe)
        r = m.close[uni].iloc[-window - 1:].pct_change().iloc[1:]
        vol = r.std().dropna().sort_values()
        pick = vol.index[:top]
        return {t: 1.0 / len(pick) for t in pick} if len(pick) else _cash()
    return Strategy(f"Низкая волатильность top{top}", "M", fn, {"top": top, "window": window})


def stock_reversal(top: int = 5, universe: int = 30, lookback: int = 5) -> Strategy:
    """Краткосрочный разворот: покупаем худшие за неделю ликвидные акции."""
    def fn(m: Market) -> Weights:
        uni = _liquid_universe(m, universe)
        c = m.close[uni]
        r = (c.iloc[-1] / c.iloc[-1 - lookback] - 1).dropna().sort_values()
        pick = r.index[:top]
        return {t: 1.0 / len(pick) for t in pick} if len(pick) else _cash()
    return Strategy(f"Разворот {lookback}д top{top}", "W", fn, {"top": top, "lookback": lookback})


def equal_weight_liquid(universe: int = 20) -> Strategy:
    def fn(m: Market) -> Weights:
        uni = _liquid_universe(m, universe)
        return {t: 1.0 / len(uni) for t in uni}
    return Strategy(f"Равные веса top{universe} ликвидных", "M", fn, {"universe": universe})


def ensure_valid(w: Weights) -> Weights:
    tot = sum(w.values())
    if not np.isclose(tot, 1.0, atol=1e-6) or any(v < -1e-9 for v in w.values()):
        raise ValueError(f"Некорректные веса: {w}")
    return w
