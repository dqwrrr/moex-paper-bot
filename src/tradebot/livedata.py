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


INTRADAY = {  # что качаем для внутридневных исследований
    "TMOS": {"market": "shares", "board": "TQTF", "start": "2020-08-01"},
    "IMOEX": {"market": "index", "board": None, "start": "2015-01-01"},
}


def update_candles(secid: str, interval: int = 10, data_dir: Path = DATA_DIR) -> int:
    """Докачивает внутридневные свечи в data/candles_<secid>_<interval>m.parquet."""
    cfg = INTRADAY[secid]
    path = data_dir / f"candles_{secid}_{interval}m.parquet"
    cur = pd.read_parquet(path) if path.exists() else None
    frm = cfg["start"] if cur is None or cur.empty else (cur.index.max() - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    # качаем по годам, чтобы не держать огромные ответы
    frames = []
    for y0 in pd.date_range(frm, pd.Timestamp.today() + pd.Timedelta(days=1), freq="YS").union([pd.Timestamp(frm)]):
        y1 = min(pd.Timestamp(year=y0.year, month=12, day=31), pd.Timestamp.today())
        df = iss.candles(secid, y0.strftime("%Y-%m-%d"), y1.strftime("%Y-%m-%d"), interval,
                         cfg["market"], cfg["board"])
        log.info("%s %s: %d свечей", secid, y0.year, len(df))
        frames.append(df)
    new = pd.concat(frames)
    out = new if cur is None else pd.concat([cur, new])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.to_parquet(path)
    return len(out)
