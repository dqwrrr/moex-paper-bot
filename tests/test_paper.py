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
