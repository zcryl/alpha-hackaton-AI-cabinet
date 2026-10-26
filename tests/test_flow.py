"""Сквозные сценарии по спецификации: три истории, матрица решений, права, защита от сбоев."""
import pytest
from conftest import submit_profile, variant

from app import rules

API = "/api/v1"
INN_CHECK_FAILS = "000012345627"     # имитация недоступности БКИ и реестра
INN_LIQUIDATED = "999912345603"      # реестр: ИП прекратил деятельность
INN_MANY_OVERDUES = "780201234584"   # 3 кредита, 2 просрочки, текущей просрочки нет


def get(client, h, app_id):
    return client.get(f"{API}/applications/{app_id}", headers=h).json()


# ---------- три истории ----------
def test_story1_express_approval(client, auth, profiles):
    p = profiles["story1_approved"]
    h = auth(p["owner"])
    app_id, app = submit_profile(client, h, p)
    assert app["status"] == "APPROVED"
    assert app["decision"]["zone"] == "green" and app["score"]["score"] >= 680
    offer = app["decision"]["offer"]
    assert offer["monthly_payment"] > 0 and len(offer["schedule"]) == 12
    assert offer["schedule"][-1]["balance"] == 0
    path = [e["to_status"] for e in get(client, h, app_id)["events"] if e["to_status"]]
    assert path == ["DRAFT", "SUBMITTED", "CHECKS", "SCORING", "APPROVED"]      # ручной этап пропущен


def test_story2_clarification_then_officer_approval(client, auth, profiles):
    p = profiles["story2_clarify"]
    cl, officer = auth(p["owner"]), auth("officer")
    app_id, app = submit_profile(client, cl, p)
    assert app["status"] == "MANUAL_REVIEW" and app["decision"]["zone"] == "gray"
    assert 520 <= app["score"]["score"] < 680
    assert "залог" in app["decision"]["review_hint"].lower()
    assert app["decision"]["clarify_template"]
    assert app["decision"]["review_reason_code"] == "GRAY_ZONE" and str(app["score"]["score"]) in app["decision"]["review_reason"]

    # на рассмотрении клиент данные менять не может
    assert client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"purpose": "x"}}, headers=cl).status_code == 409

    r = client.post(f"{API}/applications/{app_id}/clarify", headers=officer,
                    json={"message": p["clarification_message"], "field": p["clarification_field"]})
    assert r.status_code == 200 and r.json()["status"] == "ACTION_REQUIRED"
    seen = get(client, cl, app_id)["clarification"]
    assert seen["message"] and seen["field"] == "purpose"

    r = client.post(f"{API}/applications/{app_id}/submit", json={"reply": p["clarification_reply"]}, headers=cl)
    assert r.json()["status"] == "MANUAL_REVIEW"                       # серая зона: решение за сотрудником
    assert r.json()["clarification"]["reply"] == p["clarification_reply"]

    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve", "reason": "Залог подтверждён"}, headers=officer)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert r.json()["decision"]["decided_by"] == "officer" and r.json()["decision"]["offer"]["schedule"]


def test_story3_stop_factor_rejection_and_repeat(client, auth, profiles):
    p = profiles["story3_rejected"]
    h = auth(p["owner"])
    app_id, app = submit_profile(client, h, p)
    assert app["status"] == "REJECTED"
    codes = {s["code"] for s in app["decision"]["stop_factors"]}
    assert codes == {"BUSINESS_TOO_YOUNG", "CURRENT_OVERDUE"}
    assert "от 6 месяцев" in app["decision"]["client_message"] and "3 мес." in app["decision"]["client_message"]
    assert "через 3 мес." in app["decision"]["next_step"]

    # повторная заявка в 1 клик: новый черновик с теми же данными
    r = client.post(f"{API}/applications/{app_id}/repeat", headers=h)
    assert r.status_code == 201 and r.json()["status"] == "DRAFT" and r.json()["id"] != app_id
    assert r.json()["data"]["inn"] == p["data"]["inn"]
    # только после отказа
    assert client.post(f"{API}/applications/{r.json()['id']}/repeat", headers=h).status_code == 409


# ---------- матрица решений ----------
def test_green_equipment_goes_to_collateral_verification(client, auth, profiles):
    p = variant(profiles["story1_approved"], product_type="EQUIPMENT", requested_term_months=24)
    _, app = submit_profile(client, auth("client"), p)
    assert app["decision"]["zone"] == "green" and app["status"] == "MANUAL_REVIEW"
    assert "залога" in app["decision"]["review_hint"]
    assert app["decision"]["review_reason_code"] == "EQUIPMENT_COLLATERAL" and "залог" in app["decision"]["review_reason"]


def test_green_working_capital_above_1_5m_goes_to_officer(client, auth, profiles):
    p = variant(profiles["story1_approved"], requested_amount=2_000_000)
    _, app = submit_profile(client, auth("client"), p)
    assert app["decision"]["zone"] == "green" and app["status"] == "MANUAL_REVIEW"
    assert app["decision"]["review_reason_code"] == "AMOUNT_ABOVE_EXPRESS_LIMIT"
    assert "1 500 000" in app["decision"]["review_reason"]


def test_gray_working_capital_hint_is_statement(client, auth, profiles):
    p = variant(profiles["story2_clarify"], product_type="WORKING_CAPITAL", requested_amount=1_500_000, requested_term_months=12)
    _, app = submit_profile(client, auth("client"), p)
    assert app["decision"]["zone"] == "gray" and app["status"] == "MANUAL_REVIEW"
    assert "выписк" in app["decision"]["review_hint"].lower()
    assert "оборот" in app["decision"]["clarify_template"]


def test_red_zone_is_auto_rejection_without_stop_factors(client, auth, profiles):
    p = variant(profiles["story1_approved"], inn=INN_MANY_OVERDUES, business_age_months=8, monthly_revenue=200_000,
                monthly_expenses=185_000, existing_loans_monthly=5_000, requested_amount=600_000)
    _, app = submit_profile(client, auth("client"), p)
    assert app["status"] == "REJECTED" and app["decision"]["zone"] == "red"
    assert app["score"]["score"] < 520 and app["decision"]["stop_factors"] == []


def test_same_risk_different_product_gives_different_decision(client, auth, profiles):
    base = profiles["story1_approved"]
    _, a = submit_profile(client, auth("client"), base)
    _, b = submit_profile(client, auth("client"), variant(base, product_type="EQUIPMENT", requested_term_months=24))
    assert a["decision"]["zone"] == b["decision"]["zone"] == "green"
    assert (a["status"], b["status"]) == ("APPROVED", "MANUAL_REVIEW")


# ---------- проверки и сбои ----------
def test_check_failure_never_approves(client, auth, profiles):
    p = variant(profiles["story1_approved"], inn=INN_CHECK_FAILS)
    app_id, app = submit_profile(client, auth("client"), p)
    assert app["status"] == "MANUAL_REVIEW" and app["decision"]["reason_code"] == "CHECK_UNAVAILABLE"
    assert "недоступны" in app["decision"]["review_reason"]
    off = auth("officer")
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve", "reason": "ок"}, headers=off)
    assert r.status_code == 422                                      # без оценки одобрить нельзя
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "reject", "reason": "Проверки недоступны"}, headers=off)
    assert r.json()["status"] == "REJECTED"


def test_liquidated_registry_is_rejected(client, auth, profiles):
    _, app = submit_profile(client, auth("client"), variant(profiles["story1_approved"], inn=INN_LIQUIDATED))
    assert app["status"] == "REJECTED"
    assert any(s["code"] == "REGISTRY_NOT_ACTIVE" for s in app["decision"]["stop_factors"])


# ---------- валидация ----------
def test_inn_checksum():
    assert rules.inn_is_valid("770601234529") and rules.inn_is_valid(INN_CHECK_FAILS)
    assert not rules.inn_is_valid("770601234528") and not rules.inn_is_valid("12345") and not rules.inn_is_valid("77060123452a")


@pytest.mark.parametrize("changes, text", [
    ({"inn": "770601234528"}, "контрольной суммой"),
    ({"requested_term_months": 24}, "от 3 до 12"),            # оборотные: 3-12 мес.
    ({"requested_amount": 3_500_000}, "Сумма по продукту"),
    ({"monthly_revenue": 0}, "Выручка"),
])
def test_submit_validation(client, auth, profiles, changes, text):
    h = auth("client")
    p = variant(profiles["story1_approved"], **changes)
    app_id = client.post(f"{API}/applications", json={"product_type": p["product_type"]}, headers=h).json()["id"]
    client.patch(f"{API}/applications/{app_id}/draft", json={"data": p["data"]}, headers=h)
    r = client.post(f"{API}/applications/{app_id}/submit", headers=h)
    assert r.status_code == 422 and text in str(r.json()["detail"])
    assert get(client, h, app_id)["status"] == "DRAFT"


def test_equipment_term_range(client, auth, profiles):
    h = auth("client")
    p = variant(profiles["story1_approved"], product_type="EQUIPMENT", requested_term_months=6)
    app_id = client.post(f"{API}/applications", json={"product_type": "EQUIPMENT"}, headers=h).json()["id"]
    client.patch(f"{API}/applications/{app_id}/draft", json={"data": p["data"]}, headers=h)
    assert "от 12 до 36" in str(client.post(f"{API}/applications/{app_id}/submit", headers=h).json())


def test_draft_autosave_partial_and_clearing(client, auth):
    h = auth("client")
    app_id = client.post(f"{API}/applications", json={"product_type": "EQUIPMENT"}, headers=h).json()["id"]
    r = client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"monthly_revenue": 500000, "purpose": "Станок"}}, headers=h)
    assert r.json()["data"] == {"monthly_revenue": 500000, "purpose": "Станок"}
    r = client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"purpose": ""}}, headers=h)
    assert r.json()["data"] == {"monthly_revenue": 500000}           # пустое значение очищает поле
    assert client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"hack": 1}}, headers=h).status_code == 422


def test_incomplete_draft_cannot_be_submitted(client, auth):
    h = auth("client")
    app_id = client.post(f"{API}/applications", json={"product_type": "EQUIPMENT"}, headers=h).json()["id"]
    r = client.post(f"{API}/applications/{app_id}/submit", headers=h)
    assert r.status_code == 422 and "inn" in r.json()["detail"]["missing"]


# ---------- права и надёжность ----------
def test_permissions(client, auth, profiles):
    p = profiles["story2_clarify"]
    app_id, _ = submit_profile(client, auth("client2"), p)
    assert client.get(f"{API}/applications/{app_id}", headers=auth("client")).status_code == 404       # чужая заявка
    assert client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve", "reason": "x"},
                       headers=auth("client2")).status_code == 403                                  # клиент не решает
    assert client.get(f"{API}/applications").status_code == 401                                     # без входа
    assert client.get(f"{API}/applications", headers=auth("client")).json() == []                   # список только свой


def test_officer_decision_requires_reason_and_state(client, auth, profiles):
    p = profiles["story2_clarify"]
    app_id, _ = submit_profile(client, auth(p["owner"]), p)
    off = auth("officer")
    assert client.post(f"{API}/applications/{app_id}/decision", json={"action": "reject", "reason": " "}, headers=off).status_code == 422
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "reject", "reason": "Нужны документы"}, headers=off)
    assert r.json()["status"] == "REJECTED"
    assert client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve", "reason": "x"}, headers=off).status_code == 409


def test_double_submit_does_not_duplicate(client, auth, profiles):
    p = profiles["story1_approved"]
    h = auth(p["owner"])
    app_id, _ = submit_profile(client, h, p)
    client.post(f"{API}/applications/{app_id}/submit", headers=h)
    assert sum(1 for e in get(client, h, app_id)["events"] if e["kind"] == "submit") == 1


# ---------- новости ----------
def test_news_depends_on_product_and_status(client, auth):
    h = auth("client")
    ids = lambda **q: {n["id"] for n in client.get(f"{API}/news", params=q, headers=h).json()}
    eq = ids(product_type="EQUIPMENT")
    assert "n11" in eq and "n13" not in eq and "n14" not in eq
    wc = ids(product_type="WORKING_CAPITAL")
    assert "n13" in wc and "n11" not in wc
    assert "n14" in ids(product_type="WORKING_CAPITAL", status="REJECTED")        # гайд при отказе
    assert "n02" in ids(industry="food", product_type="WORKING_CAPITAL") and "n01" not in ids(industry="food", product_type="WORKING_CAPITAL")


# ---------- журнал и редактирование ----------
def test_autosave_is_coalesced_in_history(client, auth):
    h = auth("client")
    app_id = client.post(f"{API}/applications", json={"product_type": "EQUIPMENT"}, headers=h).json()["id"]
    for patch in ({"inn": "770601234529"}, {"monthly_revenue": 1}, {"purpose": "a"}, {"purpose": "ab"}):
        client.patch(f"{API}/applications/{app_id}/draft", json={"data": patch}, headers=h)
    saves = [e for e in get(client, h, app_id)["events"] if e["kind"] == "data_saved"]
    assert len(saves) == 1
    assert saves[0]["details"]["fields"] == ["inn", "monthly_revenue", "purpose"]


def test_client_can_edit_application_waiting_for_officer(client, auth, profiles):
    p = profiles["story2_clarify"]
    cl, officer = auth(p["owner"]), auth("officer")
    app_id, app = submit_profile(client, cl, p)
    assert app["status"] == "MANUAL_REVIEW"

    r = client.post(f"{API}/applications/{app_id}/edit", headers=cl)
    assert r.status_code == 200 and r.json()["status"] == "DRAFT"
    assert r.json()["score"] is None and r.json()["decision"] is None and r.json()["checks"] is None   # старый результат сброшен
    assert r.json()["data"]["inn"] == p["data"]["inn"]                                                  # данные сохранены
    assert client.get(f"{API}/applications/{app_id}", headers=officer).status_code == 404               # ушла из очереди сотрудника
    assert client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve", "reason": "x"},
                       headers=officer).status_code == 404

    client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"company_name": "ИП Смирнов (исправлено)"}}, headers=cl)
    r = client.post(f"{API}/applications/{app_id}/submit", headers=cl)
    assert r.json()["status"] == "MANUAL_REVIEW" and r.json()["data"]["company_name"] == "ИП Смирнов (исправлено)"
    kinds = [e["kind"] for e in r.json()["events"]]
    assert "reopened_for_edit" in kinds and kinds.count("submit") == 2     # вся история сохранена


def test_edit_is_allowed_only_to_owner_and_only_before_decision(client, auth, profiles):
    p = profiles["story2_clarify"]
    app_id, _ = submit_profile(client, auth("client2"), p)
    assert client.post(f"{API}/applications/{app_id}/edit", headers=auth("client")).status_code == 404      # чужая заявка
    assert client.post(f"{API}/applications/{app_id}/edit", headers=auth("officer")).status_code == 403
    client.post(f"{API}/applications/{app_id}/decision", json={"action": "reject", "reason": "x"}, headers=auth("officer"))
    assert client.post(f"{API}/applications/{app_id}/edit", headers=auth("client2")).status_code == 409    # решение принято

    p1 = profiles["story1_approved"]
    app1, a1 = submit_profile(client, auth(p1["owner"]), p1)
    assert a1["status"] == "APPROVED"
    assert client.post(f"{API}/applications/{app1}/edit", headers=auth(p1["owner"])).status_code == 409


def test_new_application_from_approved_keeps_original(client, auth, profiles):
    p = profiles["story1_approved"]
    h = auth(p["owner"])
    app_id, _ = submit_profile(client, h, p)
    r = client.post(f"{API}/applications/{app_id}/repeat", headers=h)
    assert r.status_code == 201 and r.json()["status"] == "DRAFT" and r.json()["data"]["inn"] == p["data"]["inn"]
    assert get(client, h, app_id)["status"] == "APPROVED"                                                # исходная не тронута
    assert client.post(f"{API}/applications/{r.json()['id']}/repeat", headers=h).status_code == 409       # из черновика нельзя


def test_officer_can_approve_without_reason_but_reject_needs_one(client, auth, profiles):
    p = profiles["story2_clarify"]
    off = auth("officer")
    app_id, _ = submit_profile(client, auth(p["owner"]), p)
    # отказ без причины нельзя: клиент должен увидеть, почему
    assert client.post(f"{API}/applications/{app_id}/decision", json={"action": "reject"}, headers=off).status_code == 422
    # одобрение без причины можно
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve"}, headers=off)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert r.json()["decision"]["reason"] is None and r.json()["decision"]["offer"]["schedule"]
