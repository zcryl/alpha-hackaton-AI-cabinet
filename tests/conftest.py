import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PBKDF2_ITERATIONS", "1000")
    monkeypatch.setenv("DEMO_PASSWORD", "demo123")
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def profiles():
    with open(ROOT / "fixtures" / "demo_applications.json", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def login(client, username):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": "demo123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def auth(client):
    return lambda username: login(client, username)


def submit_profile(client, headers, profile):
    app_id = client.post("/api/v1/applications", json={"product_type": profile["product_type"]}, headers=headers).json()["id"]
    assert client.patch(f"/api/v1/applications/{app_id}/draft", json={"data": profile["data"]}, headers=headers).status_code == 200
    r = client.post(f"/api/v1/applications/{app_id}/submit", headers=headers)
    assert r.status_code == 200, r.text
    return app_id, r.json()


def variant(profile, product_type=None, **changes):
    """Копия профиля с изменёнными полями данных (и, при необходимости, продуктом)."""
    return {**profile, "product_type": product_type or profile["product_type"], "data": {**profile["data"], **changes}}
