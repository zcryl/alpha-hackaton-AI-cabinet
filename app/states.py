"""Статусы заявки и допустимые переходы. Единственный источник истины для сервера и документации.

Коды статусов совпадают со спецификацией бизнес-аналитика (APPROVED, REJECTED, ACTION_REQUIRED).
"""

DRAFT = "DRAFT"                      # Черновик
SUBMITTED = "SUBMITTED"              # Отправлена
CHECKS = "CHECKS"                    # Проверки
SCORING = "SCORING"                  # Оценка
ACTION_REQUIRED = "ACTION_REQUIRED"  # Нужны данные от клиента
MANUAL_REVIEW = "MANUAL_REVIEW"      # Ручное рассмотрение
APPROVED = "APPROVED"                # Одобрено
REJECTED = "REJECTED"                # Отказ

LABELS = {
    DRAFT: "Черновик",
    SUBMITTED: "Отправлена",
    CHECKS: "Проверки",
    SCORING: "Оценка",
    ACTION_REQUIRED: "Нужны данные",
    MANUAL_REVIEW: "Ручное рассмотрение",
    APPROVED: "Одобрено",
    REJECTED: "Отказ",
}

TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {SUBMITTED},
    SUBMITTED: {CHECKS},
    CHECKS: {SCORING, MANUAL_REVIEW},        # сбой проверки -> ручное рассмотрение, не одобрение
    SCORING: {APPROVED, REJECTED, MANUAL_REVIEW},
    MANUAL_REVIEW: {APPROVED, REJECTED, ACTION_REQUIRED, DRAFT},   # DRAFT: клиент вернул заявку на правку
    ACTION_REQUIRED: {SUBMITTED},            # клиент ответил -> повторные проверки
    APPROVED: set(),
    REJECTED: set(),
}

FINAL = {APPROVED, REJECTED}
CLIENT_EDITABLE = {DRAFT, ACTION_REQUIRED}


class InvalidTransition(Exception):
    pass


def check_transition(src: str, dst: str) -> None:
    if dst not in TRANSITIONS.get(src, set()):
        raise InvalidTransition(f"Переход {src} -> {dst} запрещён")
