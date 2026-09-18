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


def load_market(start: str = "2014-01-01", data_dir: Path = DATA_DIR) -> Market:
    s = pd.read_parquet(data_dir / "shares_daily.parquet")
    s = s[s.date >= start]
    close = s.pivot(index="date", columns="ticker", values="close").sort_index()
    value = s.pivot(index="date", columns="ticker", values="value").reindex(close.index)
    idx = pd.read_parquet(data_dir / "index_daily.parquet")
    idx = idx.reindex(close.index).ffill()
    return Market(close, value, idx)
