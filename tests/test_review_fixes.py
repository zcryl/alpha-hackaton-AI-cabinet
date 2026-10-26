"""Регрессии по ревью: актуальность оценки, сброс демо, строгий ввод, ставка и нагрузка, потоки SQLite."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import login, submit_profile

API = "/api/v1"
INN_CHECK_FAILS = "000012345627"


def _to_action_required(client, auth, profiles):
    p = profiles["story2_clarify"]
    cl, officer = auth(p["owner"]), auth("officer")
    app_id, app = submit_profile(client, cl, p)
    assert app["status"] == "MANUAL_REVIEW" and app["score"]
    client.post(f"{API}/applications/{app_id}/clarify", headers=officer, json={"message": "Уточните ИНН", "field": "inn"})
    return app_id, cl, officer


def test_failed_recheck_drops_stale_score_and_blocks_approval(client, auth, profiles):
    app_id, cl, officer = _to_action_required(client, auth, profiles)
    assert client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"inn": INN_CHECK_FAILS}}, headers=cl).status_code == 200
    r = client.post(f"{API}/applications/{app_id}/submit", json={"reply": "новый ИНН"}, headers=cl)
    assert r.status_code == 200 and r.json()["status"] == "MANUAL_REVIEW"
    assert r.json()["score"] is None and r.json()["checks"] is None
    assert r.json()["decision"]["review_reason_code"] == "CHECK_UNAVAILABLE"
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve"}, headers=officer)
    assert r.status_code == 422 and r.json()["detail"]
    assert client.get(f"{API}/applications/{app_id}", headers=cl).json()["status"] == "MANUAL_REVIEW"


def test_approval_needs_score_for_current_data(client, auth, profiles):
    """Даже если оценка осталась в базе, но данные изменились, одобрить нельзя."""
    from app import db
    p = profiles["story2_clarify"]
    cl, officer = auth(p["owner"]), auth("officer")
    app_id, _ = submit_profile(client, cl, p)
    conn = db.connect()
    conn.execute("UPDATE applications SET data = json_set(data, '$.requested_amount', 1000000) WHERE id=?", (app_id,))
    conn.commit(); conn.close()
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve"}, headers=officer)
    assert r.status_code == 422 and "устарели" in r.json()["detail"]


def test_client_cannot_reset_demo_but_officer_can(client, auth, profiles):
    p = profiles["story1_approved"]
    cl = auth(p["owner"])
    submit_profile(client, cl, p)
    c2 = auth("client2")
    assert client.post(f"{API}/demo/reset", headers=c2).status_code == 403
    assert len(client.get(f"{API}/applications", headers=cl).json()) == 1
    assert client.post(f"{API}/demo/reset", headers=auth("officer")).status_code == 200


def test_reset_disabled_when_demo_mode_off(client, auth, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    assert client.post(f"{API}/demo/reset", headers=auth("officer")).status_code == 404


def _draft(client, h):
    return client.post(f"{API}/applications", json={"product_type": "WORKING_CAPITAL"}, headers=h).json()["id"]


def test_numeric_inn_is_normalized_and_submit_does_not_crash(client, auth, profiles):
    p = profiles["story1_approved"]
    h = auth(p["owner"])
    app_id = _draft(client, h)
    data = {**p["data"], "inn": int(p["data"]["inn"])}
    r = client.patch(f"{API}/applications/{app_id}/draft", json={"data": data}, headers=h)
    assert r.status_code == 200 and r.json()["data"]["inn"] == p["data"]["inn"]
    assert client.post(f"{API}/applications/{app_id}/submit", headers=h).status_code == 200


@pytest.mark.parametrize("field,value", [
    ("requested_term_months", 11.9), ("business_age_months", 7.5), ("requested_amount", "abc"),
    ("monthly_revenue", -1), ("monthly_revenue", float("inf")), ("monthly_revenue", 1e20),
    ("inn", "12ab"), ("inn", "1234567890123"), ("company_name", "x" * 500), ("purpose", 123), ("monthly_expenses", True),
])
def test_draft_rejects_bad_types(client, auth, field, value):
    h = auth("client")
    app_id = _draft(client, h)
    body = '{"data": {"%s": %s}}' % (field, "Infinity" if value == float("inf") else __import__("json").dumps(value))
    r = client.patch(f"{API}/applications/{app_id}/draft", content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 422, r.text


def test_whole_float_term_is_accepted_as_integer(client, auth):
    h = auth("client")
    app_id = _draft(client, h)
    r = client.patch(f"{API}/applications/{app_id}/draft", json={"data": {"requested_term_months": 12.0, "business_age_months": "24"}}, headers=h)
    assert r.status_code == 200 and r.json()["data"]["requested_term_months"] == 12 and r.json()["data"]["business_age_months"] == 24


def test_offer_load_uses_final_payment_and_rate_table(client, auth, profiles):
    from app import rules
    from app.scoring import annuity_payment
    assert rules.final_rate("EQUIPMENT", "low") == 0.19
    assert rules.final_rate("EQUIPMENT", "medium") == 0.22
    assert rules.final_rate("EQUIPMENT", "high") == 0.24
    p = profiles["story2_clarify"]
    cl, officer = auth(p["owner"]), auth("officer")
    app_id, app = submit_profile(client, cl, p)
    r = client.post(f"{API}/applications/{app_id}/decision", json={"action": "approve"}, headers=officer).json()
    o, d = r["decision"]["offer"], p["data"]
    assert o["annual_rate"] == rules.final_rate("EQUIPMENT", app["score"]["risk_category"])
    assert o["monthly_payment"] == round(annuity_payment(d["requested_amount"], o["annual_rate"], d["requested_term_months"]), 2)
    expected = (d["existing_loans_monthly"] + o["monthly_payment"]) / (d["monthly_revenue"] - d["monthly_expenses"])
    assert o["payment_load"] == pytest.approx(expected, abs=1e-4)
    assert o["overpayment"] == pytest.approx(o["monthly_payment"] * o["term_months"] - o["amount"], abs=0.01)
    assert o["schedule"][-1]["balance"] == 0


def test_parallel_requests_do_not_fail(client, auth):
    h = auth("client")
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: client.get(f"{API}/applications", headers=h).status_code, range(24)))
    assert codes == [200] * 24
