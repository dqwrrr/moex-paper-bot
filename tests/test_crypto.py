import numpy as np
import pandas as pd

from tradebot import crypto as C
from tradebot import intraday as I


def bars(n=500, drift=0.0005, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    c = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, n))
    return pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "value": 1, "volume": 1}, index=idx)


def test_crypto_strategies_no_lookahead_and_24_7():
    b = bars()
    for s in [C.buy_hold(), C.sma_cross(12, 48), C.donchian(24, 12), C.ts_momentum(48), C.mean_reversion(24, 2)]:
        pos = s.positions(b)
        b2 = b.copy()
        b2.iloc[-1, b2.columns.get_loc("close")] *= 1.3
        b2.iloc[-1, b2.columns.get_loc("high")] *= 1.3
        assert s.positions(b2).iloc[:-1].equals(pos.iloc[:-1]), s.name
    assert (C.buy_hold().positions(b) == 1).all()       # нет принудительных ночных выходов
    r = I.backtest(C.buy_hold(), b, C.FEE)
    assert r.trades == 1


def test_klines_paging(monkeypatch):
    t0 = pd.Timestamp("2024-01-01")
    ms = int(t0.timestamp() * 1000)
    page1 = [[ms + i * 3600_000, "1", "1", "1", "1", "1", 0, "1"] for i in range(1000)]
    page2 = [[ms + (1000 + i) * 3600_000, "2", "2", "2", "2", "1", 0, "1"] for i in range(5)]
    pages = iter([page1, page2])
    monkeypatch.setattr(C, "_get", lambda url, params, retries=3: next(pages))
    df = C.klines("BTCUSDT", t0, t0 + pd.Timedelta(hours=2000))
    assert len(df) == 1005 and df["close"].iloc[-1] == 2.0


def test_crypto_step_and_body_withdrawal(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from tradebot import runner

    b = bars(800)
    c = np.linspace(100, 200, len(b))                            # ровный рост → стратегия в монете
    b["open"] = b["close"] = b["high"] = b["low"] = c
    b.index = pd.date_range(end=pd.Timestamp.now("UTC").tz_localize(None).floor("h") - pd.Timedelta(hours=1),
                            periods=len(b), freq="h")
    price = {"v": float(b["close"].iloc[-1])}
    monkeypatch.setattr(C, "klines", lambda *a, **k: b)
    monkeypatch.setattr(C, "last_price", lambda s: price["v"])
    cfg = {"risk": {"dd_limit": 0.2, "cooldown_days": 21, "withdraw_trigger": 0.15, "withdraw_share": 0.5,
                    "withdraw_freq": "QE"},
           "portfolio": [{"id": "c", "title": "Крипто", "kind": "crypto", "symbol": "BTCUSDT",
                          "strategy": "sma_cross", "params": {"fast": 12, "slow": 48}, "capital": 10,
                          "withdraw_min_profit": 0.05}]}
    ctx = runner.Ctx()
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    site = runner.step(cfg, tmp_path, now, update_data=False, ctx=ctx)
    p = site["portfolios"][0]
    assert p["currency"] == "USDT" and p["positions"] and p["positions"][0]["ticker"] == "BTCUSDT"
    price["v"] *= 1.2
    text = runner.summarize(cfg, tmp_path, now, ctx)
    assert "вывести прибыль" in text and "$" in text
    st = json.loads((tmp_path / "c.json").read_text())
    eq = st["cash_rub"] + sum(q * price["v"] for q in st["positions"].values())
    assert 9.9 <= eq <= 10.01
