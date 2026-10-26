"""Локальные имитации внешних проверок (БКИ и реестр ЕГРИП). Реальных запросов наружу нет.

Правила для ИНН, которых нет в fixtures/:
  * ИНН начинается с 9999 -> реестр: ИП прекратил деятельность (стоп-фактор);
  * ИНН начинается с 0000 -> имитация недоступности сервиса (CheckUnavailable);
  * иначе -> ИП действует, кредитов и просрочек нет.
"""
import json

from . import config


class CheckUnavailable(Exception):
    """Внешняя проверка недоступна. Заявка не может быть одобрена автоматически."""


def _load(name: str) -> dict:
    with open(config.fixtures_dir() / name, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def check_registry(inn: str) -> dict:
    if inn.startswith("0000"):
        raise CheckUnavailable("Реестр недоступен (имитация сбоя)")
    if inn.startswith("9999"):
        return {"status": "liquidated", "source": "mock_registry"}
    rec = _load("mock_registry.json").get(inn, {"status": "active"})
    return {**rec, "source": "mock_registry"}


def check_bki(inn: str) -> dict:
    if inn.startswith("0000"):
        raise CheckUnavailable("БКИ недоступно (имитация сбоя)")
    default = {"active_loans_count": 0, "overdue_count_12m": 0, "current_overdue_amount": 0}
    rec = _load("mock_bki.json").get(inn, default)
    return {**rec, "source": "mock_bki"}


def run_checks(inn: str) -> dict:
    return {"registry": check_registry(inn), "bki": check_bki(inn)}
