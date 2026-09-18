"""Метрики качества стратегии."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .rates import cash_daily_returns


def max_drawdown(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1).min())


def summary(eq: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    cash = cash_daily_returns(eq.index).iloc[1:]
    ex = r - cash.values
    sharpe = float(ex.mean() / ex.std() * np.sqrt(252)) if ex.std() > 0 else 0.0
    mdd = max_drawdown(eq)
    monthly = eq.resample("ME").last().pct_change().dropna()
    yearly = eq.resample("YE").last().pct_change().dropna()
    return {
        "CAGR": cagr, "Vol": float(r.std() * np.sqrt(252)), "Sharpe": sharpe, "MaxDD": mdd,
        "Calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "Мес.в плюсе": float((monthly > 0).mean()), "Худший мес.": float(monthly.min()),
        "Худший год": float(yearly.min()) if len(yearly) else np.nan,
    }
