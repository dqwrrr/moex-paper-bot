import numpy as np
import pandas as pd

from tradebot import intraday as I


def make_bars(days=10, seed=0):
    rng = np.random.default_rng(seed)
    idx = []
    for d in pd.bdate_range("2024-01-08", periods=days):
        idx += list(pd.date_range(d + pd.Timedelta(hours=10), d + pd.Timedelta(hours=18, minutes=30), freq="10min"))
    idx = pd.DatetimeIndex(idx)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.002, len(idx)))
    return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999, "close": c, "value": 1, "volume": 1}, index=idx)


def test_flat_overnight_and_no_late_entries():
    b = make_bars()
    pos = I.always_long().positions(b)
    last = pd.Series(b.index.date, index=b.index)
    assert (pos[last != last.shift(-1)] == 0).all()


def test_costs_and_no_lookahead():
    b = make_bars()
    s = I.sma_cross(3, 10)
    r0 = I.backtest(s, b, 0.0)
    r1 = I.backtest(s, b, 0.001)
    assert r1.equity.iloc[-1] < r0.equity.iloc[-1] and r0.trades == r1.trades > 0
    # позиция, решённая на свече i, не влияет на доходность свечи i
    pos = s.positions(b)
    b2 = b.copy()
    b2.iloc[-1, b2.columns.get_loc("close")] *= 1.5   # меняем последнюю свечу
    assert s.positions(b2).iloc[:-1].equals(pos.iloc[:-1])


def test_opening_range_resets_daily():
    b = make_bars(3)
    pos = I.opening_range_breakout(3).positions(b)
    first = pd.Series(b.index.date, index=b.index).groupby(b.index.date).cumcount() < 3
    assert (pos[first.values] == 0).all()


def test_no_new_entries_after_18():
    b = make_bars(1)
    s = I.IntradayStrategy("t", lambda x: pd.Series((x.index.hour >= 18).astype(float), index=x.index))
    assert (s.positions(b) == 0).all()   # сигнал появился только после 18:00 — входа нет
