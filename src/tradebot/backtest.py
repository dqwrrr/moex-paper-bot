"""Бэктест на дневных данных: сигнал по закрытию дня t, исполнение по закрытию дня t+1."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .costs import INDEX_FUND_FEE, cost_rate
from .data import Market
from .rates import cash_daily_returns
from .strategies import Strategy, ensure_valid

REBALANCE_TOLERANCE = 0.02  # не торгуем, если отклонение весов < 2 п.п.


@dataclass
class BacktestResult:
    name: str
    equity: pd.Series        # стоимость портфеля, старт = 1
    exposure: pd.Series      # доля в рискованных активах (не CASH)
    turnover: float          # средний годовой оборот (сумма |Δw| в год)
    trades: int              # число ребалансировок, изменивших портфель
    weights_log: dict        # дата исполнения -> веса


def instrument_returns(m: Market) -> pd.DataFrame:
    stocks = m.close.pct_change(fill_method=None)
    idx = m.index["MCFTR"].pct_change(fill_method=None).fillna(0.0)
    days = pd.Series(m.close.index, index=m.close.index).diff().dt.days.fillna(1)
    fee = (1 - INDEX_FUND_FEE) ** (days / 365) - 1
    stocks["INDEX"] = (1 + idx) * (1 + fee) - 1
    stocks["CASH"] = cash_daily_returns(m.close.index)
    return stocks


def is_rebalance_day(dates: pd.DatetimeIndex, i: int, freq: str) -> bool:
    if freq == "D":
        return True
    if i + 1 >= len(dates):
        return True
    t, nxt = dates[i], dates[i + 1]
    if freq == "W":
        return nxt.isocalendar().week != t.isocalendar().week
    if freq == "M":
        return nxt.month != t.month
    raise ValueError(freq)


def run_backtest(strategy: Strategy, m: Market, start: str, end: str | None = None,
                 warmup_ok: bool = True) -> BacktestResult:
    rets = instrument_returns(m)
    dates = m.close.loc[start:end].index
    w = pd.Series({"CASH": 1.0})
    equity, eq, expo = 1.0, [], []
    pending: pd.Series | None = None
    total_turn, trades, log = 0.0, 0, {}
    for i, t in enumerate(dates):
        if i > 0:
            r = rets.loc[t, w.index].fillna(0.0)
            port_r = float((w * r).sum())
            equity *= 1 + port_r
            w = w * (1 + r) / (1 + port_r)
        if pending is not None:
            allk = w.index.union(pending.index)
            delta = pending.reindex(allk, fill_value=0) - w.reindex(allk, fill_value=0)
            turn = delta.abs()
            if turn.sum() > 1e-9:
                cost = float(sum(turn[k] * cost_rate(k) for k in allk))
                equity *= 1 - cost
                total_turn += turn.sum() / 2
                trades += 1
                log[t] = pending.to_dict()
            w = pending
            pending = None
        if is_rebalance_day(dates, i, strategy.rebalance) and i + 1 < len(dates):
            target = ensure_valid(strategy.weights(m.upto(t)))
            tgt = pd.Series(target, dtype=float)
            tgt = tgt[tgt > 1e-12]
            allk = tgt.index.union(w.index)
            diff = (tgt.reindex(allk, fill_value=0) - w.reindex(allk, fill_value=0)).abs().max()
            if diff > REBALANCE_TOLERANCE:
                pending = tgt
        eq.append(equity)
        expo.append(1.0 - float(w.get("CASH", 0.0)))
    years = (dates[-1] - dates[0]).days / 365.25
    return BacktestResult(strategy.name, pd.Series(eq, index=dates), pd.Series(expo, index=dates),
                          total_turn / years if years else 0.0, trades, log)
