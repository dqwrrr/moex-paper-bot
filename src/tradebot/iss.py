"""Клиент MOEX ISS (https://iss.moex.com) — бесплатные данные Мосбиржи без токена.

Важно: бесплатные текущие котировки ISS идут с задержкой ~15 минут.
Для дневной стратегии это не критично.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests

BASE = "https://iss.moex.com/iss"
TIMEOUT = 20


class IssError(RuntimeError):
    pass


def _get(path: str, params: dict[str, Any] | None = None, retries: int = 3) -> dict:
    params = {"iss.meta": "off", **(params or {})}
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:  # сеть/JSON
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise IssError(f"ISS недоступен: {path}: {last}")


def block_to_df(payload: dict, block: str) -> pd.DataFrame:
    """ISS отдаёт таблицы как {"columns": [...], "data": [[...], ...]}."""
    b = payload.get(block)
    if not b:
        return pd.DataFrame()
    return pd.DataFrame(b["data"], columns=b["columns"])


def _paged_history(path: str, params: dict) -> pd.DataFrame:
    frames, start = [], 0
    while True:
        p = _get(path, {**params, "start": start})
        df = block_to_df(p, "history")
        if df.empty:
            break
        frames.append(df)
        cur = block_to_df(p, "history.cursor")
        if cur.empty:
            if len(df) < 100:
                break
            start += len(df)
            continue
        c = cur.iloc[0]
        start = int(c["INDEX"]) + int(c["PAGESIZE"])
        if start >= int(c["TOTAL"]):
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def index_history(secid: str, date_from: str, date_till: str | None = None) -> pd.Series:
    params = {"from": date_from, "history.columns": "TRADEDATE,CLOSE"}
    if date_till:
        params["till"] = date_till
    df = _paged_history(f"history/engines/stock/markets/index/securities/{secid}.json", params)
    if df.empty:
        return pd.Series(dtype=float, name=secid)
    s = df.dropna(subset=["CLOSE"]).set_index(pd.to_datetime(df["TRADEDATE"]))["CLOSE"].astype(float)
    return s[~s.index.duplicated()].rename(secid)


SHARE_COLS = ["BOARDID", "TRADEDATE", "SECID", "OPEN", "HIGH", "LOW", "CLOSE", "VALUE", "VOLUME"]


def shares_on_date(date: str, board: str = "TQBR") -> pd.DataFrame:
    """Дневные итоги всех акций режима TQBR за одну дату (для пополнения истории)."""
    df = _paged_history(f"history/engines/stock/markets/shares/boards/{board}/securities.json",
                        {"date": date, "history.columns": ",".join(SHARE_COLS)})
    if df.empty:
        return df
    df = df.rename(columns=str.lower).rename(columns={"tradedate": "date", "secid": "ticker"})
    df["date"] = pd.to_datetime(df["date"])
    return df.drop(columns="boardid").dropna(subset=["close"])


def quotes(tickers: list[str], board: str) -> pd.DataFrame:
    """Текущие котировки (с задержкой) + размер лота. Индекс — тикер."""
    p = _get(f"engines/stock/markets/shares/boards/{board}/securities.json",
             {"securities": ",".join(tickers), "iss.only": "securities,marketdata",
              "securities.columns": "SECID,LOTSIZE,PREVPRICE,SHORTNAME",
              "marketdata.columns": "SECID,LAST,BID,OFFER,TRADINGSTATUS,SYSTIME,UPDATETIME"})
    sec = block_to_df(p, "securities").set_index("SECID")
    md = block_to_df(p, "marketdata").set_index("SECID")
    df = sec.join(md, how="left")
    df["PRICE"] = df["LAST"].where(df["LAST"].notna() & (df["LAST"] > 0), df["PREVPRICE"])
    df["LIVE"] = df["LAST"].notna() & (df["LAST"] > 0) & (df.get("TRADINGSTATUS") == "T")
    return df


CANDLE_PAGE = 500


def candles(secid: str, date_from: str, date_till: str | None = None, interval: int = 10,
            market: str = "shares", board: str | None = "TQTF") -> pd.DataFrame:
    """Свечи (1, 10, 60, 24 = день). Для индекса: market="index", board=None."""
    path = (f"engines/stock/markets/{market}/boards/{board}/securities/{secid}/candles.json" if board
            else f"engines/stock/markets/{market}/securities/{secid}/candles.json")
    params: dict[str, Any] = {"from": date_from, "interval": interval}
    if date_till:
        params["till"] = date_till
    frames, start = [], 0
    while True:
        df = block_to_df(_get(path, {**params, "start": start}), "candles")
        if df.empty:
            break
        frames.append(df)
        if len(df) < CANDLE_PAGE:
            break
        start += len(df)
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "value", "volume"])
    out = pd.concat(frames, ignore_index=True)
    out["begin"] = pd.to_datetime(out["begin"])
    out = out.drop_duplicates("begin").set_index("begin").sort_index()
    return out[["open", "high", "low", "close", "value", "volume"]].astype(float)


def spread_snapshot(tickers: list[str], board: str = "TQTF") -> pd.DataFrame:
    """Текущие лучшие цены покупки/продажи и шаг цены — для оценки спреда."""
    p = _get(f"engines/stock/markets/shares/boards/{board}/securities.json",
             {"securities": ",".join(tickers), "iss.only": "securities,marketdata",
              "securities.columns": "SECID,MINSTEP,LOTSIZE,PREVPRICE",
              "marketdata.columns": "SECID,BID,OFFER,LAST,SPREAD,SYSTIME"})
    return block_to_df(p, "securities").set_index("SECID").join(block_to_df(p, "marketdata").set_index("SECID"))
