import pytest

from app.scoring import SCALE_MAX, SCALE_MIN, SCORING_VERSION, ScoringInputError, score, to_scale

GOOD = dict(business_age_months=48, monthly_revenue=1_500_000, monthly_expenses=1_000_000,
            monthly_obligations=50_000, estimated_payment=31_000, requested_amount=600_000,
            overdue_count_12m=0, active_loans_count=1)


def test_deterministic():
    assert score(GOOD) == score(dict(GOOD))


def test_good_profile_is_green_and_versioned():
    r = score(GOOD)
    assert r["version"] == SCORING_VERSION
    assert r["risk_category"] == "low" and r["score"] >= 680
    assert r["scale"] == {"min": 300, "max": 850}
    assert r["features"]["dti"] == r["features"]["debt_service_ratio"]
    assert all({"code", "points", "text"} <= set(x) for x in r["reasons"])


def test_score_is_on_300_850_scale():
    bad = dict(GOOD, business_age_months=1, monthly_expenses=1_499_000, overdue_count_12m=5, active_loans_count=6,
               requested_amount=50_000_000, monthly_obligations=900_000)
    r = score(bad)
    assert SCALE_MIN <= r["score"] <= SCALE_MAX and r["risk_category"] == "high"
    assert r["score"] == 300


def test_zone_boundaries_match_spec():
    # красная зона < 520, зелёная >= 680
    assert to_scale(39) < 520 <= to_scale(40)
    assert to_scale(69) < 680 <= to_scale(70)
    assert to_scale(0) == 300 and to_scale(100) == 850


@pytest.mark.parametrize("key", list(GOOD))
def test_missing_feature_is_error_not_default(key):
    with pytest.raises(ScoringInputError) as e:
        score({k: v for k, v in GOOD.items() if k != key})
    assert key in e.value.missing


@pytest.mark.parametrize("patch", [{"monthly_revenue": 0}, {"monthly_revenue": -1}, {"requested_amount": 0},
                                   {"overdue_count_12m": float("nan")}, {"business_age_months": "10"}, {"active_loans_count": True}])
def test_invalid_values_rejected(patch):
    with pytest.raises(ScoringInputError):
        score(dict(GOOD, **patch))
