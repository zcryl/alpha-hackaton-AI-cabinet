"""HTTP API (FastAPI), версия v1. Права проверяются на сервере; интерфейс решений не принимает."""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline, rules, security, states
from .scoring import SCORING_VERSION

STATIC_DIR = config.BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Умный кредитный кабинет (учебный прототип)", version="1.0.0", lifespan=lifespan)
api = APIRouter(prefix="/api/v1")


# ---------- зависимости ----------
def get_conn():
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


bearer = HTTPBearer(auto_error=False, description="Токен из POST /api/v1/auth/login (без слова Bearer)")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), conn: sqlite3.Connection = Depends(get_conn)):
    if not creds or not creds.credentials:
        raise HTTPException(401, "Требуется вход")
    row = conn.execute(
        "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (security.token_digest(creds.credentials.strip()),)).fetchone()
    if not row or row["expires_at"] < datetime.now(timezone.utc).isoformat(timespec="seconds"):
        raise HTTPException(401, "Сессия недействительна, войдите снова")
    return row


def require_officer(user=Depends(current_user)):
    if user["role"] != "officer":
        raise HTTPException(403, "Доступно только сотруднику банка")
    return user


def get_app_for(conn: sqlite3.Connection, app_id: int, user) -> sqlite3.Row:
    """Клиент видит только свои заявки, сотрудник — все отправленные. Чужая заявка выглядит как отсутствующая."""
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    if user["role"] == "client" and row["owner_id"] != user["id"]:
        raise HTTPException(404, "Заявка не найдена")
    if user["role"] == "officer" and row["status"] == states.DRAFT:
        raise HTTPException(404, "Заявка не найдена")
    return row


def fetch(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()


def view(row: sqlite3.Row, conn: sqlite3.Connection, with_events: bool = False) -> dict:
    out = {
        "id": row["id"], "product_type": row["product"], "product_name": rules.PRODUCTS[row["product"]]["name"],
        "status": row["status"], "status_label": states.LABELS[row["status"]],
        "data": db.loads(row["data"]), "checks": db.loads(row["checks"]), "score": db.loads(row["score"]),
        "decision": db.loads(row["decision"]), "clarification": db.loads(row["clarification"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if with_events:
        out["events"] = [
            {**dict(e), "details": db.loads(e["details"])}
            for e in conn.execute("SELECT at,actor,kind,from_status,to_status,details FROM events WHERE application_id=? ORDER BY id",
                                  (row["id"],))]
    return out


# ---------- схемы ----------
class LoginIn(BaseModel):
    username: str
    password: str


class CreateIn(BaseModel):
    product_type: Literal["WORKING_CAPITAL", "EQUIPMENT"]


class DraftIn(BaseModel):
    data: dict


class SubmitIn(BaseModel):
    reply: str | None = None   # ответ клиента на запрос уточнения


class ClarifyIn(BaseModel):
    message: str
    field: str | None = None   # поле формы, которое нужно подсветить клиенту


class DecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    reason: str = ""   # обязательна только для отказа


# ---------- служебное ----------
@api.get("/health")
def health():
    return {"status": "ok", "scoring_version": SCORING_VERSION}


@api.post("/auth/login")
def login(body: LoginIn, conn: sqlite3.Connection = Depends(get_conn)):
    user = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not user or not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Неверный логин или пароль")
    token = security.new_token()
    conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (?,?,?)",
                 (security.token_digest(token), user["id"], security.expiry_iso()))
    return {"token": token, "role": user["role"], "full_name": user["full_name"]}


@api.get("/me")
def me(user=Depends(current_user)):
    return {"username": user["username"], "role": user["role"], "full_name": user["full_name"]}


@api.get("/products")
def products(user=Depends(current_user)):
    return [{"code": k, **v} for k, v in rules.PRODUCTS.items()]


@api.get("/news")
def news(industry: str | None = None, product_type: str | None = None, status: str | None = None,
         user=Depends(current_user)):
    """Лента под клиента: по продукту, отрасли и статусу заявки (например, гайд при отказе)."""
    with open(config.fixtures_dir() / "news.json", encoding="utf-8") as f:
        items = json.load(f)
    product = (product_type or "").lower()

    def match(n):
        return ((not n["industries"] or industry in n["industries"])
                and (not n["products"] or product in n["products"])
                and (not n.get("statuses") or status in n["statuses"]))
    picked = [n for n in items if match(n)] if (industry or product_type or status) else items
    # новости под текущий статус выше остальных, затем свежие
    return sorted(picked, key=lambda n: (bool(n.get("statuses")), n["published_at"]), reverse=True)[:5]


# ---------- заявки ----------
@api.post("/applications", status_code=201)
def create_application(body: CreateIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    if user["role"] != "client":
        raise HTTPException(403, "Заявку создаёт предприниматель")
    return view(_new_draft(conn, user, body.product_type, {}), conn)


def _new_draft(conn: sqlite3.Connection, user, product: str, data: dict) -> sqlite3.Row:
    now = security.now_iso()
    cur = conn.execute("INSERT INTO applications(owner_id,product,status,data,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                       (user["id"], product, states.DRAFT, db.dumps(data), now, now))
    pipeline.log(conn, cur.lastrowid, user["username"], "created", None, states.DRAFT)
    return fetch(conn, cur.lastrowid)


@api.get("/applications")
def list_applications(user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    if user["role"] == "client":
        rows = conn.execute("SELECT * FROM applications WHERE owner_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM applications WHERE status != ? ORDER BY updated_at DESC", (states.DRAFT,)).fetchall()
    return [view(r, conn) for r in rows]


@api.get("/applications/{app_id}")
def get_application(app_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return view(get_app_for(conn, app_id, user), conn, with_events=True)


@api.patch("/applications/{app_id}/draft")
def save_draft(app_id: int, body: DraftIn, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Автосохранение: принимает любое подмножество полей, пустые значения очищают поле."""
    row = get_app_for(conn, app_id, user)
    if user["role"] != "client":
        raise HTTPException(403, "Данные заявки редактирует клиент")
    if row["status"] not in states.CLIENT_EDITABLE:
        raise HTTPException(409, "Заявка уже на рассмотрении, данные менять нельзя")
    unknown = sorted(set(body.data) - rules.ALLOWED_FIELDS)
    if unknown:
        raise HTTPException(422, "Неизвестные поля: " + ", ".join(unknown))
    clean, errors = rules.normalize_draft(body.data)
    if errors:
        raise HTTPException(422, {"message": "Проверьте введённые значения", "errors": errors})
    merged = {**(db.loads(row["data"]) or {}), **clean}
    merged = {k: v for k, v in merged.items() if v not in (None, "")}
    conn.execute("UPDATE applications SET data=?, updated_at=? WHERE id=?", (db.dumps(merged), security.now_iso(), app_id))
    pipeline.log_data_saved(conn, app_id, user["username"], sorted(clean))
    return view(fetch(conn, app_id), conn)


@api.post("/applications/{app_id}/submit")
def submit(app_id: int, body: SubmitIn | None = None, user=Depends(current_user),
           conn: sqlite3.Connection = Depends(get_conn)):
    row = get_app_for(conn, app_id, user)
    if user["role"] != "client":
        raise HTTPException(403, "Заявку отправляет клиент")
    if row["status"] not in states.CLIENT_EDITABLE:
        # Повторный клик: возвращаем текущее состояние, дубликата действий нет.
        return view(row, conn, with_events=True)
    data = db.loads(row["data"]) or {}
    missing = rules.missing_fields(data)
    if missing:
        raise HTTPException(422, {"message": "Заполните обязательные поля", "missing": missing})
    errors = rules.validate_request(row["product"], data)
    if errors:
        raise HTTPException(422, {"message": "Проверьте условия заявки", "errors": errors})
    pipeline.submit(conn, row, user["username"], reply=body.reply if body else None)
    return view(fetch(conn, app_id), conn, with_events=True)


@api.post("/applications/{app_id}/edit")
def edit(app_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Правка отправленной заявки: пока она ждёт сотрудника, клиент возвращает её в черновик и отправляет заново."""
    row = get_app_for(conn, app_id, user)
    if user["role"] != "client":
        raise HTTPException(403, "Заявку правит клиент")
    if row["status"] != states.MANUAL_REVIEW:
        raise HTTPException(409, "Изменить можно заявку, которая ждёт решения сотрудника. "
                                 "После решения создайте новую заявку на основе этой")
    if not pipeline.reopen(conn, row, user["username"]):
        raise HTTPException(409, "Статус заявки уже изменился, обновите страницу")
    return view(fetch(conn, app_id), conn, with_events=True)


@api.post("/applications/{app_id}/repeat", status_code=201)
def repeat(app_id: int, user=Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    """Новая заявка в 1 клик: черновик с теми же данными. Исходная заявка и её история не меняются."""
    row = get_app_for(conn, app_id, user)
    if user["role"] != "client":
        raise HTTPException(403, "Новую заявку подаёт клиент")
    if row["status"] not in states.FINAL:
        raise HTTPException(409, "Новую заявку на основе этой можно создать после решения по ней")
    return view(_new_draft(conn, user, row["product"], db.loads(row["data"]) or {}), conn)


@api.post("/applications/{app_id}/clarify")
def clarify(app_id: int, body: ClarifyIn, user=Depends(require_officer), conn: sqlite3.Connection = Depends(get_conn)):
    row = get_app_for(conn, app_id, user)
    if row["status"] != states.MANUAL_REVIEW:
        raise HTTPException(409, "Уточнение можно запросить только при ручном рассмотрении")
    if not body.message.strip():
        raise HTTPException(422, "Укажите, какие данные нужны")
    if body.field and body.field not in rules.ALLOWED_FIELDS:
        raise HTTPException(422, "Неизвестное поле: " + body.field)
    if not pipeline.officer_clarify(conn, row, user["username"], body.message.strip(), body.field or None):
        raise HTTPException(409, "Статус заявки уже изменился")
    return view(fetch(conn, app_id), conn, with_events=True)


@api.post("/applications/{app_id}/decision")
def decision(app_id: int, body: DecisionIn, user=Depends(require_officer), conn: sqlite3.Connection = Depends(get_conn)):
    row = get_app_for(conn, app_id, user)
    if row["status"] != states.MANUAL_REVIEW:
        raise HTTPException(409, "Решение вручную принимается только при ручном рассмотрении")
    if body.action == "reject" and not body.reason.strip():
        raise HTTPException(422, "Укажите причину отказа: клиент увидит её в кабинете")
    if body.action == "approve":
        blocker = pipeline.approval_blocker(row)
        if blocker:
            raise HTTPException(422, blocker)
    if not pipeline.officer_decide(conn, row, user["username"], body.action, body.reason.strip()):
        raise HTTPException(409, "Статус заявки уже изменился")
    return view(fetch(conn, app_id), conn, with_events=True)


# ---------- демо ----------
@api.post("/demo/reset")
def demo_reset(user=Depends(require_officer)):
    """Глобальный сброс демо: только сотрудник и только при DEMO_MODE=1 (на общем стенде выключать)."""
    if not config.demo_mode():
        raise HTTPException(404, "Недоступно")
    db.reset_demo()
    return {"status": "reset"}


app.include_router(api)

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")
