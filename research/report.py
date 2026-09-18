"""Полное исследование стратегий → docs/research-report.md и графики docs/img/*.png.

Запуск: python research/report.py   (~1–2 минуты)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from tradebot import strategies as S  # noqa: E402
from tradebot.backtest import run_backtest  # noqa: E402
from tradebot.data import Market, load_market  # noqa: E402
from tradebot.metrics import summary  # noqa: E402
from tradebot.risk import RiskRules, apply_overlay  # noqa: E402

IMG = ROOT / "docs" / "img"
BLUE, GRAY, ORANGE, INK, MUTE = "#2a78d6", "#8a8984", "#eb6834", "#0b0b0b", "#52514e"


def fmt_table(rows: list[dict], cols: list[str]) -> str:
    pct = {"CAGR", "MaxDD", "Vol", "Худший год", "Мес.в плюсе", "Доля в риске", "MaxDD 2021–26"}
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                v = f"{v:.1%}" if c in pct else f"{v:.2f}"
            cells.append(str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def line_chart(series: dict[str, pd.Series], title: str, path: Path, log: bool = True) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=130)
    colors = [BLUE, GRAY, ORANGE]
    for (name, s), c in zip(series.items(), colors, strict=False):
        ax.plot(s.index, s.values, color=c, lw=2 if c == BLUE else 1.5, ls="-" if c != GRAY else "--", label=name)
        ax.annotate(name, (s.index[-1], s.values[-1]), xytext=(4, 0), textcoords="offset points",
                    fontsize=8, color=INK, va="center")
    if log:
        from matplotlib.ticker import FuncFormatter, NullFormatter
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    ax.grid(axis="y", color="#e4e3de", lw=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTE, labelsize=8)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def index_only_market() -> Market:
    idx = pd.read_parquet(ROOT / "data" / "index_daily.parquet")[["IMOEX", "MCFTR"]].loc["2003-06-01":]
    idx = idx.dropna(subset=["MCFTR"])
    empty = pd.DataFrame(index=idx.index)
    return Market(empty, empty, idx)


def main() -> None:
    IMG.mkdir(parents=True, exist_ok=True)
    md: list[str] = []
    # ---------- 1. Тайминг индекса, 2004–2021 ----------
    mi = index_only_market()
    periods = {"2004–2012 (подбор)": ("2004-06-01", "2012-12-31"), "2013–2021.06 (проверка)": ("2013-01-01", "2021-06-11"),
               "2021.06–2026 (новые данные)": ("2021-06-14", None)}
    strats = [S.buy_hold_index(), S.cash_only()] + [S.index_sma(n) for n in (50, 100, 150, 200, 250)] + \
             [S.index_abs_momentum(n) for n in (63, 126, 252)]
    res_idx = {s.name: run_backtest(s, mi, "2004-06-01") for s in strats}
    rows = []
    for name, r in res_idx.items():
        for lab, (a, b) in periods.items():
            sm = summary(r.equity.loc[a:b])
            rows.append({"Стратегия": name, "Период": lab, **sm, "Доля в риске": r.exposure.loc[a:b].mean()})
    rows.sort(key=lambda x: (x["Период"], -x["Calmar"] if x["Calmar"] == x["Calmar"] else 0))
    md.append("## 1. Тайминг индекса: фонд на индекс ↔ денежный рынок (2004–2026)\n")
    md.append(fmt_table(rows, ["Стратегия", "Период", "CAGR", "MaxDD", "Sharpe", "Calmar", "Худший год", "Доля в риске"]))
    line_chart({"SMA150: индекс/кэш": res_idx["Индекс выше SMA150"].equity,
                "Купить и держать": res_idx["Купить и держать индекс"].equity,
                "Денежный рынок": res_idx["Только денежный рынок"].equity},
               "Рост 1 ₽, 2004–2026, логарифмическая шкала", IMG / "index_timing.png")
    md.append("\n![Тайминг индекса](img/index_timing.png)\n")

    # ---------- 2. Акции, 2014–2021 ----------
    m = load_market("2013-03-01")
    stock_strats = [S.buy_hold_index(), S.equal_weight_liquid(20), S.stock_low_vol(10), S.stock_reversal(5)]
    stock_strats += [S.stock_momentum(lb, 21, top, 30, None) for lb in (63, 126, 189, 252) for top in (3, 5, 8, 10)]
    stock_strats += [S.stock_momentum(126, 21, 5, 30, 200)]
    res_st = {s.name: run_backtest(s, m, "2014-06-01") for s in stock_strats}
    rows = []
    for name, r in res_st.items():
        a, b = summary(r.equity.loc[:"2017-12-31"]), summary(r.equity.loc["2018-01-01":"2021-06-11"])
        c = summary(r.equity.loc["2021-06-14":])
        rows.append({"Стратегия": name, "Sharpe 2014–17": a["Sharpe"], "Sharpe 2018–21": b["Sharpe"],
                     "Sharpe 2021–26": c["Sharpe"], "CAGR 2014–17": f"{a['CAGR']:.1%}",
                     "CAGR 2018–21": f"{b['CAGR']:.1%}", "CAGR 2021–26": f"{c['CAGR']:.1%}",
                     "MaxDD 2021–26": c["MaxDD"]})
    rows.sort(key=lambda x: -x["Sharpe 2021–26"])
    md.append("\n## 2. Стратегии на акциях (2014–2026, без дивидендов)\n")
    md.append(fmt_table(rows, ["Стратегия", "Sharpe 2014–17", "Sharpe 2018–21", "Sharpe 2021–26", "CAGR 2014–17",
                               "CAGR 2018–21", "CAGR 2021–26", "MaxDD 2021–26"]))
    mom = res_st["Моментум акций 126д top5"].equity
    line_chart({"Моментум top5": mom, "Индекс (с дивидендами)": res_st["Купить и держать индекс"].equity},
               "Моментум акций vs индекс, 2014–2026", IMG / "momentum.png")
    md.append("\n![Моментум](img/momentum.png)\n")

    # ---------- 3. Правила вывода ----------
    rules_set = {"без правил": RiskRules(None, 0, None), "только вывод 50% при +15%": RiskRules(None, 0, 0.15, 0.5),
                 "стоп 20% + вывод": RiskRules(0.20, 21, 0.15, 0.5)}
    cases = {"Тайминг SMA150 (2004–2026)": res_idx["Индекс выше SMA150"].equity,
             "Купить и держать индекс (2004–2026)": res_idx["Купить и держать индекс"].equity,
             "Моментум top5 (2014–2026)": mom,
             "Тайминг SMA150 (2021.06–2026)": res_idx["Индекс выше SMA150"].equity.loc["2021-06-14":],
             "Купить и держать (2021.06–2026)": res_idx["Купить и держать индекс"].equity.loc["2021-06-14":],
             "Денежный рынок (2021.06–2026)": res_idx["Только денежный рынок"].equity.loc["2021-06-14":]}
    rows = []
    for cname, e in cases.items():
        r = e.pct_change().fillna(0)
        for rname, rules in rules_set.items():
            acc, taken, log = apply_overlay(r, rules, 100_000)
            back = next((w.date.date().isoformat() for w in log if taken.loc[w.date] >= 100_000), "—")
            rows.append({"Стратегия": cname, "Правила": rname, "На счёте, ₽": f"{acc.iloc[-1]:,.0f}",
                         "Выведено, ₽": f"{taken.iloc[-1]:,.0f}", "Минимум на счёте, ₽": f"{acc.min():,.0f}",
                         "Вложения вернулись": back})
    md.append("\n## 3. Правила вывода прибыли и стоп-кран (старт 100 000 ₽)\n")
    md.append(fmt_table(rows, ["Стратегия", "Правила", "На счёте, ₽", "Выведено, ₽", "Минимум на счёте, ₽",
                               "Вложения вернулись"]))
    (ROOT / "docs" / "research-tables.md").write_text("# Таблицы исследования (генерируются автоматически)\n\n"
                                                      + "\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
