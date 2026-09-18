"""Один «тик» бумажного бота: обновить данные → сигнал → исполнение → оценка → сайт.

Запуск: python -m tradebot.runner tick   (GitHub Actions каждые 30 минут в будни)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import iss, livedata
from . import strategies as S
from .data import DATA_DIR, load_market
from .paper import INSTRUMENTS, Portfolio, resolve
from .risk import RiskRules

ROOT = Path(__file__).resolve().parents[2]
MSK = ZoneInfo("Europe/Moscow")
log = logging.getLogger("tradebot")

FACTORIES = {
    "index_sma": lambda p: S.index_sma(int(p.get("n", 150))),
    "stock_momentum": lambda p: S.stock_momentum(int(p.get("lookback", 126)), int(p.get("skip", 21)),
                                                 int(p.get("top", 5)), int(p.get("universe", 30)),
                                                 int(p.get("trend_filter", 0)) or None),
    "buy_hold_index": lambda p: S.buy_hold_index(),
}
TOLERANCE = 0.02


def load_config(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def period_key(d: pd.Timestamp, freq: str) -> str:
    if freq == "D":
        return d.strftime("%Y-%m-%d")
    if freq == "W":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if freq == "M":
        return d.strftime("%Y-%m")
    if freq == "QE":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if freq == "ME":
        return d.strftime("%Y-%m")
    if freq == "YE":
        return str(d.year)
    raise ValueError(freq)


def current_weights(p: Portfolio, prices: dict[str, float]) -> dict[str, float]:
    eq = p.equity(prices)
    back = {v["ticker"]: k for k, v in INSTRUMENTS.items()}
    w = {back.get(t, t): q * prices[t] / eq for t, q in p.positions.items()}
    w["_RUB"] = p.cash_rub / eq
    return w


def differs(cur: dict[str, float], target: dict[str, float]) -> bool:
    """Отличается ли текущий портфель от целевого. Остаток рублей из-за округления
    до лотов не считается отличием (веса бумаг нормируются)."""
    invested = {k: v for k, v in cur.items() if k != "_RUB"}
    tot = sum(invested.values())
    if tot < 1e-9:
        return bool(target)
    invested = {k: v / tot for k, v in invested.items()}
    return any(abs(invested.get(k, 0) - target.get(k, 0)) > TOLERANCE for k in set(invested) | set(target))


def fetch_prices(tickers: set[str]) -> tuple[dict[str, float], dict[str, int], bool]:
    by_board: dict[str, list[str]] = {}
    for t in tickers:
        board = next((v["board"] for v in INSTRUMENTS.values() if v["ticker"] == t), "TQBR")
        by_board.setdefault(board, []).append(t)
    prices, lots, live = {}, {}, True
    for board, ts in by_board.items():
        q = iss.quotes(sorted(ts), board)
        for t in ts:
            if t not in q.index or pd.isna(q.loc[t, "PRICE"]):
                raise iss.IssError(f"Нет цены для {t}")
            prices[t] = float(q.loc[t, "PRICE"])
            lots[t] = int(q.loc[t, "LOTSIZE"] or 1)
            live &= bool(q.loc[t, "LIVE"])
    return prices, lots, live


def tick(cfg: dict, state_dir: Path, now: datetime | None = None, update_data: bool = True) -> dict:
    now = now or datetime.now(MSK)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    rules = RiskRules(**cfg["risk"])
    if update_data:
        livedata.update_index()
    last_day = pd.read_parquet(DATA_DIR / "index_daily.parquet")["MCFTR"].dropna().index.max()
    market = None
    summary = []
    for pc in cfg["portfolio"]:
        path = state_dir / f"{pc['id']}.json"
        strat = FACTORIES[pc["strategy"]](pc.get("params", {}))
        p = Portfolio.load(path) if path.exists() else Portfolio(
            pc["id"], pc["title"], strat.name, float(pc["capital"]), float(pc["capital"]),
            last_withdraw_period=period_key(pd.Timestamp(now.date()), rules.withdraw_freq))
        needed = {v["ticker"] for v in INSTRUMENTS.values()} | set(p.positions)

        # 1) Новый торговый день в данных → риск-правила и сигнал
        new_day = p.last_signal_date is None or last_day > pd.Timestamp(p.last_signal_date)
        if new_day:
            due = p.last_signal_date is None or strat.rebalance == "D" or \
                period_key(last_day, strat.rebalance) != period_key(pd.Timestamp(p.last_signal_date), strat.rebalance)
            prices, lots, _ = fetch_prices(needed)
            eq = p.equity(prices)
            stop, _ = p.apply_risk(eq, rules, period_key(last_day, rules.withdraw_freq), stamp, prices, lots)
            if stop:
                target = {"CASH": 1.0}
            elif due:
                if strat.name.startswith("Моментум акций") and update_data:
                    livedata.update_shares()
                if market is None or strat.name.startswith("Моментум акций"):
                    market = load_market((last_day - pd.Timedelta(days=800)).strftime("%Y-%m-%d"))
                target = S.ensure_valid(strat.weights(market.upto(last_day)))
                target = {k: v for k, v in target.items() if v > 1e-9}
                p.log(stamp, f"Сигнал по данным на {last_day.date()}: " +
                      ", ".join(f"{resolve(k)['ticker']} {v:.0%}" for k, v in target.items()), "signal")
            else:
                target = p.target or None
            if target and differs(current_weights(p, prices), target):
                p.pending = target
            p.last_signal_date = last_day.strftime("%Y-%m-%d")

        # 2) Исполнение заявки, если рынок открыт
        if p.pending:
            tickers = needed | {resolve(k)["ticker"] for k in p.pending}
            prices, lots, live = fetch_prices(tickers)
            if live:
                trades = p.rebalance(p.pending, prices, lots, stamp, "сигнал стратегии")
                for tr in trades:
                    p.log(stamp, f"{'Купил' if tr.side == 'BUY' else 'Продал'} {tr.qty} {tr.ticker} по {tr.price}", "trade")
                p.pending = None

        # 3) Оценка портфеля
        prices, lots, live = fetch_prices(needed | set(p.positions))
        eq = p.equity(prices)
        tmos = prices["TMOS"]
        if not p.history:
            p.risk = p.risk or {"peak": eq, "withdraw_mark": eq, "cooldown_left": 0, "withdrawn_total": 0.0}
        bench0 = p.history[0]["tmos"] if p.history else tmos
        p.history.append({"t": stamp, "equity": round(eq, 2), "withdrawn": p.withdrawn(),
                          "tmos": tmos, "bench": round(p.start_capital * tmos / bench0, 2), "live": live})
        p.history = compact(p.history)
        p.save(path)
        summary.append(site_block(p, prices, pc))
    site = {"updated": stamp, "last_data_day": str(last_day.date()), "portfolios": summary,
            "risk": cfg["risk"], "disclaimer": "Виртуальные деньги. Не является инвестиционной рекомендацией."}
    (state_dir / "site.json").write_text(json.dumps(site, ensure_ascii=False, indent=1), encoding="utf-8")
    return site


def compact(hist: list[dict], keep_intraday_days: int = 5) -> list[dict]:
    """Храним последнюю точку каждого дня + все точки за последние N дней."""
    if not hist:
        return hist
    days = sorted({h["t"][:10] for h in hist})
    recent = set(days[-keep_intraday_days:])
    last_of_day = {}
    for h in hist:
        last_of_day[h["t"][:10]] = h
    return [h for h in hist if h["t"][:10] in recent or last_of_day[h["t"][:10]] is h]


def site_block(p: Portfolio, prices: dict[str, float], pc: dict) -> dict:
    eq = p.equity(prices)
    names = {v["ticker"]: v["name"] for v in INSTRUMENTS.values()}
    pos = [{"ticker": t, "name": names.get(t, t), "qty": q, "price": prices[t],
            "value": round(q * prices[t], 2), "weight": q * prices[t] / eq} for t, q in p.positions.items()]
    return {"id": p.id, "title": p.title, "note": pc.get("note", ""), "strategy": p.strategy,
            "start_capital": p.start_capital, "equity": round(eq, 2), "cash_rub": p.cash_rub,
            "withdrawn": p.withdrawn(), "total": round(eq + p.withdrawn(), 2),
            "pnl_pct": (eq + p.withdrawn()) / p.start_capital - 1, "positions": pos,
            "pending": p.pending, "risk": p.risk, "history": p.history,
            "trades": p.trades[-50:][::-1], "events": p.events[-50:][::-1], "withdrawals": p.withdrawals}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "update-data"])
    ap.add_argument("--config", default=str(ROOT / "config" / "portfolios.toml"))
    ap.add_argument("--state", default=str(ROOT / "state"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "update-data":
        d = livedata.update_index()
        n = livedata.update_shares()
        log.info("Индексы до %s, добавлено дней по акциям: %d", d.date(), n)
        return 0
    site = tick(load_config(Path(a.config)), Path(a.state))
    for p in site["portfolios"]:
        log.info("%s: капитал %.2f ₽ (выведено %.2f), результат %+.2f%%",
                 p["title"], p["equity"], p["withdrawn"], p["pnl_pct"] * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
