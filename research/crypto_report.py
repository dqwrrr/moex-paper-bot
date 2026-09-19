"""Исследование крипто-стратегий на часовых свечах: подбор 2020–2023, проверка 2024–сейчас.

Запуск (нужен доступ к data-api.binance.vision): python research/crypto_report.py
Результат: docs/crypto-research.md и research/out/crypto_results.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tradebot import crypto as C  # noqa: E402
from tradebot import intraday as I  # noqa: E402

PERIODS = {"2020–2023 (подбор)": ("2020-01-01", "2023-12-31"), "2024–2026 (проверка)": ("2024-01-01", None)}


def stats(eq: pd.Series) -> dict:
    d = eq.resample("D").last().dropna()
    r = d.pct_change().dropna()
    years = max((d.index[-1] - d.index[0]).days / 365.25, 1e-9)
    cagr = (d.iloc[-1] / d.iloc[0]) ** (1 / years) - 1
    mdd = float((d / d.cummax() - 1).min())
    return {"CAGR": cagr, "MaxDD": mdd, "Sharpe": float(r.mean() / r.std() * np.sqrt(365)) if r.std() else 0.0,
            "Лучший день": float(r.max()), "Худший день": float(r.min()),
            "Дней с +100%": int((r >= 1.0).sum())}


def grid() -> list:
    s = [C.buy_hold()]
    s += [C.sma_cross(f, sl) for f, sl in [(12, 48), (24, 120), (24, 240), (48, 240), (72, 480)]]
    s += [C.donchian(e, x) for e, x in [(24, 12), (48, 24), (120, 48), (240, 120)]]
    s += [C.ts_momentum(lb) for lb in (24, 72, 168, 336, 720)]
    s += [C.mean_reversion(n, k) for n, k in [(24, 2.0), (48, 2.5), (72, 3.0)]]
    return s


def main() -> None:
    rows = []
    for sym in C.SYMBOLS:
        path = ROOT / "data" / f"crypto_{sym}_1h.parquet"
        if not path.exists():
            print("нет данных", sym)
            continue
        bars = pd.read_parquet(path)
        for st in grid():
            res = I.backtest(st, bars, C.FEE)
            for lab, (a, b) in PERIODS.items():
                e = res.equity.loc[a:b]
                if len(e) < 24 * 60:
                    continue
                yrs = (e.index[-1] - e.index[0]).days / 365.25
                pos = st.positions(bars).loc[a:b]
                trades = int((pos.diff().abs() > 0).sum())
                rows.append({"Монета": sym, "Стратегия": st.name, "Период": lab, **stats(e),
                             "Сделок в год": trades / yrs if yrs else 0})
    df = pd.DataFrame(rows)
    (ROOT / "research" / "out").mkdir(parents=True, exist_ok=True)
    df.to_csv(ROOT / "research" / "out" / "crypto_results.csv", index=False)
    md = ["# Крипто-стратегии: результаты (генерируется автоматически)\n",
          f"Комиссия+проскальзывание: {C.FEE:.2%} на сторону. Позиция: 100% в монете или 100% в USDT (без плеча).\n"]
    for sym in df["Монета"].unique():
        md.append(f"\n## {sym}\n")
        piv = df[df["Монета"] == sym].pivot_table(index="Стратегия", columns="Период",
                                                  values=["CAGR", "MaxDD", "Sharpe"], aggfunc="first")
        piv = piv.sort_values(("Sharpe", "2024–2026 (проверка)"), ascending=False)
        md.append("| Стратегия | CAGR подбор | CAGR проверка | MaxDD проверка | Sharpe подбор | Sharpe проверка |")
        md.append("|---|---|---|---|---|---|")
        for name, r in piv.iterrows():
            g = lambda m, p, r=r: r.get((m, p), np.nan)  # noqa: E731
            md.append(f"| {name} | {g('CAGR', '2020–2023 (подбор)'):.0%} | {g('CAGR', '2024–2026 (проверка)'):.0%} | "
                      f"{g('MaxDD', '2024–2026 (проверка)'):.0%} | {g('Sharpe', '2020–2023 (подбор)'):.2f} | "
                      f"{g('Sharpe', '2024–2026 (проверка)'):.2f} |")
    bd = df.groupby("Монета")[["Лучший день", "Худший день", "Дней с +100%"]].max()
    md.append("\n## Сколько раз стратегии удваивали капитал за один день\n")
    md.append("| Монета | Лучший день | Худший день | Дней с +100% |\n|---|---|---|---|")
    for sym, r in bd.iterrows():
        md.append(f"| {sym} | {r['Лучший день']:+.0%} | {r['Худший день']:+.0%} | {int(r['Дней с +100%'])} |")
    (ROOT / "docs" / "crypto-research.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
