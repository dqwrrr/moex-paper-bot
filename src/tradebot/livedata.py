"""Пополнение локальной истории свежими данными ISS (запускается в GitHub Actions)."""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from . import iss
from .data import DATA_DIR

log = logging.getLogger(__name__)
INDEXES = ["IMOEX", "MCFTR"]


def update_index(data_dir: Path = DATA_DIR) -> pd.Timestamp:
    path = data_dir / "index_daily.parquet"
    cur = pd.read_parquet(path)
    last = cur["MCFTR"].dropna().index.max()
    frm = (last - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    new = pd.concat([iss.index_history(s, frm) for s in INDEXES], axis=1)
    if not new.empty:
        cur = cur.combine_first(new)
        cur.loc[new.index, new.columns] = new
        cur.sort_index().to_parquet(path)
    return cur["MCFTR"].dropna().index.max()


def update_shares(data_dir: Path = DATA_DIR, max_days: int | None = None) -> int:
    """Докачивает дневные итоги акций TQBR за пропущенные торговые дни."""
    path = data_dir / "shares_daily.parquet"
    cur = pd.read_parquet(path)
    idx = pd.read_parquet(data_dir / "index_daily.parquet")["MCFTR"].dropna()
    have = set(cur["date"].unique())
    todo = [d for d in idx.index if d > cur["date"].max() - pd.Timedelta(days=5) and d not in have]
    if max_days:
        todo = todo[:max_days]
    frames = []
    for i, d in enumerate(todo):
        df = iss.shares_on_date(d.strftime("%Y-%m-%d"))
        if not df.empty:
            frames.append(df)
        if (i + 1) % 50 == 0:
            log.info("Загружено дней: %d/%d", i + 1, len(todo))
    if frames:
        out = pd.concat([cur, *frames], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
        out.sort_values(["date", "ticker"]).to_parquet(path, index=False)
    return len(frames)
