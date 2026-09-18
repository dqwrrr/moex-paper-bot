"""Торговые издержки (доля от объёма сделки, в одну сторону)."""
STOCK_COST = 0.0005 + 0.0005   # комиссия тарифа «Трейдер» 0,05% + проскальзывание 0,05%
FUND_COST = 0.0003              # фонды Т-Капитала без комиссии брокера; 0,03% — спред
INDEX_FUND_FEE = 0.008          # расходы фонда на индекс (TMOS@ ≈ 0,8% годовых)


def cost_rate(instrument: str) -> float:
    return FUND_COST if instrument in ("INDEX", "CASH") else STOCK_COST
