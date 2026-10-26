"""Балльный скоринг (демонстрационный), версия score-v1.1.

Это НЕ банковская модель: баллы и пороги — учебные допущения команды.
Функция чистая и детерминированная: одинаковый вход всегда даёт одинаковый результат.
Скоринг даёт только оценку риска; решение по заявке принимают отдельные правила (app/rules.py).

Контракт (см. docs/scoring.md):
  вход  — dict с признаками из REQUIRED_FEATURES;
  выход — {version, score (300-850), model_points (0-100), risk_category, base, raw_score, reasons[], features{}}.
"""
from __future__ import annotations

SCORING_VERSION = "score-v1.1"
BASE_SCORE = 50

# Итоговый балл выводится на шкале 300-850: scaled = 300 + model_points * 5.5 (целочисленно, с округлением вниз).
# Внутренние баллы модели 0-100: 39 -> 514 (красная зона), 40 -> 520, 69 -> 679, 70 -> 685 (зелёная зона).
SCALE_MIN, SCALE_MAX = 300, 850


def to_scale(model_points: int) -> int:
    return SCALE_MIN + (model_points * 550) // 100

REQUIRED_FEATURES = {
    "business_age_months": int,      # срок работы бизнеса, мес.
    "monthly_revenue": float,        # средняя выручка в месяц, руб.
    "monthly_expenses": float,       # средние расходы в месяц, руб.
    "monthly_obligations": float,    # текущие ежемесячные платежи по кредитам, руб.
    "estimated_payment": float,      # оценка платежа по запрашиваемому кредиту, руб./мес.
    "requested_amount": float,       # запрашиваемая сумма, руб.
    "overdue_count_12m": int,        # число просрочек за 12 мес. (из БКИ)
    "active_loans_count": int,       # число действующих кредитов (из БКИ)
}

RISK_LABELS = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}


class ScoringInputError(ValueError):
    """Вход скоринга неполный или некорректен. Решение по такой заявке не принимается."""

    def __init__(self, message: str, missing: list[str] | None = None, invalid: list[str] | None = None):
        super().__init__(message)
        self.missing = missing or []
        self.invalid = invalid or []


def annuity_payment(amount: float, annual_rate: float, term_months: int) -> float:
    """Аннуитетный платёж. Используется и скорингом (оценка нагрузки), и условиями предложения."""
    r = annual_rate / 12
    if r == 0:
        return amount / term_months
    return amount * r / (1 - (1 + r) ** (-term_months))


def max_amount_for_payment(payment: float, annual_rate: float, term_months: int) -> float:
    r = annual_rate / 12
    if r == 0:
        return payment * term_months
    return payment * (1 - (1 + r) ** (-term_months)) / r


def validate_features(features: dict) -> dict:
    missing = [k for k in REQUIRED_FEATURES if features.get(k) is None]
    if missing:
        raise ScoringInputError("Не хватает данных для оценки: " + ", ".join(missing), missing=missing)
    invalid = []
    clean: dict = {}
    for key, typ in REQUIRED_FEATURES.items():
        v = features[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            invalid.append(key)
            continue
        if v < 0:
            invalid.append(key)
            continue
        clean[key] = int(v) if typ is int else float(v)
    if not invalid and clean["monthly_revenue"] <= 0:
        invalid.append("monthly_revenue")
    if not invalid and clean["requested_amount"] <= 0:
        invalid.append("requested_amount")
    if invalid:
        raise ScoringInputError("Некорректные значения: " + ", ".join(invalid), invalid=invalid)
    return clean


def _factor(code: str, points: int, text: str) -> dict:
    return {"code": code, "points": points, "direction": "plus" if points > 0 else "minus" if points < 0 else "neutral", "text": text}


def score(features: dict) -> dict:
    f = validate_features(features)
    factors: list[dict] = []

    # 1. Срок работы бизнеса
    age = f["business_age_months"]
    if age < 6:
        factors.append(_factor("BUSINESS_AGE_VERY_SHORT", -15, f"Бизнес работает меньше 6 месяцев ({age} мес.)"))
    elif age < 12:
        factors.append(_factor("BUSINESS_AGE_SHORT", -8, f"Бизнес работает меньше года ({age} мес.)"))
    elif age < 24:
        factors.append(_factor("BUSINESS_AGE_MODERATE", 0, f"Бизнес работает от года до двух лет ({age} мес.)"))
    elif age < 60:
        factors.append(_factor("BUSINESS_AGE_STABLE", 8, f"Бизнес работает больше двух лет ({age} мес.)"))
    else:
        factors.append(_factor("BUSINESS_AGE_LONG", 12, f"Бизнес работает больше пяти лет ({age} мес.)"))

    # 2. Маржинальность: (выручка - расходы) / выручка
    revenue, expenses = f["monthly_revenue"], f["monthly_expenses"]
    net = revenue - expenses
    margin = net / revenue
    pct = f"{margin * 100:.0f}%"
    if margin < 0.05:
        factors.append(_factor("MARGIN_VERY_LOW", -15, f"Расходы почти равны выручке (маржа {pct})"))
    elif margin < 0.15:
        factors.append(_factor("MARGIN_LOW", -5, f"Невысокая маржа ({pct})"))
    elif margin < 0.30:
        factors.append(_factor("MARGIN_HEALTHY", 5, f"Устойчивая маржа ({pct})"))
    else:
        factors.append(_factor("MARGIN_HIGH", 10, f"Высокая маржа ({pct})"))

    # 3. Долговая нагрузка: (текущие платежи + новый платёж) / свободные средства
    dsr = None
    if net <= 0:
        factors.append(_factor("NO_FREE_CASH", -25, "Расходы не меньше выручки: нет средств на платежи"))
    else:
        dsr = (f["monthly_obligations"] + f["estimated_payment"]) / net
        d = f"{dsr * 100:.0f}%"
        if dsr <= 0.3:
            factors.append(_factor("DEBT_LOAD_LOW", 15, f"Низкая долговая нагрузка ({d} свободных средств)"))
        elif dsr <= 0.5:
            factors.append(_factor("DEBT_LOAD_MODERATE", 5, f"Умеренная долговая нагрузка ({d} свободных средств)"))
        elif dsr <= 0.7:
            factors.append(_factor("DEBT_LOAD_HIGH", -8, f"Высокая долговая нагрузка ({d} свободных средств)"))
        else:
            factors.append(_factor("DEBT_LOAD_VERY_HIGH", -20, f"Очень высокая долговая нагрузка ({d} свободных средств)"))

    # 4. Просрочки за 12 месяцев
    overdue = f["overdue_count_12m"]
    if overdue == 0:
        factors.append(_factor("CLEAN_PAYMENT_HISTORY", 10, "Просрочек за последние 12 месяцев нет"))
    elif overdue == 1:
        factors.append(_factor("OVERDUE_ONCE", -10, "Была одна просрочка за последние 12 месяцев"))
    else:
        factors.append(_factor("OVERDUE_REPEATED", -20, f"Просрочки повторялись ({overdue} за 12 месяцев)"))

    # 5. Число действующих кредитов
    loans = f["active_loans_count"]
    if loans >= 3:
        factors.append(_factor("MANY_ACTIVE_LOANS", -8, f"Много действующих кредитов ({loans})"))
    else:
        factors.append(_factor("ACTIVE_LOANS_OK", 0, f"Действующих кредитов: {loans}"))

    # 6. Размер запроса относительно годовой выручки
    ratio = f["requested_amount"] / (revenue * 12)
    r = f"{ratio * 100:.0f}%"
    if ratio <= 0.25:
        factors.append(_factor("REQUEST_SMALL_VS_REVENUE", 5, f"Запрос небольшой относительно годовой выручки ({r})"))
    elif ratio <= 0.6:
        factors.append(_factor("REQUEST_MODERATE_VS_REVENUE", 0, f"Запрос соразмерен годовой выручке ({r})"))
    elif ratio <= 1.0:
        factors.append(_factor("REQUEST_LARGE_VS_REVENUE", -8, f"Запрос большой относительно годовой выручки ({r})"))
    else:
        factors.append(_factor("REQUEST_VERY_LARGE", -15, f"Запрос превышает годовую выручку ({r})"))

    raw = BASE_SCORE + sum(x["points"] for x in factors)
    final = max(0, min(100, raw))
    category = "low" if final >= 70 else "medium" if final >= 50 else "high"
    reasons = sorted((x for x in factors if x["points"] != 0), key=lambda x: (-abs(x["points"]), x["code"]))
    return {
        "version": SCORING_VERSION,
        "score": to_scale(final),
        "scale": {"min": SCALE_MIN, "max": SCALE_MAX},
        "model_points": final,
        "risk_category": category,
        "base": BASE_SCORE,
        "raw_score": raw,
        "reasons": reasons,
        "features": {
            "net_income": round(net, 2),
            "margin": round(margin, 4),
            "debt_service_ratio": None if dsr is None else round(dsr, 4),
            "dti": None if dsr is None else round(dsr, 4),   # то же значение под названием из спецификации
            "amount_to_annual_revenue": round(ratio, 4),
        },
    }
