"""Загрузка исторических данных (panel) из parquet."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Market:
    close: pd.DataFrame   # date × ticker
    value: pd.DataFrame   # оборот в рублях, date × ticker
    index: pd.DataFrame   # date × {IMOEX, MCFTR, ...}

    def upto(self, asof: pd.Timestamp) -> Market:
        """Срез данных, доступных на дату asof включительно (защита от заглядывания в будущее)."""
        return Market(self.close.loc[:asof], self.value.loc[:asof], self.index.loc[:asof])


SPLIT_LOW, SPLIT_HIGH = 0.4, 2.5   # скачок цены за день за этими пределами считаем сплитом/консолидацией


def adjust_splits(close: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple]]:
    """Убирает скачки цены от сплитов (напр. GMKN, TRNFP 2024) и консолидаций (VTBR 2024):
    цены ДО события умножаются на коэффициент. Возвращает (цены, список событий)."""
    out, events = close.copy(), []
    ratio = close / close.ffill().shift(1)
    for t in close.columns:
        r = ratio[t]
        for d, v in r[(r < SPLIT_LOW) | (r > SPLIT_HIGH)].items():
            if not _looks_like_split(float(v)):
                continue                      # настоящий обвал/взлёт (напр. POGR 2022) — не трогаем
            out.loc[:d - pd.Timedelta(days=1), t] *= v
            events.append((t, d.date(), round(float(v), 4)))
    return out, events


def _looks_like_split(v: float) -> bool:
    """Коэффициент сплита обычно «круглый»: 2, 3, 5, 10, 100, 1000, 5000…"""
    f = v if v >= 1 else 1 / v
    nice = [2, 3, 4, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
    return any(abs(f / n - 1) < (0.03 if n < 10 else 0.10) for n in nice)


def load_market(start: str = "2014-01-01", data_dir: Path = DATA_DIR) -> Market:
    s = pd.read_parquet(data_dir / "shares_daily.parquet")
    s = s[s.date >= start]
    close, _ = adjust_splits(s.pivot(index="date", columns="ticker", values="close").sort_index())
    value = s.pivot(index="date", columns="ticker", values="value").reindex(close.index)
    idx = pd.read_parquet(data_dir / "index_daily.parquet")
    idx = idx.reindex(close.index).ffill()
    return Market(close, value, idx)
