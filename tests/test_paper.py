import json

import pandas as pd
import pytest

from tradebot import iss, runner
from tradebot.paper import Portfolio


def quotes_stub(prices, live=True):
    def _q(tickers, board):
        rows = {t: {"LOTSIZE": 1 if board == "TQTF" else 10, "PREVPRICE": prices[t], "SHORTNAME": t,
                    "LAST": prices[t] if live else None, "TRADINGSTATUS": "T" if live else "N",
                    "PRICE": prices[t], "LIVE": live} for t in tickers}
        return pd.DataFrame.from_dict(rows, orient="index")
    return _q


def test_rebalance_respects_lots_and_cash():
    p = Portfolio("x", "x", "s", 1000, 1000)
    prices, lots = {"TMOS": 6.37, "TMON": 142.1, "SBER": 300.0}, {"TMOS": 1, "TMON": 1, "SBER": 10}
    p.rebalance({"INDEX": 1.0}, prices, lots, "t", "test")
    assert p.positions["TMOS"] == int(1000 / 6.37 / 1.0003)
    assert 0 <= p.cash_rub < 6.37 * 2
    p.rebalance({"CASH": 1.0}, prices, lots, "t", "test")   # продаём TMOS, покупаем TMON
    assert "TMOS" not in p.positions and p.positions["TMON"] == 7
    assert p.cash_rub >= 0
    p.rebalance({"SBER": 1.0}, prices, lots, "t", "test")   # лот 10 × 300 = 3000 ₽ > капитала
    assert "SBER" not in p.positions and p.cash_rub > 990


def test_tick_end_to_end(tmp_path, monkeypatch):
    prices = {"TMOS": 6.5, "TMON": 140.0}
    monkeypatch.setattr(iss, "quotes", quotes_stub(prices))
    cfg = {"risk": {"dd_limit": 0.2, "cooldown_days": 21, "withdraw_trigger": 0.15, "withdraw_share": 0.5,
                    "withdraw_freq": "QE"},
           "portfolio": [{"id": "defensive", "title": "t", "strategy": "index_sma", "params": {"n": 150},
                          "capital": 1000}]}
    site = runner.tick(cfg, tmp_path, update_data=False)
    p = site["portfolios"][0]
    assert p["positions"], "первый тик должен сформировать и исполнить портфель"
    assert p["equity"] == pytest.approx(1000, rel=0.01)
    assert (tmp_path / "site.json").exists()
    # повторный тик в тот же день не торгует повторно
    n_trades = len(p["trades"])
    site2 = runner.tick(cfg, tmp_path, update_data=False)
    assert len(site2["portfolios"][0]["trades"]) == n_trades
    assert json.loads((tmp_path / "defensive.json").read_text())["last_signal_date"]


def test_tick_waits_when_market_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(iss, "quotes", quotes_stub({"TMOS": 6.5, "TMON": 140.0}, live=False))
    cfg = {"risk": {"dd_limit": 0.2, "cooldown_days": 21, "withdraw_trigger": 0.15, "withdraw_share": 0.5,
                    "withdraw_freq": "QE"},
           "portfolio": [{"id": "d", "title": "t", "strategy": "index_sma", "params": {"n": 150}, "capital": 1000}]}
    p = runner.tick(cfg, tmp_path, update_data=False)["portfolios"][0]
    assert p["pending"] and not p["positions"]


def test_iss_paging(monkeypatch):
    pages = [
        {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [["2024-01-02", 1.0], ["2024-01-03", 2.0]]},
         "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 3, 2]]}},
        {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [["2024-01-04", 3.0]]},
         "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[2, 3, 2]]}},
    ]
    calls = []

    def fake_get(path, params=None, retries=3):
        calls.append(params["start"])
        return pages[len(calls) - 1]
    monkeypatch.setattr(iss, "_get", fake_get)
    s = iss.index_history("MCFTR", "2024-01-01")
    assert list(s.values) == [1.0, 2.0, 3.0] and calls == [0, 2]


def test_compact_history():
    h = [{"t": f"2024-01-{d:02d} {hh:02d}:00"} for d in range(1, 11) for hh in (10, 12, 18)]
    c = runner.compact(h, keep_intraday_days=2)
    assert len(c) == 8 + 6


def test_iss_candles_paging(monkeypatch):
    cols = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    full = [[1, 1, 1, 1, 1, 1, f"2024-01-02 10:{i % 60:02d}:00", ""] for i in range(iss.CANDLE_PAGE)]
    pages = [{"candles": {"columns": cols, "data": full}},
             {"candles": {"columns": cols, "data": [[2, 2, 2, 2, 2, 2, "2024-01-03 10:00:00", ""]]}}]
    calls = []

    def fake_get(path, params=None, retries=3):
        calls.append(params["start"])
        return pages[len(calls) - 1]
    monkeypatch.setattr(iss, "_get", fake_get)
    df = iss.candles("TMOS", "2024-01-01")
    assert calls == [0, iss.CANDLE_PAGE] and df.index.is_monotonic_increasing and df["close"].iloc[-1] == 2


def test_intraday_step_trades_comments_and_body_withdrawal(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import numpy as np

    idx = pd.date_range("2026-09-18 10:00", "2026-09-18 12:00", freq="10min")
    c = np.linspace(6.0, 6.6, len(idx))                     # ровный рост → стратегия «в рынке»
    bars = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "value": 1.0, "volume": 1.0}, index=idx)
    monkeypatch.setattr(iss, "candles", lambda *a, **k: bars)
    prices = {"TMOS": 6.0, "TMON": 140.0}
    monkeypatch.setattr(iss, "quotes", quotes_stub(prices))
    cfg = {"risk": {"dd_limit": 0.2, "cooldown_days": 21, "withdraw_trigger": 0.15, "withdraw_share": 0.5,
                    "withdraw_freq": "QE"},
           "portfolio": [{"id": "a", "title": "Активный", "kind": "intraday", "strategy": "sma_cross",
                          "params": {"fast": 2, "slow": 5}, "capital": 1000, "withdraw_min_profit": 0.02}]}
    ctx = runner.Ctx()
    now = datetime(2026, 9, 18, 12, 5, tzinfo=ZoneInfo("Europe/Moscow"))
    site = runner.step(cfg, tmp_path, now, update_data=False, ctx=ctx)
    p = site["portfolios"][0]
    assert p["positions"][0]["ticker"] == "TMOS"
    assert any(e["kind"] == "comment" for e in p["events"]) and any(e["kind"] == "trade" for e in p["events"])
    # цена выросла на 10% → по правилу «тела» выводим всё сверх 1000 ₽
    prices["TMOS"] = 6.6
    text = runner.summarize(cfg, tmp_path, now, ctx)
    assert "вывести прибыль" in text
    st = json.loads((tmp_path / "a.json").read_text())
    eq_after = st["cash_rub"] + sum(q * prices[t] for t, q in st["positions"].items())
    assert 995 <= eq_after <= 1000.01 and st["withdrawals"][0]["amount"] > 90


def test_one_broken_portfolio_does_not_stop_others(tmp_path, monkeypatch):
    monkeypatch.setattr(iss, "quotes", quotes_stub({"TMOS": 6.5, "TMON": 140.0}))
    cfg = {"risk": {"dd_limit": 0.2, "cooldown_days": 21, "withdraw_trigger": 0.15, "withdraw_share": 0.5,
                    "withdraw_freq": "QE"},
           "portfolio": [{"id": "bad", "title": "b", "strategy": "no_such_strategy", "capital": 1000},
                         {"id": "ok", "title": "t", "strategy": "index_sma", "params": {"n": 150}, "capital": 1000}]}
    site = runner.tick(cfg, tmp_path, update_data=False)
    assert [p["id"] for p in site["portfolios"]] == ["ok"]
    st = json.loads((tmp_path / "status.json").read_text())
    assert not st["ok"] and st["errors"][0]["portfolio"] == "bad"
