"""Криптовалюта: публичные рыночные данные (без ключей) и стратегии на часовых свечах.

Источник — публичный зеркальный API Binance для рыночных данных (data-api.binance.vision),
запасной — Coinbase Exchange (только BTC, ETH). Торговля бумажная: никаких ключей и кошельков.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import intraday as I
from .data import DATA_DIR

log = logging.getLogger(__name__)
BINANCE = "https://data-api.binance.vision/api/v3"
COINBASE = "https://api.exchange.coinbase.com"
COINBASE_PAIRS = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD"}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "TONUSDT"]
FEE = 0.001 + 0.0005      # комиссия спот-биржи 0,1% + проскальзывание 0,05% на сторону


class CryptoDataError(RuntimeError):
    pass


def _get(url: str, params: dict, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise CryptoDataError(f"{url}: {last}")


def klines(symbol: str, start: pd.Timestamp, end: pd.Timestamp | None = None, interval: str = "1h") -> pd.DataFrame:
    """Часовые свечи (время — UTC, начало свечи)."""
    end = end or pd.Timestamp.now("UTC").tz_localize(None)
    rows, t = [], int(start.timestamp() * 1000)
    stop = int(end.timestamp() * 1000)
    while t < stop:
        batch = _get(f"{BINANCE}/klines", {"symbol": symbol, "interval": interval, "startTime": t, "limit": 1000})
        if not batch:
            break
        rows += batch
        t = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "value", "volume"])
    df = pd.DataFrame(rows).iloc[:, :8]
    df.columns = ["begin", "open", "high", "low", "close", "volume", "end", "value"]
    df.index = pd.to_datetime(df["begin"], unit="ms")
    return df[["open", "high", "low", "close", "value", "volume"]].astype(float)


def last_price(symbol: str) -> float:
    try:
        return float(_get(f"{BINANCE}/ticker/price", {"symbol": symbol})["price"])
    except CryptoDataError:
        if symbol in COINBASE_PAIRS:
            return float(_get(f"{COINBASE}/products/{COINBASE_PAIRS[symbol]}/ticker", {})["price"])
        raise


def update_history(symbol: str, data_dir: Path = DATA_DIR, start: str = "2020-01-01") -> int:
    path = data_dir / f"crypto_{symbol}_1h.parquet"
    cur = pd.read_parquet(path) if path.exists() else None
    frm = pd.Timestamp(start) if cur is None or cur.empty else cur.index.max() - pd.Timedelta(hours=3)
    new = klines(symbol, frm)
    out = new if cur is None else pd.concat([cur, new])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.to_parquet(path)
    log.info("%s: %d свечей, по %s", symbol, len(out), out.index.max())
    return len(out)


# ---------------- стратегии (позиция 0/1 по закрытию свечи, без ночных выходов) ----------------
def buy_hold() -> I.IntradayStrategy:
    s = I.always_long(flat_overnight=False)
    return I.IntradayStrategy("Купить и держать", s.fn, {}, False)


def sma_cross(fast: int, slow: int) -> I.IntradayStrategy:
    return I.sma_cross(fast, slow, flat_overnight=False)


def donchian(entry: int = 48, exit_: int = 24) -> I.IntradayStrategy:
    """Пробой максимума за entry часов — покупка; пробой минимума за exit_ часов — выход."""
    def fn(b):
        hi = b["high"].rolling(entry).max().shift(1)
        lo = b["low"].rolling(exit_).min().shift(1)
        sig = pd.Series(np.nan, index=b.index)
        sig[b["close"] > hi] = 1.0
        sig[b["close"] < lo] = 0.0
        return sig.ffill().fillna(0.0)
    return I.IntradayStrategy(f"Пробой канала {entry}ч/{exit_}ч", fn, {"entry": entry, "exit": exit_}, False)


def ts_momentum(lookback: int = 168) -> I.IntradayStrategy:
    """В позиции, если цена выше, чем lookback часов назад."""
    def fn(b):
        return (b["close"] > b["close"].shift(lookback)).astype(float)
    return I.IntradayStrategy(f"Импульс {lookback}ч", fn, {"lookback": lookback}, False)


def mean_reversion(n: int = 48, k: float = 2.5) -> I.IntradayStrategy:
    s = I.mean_reversion(n, k)
    return I.IntradayStrategy(s.name, s.fn, s.params, False)


FACTORIES = {
    "buy_hold": lambda p: buy_hold(),
    "sma_cross": lambda p: sma_cross(int(p.get("fast", 24)), int(p.get("slow", 120))),
    "donchian": lambda p: donchian(int(p.get("entry", 48)), int(p.get("exit", 24))),
    "ts_momentum": lambda p: ts_momentum(int(p.get("lookback", 168))),
    "mean_reversion": lambda p: mean_reversion(int(p.get("n", 48)), float(p.get("k", 2.5))),
}
