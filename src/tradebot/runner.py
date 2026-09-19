"""Бумажный бот: шаг (step), онлайн-сессия (session), сводки и публикация.

  python -m tradebot.runner tick                      # один шаг
  python -m tradebot.runner session --state live --publish "bash scripts/publish.sh live"
  python -m tradebot.runner update-data | update-intraday
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import subprocess
import sys
import time as systime
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import crypto as C
from . import intraday as I
from . import iss, livedata, notify
from . import strategies as S
from .data import DATA_DIR, Market, load_market
from .paper import INSTRUMENTS, Portfolio, crypto_switch, resolve
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
INTRADAY_FACTORIES = {
    "sma_cross": lambda p: I.sma_cross(int(p.get("fast", 6)), int(p.get("slow", 30))),
    "orb": lambda p: I.opening_range_breakout(int(p.get("n", 3))),
    "mean_reversion": lambda p: I.mean_reversion(int(p.get("n", 20)), float(p.get("k", 2.0))),
    "momentum": lambda p: I.intraday_momentum(float(p.get("threshold", 0.003))),
    "always_long": lambda p: I.always_long(),
}
TOLERANCE = 0.02
INDEX_REFRESH = timedelta(minutes=30)
BARS_REFRESH = timedelta(minutes=5)
HISTORY_EVERY = timedelta(minutes=5)


@dataclass
class Ctx:
    """Кэш между шагами одной сессии."""
    index_at: datetime | None = None
    bars: pd.DataFrame | None = None
    bars_at: datetime | None = None
    market: Market | None = None
    crypto_bars: dict = field(default_factory=dict)   # symbol -> (время загрузки, свечи)
    new_events: list[tuple[str, dict]] = field(default_factory=list)


def load_config(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def period_key(d: pd.Timestamp, freq: str) -> str:
    if freq == "D":
        return d.strftime("%Y-%m-%d")
    if freq == "W":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if freq in ("M", "ME"):
        return d.strftime("%Y-%m")
    if freq == "QE":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
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
    """Отличается ли портфель от целевого. Остаток рублей из-за лотов отличием не считается."""
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


def rub(x: float, cur: str = "RUB") -> str:
    sign = {"RUB": " ₽", "USDT": " $"}.get(cur, " " + cur)
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + sign


def pct(x: float) -> str:
    return f"{x:+.2%}".replace(".", ",")


def new_portfolio(pc: dict, strategy_name: str, rules: RiskRules, now: datetime) -> Portfolio:
    cap = float(pc["capital"])
    kind = pc.get("kind", "daily")
    return Portfolio(pc["id"], pc["title"], strategy_name, cap, cap,
                     last_withdraw_period=period_key(pd.Timestamp(now.date()), rules.withdraw_freq),
                     kind=kind, body=float(pc.get("body", cap)),
                     currency="USDT" if kind == "crypto" else "RUB")


def emit(ctx: Ctx, p: Portfolio, stamp: str, text: str, kind: str) -> None:
    p.log(stamp, text, kind)
    ctx.new_events.append((p.title, {"t": stamp, "kind": kind, "text": text}))


# ---------------- долгосрочные портфели ----------------
def step_daily(p: Portfolio, pc: dict, rules: RiskRules, last_day: pd.Timestamp, stamp: str,
               ctx: Ctx, update_data: bool) -> None:
    strat = FACTORIES[pc["strategy"]](pc.get("params", {}))
    needed = {v["ticker"] for v in INSTRUMENTS.values()} | set(p.positions)
    if p.last_signal_date is None or last_day > pd.Timestamp(p.last_signal_date):
        due = p.last_signal_date is None or strat.rebalance == "D" or \
            period_key(last_day, strat.rebalance) != period_key(pd.Timestamp(p.last_signal_date), strat.rebalance)
        prices, lots, _ = fetch_prices(needed)
        stop, amount = p.apply_risk(p.equity(prices), rules, period_key(last_day, rules.withdraw_freq),
                                    stamp, prices, lots)
        if amount:
            ctx.new_events.append((p.title, p.events[-1]))
        if stop:
            target = {"CASH": 1.0}
        elif due:
            is_stocks = pc["strategy"] == "stock_momentum"
            if is_stocks and update_data:
                livedata.update_shares()
            if ctx.market is None or is_stocks:
                ctx.market = load_market((last_day - pd.Timedelta(days=800)).strftime("%Y-%m-%d"))
            target = S.ensure_valid(strat.weights(ctx.market.upto(last_day)))
            target = {k: v for k, v in target.items() if v > 1e-9}
            emit(ctx, p, stamp, f"Сигнал по данным на {last_day.date()}: " +
                 ", ".join(f"{resolve(k)['ticker']} {v:.0%}" for k, v in target.items()), "signal")
        else:
            target = p.target or None
        if target and differs(current_weights(p, prices), target):
            p.pending = target
        p.last_signal_date = last_day.strftime("%Y-%m-%d")
    if p.pending:
        prices, lots, live = fetch_prices(needed | {resolve(k)["ticker"] for k in p.pending})
        if live:
            for tr in p.rebalance(p.pending, prices, lots, stamp, "сигнал стратегии"):
                emit(ctx, p, stamp, f"{'Купил' if tr.side == 'BUY' else 'Продал'} {tr.qty} {tr.ticker} "
                                    f"по {tr.price}", "trade")
            p.pending = None


# ---------------- активный (внутридневной) портфель ----------------
def get_bars(ctx: Ctx, now: datetime, live: bool) -> pd.DataFrame:
    if ctx.bars is None or ctx.bars_at is None or now - ctx.bars_at >= BARS_REFRESH:
        frm = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        ctx.bars = iss.candles("TMOS", frm, interval=10, market="shares", board="TQTF")
        ctx.bars_at = now
    bars = I.main_session(ctx.bars)
    # свеча закрыта, если она кончилась до «времени данных» (бесплатные данные ISS — с задержкой ~15 мин)
    data_time = (now - timedelta(minutes=15)).replace(tzinfo=None)
    return bars[bars.index + timedelta(minutes=10) <= data_time]


def step_intraday(p: Portfolio, pc: dict, now: datetime, stamp: str, ctx: Ctx) -> None:
    strat = INTRADAY_FACTORIES[pc["strategy"]](pc.get("params", {}))
    prices, lots, live = fetch_prices({"TMOS", "TMON"} | set(p.positions))
    eq = p.equity(prices)
    today = now.date().isoformat()
    if p.day != today and live:
        p.day, p.day_start_equity, p.day_trades, p.day_stopped = today, eq, 0, False
    bars = get_bars(ctx, now, live)
    want = float(strat.positions(bars).iloc[-1]) if len(bars) else 0.0
    limit = float(pc.get("daily_loss_limit", 0.03))
    if p.day_start_equity and eq < p.day_start_equity * (1 - limit) and not p.day_stopped:
        p.day_stopped = True
        emit(ctx, p, stamp, f"Дневной лимит убытка {limit:.0%} достигнут — до конца дня вне рынка.", "risk")
    if p.day_stopped:
        want = 0.0
    have = p.positions.get("TMOS", 0) * prices["TMOS"] / eq if eq else 0.0
    if live and abs(want - have) > 0.5:
        target = {"INDEX": 1.0} if want else {}
        why = "сигнал на покупку" if want else "сигнал на выход"
        for tr in p.rebalance(target, prices, lots, stamp, why):
            p.day_trades += 1
            emit(ctx, p, stamp, f"{'Купил' if tr.side == 'BUY' else 'Продал'} {tr.qty} {tr.ticker} "
                                f"по {tr.price:.4g} ({why})", "trade")
    every = timedelta(minutes=int(pc.get("comment_every_min", 5)))
    last = datetime.fromisoformat(p.last_comment).replace(tzinfo=MSK) if p.last_comment else None
    if live and (last is None or now - last >= every):
        eq = p.equity(prices)
        day_open = bars["open"][bars.index.date == now.date()]
        chg = prices["TMOS"] / day_open.iloc[0] - 1 if len(day_open) else 0.0
        state = "в позиции" if p.positions.get("TMOS") else "вне рынка"
        dpl = eq / p.day_start_equity - 1 if p.day_start_equity else 0.0
        p.log(stamp, f"TMOS {prices['TMOS']:.4g} ({pct(chg)} за день) · {state} · капитал {rub(eq)} "
                     f"({pct(dpl)} за день)", "comment")
        p.last_comment = now.strftime("%Y-%m-%d %H:%M")


# ---------------- крипто-портфель (24/7) ----------------
def get_crypto_bars(ctx: Ctx, symbol: str, now: datetime) -> pd.DataFrame:
    cached = ctx.crypto_bars.get(symbol)
    if cached is None or now - cached[0] >= BARS_REFRESH:
        start = pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=30)
        ctx.crypto_bars[symbol] = (now, C.klines(symbol, start))
    bars = ctx.crypto_bars[symbol][1]
    utc_now = pd.Timestamp.now("UTC").tz_localize(None)
    return bars[bars.index + pd.Timedelta(hours=1) <= utc_now]          # только закрытые свечи


def step_crypto(p: Portfolio, pc: dict, now: datetime, stamp: str, ctx: Ctx) -> dict[str, float]:
    sym = pc["symbol"]
    strat = C.FACTORIES[pc["strategy"]](pc.get("params", {}))
    price = C.last_price(sym)
    prices = {sym: price}
    eq = p.equity(prices)
    today = now.date().isoformat()
    if p.day != today:
        p.day, p.day_start_equity, p.day_trades, p.day_stopped = today, eq, 0, False
    bars = get_crypto_bars(ctx, sym, now)
    want = bool(len(bars)) and float(strat.positions(bars).iloc[-1]) > 0.5
    limit = float(pc.get("daily_loss_limit", 0.05))
    if p.day_start_equity and eq < p.day_start_equity * (1 - limit) and not p.day_stopped:
        p.day_stopped = True
        emit(ctx, p, stamp, f"Дневной лимит убытка {limit:.0%} достигнут — до конца дня в USDT.", "risk")
    if p.day_stopped:
        want = False
    why = "сигнал на покупку" if want else "сигнал на выход"
    tr = crypto_switch(p, sym, want, price, C.FEE, stamp, why)
    if tr:
        p.day_trades += 1
        emit(ctx, p, stamp, f"{'Купил' if tr.side == 'BUY' else 'Продал'} {tr.qty:.6g} {sym[:-4]} по "
                            f"{price:,.4g} $ ({why})", "trade")
    every = timedelta(minutes=int(pc.get("comment_every_min", 5)))
    last = datetime.fromisoformat(p.last_comment).replace(tzinfo=MSK) if p.last_comment else None
    if last is None or now - last >= every:
        eq = p.equity(prices)
        day_bars = bars[bars.index >= utc_day_start(now)]
        chg = price / day_bars["open"].iloc[0] - 1 if len(day_bars) else 0.0
        state = f"в {sym[:-4]}" if p.positions.get(sym) else "в USDT"
        dpl = eq / p.day_start_equity - 1 if p.day_start_equity else 0.0
        p.log(stamp, f"{sym[:-4]} {price:,.6g} $ ({pct(chg)} за сутки UTC) · {state} · капитал "
                     f"{rub(eq, p.currency)} ({pct(dpl)} за день)", "comment")
        p.last_comment = now.strftime("%Y-%m-%d %H:%M")
    return prices


def utc_day_start(now: datetime) -> pd.Timestamp:
    return pd.Timestamp(now.astimezone(ZoneInfo("UTC")).date())


# ---------------- общий шаг ----------------
def step(cfg: dict, state_dir: Path, now: datetime | None = None, update_data: bool = True,
         ctx: Ctx | None = None) -> dict:
    now = now or datetime.now(MSK)
    ctx = ctx or Ctx()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    rules = RiskRules(**cfg["risk"])
    if update_data and (ctx.index_at is None or now - ctx.index_at >= INDEX_REFRESH):
        livedata.update_index()
        ctx.index_at = now
    last_day = pd.read_parquet(DATA_DIR / "index_daily.parquet")["MCFTR"].dropna().index.max()
    blocks = []
    for pc in cfg["portfolio"]:
        path = state_dir / f"{pc['id']}.json"
        kind = pc.get("kind", "daily")
        fac = {"intraday": INTRADAY_FACTORIES, "crypto": C.FACTORIES}.get(kind, FACTORIES)
        name = fac[pc["strategy"]](pc.get("params", {})).name
        p = Portfolio.load(path) if path.exists() else new_portfolio(pc, name, rules, now)
        if kind == "crypto":
            try:
                prices = step_crypto(p, pc, now, stamp, ctx)
            except C.CryptoDataError as e:
                log.warning("%s: %s", p.id, e)
                last_px = p.history[-1]["tmos"] if p.history else 0.0
                blocks.append(site_block(p, {pc["symbol"]: last_px}, pc))
                p.save(path)
                continue
            record_history(p, prices, True, now, pc["symbol"])
            p.save(path)
            blocks.append(site_block(p, prices, pc))
            continue
        try:
            if kind == "intraday":
                step_intraday(p, pc, now, stamp, ctx)
            else:
                step_daily(p, pc, rules, last_day, stamp, ctx, update_data)
        except iss.IssError as e:
            log.warning("%s: %s", p.id, e)
        prices, _, live = fetch_prices({v["ticker"] for v in INSTRUMENTS.values()} | set(p.positions))
        record_history(p, prices, live, now)
        p.save(path)
        blocks.append(site_block(p, prices, pc))
    return write_site(state_dir, blocks, cfg, stamp, last_day)


def record_history(p: Portfolio, prices: dict[str, float], live: bool, now: datetime,
                   bench_ticker: str = "TMOS") -> None:
    eq = p.equity(prices)
    if p.history and now - datetime.strptime(p.history[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=MSK) \
            < HISTORY_EVERY:
        return
    if not p.risk:
        p.risk = {"peak": eq, "withdraw_mark": eq, "cooldown_left": 0, "withdrawn_total": 0.0}
    tmos = prices[bench_ticker]
    bench0 = p.history[0]["tmos"] if p.history else tmos
    p.history.append({"t": now.strftime("%Y-%m-%d %H:%M"), "equity": round(eq, 2), "withdrawn": p.withdrawn(),
                      "tmos": tmos, "bench": round(p.start_capital * tmos / bench0, 2), "live": live})
    p.history = compact(p.history)


def write_site(state_dir: Path, blocks: list[dict], cfg: dict, stamp: str, last_day) -> dict:
    site = {"updated": stamp, "last_data_day": str(pd.Timestamp(last_day).date()), "portfolios": blocks,
            "risk": cfg["risk"], "disclaimer": "Виртуальные деньги. Не является инвестиционной рекомендацией."}
    (state_dir / "site.json").write_text(json.dumps(site, ensure_ascii=False), encoding="utf-8")
    return site


def tick(cfg: dict, state_dir: Path, now: datetime | None = None, update_data: bool = True) -> dict:
    """Совместимость: один шаг без сессии."""
    return step(cfg, state_dir, now, update_data)


def compact(hist: list[dict], keep_intraday_days: int = 5) -> list[dict]:
    """Храним последнюю точку каждого дня + все точки за последние N дней."""
    if not hist:
        return hist
    days = sorted({h["t"][:10] for h in hist})
    recent = set(days[-keep_intraday_days:])
    last_of_day = {h["t"][:10]: h for h in hist}
    return [h for h in hist if h["t"][:10] in recent or last_of_day[h["t"][:10]] is h]


def site_block(p: Portfolio, prices: dict[str, float], pc: dict) -> dict:
    eq = p.equity(prices)
    names = {v["ticker"]: v["name"] for v in INSTRUMENTS.values()}
    pos = [{"ticker": t, "name": names.get(t, t), "qty": q, "price": prices[t],
            "value": round(q * prices[t], 2), "weight": q * prices[t] / eq} for t, q in p.positions.items()]
    return {"id": p.id, "title": p.title, "kind": p.kind, "currency": p.currency,
            "bench_name": pc.get("symbol", "TMOS")[:-4] if p.kind == "crypto" else "TMOS",
            "note": pc.get("note", ""), "strategy": p.strategy,
            "start_capital": p.start_capital, "body": p.body, "equity": round(eq, 2), "cash_rub": p.cash_rub,
            "withdrawn": p.withdrawn(), "total": round(eq + p.withdrawn(), 2),
            "pnl_pct": (eq + p.withdrawn()) / p.start_capital - 1,
            "day_pnl_pct": eq / p.day_start_equity - 1 if p.day_start_equity else None,
            "day_trades": p.day_trades, "positions": pos, "pending": p.pending, "risk": p.risk,
            "history": p.history, "trades": p.trades[-50:][::-1], "events": p.events[-120:][::-1],
            "withdrawals": p.withdrawals}


# ---------------- сводки и правило «тела» ----------------
def summarize(cfg: dict, state_dir: Path, now: datetime, ctx: Ctx) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M")
    lines = [f"Сводка {now:%d.%m %H:%M} МСК"]
    for pc in cfg["portfolio"]:
        path = state_dir / f"{pc['id']}.json"
        if not path.exists():
            continue
        p = Portfolio.load(path)
        if p.kind == "crypto":
            prices, lots = {pc["symbol"]: C.last_price(pc["symbol"])}, {}
        else:
            prices, lots, _ = fetch_prices({"TMOS", "TMON"} | set(p.positions))
        eq = p.equity(prices)
        rec = "продолжать без изменений"
        if p.kind in ("intraday", "crypto"):
            min_profit = float(pc.get("withdraw_min_profit", 0.02))
            if p.body and eq >= p.body * (1 + min_profit):
                want_out = math.floor((eq - p.body) * 100) / 100
                if p.kind == "crypto":
                    amount = crypto_withdraw(p, pc["symbol"], want_out, prices[pc["symbol"]], stamp)
                else:
                    amount = p._raise_cash(want_out, prices, lots, stamp)
                if amount > 0:
                    p.withdrawals.append({"t": stamp, "amount": amount, "equity_before": round(eq, 2)})
                    rec = (f"вывести прибыль {rub(amount, p.currency)}, торговать дальше с телом "
                           f"{rub(p.body, p.currency)}")
                    emit(ctx, p, stamp, f"РЕКОМЕНДАЦИЯ: {rec}. В бумажном режиме вывод выполнен.", "withdraw")
                    eq = p.equity(prices)
            elif p.day_stopped:
                rec = "дневной лимит убытка сработал, бот ждёт следующего дня"
            else:
                rec = f"вывод не нужен: прибыль сверх тела меньше {min_profit:.0%}"
        elif p.pending:
            rec = "ждёт исполнения заявки при открытии рынка"
        day = f" · за день {pct(eq / p.day_start_equity - 1)}" if p.day_start_equity else ""
        total = (eq + p.withdrawn()) / p.start_capital - 1
        text = (f"{p.title}: капитал {rub(eq, p.currency)}{day} · с начала {pct(total)} "
                f"(выведено {rub(p.withdrawn(), p.currency)})"
                f" · сделок сегодня {p.day_trades if p.kind != 'daily' else '—'} · рекомендация: {rec}")
        p.log(stamp, text, "summary")
        p.save(path)
        lines.append("• " + text)
    return "\n".join(lines)


def crypto_withdraw(p: Portfolio, symbol: str, amount: float, price: float, stamp: str) -> float:
    """Вывод прибыли из крипто-портфеля: при нехватке USDT продаётся часть монеты."""
    need = amount - p.cash_rub
    if need > 0 and p.positions.get(symbol):
        qty = min(p.positions[symbol], round(need / (price * (1 - C.FEE)) + 1e-8, 8))
        gross = qty * price
        p.cash_rub += gross - gross * C.FEE
        p.positions[symbol] = round(p.positions[symbol] - qty, 8)
        if p.positions[symbol] <= 0:
            del p.positions[symbol]
        p.trades.append({"time": stamp, "ticker": symbol, "side": "SELL", "qty": qty, "price": price,
                         "cost": round(gross * C.FEE, 6), "reason": "вывод прибыли"})
    amount = round(min(amount, p.cash_rub), 2)
    p.cash_rub = round(p.cash_rub - amount, 8)
    return amount


def session_end(now: datetime) -> datetime:
    """Сессия длится ~5 ч 50 мин (лимит задачи GitHub Actions — 6 ч); запуски — каждые 6 часов."""
    return now + timedelta(minutes=350)


def session(cfg: dict, state_dir: Path, every: int, publish_every: int, publish_cmd: str | None,
            until: datetime | None = None) -> None:
    ctx = Ctx()
    now = datetime.now(MSK)
    until = until or session_end(now)
    slots = cfg.get("session", {}).get("summary_times", ["12:00", "15:00", "18:45", "23:45"])
    done = {s for s in slots if now.strftime("%H:%M") >= s}
    last_pub, errors = 0.0, 0
    log.info("Сессия до %s МСК", until.strftime("%H:%M"))
    while datetime.now(MSK) < until:
        now = datetime.now(MSK)
        try:
            step(cfg, state_dir, now, True, ctx)
            errors = 0
        except Exception:  # noqa: BLE001 — сессия не должна падать из-за одного сбоя сети
            errors += 1
            log.exception("Ошибка шага (%d подряд)", errors)
            if errors >= 10:
                raise
        due = [s for s in slots if s not in done and now.strftime("%H:%M") >= s]
        if due:
            done.update(due)
            try:
                notify.telegram(summarize(cfg, state_dir, now, ctx))
            except Exception:  # noqa: BLE001
                log.exception("Сводка не удалась")
        important = [(t, e) for t, e in ctx.new_events if e["kind"] in ("trade", "withdraw", "risk")]
        if important:
            notify.telegram("\n".join(f"{t}: {e['text']}" for t, e in important))
        had_events = bool(ctx.new_events)
        ctx.new_events.clear()
        if publish_cmd and (had_events or systime.time() - last_pub >= publish_every):
            subprocess.run(publish_cmd, shell=True, check=False)
            last_pub = systime.time()
        systime.sleep(every)
    if publish_cmd:
        subprocess.run(publish_cmd, shell=True, check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "session", "update-data", "update-intraday", "update-crypto"])
    ap.add_argument("--config", default=str(ROOT / "config" / "portfolios.toml"))
    ap.add_argument("--state", default=str(ROOT / "state"))
    ap.add_argument("--every", type=int, default=60, help="секунд между шагами сессии")
    ap.add_argument("--publish-every", type=int, default=180, help="секунд между публикациями")
    ap.add_argument("--publish", default=None, help="команда публикации состояния")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    if a.cmd == "update-crypto":
        for sym in C.SYMBOLS:
            C.update_history(sym)
        return 0
    if a.cmd == "update-intraday":
        for secid in livedata.INTRADAY:
            log.info("%s: всего свечей %d", secid, livedata.update_candles(secid))
        snap = iss.spread_snapshot(["TMOS", "TMON"])
        (DATA_DIR / "spread_snapshot.json").write_text(snap.to_json(orient="index", force_ascii=False),
                                                       encoding="utf-8")
        log.info("Спред сейчас:\n%s", snap.to_string())
        return 0
    if a.cmd == "update-data":
        d = livedata.update_index()
        log.info("Индексы до %s, добавлено дней по акциям: %d", d.date(), livedata.update_shares())
        return 0
    cfg = load_config(Path(a.config))
    if a.cmd == "session":
        session(cfg, state, a.every, a.publish_every, a.publish)
        return 0
    site = tick(cfg, state)
    for p in site["portfolios"]:
        log.info("%s: капитал %.2f ₽ (выведено %.2f), результат %+.2f%%",
                 p["title"], p["equity"], p["withdrawn"], p["pnl_pct"] * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
