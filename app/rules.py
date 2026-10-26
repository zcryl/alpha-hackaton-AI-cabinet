"""Продукты и правила решения. Решение принимается здесь, а не в скоринге и не во фронтенде.

Матрица решений (спецификация бизнес-аналитика, п. 2.1):
  красная зона (< 520)                 -> автоматический отказ при любых стоп-факторах
  стоп-фактор                          -> отказ (заявка не доходит до ручной проверки)
  серая зона (520-679), без стоп-фактора -> кредитный аналитик (оборотные: выписка; оборудование: залог)
  зелёная (>= 680), оборотные <= 1,5 млн -> экспресс-одобрение без ручного этапа
  зелёная (>= 680), оборудование или > 1,5 млн -> верификация залога сотрудником

Все числа — учебные допущения команды, не реальные тарифы банка.
"""
from __future__ import annotations

import hashlib
import json
import math

from .scoring import annuity_payment

WORKING_CAPITAL = "WORKING_CAPITAL"
EQUIPMENT = "EQUIPMENT"

PRODUCTS = {
    WORKING_CAPITAL: {
        "name": "Оборотные средства",
        "annual_rate": 0.22, "min_term_months": 3, "max_term_months": 12, "max_amount": 3_000_000,
    },
    EQUIPMENT: {
        "name": "Кредит на оборудование",
        "annual_rate": 0.19, "min_term_months": 12, "max_term_months": 36, "max_amount": 10_000_000,
    },
}

RED_BELOW = 520              # < 520: красная зона
GREEN_FROM = 680             # >= 680: зелёная зона
EXPRESS_MAX_AMOUNT = 1_500_000
MIN_BUSINESS_AGE_MONTHS = 6  # стоп-фактор: менее 6 месяцев
# Надбавка к ставке продукта по категории риска скоринга (учебное допущение; политику ставок определяет команда).
RISK_RATE_SURCHARGE = {"low": 0.0, "medium": 0.03, "high": 0.05}
MEDIUM_RISK_RATE_SURCHARGE = RISK_RATE_SURCHARGE["medium"]   # для обратной совместимости

# Поля контракта данных (п. 1.3 спецификации) + служебные поля кабинета.
REQUIRED_FIELDS = [
    "inn", "business_age_months", "monthly_revenue", "monthly_expenses",
    "existing_loans_monthly", "requested_amount", "requested_term_months",
    "company_name", "industry",
]
OPTIONAL_FIELDS = ["purpose"]
ALLOWED_FIELDS = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
NUMERIC_FIELDS = ["business_age_months", "monthly_revenue", "monthly_expenses",
                  "existing_loans_monthly", "requested_amount", "requested_term_months"]

INTEGER_FIELDS = ["business_age_months", "requested_term_months"]
MONEY_FIELDS = ["monthly_revenue", "monthly_expenses", "existing_loans_monthly", "requested_amount"]
TEXT_LIMITS = {"company_name": 120, "industry": 60, "purpose": 500}
MAX_MONEY = 1_000_000_000_000
MAX_MONTHS = 1200

REVIEW_HINTS = {
    WORKING_CAPITAL: "Дозапрос выписки / одобрение меньшей суммы",
    EQUIPMENT: "Дозапрос спецификации залога",
}
CLARIFY_TEMPLATES = {
    WORKING_CAPITAL: ("Банку требуются дополнительные сведения. Чтобы предложить вам сниженную процентную ставку, "
                      "аналитику необходимо подтвердить среднемесячный оборот по счету. Пожалуйста, опишите оборот "
                      "за последние 3 месяца или приложите данные выписки в ответе ниже."),
    EQUIPMENT: ("Банку требуются дополнительные сведения по залогу. Пожалуйста, укажите в ответе ниже, "
                "что вы приобретаете (модель, поставщик, стоимость) и будет ли оборудование передано в залог."),
}


def inn_is_valid(inn: str) -> bool:
    """Контрольная сумма ИНН физического лица / ИП (12 цифр)."""
    if not (isinstance(inn, str) and inn.isdigit() and len(inn) == 12):
        return False
    d = [int(c) for c in inn]
    n11 = sum(a * b for a, b in zip(d[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    n12 = sum(a * b for a, b in zip(d[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))) % 11 % 10
    return d[10] == n11 and d[11] == n12


def normalize_draft(patch: dict) -> tuple[dict, list[str]]:
    """Приводит значения черновика к типам и возвращает (чистые значения, ошибки).

    ИНН -> строка из цифр; сроки -> целые; деньги -> конечные неотрицательные числа; тексты -> строки ограниченной длины.
    Пустые значения (None, "") сохраняются как "" и очищают поле.
    """
    clean, errs = {}, []
    for k, v in patch.items():
        if v is None or (isinstance(v, str) and not v.strip()):
            clean[k] = ""
            continue
        if k == "inn":
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                errs.append("ИНН: укажите 12 цифр"); continue
            t = str(v).strip()
            if not t.isdigit() or len(t) > 12:
                errs.append("ИНН: только цифры, не больше 12"); continue
            clean[k] = t.zfill(12) if isinstance(v, int) else t
        elif k in INTEGER_FIELDS or k in MONEY_FIELDS:
            try:
                if isinstance(v, bool): raise ValueError
                num = float(v.replace(",", ".").replace(" ", "")) if isinstance(v, str) else float(v)
            except (ValueError, TypeError):
                errs.append(f"Поле «{k}»: нужно число"); continue
            if not math.isfinite(num) or num < 0:
                errs.append(f"Поле «{k}»: нужно конечное неотрицательное число"); continue
            if k in INTEGER_FIELDS:
                if num != int(num):
                    errs.append(f"Поле «{k}»: нужно целое число месяцев"); continue
                if num > MAX_MONTHS:
                    errs.append(f"Поле «{k}»: слишком большое значение"); continue
                clean[k] = int(num)
            else:
                if num > MAX_MONEY:
                    errs.append(f"Поле «{k}»: слишком большая сумма"); continue
                clean[k] = round(num, 2) if num != int(num) else int(num)
        elif k in TEXT_LIMITS:
            if not isinstance(v, str):
                errs.append(f"Поле «{k}»: нужен текст"); continue
            if len(v.strip()) > TEXT_LIMITS[k]:
                errs.append(f"Поле «{k}»: не больше {TEXT_LIMITS[k]} символов"); continue
            clean[k] = v.strip()
    return clean, errs


def data_fingerprint(product: str, data: dict) -> str:
    """Отпечаток данных заявки: проверки и оценка действительны только для той версии данных, по которой посчитаны."""
    raw = json.dumps({"p": product, "d": {k: data.get(k) for k in sorted(ALLOWED_FIELDS - {"purpose"})}},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def missing_fields(data: dict) -> list[str]:
    return [k for k in REQUIRED_FIELDS if data.get(k) in (None, "")]


def validate_request(product: str, data: dict) -> list[str]:
    """Ошибки по формату и условиям продукта (список понятных сообщений)."""
    p, errs = PRODUCTS[product], []
    for k in NUMERIC_FIELDS:
        v = data.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            errs.append(f"Поле «{k}» должно быть неотрицательным числом")
    if errs:
        return errs
    if not data.get("monthly_revenue", 0) > 0:
        errs.append("Выручка должна быть больше нуля")
    amount, term = data.get("requested_amount"), data.get("requested_term_months")
    if amount is not None and not (0 < amount <= p["max_amount"]):
        errs.append(f"Сумма по продукту: от 1 до {p['max_amount']:,} руб.".replace(",", " "))
    if term is not None and not (p["min_term_months"] <= term <= p["max_term_months"]):
        errs.append(f"Срок по продукту: от {p['min_term_months']} до {p['max_term_months']} мес.")
    if data.get("inn") and not inn_is_valid(str(data["inn"])):
        errs.append("ИНН индивидуального предпринимателя: 12 цифр с верной контрольной суммой")
    return errs


def scoring_features(product: str, data: dict, checks: dict) -> dict:
    """Собирает вход скоринга из заявки и результатов проверок."""
    p = PRODUCTS[product]
    return {
        "business_age_months": data["business_age_months"],
        "monthly_revenue": data["monthly_revenue"],
        "monthly_expenses": data["monthly_expenses"],
        "monthly_obligations": data["existing_loans_monthly"],
        "estimated_payment": annuity_payment(data["requested_amount"], p["annual_rate"], int(data["requested_term_months"])),
        "requested_amount": data["requested_amount"],
        "overdue_count_12m": checks["bki"]["overdue_count_12m"],
        "active_loans_count": checks["bki"]["active_loans_count"],
    }


def zone_of(score: int) -> str:
    return "red" if score < RED_BELOW else "green" if score >= GREEN_FROM else "gray"


def stop_factors(data: dict, checks: dict) -> list[dict]:
    """Стоп-факторы банка: при любом балле ведут к отказу."""
    stops = []
    age = data["business_age_months"]
    if age < MIN_BUSINESS_AGE_MONTHS:
        stops.append({"code": "BUSINESS_TOO_YOUNG", "retry_after_months": MIN_BUSINESS_AGE_MONTHS - int(age),
                      "text": f"Кредитование доступно для ИП со сроком регистрации от {MIN_BUSINESS_AGE_MONTHS} месяцев "
                              f"(текущий срок вашего ИП: {int(age)} мес.)"})
    if checks["bki"].get("current_overdue_amount", 0) > 0:
        stops.append({"code": "CURRENT_OVERDUE", "text": "По данным БКИ есть действующая просрочка по кредитам"})
    if checks["registry"]["status"] != "active":
        stops.append({"code": "REGISTRY_NOT_ACTIVE", "text": "По данным реестра ИП не ведёт деятельность"})
    return stops


def payment_schedule(amount: float, annual_rate: float, term: int) -> list[dict]:
    pay = annuity_payment(amount, annual_rate, term)
    balance, rows = float(amount), []
    for m in range(1, term + 1):
        interest = balance * annual_rate / 12
        principal = pay - interest
        balance = max(balance - principal, 0.0) if m < term else 0.0
        rows.append({"month": m, "payment": round(pay, 2), "interest": round(interest, 2),
                     "principal": round(principal, 2), "balance": round(balance, 2)})
    return rows


def final_rate(product: str, risk_category: str) -> float:
    """Единственное место, где считается итоговая ставка: ставка продукта + надбавка за риск."""
    return PRODUCTS[product]["annual_rate"] + RISK_RATE_SURCHARGE.get(risk_category, RISK_RATE_SURCHARGE["high"])


def payment_load(data: dict, payment: float) -> float | None:
    """Доля свободных средств, уходящая на платежи (текущие + новый)."""
    net = data["monthly_revenue"] - data["monthly_expenses"]
    return None if net <= 0 else round((data["existing_loans_monthly"] + payment) / net, 4)


def build_offer(product: str, data: dict, result: dict) -> dict:
    rate = final_rate(product, result["risk_category"])
    term, amount = int(data["requested_term_months"]), data["requested_amount"]
    payment = round(annuity_payment(amount, rate, term), 2)
    prelim = (result.get("features") or {}).get("dti")
    return {
        "amount": amount, "term_months": term, "annual_rate": round(rate, 4),
        "base_rate": PRODUCTS[product]["annual_rate"], "risk_category": result["risk_category"],
        "monthly_payment": payment,
        "total_payments": round(payment * term, 2), "overpayment": round(payment * term - amount, 2),
        "payment_load": payment_load(data, payment),          # нагрузка по итоговому платежу
        "preliminary_load": prelim,                           # нагрузка в оценке (платёж по базовой ставке)
        "schedule": payment_schedule(amount, rate, term),
        "note": "Учебное предложение, не является офертой банка",
    }


def decide(product: str, data: dict, checks: dict, result: dict) -> dict:
    """Итог: approved | rejected | manual_review (по матрице решений)."""
    stops = stop_factors(data, checks)
    zone = zone_of(result["score"])
    base = {"zone": zone, "stop_factors": stops, "offer": None}

    if stops:
        retry = max((s.get("retry_after_months", 0) for s in stops), default=0)
        when = (f"Возвращайтесь через {retry} мес. — повторную заявку можно будет отправить в 1 клик"
                if retry else "После устранения причин повторную заявку можно отправить в 1 клик")
        return {**base, "outcome": "rejected",
                "client_message": "К сожалению, сейчас мы не можем открыть кредитную линию. " + " ".join(s["text"] + "." for s in stops),
                "next_step": "Ваши данные сохранены в кабинете. " + when}
    if zone == "red":
        return {**base, "outcome": "rejected",
                "client_message": "К сожалению, по итогам оценки мы не можем предложить кредит на запрошенных условиях.",
                "next_step": "Ваши данные сохранены в кабинете. Уменьшите сумму или увеличьте срок и отправьте заявку повторно в 1 клик"}
    if zone == "green" and product == WORKING_CAPITAL and data["requested_amount"] <= EXPRESS_MAX_AMOUNT:
        return {**base, "outcome": "approved", "offer": build_offer(product, data, result),
                "client_message": "Заявка одобрена без ручного этапа.",
                "next_step": "Изучите условия и график платежей"}
    if zone == "gray":
        hint, code = REVIEW_HINTS[product], "GRAY_ZONE"
        why = (f"Оценка средняя ({result['score']} из 850), поэтому окончательное решение принимает кредитный аналитик "
               "и может запросить у вас дополнительные сведения.")
    elif product == EQUIPMENT:
        hint, code = "Верификация залога сотрудником", "EQUIPMENT_COLLATERAL"
        why = "Для кредита на оборудование сотрудник банка дополнительно проверяет залог, поэтому заявка не одобряется автоматически."
    else:
        hint, code = "Верификация залога сотрудником", "AMOUNT_ABOVE_EXPRESS_LIMIT"
        why = (f"Сумма больше {EXPRESS_MAX_AMOUNT:,} ₽ — такие заявки одобряются после проверки сотрудником банка."
               .replace(",", " "))
    return {**base, "outcome": "manual_review", "review_hint": hint,
            "review_reason_code": code, "review_reason": why,
            "clarify_template": CLARIFY_TEMPLATES[product],
            "client_message": "Заявку рассматривает кредитный аналитик.",
            "next_step": "Мы напишем в кабинете, если потребуются дополнительные сведения"}
