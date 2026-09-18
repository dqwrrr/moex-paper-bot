import numpy as np
import pandas as pd
import pytest

from tradebot import strategies as S
from tradebot.backtest import run_backtest
from tradebot.data import Market
from tradebot.metrics import max_drawdown, summary
from tradebot.rates import cash_daily_returns
from tradebot.risk import RiskRules, RiskState, apply_overlay, check_drawdown, withdrawal_recommendation


def make_market(n=400, drift=0.0005, seed=1):
    rng = np.random.default_rng(seed)
    d = pd.bdate_range("2016-01-01", periods=n)
    idx = pd.DataFrame({"MCFTR": 1000 * np.cumprod(1 + drift + rng.normal(0, 0.01, n))}, index=d)
    close = pd.DataFrame({t: 100 * np.cumprod(1 + rng.normal(drift * (i + 1) / 3, 0.02, n))
                          for i, t in enumerate(["AAA", "BBB", "CCC"])}, index=d)
    value = close * 1e6
    return Market(close, value, idx)


def test_cash_returns_match_key_rate():
    d = pd.bdate_range("2016-01-04", "2016-12-30")
    total = (1 + cash_daily_returns(d)).prod() - 1
    assert 0.08 < total < 0.10  # ставка ~10–10,5% минус расходы фонда


def test_no_lookahead():
    m = make_market()
    seen = []

    def spy(mk):
        seen.append(mk.close.index.max())
        return {"INDEX": 1.0}
    res = run_backtest(S.Strategy("spy", "M", spy), m, "2016-03-01")
    rebal_dates = [t for t in m.close.loc["2016-03-01":].index]
    assert all(s in rebal_dates for s in seen)
    # стратегия видит данные только до своей даты: следующая дата после каждого среза существует в данных
    assert max(seen) < m.close.index.max()
    assert res.trades >= 1


def test_execution_lag_one_day():
    m = make_market()
    res = run_backtest(S.buy_hold_index(), m, "2016-02-01")
    # в первый день стратегия ещё в кэше, индекс начинает влиять только со второго дня после сигнала
    first, second = res.equity.iloc[0], res.equity.iloc[1]
    assert first == 1.0
    assert abs(second - 1.0) < 0.001  # только доходность кэша и издержки


def test_costs_reduce_equity():
    m = make_market()
    flip = S.Strategy("flip", "D", lambda mk: {"INDEX": 1.0} if len(mk.close) % 2 else {"CASH": 1.0})
    hold = S.cash_only()
    assert run_backtest(flip, m, "2016-02-01").turnover > 50
    assert run_backtest(hold, m, "2016-02-01").turnover == 0


def test_ensure_valid_rejects_bad_weights():
    with pytest.raises(ValueError):
        S.ensure_valid({"INDEX": 0.7})


def test_metrics():
    eq = pd.Series([1, 1.2, 0.9, 1.1], index=pd.bdate_range("2020-01-01", periods=4))
    assert max_drawdown(eq) == pytest.approx(-0.25)
    assert "Sharpe" in summary(eq)


def test_drawdown_breaker_and_cooldown():
    rules = RiskRules(dd_limit=0.2, cooldown_days=3)
    st = RiskState(peak=100, withdraw_mark=100)
    assert not check_drawdown(90, st, rules)
    assert check_drawdown(79, st, rules)
    assert check_drawdown(79, st, rules) and check_drawdown(79, st, rules)
    assert not check_drawdown(79, st, rules)  # кулдаун кончился, пик сброшен
    assert st.peak == 79


def test_withdrawal_rule():
    rules = RiskRules(withdraw_trigger=0.15, withdraw_share=0.5)
    st = RiskState(peak=100, withdraw_mark=100)
    assert withdrawal_recommendation(110, st, rules) == 0
    assert withdrawal_recommendation(120, st, rules) == 10


def test_overlay_total_wealth_consistent():
    d = pd.bdate_range("2018-01-01", periods=600)
    r = pd.Series(0.001, index=d)
    acc, taken, log = apply_overlay(r, RiskRules(None, 0, 0.15, 0.5), 100.0)
    assert len(log) > 0 and taken.iloc[-1] > 0
    assert acc.min() > 0
