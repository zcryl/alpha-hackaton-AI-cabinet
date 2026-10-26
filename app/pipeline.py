"""Конвейер заявки: переходы статусов, журнал событий, проверки, скоринг, решение.

Правила безопасности:
  * каждый переход проходит через move(): допустимость + защита от гонок (UPDATE ... WHERE status = src);
  * сбой проверки или скоринга никогда не ведёт к одобрению: заявка уходит на ручное рассмотрение.
"""
from __future__ import annotations

import sqlite3

from . import rules, states
from .checks import CheckUnavailable, run_checks
from .db import dumps, loads
from .scoring import ScoringInputError, score as run_score
from .security import now_iso

OUTCOME_TO_STATUS = {"approved": states.APPROVED, "rejected": states.REJECTED, "manual_review": states.MANUAL_REVIEW}


def log(conn: sqlite3.Connection, app_id: int, actor: str, kind: str,
        src: str | None = None, dst: str | None = None, details: dict | None = None) -> None:
    conn.execute("INSERT INTO events(application_id,at,actor,kind,from_status,to_status,details) VALUES (?,?,?,?,?,?,?)",
                 (app_id, now_iso(), actor, kind, src, dst, dumps(details) if details else None))


def log_data_saved(conn: sqlite3.Connection, app_id: int, actor: str, fields: list[str]) -> None:
    """Автосохранение пишет в журнал одну запись: подряд идущие сохранения одного автора склеиваются."""
    last = conn.execute("SELECT id, actor, kind, details FROM events WHERE application_id=? ORDER BY id DESC LIMIT 1",
                        (app_id,)).fetchone()
    if last and last["kind"] == "data_saved" and last["actor"] == actor:
        merged = sorted(set((loads(last["details"]) or {}).get("fields", [])) | set(fields))
        conn.execute("UPDATE events SET at=?, details=? WHERE id=?", (now_iso(), dumps({"fields": merged}), last["id"]))
    else:
        log(conn, app_id, actor, "data_saved", details={"fields": sorted(fields)})


def move(conn: sqlite3.Connection, app_id: int, src: str, dst: str, actor: str,
         kind: str = "status", details: dict | None = None, **columns) -> bool:
    """Переводит заявку src -> dst. False, если статус уже изменён (повторный клик, гонка)."""
    states.check_transition(src, dst)
    sets = ["status = ?", "updated_at = ?"] + [f"{c} = ?" for c in columns]
    params = [dst, now_iso(), *[dumps(v) for v in columns.values()], app_id, src]
    cur = conn.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id = ? AND status = ?", params)
    if cur.rowcount == 0:
        return False
    log(conn, app_id, actor, kind, src, dst, details)
    return True


def _to_manual(conn, app_id: int, src: str, code: str, error: str, kind: str, next_step: str, reason_text: str = "") -> str:
    decision = {"outcome": "manual_review", "zone": None, "stop_factors": [], "offer": None, "reason_code": code, "review_reason_code": code,
                "review_reason": reason_text,
                "review_hint": "Автоматическая оценка недоступна, нужна ручная проверка",
                "client_message": "Заявку рассматривает кредитный аналитик.", "next_step": next_step}
    move(conn, app_id, src, states.MANUAL_REVIEW, "system", kind, {"error": error}, decision=decision)
    return states.MANUAL_REVIEW


def submit(conn: sqlite3.Connection, app: sqlite3.Row, actor: str, reply: str | None = None) -> str:
    """DRAFT | ACTION_REQUIRED -> SUBMITTED -> CHECKS -> SCORING -> итог. Возвращает итоговый статус."""
    app_id, product, was = app["id"], app["product"], app["status"]
    clarification = loads(app["clarification"])
    extra = {}
    if was == states.ACTION_REQUIRED and clarification:
        clarification = {**clarification, "reply": (reply or "").strip(), "answered_at": now_iso()}
        extra["clarification"] = clarification          # история запроса и ответа сохраняется
    else:
        extra["clarification"] = None
    kind = "clarification_answered" if was == states.ACTION_REQUIRED else "submit"
    details = {"reply": clarification["reply"]} if extra["clarification"] and clarification.get("reply") else None
    # Новое рассмотрение: результаты прошлых проверок, оценки и решения недействительны.
    extra.update(checks=None, score=None, decision=None)
    if not move(conn, app_id, was, states.SUBMITTED, actor, kind, details, **extra):
        return conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()["status"]

    data = loads(app["data"])
    move(conn, app_id, states.SUBMITTED, states.CHECKS, "system")
    try:
        checks = run_checks(data["inn"])
    except CheckUnavailable as e:
        return _to_manual(conn, app_id, states.CHECKS, "CHECK_UNAVAILABLE", str(e), "check_failed",
                          "Проверки временно недоступны, заявку рассмотрит сотрудник",
                          "Автоматические проверки (БКИ и реестр) временно недоступны, поэтому заявку рассматривает сотрудник банка.")

    fp = rules.data_fingerprint(product, data)
    checks = {**checks, "data_hash": fp}
    move(conn, app_id, states.CHECKS, states.SCORING, "system", checks=checks)
    try:
        result = run_score(rules.scoring_features(product, data, checks))
    except ScoringInputError as e:
        return _to_manual(conn, app_id, states.SCORING, "SCORING_INPUT_ERROR", str(e), "scoring_failed",
                          "Данные требуют проверки сотрудником",
                          "Автоматическая оценка не смогла обработать данные заявки, поэтому её проверяет сотрудник банка.")

    result = {**result, "data_hash": fp}
    decision = rules.decide(product, data, checks, result)
    final = OUTCOME_TO_STATUS[decision["outcome"]]
    move(conn, app_id, states.SCORING, final, "system", "decision",
         {"outcome": decision["outcome"], "zone": decision["zone"]}, score=result, decision=decision)
    return final


def reopen(conn: sqlite3.Connection, app: sqlite3.Row, actor: str) -> bool:
    """Клиент забирает заявку с ручного рассмотрения на правку: результаты проверок и оценки сбрасываются,
    после правок заявка проходит весь конвейер заново. История событий сохраняется."""
    return move(conn, app["id"], app["status"], states.DRAFT, actor, "reopened_for_edit", None,
                checks=None, score=None, decision=None, clarification=None)


def approval_blocker(app: sqlite3.Row) -> str | None:
    """Почему нельзя одобрить: нет успешных проверок/оценки либо они посчитаны по другой версии данных."""
    checks, score_, data = loads(app["checks"]), loads(app["score"]), loads(app["data"]) or {}
    if not checks or not score_:
        return "Нельзя одобрить без оценки: автоматические проверки не были пройдены. Можно отказать или запросить данные"
    fp = rules.data_fingerprint(app["product"], data)
    if checks.get("data_hash") != fp or score_.get("data_hash") != fp:
        return "Проверки и оценка устарели: данные заявки изменились. Запросите повторную отправку"
    return None


def officer_decide(conn: sqlite3.Connection, app: sqlite3.Row, officer: str, action: str, reason: str) -> bool:
    dst = {"approve": states.APPROVED, "reject": states.REJECTED}[action]
    decision = loads(app["decision"]) or {}
    decision = {**decision, "outcome": "approved" if action == "approve" else "rejected",
                "decided_by": officer, "reason": reason or None, "stop_factors": decision.get("stop_factors", []),
                "client_message": "Решение принято сотрудником банка.",
                "next_step": "Изучите условия и график платежей" if action == "approve"
                else "Ваши данные сохранены в кабинете. Повторную заявку можно отправить в 1 клик"}
    if action == "approve":
        if approval_blocker(app):
            return False
        score_ = loads(app["score"])
        decision["offer"] = rules.build_offer(app["product"], loads(app["data"]), score_)
    return move(conn, app["id"], app["status"], dst, officer, "officer_decision", {"reason": reason}, decision=decision)


def officer_clarify(conn: sqlite3.Connection, app: sqlite3.Row, officer: str, message: str, field: str | None = None) -> bool:
    clarification = {"message": message, "field": field, "requested_by": officer, "requested_at": now_iso()}
    return move(conn, app["id"], app["status"], states.ACTION_REQUIRED, officer, "clarification_requested",
                {"message": message, "field": field}, clarification=clarification)
